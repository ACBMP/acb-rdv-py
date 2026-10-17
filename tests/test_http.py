import asyncio
import socket

import httpx
import pytest

from quazal.client import QuazalClient
from quazal.ddl import Reader
from quazal.storage import MemoryStorage, User

from acb_rdv.config import Config
from acb_rdv.server import AcbServer


def free(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(tmp_path):
    storage = MemoryStorage()
    storage.add_user(User(10001, "Alice"), "pw")
    storage.add_user(User(10002, "Bob"), "pw")
    cfg = Config(public_address="127.0.0.1", bind_host="127.0.0.1", status_host="127.0.0.1",
                 auth_port=free(socket.SOCK_DGRAM), secure_port=free(socket.SOCK_DGRAM),
                 status_port=free(socket.SOCK_STREAM),
                 relay_port_start=0, internal_key="secret", game_settings=str(tmp_path / "x"))
    srv = AcbServer(cfg, storage=storage)
    await srv.start()
    yield srv
    await srv.stop()


async def test_status_and_notify_webhook(server):
    cfg = server.config
    alice = await QuazalClient.open("127.0.0.1")
    try:
        assert (await alice.login(cfg.auth_port, "Alice", "pw")).success
        await alice.connect_secure(cfg.secure_port)
        base = f"http://127.0.0.1:{cfg.status_port}"
        async with httpx.AsyncClient() as http:
            assert (await http.get(base + "/")).text == "Status: OK"
            assert "[Name: Alice]" in (await http.get(base + "/clients")).text
            assert "No active sessions." in (await http.get(base + "/sessions")).text
            assert "InitiateProbe" in (await http.get(base + "/nat")).text
            event = {"type": "friend_request", "to_pid": 10001, "from_pid": 10002, "from_name": "Bob"}
            assert (await http.post(base + "/internal/notify", json=event)).status_code == 401
            ok = await http.post(base + "/internal/notify", json=event, headers={"X-Internal-Key": "secret"})
            assert ok.json() == {"delivered": True}
            offline = dict(event, to_pid=4242)
            resp = await http.post(base + "/internal/notify", json=offline, headers={"X-Internal-Key": "secret"})
            assert resp.json() == {"delivered": False}
        push = await asyncio.wait_for(alice.pushes.get(), 2)
        r = Reader(push.body)
        assert (push.protocol, r.u32(), r.u32(), r.u32(), r.u32(), r.string()) == (0x0E, 10002, 1001, 10002, 2, "Bob")
    finally:
        alice.close()


async def test_launcher_accept_updates_both_players(server):
    cfg = server.config
    alice, bob = await QuazalClient.open("127.0.0.1"), await QuazalClient.open("127.0.0.1")
    try:
        for c, name in ((alice, "Alice"), (bob, "Bob")):
            assert (await c.login(cfg.auth_port, name, "pw")).success
            await c.connect_secure(cfg.secure_port)
        event = {"type": "friend_accepted", "to_pid": 10001, "from_pid": 10002, "from_name": "Bob"}
        async with httpx.AsyncClient() as http:
            resp = await http.post(f"http://127.0.0.1:{cfg.status_port}/internal/notify", json=event,
                                   headers={"X-Internal-Key": "secret"})
        assert resp.json() == {"delivered": True}

        async def notes(client, n):
            out = []
            for _ in range(n):
                r = Reader((await asyncio.wait_for(client.pushes.get(), 2)).body)
                out.append((r.u32(), r.u32(), r.u32(), r.u32(), r.string()))
            return out

        # Alice: request accepted by Bob, and Bob is online
        assert await notes(alice, 2) == [(10002, 1001, 10002, 1, "Bob"), (10002, 5001, 10002, 1, "Bob")]
        # Bob: Alice is online
        assert await notes(bob, 1) == [(10001, 5001, 10001, 1, "Alice")]
    finally:
        alice.close()
        bob.close()
