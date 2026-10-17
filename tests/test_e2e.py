"""End-to-end tests over real UDP sockets with the test client."""

from __future__ import annotations

import asyncio
import socket

import pytest

from quazal.context import player
from quazal.client import QuazalClient
from quazal.ddl import Reader, StationURL, Writer
from quazal.errors import QError
from quazal.prudp.packet import Flag, Packet, PacketType, VPort
from quazal.rmc.message import RMCMessage
from quazal.storage import MemoryStorage, RelationshipType, User

from acb_rdv.config import Config
from acb_rdv.server import AcbServer
from quazal.nat.service import NatConfig


def free_udp_ports(n: int) -> list[int]:
    socks = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        socks.append(s)
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


@pytest.fixture
async def env(tmp_path):
    auth, secure, relay = free_udp_ports(3)
    settings = tmp_path / "settings.cxb"
    settings.write_bytes(bytes(range(256)) * 20)  # 5 KB: forces fragmentation
    storage = MemoryStorage()
    storage.add_user(User(10001, "Alice"), "pw-a")
    storage.add_user(User(10002, "Bob"), "pw-b")
    cfg = Config(public_address="127.0.0.1", bind_host="127.0.0.1", auth_port=auth, secure_port=secure,
                 status_port=0, relay_port_start=relay, relay_port_end=relay, nat=NatConfig(relay=True),
                 game_settings=str(settings), backend="memory", news="")
    server = AcbServer(cfg, storage=storage)
    await server.start()
    clients: list[QuazalClient] = []

    async def login(name: str, password: str) -> QuazalClient:
        c = await QuazalClient.open("127.0.0.1")
        clients.append(c)
        reply = await c.login(auth, name, password)
        assert reply.success, hex(reply.error)
        connect = await c.connect_secure(secure)
        assert connect.payload[4:] == (0x1234 + 1).to_bytes(4, "little")
        w = Writer().string_list([f"prudp:/address=192.168.1.5;port={c.local_address[1]};sid=15"])
        w.string("UbiAuthenticationLoginCustomData").u32(4).u32(0)
        reg = await c.call(0x0B, 4, w.bytes())
        assert reg.success
        return c

    yield server, storage, login, (auth, secure, relay)
    for c in clients:
        c.close()
    await server.stop()


async def next_push(client: QuazalClient, protocol: int, timeout: float = 2.0) -> RMCMessage:
    while True:
        msg = await asyncio.wait_for(client.pushes.get(), timeout)
        if msg.protocol == protocol:
            return msg


async def test_login_and_account(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    assert alice.pid == 10001
    account = await alice.call(0x1D, 3)
    r = Reader(account.body)
    r.string()
    assert r.string() == "Alice"
    assert server.ctx.online(10001) is not None


async def test_bad_password_and_unauthenticated_calls(env):
    server, storage, login, (auth, secure, _) = env
    c = await QuazalClient.open("127.0.0.1")
    try:
        reply = await c.login(auth, "Alice", "wrong")
        assert not reply.success and reply.error == QError.RendezVous_InvalidPassword
        denied = await c.call(0x1D, 3)
        assert not denied.success and denied.error == QError.RendezVous_NotAuthenticated
        unknown = await c.call(0x5A, 1)
        assert not unknown.success and unknown.error == QError.Core_NotImplemented
    finally:
        c.close()


async def test_secure_connect_requires_login(env):
    server, storage, login, (auth, secure, _) = env
    c = await QuazalClient.open("127.0.0.1")
    try:
        c.pid, c.session_key = 10002, bytes(16)
        with pytest.raises(asyncio.TimeoutError):
            c.timeout = 0.5
            await c.connect_secure(secure)
    finally:
        c.close()


async def test_fragmented_response(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    reply = await alice.call(0x18, 4, Writer().u32(0).string("settings").bytes())
    r = Reader(reply.body)
    assert r.buffer() == bytes(range(256)) * 20
    assert r.bool() is True
    frags = [p for p in alice.raw if p.type == PacketType.DATA and p.has(Flag.RELIABLE)]
    assert len(frags) >= 5 and frags[-1].part == 0 and frags[0].part == 1


async def test_duplicate_request_is_not_run_twice(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    await login("Bob", "pw-b")
    conn = alice.conn
    body = Writer().string("Bob").u32(0).string("Alice").bytes()
    msg = RMCMessage.request(0x14, 4, 900, body).encode()
    src, dst = VPort(3, conn.port), VPort(3, 1)
    pkt = Packet(src, dst, PacketType.DATA, Flag.RELIABLE | Flag.NEED_ACK, conn.session_id, conn.server_sig,
                 conn.seq, payload=msg)
    conn.seq += 1
    fut = asyncio.get_running_loop().create_future()
    alice.responses[900] = fut
    alice._send(pkt)
    first = await asyncio.wait_for(fut, 2)
    assert first.success
    fut2 = asyncio.get_running_loop().create_future()
    alice.responses[900] = fut2
    alice._send(pkt)  # retransmission of the same request
    second = await asyncio.wait_for(fut2, 2)
    assert second.body == first.body
    assert len(await storage.get_relationships(10001, RelationshipType.PENDING)) == 1


async def test_friend_request_notifies_online_invitee(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    reply = await alice.call(0x14, 4, Writer().string("Bob").u32(0).string("Alice").bytes())
    assert reply.success
    notification = await next_push(bob, 0x0E)
    r = Reader(notification.body)
    assert r.u32() == 10001          # source
    assert r.u32() == 1 * 1000 + 1   # Friends event
    assert (r.u32(), r.u32(), r.string()) == (10001, 2, "Alice")  # requested
    accept = await bob.call(0x14, 5, Writer().u32(10001).u32(0).bytes())
    assert Reader(accept.body).bool() is True
    await next_push(alice, 0x0E)
    friends = await alice.call(0x14, 12, Writer().u8(1).bool(False).bytes())
    fr = Reader(friends.body)
    assert fr.u32() == 1 and fr.u32() == 10002


async def test_logout_notifies_friends(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    await alice.call(0x14, 4, Writer().string("Bob").u32(0).string("Alice").bytes())
    await bob.call(0x14, 5, Writer().u32(10001).u32(0).bytes())
    await alice.disconnect()
    while True:  # skip the "online" status sent when the friendship was accepted
        r = Reader((await next_push(bob, 0x0E)).body)
        fields = (r.u32(), r.u32(), r.u32(), r.u32())
        if fields[3] == 0:
            break
    assert fields == (10001, 5001, 10001, 0)  # friend status change: offline
    await asyncio.sleep(0.05)
    assert server.ctx.online(10001) is None


def session_attributes(game_type: int = 1) -> list[tuple[int, int]]:
    return [(3, 6), (4, 0), (7, 0), (0x6C, 3), (0x6E, game_type), (0x69, 0), (0x6A, 6), (0x72, 0)]


def create_session_body(attrs) -> bytes:
    w = Writer().u32(1).u32(len(attrs))
    for i, v in attrs:
        w.u32(i).u32(v)
    return w.bytes()


async def host_session(bob: QuazalClient) -> int:
    created = await bob.call(0x2A, 1, create_session_body(session_attributes()))
    r = Reader(created.body)
    r.u32()
    session_id = r.u32()
    body = Writer().u32(1).u32(session_id).u32_list([10002]).u32_list([]).bytes()
    assert (await bob.call(0x2A, 8, body)).success
    urls = [f"prudp:/address=192.168.1.9;port={bob.local_address[1]};PID=10002;RVCID=1"]
    assert (await bob.call(0x2A, 21, Writer().string_list(urls).bytes())).success
    return session_id


async def test_matchmaking_search_finds_session(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    session_id = await host_session(bob)
    query = Writer().u32(1).u32(0).u32(5)
    for i, v in [(0x69, 0), (0x6A, 6), (0x6C, 3), (0x6D, 5), (0x6E, 1)]:
        query.u32(i).u32(v)
    found = await alice.call(0x2A, 7, query.bytes())
    r = Reader(found.body)
    assert r.u32() == 1
    assert (r.u32(), r.u32(), r.u32()) == (1, session_id, 10002)
    urls = [StationURL.parse(u) for u in r.string_list()]
    # the registered URL plus the address the server sees Bob at
    assert urls[0].address == "192.168.1.9"
    assert any(u.address == "127.0.0.1" and u.pid == 10002 for u in urls)
    attrs = {r.u32(): r.u32() for _ in range(r.u32())}
    assert 7 not in attrs and attrs[5] == 1  # IsPrivate hidden, one public participant


async def test_nat_probe_pushes_and_relay(env):
    server, storage, login, (_, _, relay_port) = env
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    await host_session(bob)
    target = f"prudp:/address=192.168.1.9;port={bob.local_address[1]};PID=10002;RVCID=1"
    reply = await alice.call(0x03, 1, Writer().string_list([target]).bytes())
    assert reply.success

    async def probes(client, n):
        got = []
        for _ in range(n):
            msg = await next_push(client, 0x03)
            assert msg.method == 2
            got.append(StationURL.parse(Reader(msg.body).string()))
        return got

    alice_rvcid = server.ctx.online(10001).rvcid
    bob_rvcid = server.ctx.online(10002).rvcid
    # caller: the requested URL, Bob's reflected address and the relay
    to_alice = await probes(alice, 3)
    # the relay URL a player gets names the peer behind it (the game matches
    # probe replies by IP + RVCID, and relayed replies carry the peer's RVCID)
    relay_for_alice = next(u for u in to_alice if u.port == relay_port)
    assert (relay_for_alice.pid, relay_for_alice.rvcid) == (10002, bob_rvcid)
    assert relay_for_alice.params["stream"] == "3" and relay_for_alice.params["type"] == "2"
    # target: the caller's own URL (acb-rdv form) and the relay
    to_bob = await probes(bob, 2)
    caller = next(u for u in to_bob if u.port != relay_port)
    assert caller.address == "127.0.0.1" and caller.params["stream"] == "3" and caller.rvcid
    relay_for_bob = next(u for u in to_bob if u.port == relay_port)
    assert (relay_for_bob.pid, relay_for_bob.rvcid) == (10001, alice_rvcid)
    # target, expand_caller: every URL Alice registered at login, naming Alice
    alice_own = {(u.address, u.port) for u in player(server.ctx.online(10001)).urls} - {("127.0.0.1", caller.port)}
    assert alice_own  # the test login registers a local URL
    own = await probes(bob, len(alice_own))
    assert {(u.address, u.port) for u in own} == alice_own
    assert all((u.pid, u.rvcid) == (10001, alice_rvcid) for u in own)
    await asyncio.sleep(0.1)
    assert server.nat.stats[("relay", "ok")] >= 2

    # the relay forwards between the first two endpoints that use it
    loop = asyncio.get_running_loop()
    a_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for s in (a_sock, b_sock):
        s.bind(("127.0.0.1", 0))
        s.setblocking(False)
    try:
        a_sock.sendto(b"hello-from-a", ("127.0.0.1", relay_port))
        await asyncio.sleep(0.05)
        b_sock.sendto(b"hello-from-b", ("127.0.0.1", relay_port))
        assert await asyncio.wait_for(loop.sock_recv(a_sock, 100), 1) == b"hello-from-b"
        a_sock.sendto(b"again", ("127.0.0.1", relay_port))
        assert await asyncio.wait_for(loop.sock_recv(b_sock, 100), 1) == b"again"
    finally:
        a_sock.close()
        b_sock.close()


async def test_contact_join_offers_relay(env):
    server, storage, login, (_, _, relay_port) = env
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    session_id = await host_session(bob)
    reply = await alice.call(0x72, 3, Writer().u32(1).string_list(["Bob"]).bytes())
    r = Reader(reply.body)
    assert r.u32() == 1
    assert (r.string(), r.u32(), r.bool(), r.bool()) == ("Bob", 10002, True, True)
    assert (r.u32(), r.u32(), r.u32()) == (1, session_id, 10002)
    urls = [StationURL.parse(u) for u in r.string_list()]
    assert urls[-1].port == relay_port
    assert (urls[-1].pid, urls[-1].rvcid) == (10002, server.ctx.online(10002).rvcid)
    # a later search by Alice lists the same per-recipient relay URL for Bob
    assert server.nat.host_candidates(server.ctx.online(10002), server.ctx.online(10001))[-1] == urls[-1]


AC_RELAY = "198.51.100.7"


async def host_session_via_ac_relay(bob: QuazalClient, server: AcbServer) -> str:
    """Bob hosts and registers a LAN URL plus an AC Relay URL (not his own IP)."""
    await host_session(bob)
    rvcid = server.ctx.online(10002).rvcid
    relayed = f"prudp:/address={AC_RELAY};port=40250;PID=10002;sid=1;RVCID={rvcid};stream=3;type=2"
    urls = [f"prudp:/address=192.168.1.9;port={bob.local_address[1]};PID=10002;RVCID={rvcid}", relayed]
    assert (await bob.call(0x2A, 21, Writer().string_list(urls).bytes())).success
    return relayed


async def test_ac_relay_url_passes_through_search(env):
    server, storage, login, _ = env
    server.nat.ac_relays = frozenset({AC_RELAY})
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    relayed = await host_session_via_ac_relay(bob, server)
    query = Writer().u32(1).u32(0).u32(5)
    for i, v in [(0x69, 0), (0x6A, 6), (0x6C, 3), (0x6D, 5), (0x6E, 1)]:
        query.u32(i).u32(v)
    found = await alice.call(0x2A, 7, query.bytes())
    r = Reader(found.body)
    assert r.u32() == 1
    r.u32(), r.u32(), r.u32()
    urls = r.string_list()
    # kept unchanged (address, port, RVCID), next to the reflected address
    assert relayed in urls
    assert any(StationURL.parse(u).address == "127.0.0.1" for u in urls)
    tags = dict((str(u), t) for u, t in server.nat.tagged_candidates(server.ctx.online(10002), None))
    assert tags[relayed] == "acrelay"
    assert "registered AC Relay URLs: 1" in server.nat.report()


async def test_ac_relay_url_pushed_as_probe_candidate(env):
    server, storage, login, (_, _, relay_port) = env
    server.nat.ac_relays = frozenset({AC_RELAY})
    alice = await login("Alice", "pw-a")
    bob = await login("Bob", "pw-b")
    relayed = await host_session_via_ac_relay(bob, server)
    target = f"prudp:/address=192.168.1.9;port={bob.local_address[1]};PID=10002"
    assert (await alice.call(0x03, 1, Writer().string_list([target]).bytes())).success
    pushed = set()
    for _ in range(4):  # requested URL, Bob's LAN URL, AC Relay URL, server relay
        msg = await next_push(alice, 0x03)
        pushed.add(Reader(msg.body).string())
    assert relayed in pushed
    await asyncio.sleep(0.1)
    assert server.nat.stats[("acrelay", "sent")] == 1
    # the caller listing the AC Relay URL itself is tagged too; this server's
    # own relay on the same address is not
    assert server.nat.origin(StationURL.parse(relayed), "client") == "acrelay"
    server.nat.ac_relays = frozenset({"127.0.0.1"})
    own = StationURL("prudp", address="127.0.0.1", port=relay_port)
    assert server.nat.origin(own, "client") == "client"
    assert server.nat.origin(own.with_params(port=1), "client") == "acrelay"


async def test_ac_relay_without_config_is_plain_known(env):
    server, storage, login, _ = env
    bob = await login("Bob", "pw-b")
    relayed = await host_session_via_ac_relay(bob, server)
    tags = dict((str(u), t) for u, t in server.nat.tagged_candidates(server.ctx.online(10002), None))
    assert tags[relayed] == "known"
    assert "AC Relay servers: none configured" in server.nat.report()


async def test_ping_and_idle_timeout(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    pong = await alice.ping()
    assert pong.has(Flag.ACK)
    server.prudp.idle_timeout = 0.2
    await asyncio.sleep(1.0)
    assert server.ctx.online(10001) is None


async def test_unacked_response_is_retransmitted(env):
    server, storage, login, _ = env
    alice = await login("Alice", "pw-a")
    server.prudp.retransmit_timeout = 0.2
    original = alice._on_data

    def no_acks(pkt, addr):
        # record the packet but never acknowledge it
        if not pkt.has(Flag.ACK) and pkt.payload:
            seen.append((pkt.seq, pkt.part))
            fut = alice.responses.pop(alice.call_id, None)
            if fut and not fut.done():
                fut.set_result(RMCMessage.decode(pkt.payload))
            return
        original(pkt, addr)

    seen: list = []
    alice._on_data = no_acks
    await alice.call(0x6F, 1, Writer().string("x").bytes())
    await asyncio.sleep(1.6)
    # the original send plus at least one resend of the same sequence number
    assert len(seen) >= 2 and len(set(seen)) == 1, seen
