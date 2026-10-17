"""PRUDP v0 server transport.

Model
-----
* A :class:`Client` is one remote UDP endpoint. Quazal clients reuse one socket
  for the auth server, the secure server and the "Tracking" telemetry user, so
  per-player state (user, session key, server connection id) lives here.
* A :class:`Connection` is one PRUDP connection: a (server socket, client
  endpoint, client vport) triple established by SYN/CONNECT. Each has its own
  signature, session id, sequence counters, duplicate filter, fragment buffer
  and retransmission queue. (acb-rdv shared one set of these per client and
  patched over the resulting collisions; see ``PrudpHandler.cs``.)

Semantics kept from acb-rdv (verified against a packet capture of it):

* SYN is answered with the server connection id (``0x12345678`` counting up),
  reused for every connection from the same endpoint;
* CONNECT to the secure server carries a Kerberos ticket and an RC4-encrypted
  ``{pid, cid, challenge}``; the reply is ``{4, challenge + 1}`` (the Tracking
  vport gets a fixed challenge); payload-less CONNECTs are answered with the
  auth-server trailer form;
* requests are ACKed, responses go out ``NEED_ACK`` on an "unreliable"
  sequence counter, messages above the fragment size go out ``RELIABLE`` on a
  second counter with fragment ids ``1..n`` and ``0`` for the last;
* server-initiated requests go out from the server vport to the player vport.

Additions: incoming fragment reassembly, duplicate suppression (a retransmitted
request is re-ACKed and its cached response replayed instead of re-running the
handler), outgoing retransmission until ACKed, and per-connection state.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable

from ..capture import CaptureWriter
from ..crypto import rc4
from ..profile import GameProfile
from ..rmc.message import RMCMessage
from .packet import Flag, Packet, PacketCodec, PacketType, VPort

if TYPE_CHECKING:
    from ..rmc.dispatch import Dispatcher

log = logging.getLogger("quazal.prudp")

Address = tuple[str, int]

TRACKING_CHALLENGE = 0xF94C56FB
DEFAULT_SESSION_KEY = bytes.fromhex("9CB01D7A2C5A6C5BED12684569AE090D")


@dataclass
class Pending:
    data: bytes
    sent_at: float
    attempts: int = 1


@dataclass
class Connection:
    socket: "ServerSocket"
    client: "Client"
    client_vport: VPort
    server_vport: VPort
    signature: int = 0            # the client's connection signature (from CONNECT)
    session_id: int = 0
    connected: bool = False
    seq_unreliable: int = 1
    seq_reliable: int = 1
    last_seen: float = field(default_factory=time.monotonic)
    # duplicate filter: seq -> cached response datagrams (None while in flight)
    seen: "OrderedDict[int, list[bytes] | None]" = field(default_factory=OrderedDict)
    fragments: dict[int, tuple[int, bytes]] = field(default_factory=dict)
    unacked: dict[tuple[int, int], Pending] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def port(self) -> int:
        return self.client_vport.port

    def __str__(self) -> str:
        return f"{self.client}/{self.socket.name}:{self.port}"

    def next_seq(self, reliable: bool) -> int:
        if reliable:
            seq, self.seq_reliable = self.seq_reliable, (self.seq_reliable + 1) & 0xFFFF
        else:
            seq, self.seq_unreliable = self.seq_unreliable, (self.seq_unreliable + 1) & 0xFFFF
        return seq

    def send_rmc(
        self,
        message: bytes,
        *,
        session_id: int | None = None,
        part: int = 0,
        compressed: bool = True,
        src: VPort | None = None,
        dst: VPort | None = None,
    ) -> list[bytes]:
        """Send an RMC message, fragmenting it if needed; returns the datagrams."""
        server = self.socket.server
        src = src or self.server_vport
        dst = dst or self.client_vport
        sid = self.session_id if session_id is None else session_id
        size = server.profile.fragment_size
        datagrams = []
        if len(message) < size:
            pkt = Packet(src, dst, PacketType.DATA, Flag.NEED_ACK, sid, self.signature,
                         self.next_seq(False), part=part, payload=message, compressed=compressed)
            datagrams.append(self._send_tracked(pkt))
        else:
            chunks = [message[i : i + size] for i in range(0, len(message), size)]
            for n, chunk in enumerate(chunks, 1):
                pkt = Packet(src, dst, PacketType.DATA, Flag.RELIABLE | Flag.NEED_ACK, sid, self.signature,
                             self.next_seq(True), part=0 if n == len(chunks) else n,
                             payload=chunk, compressed=compressed)
                datagrams.append(self._send_tracked(pkt))
        return datagrams

    def _send_tracked(self, pkt: Packet) -> bytes:
        data = self.socket.server.codec.encode(pkt)
        self.unacked[(pkt.seq, pkt.part)] = Pending(data, time.monotonic())
        self.socket.send(data, self.client.addr, pkt)
        return data

    def acknowledge(self, seq: int, part: int) -> None:
        self.unacked.pop((seq, part), None)


@dataclass
class Client:
    addr: Address
    conn_id: int
    rvcid: int = field(default_factory=lambda: random.getrandbits(32))
    session_key: bytes = DEFAULT_SESSION_KEY
    user: Any = None              # set by the authentication protocol
    tracking_user: Any = None
    server_pid: int = 0
    server_port: int = 0
    call_counter: int = 0
    connections: dict[tuple[str, int], Connection] = field(default_factory=dict)
    pending_calls: dict[int, asyncio.Future] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.monotonic)
    # free-form per-client state for protocol implementations
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return getattr(self.user, "pid", 0) if self.user is not None else 0

    @property
    def name(self) -> str:
        return getattr(self.user, "name", "") if self.user is not None else ""

    def __str__(self) -> str:
        who = f"{self.name}({self.pid})" if self.user is not None else "anonymous"
        return f"{who}@{self.addr[0]}:{self.addr[1]}"

    def player_connection(self, socket_name: str | None = None) -> Connection | None:
        """The connection that carries the player's RMC traffic (secure server first)."""
        best = None
        for (sock, port), conn in self.connections.items():
            if not conn.connected or port != conn.socket.server.profile.player_port_number:
                continue
            if socket_name is not None and sock != socket_name:
                continue
            if best is None or conn.socket.secure and not best.socket.secure:
                best = conn
        return best


class ServerSocket(asyncio.DatagramProtocol):
    """One listening UDP port (e.g. the auth server or the secure server)."""

    def __init__(self, server: "PRUDPServer", name: str, server_pid: int, target_port: int, secure: bool):
        self.server = server
        self.name = name
        self.server_pid = server_pid
        self.target_port = target_port   # the secure server port clients get from this socket
        self.secure = secure
        self.transport: asyncio.DatagramTransport | None = None
        self.local_port = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.local_port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data: bytes, addr: Address) -> None:
        self.server.datagram_received(self, data, (addr[0], addr[1]))

    def error_received(self, exc: Exception) -> None:
        log.debug("%s socket error: %s", self.name, exc)

    def send(self, data: bytes, addr: Address, pkt: Packet | None = None) -> None:
        if self.transport is None:
            return
        self.transport.sendto(data, addr)
        if self.server.capture:
            self.server.capture.write(True, data)
        if pkt is not None and log.isEnabledFor(logging.DEBUG):
            log.debug("[%s] -> %s:%d %s", self.name, addr[0], addr[1], pkt.short())


ConnectHook = Callable[[Client, Connection, int, int], Awaitable[bool]]
RemovedHook = Callable[[Client, str], Awaitable[None]]


class PRUDPServer:
    """Owns the sockets, the client table and the PRUDP state machine."""

    def __init__(
        self,
        profile: GameProfile,
        dispatcher: "Dispatcher",
        *,
        idle_timeout: float = 20.0,
        retransmit_timeout: float = 1.5,
        max_retransmits: int = 3,
        capture: CaptureWriter | None = None,
    ):
        self.profile = profile
        self.codec: PacketCodec = profile.codec()
        self.dispatcher = dispatcher
        self.idle_timeout = idle_timeout
        self.retransmit_timeout = retransmit_timeout
        self.max_retransmits = max_retransmits
        self.capture = capture
        self.sockets: dict[str, ServerSocket] = {}
        self.clients_by_addr: dict[Address, Client] = {}
        self.clients_by_id: dict[int, Client] = {}
        self._next_conn_id = profile.first_connection_id or 0
        self._ignored_nat_times: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._housekeeping: asyncio.Task | None = None
        self.on_connect: ConnectHook | None = None
        self.on_client_removed: list[RemovedHook] = []

    # --- lifecycle -------------------------------------------------------

    async def listen(self, name: str, host: str, port: int, *, server_pid: int,
                     target_port: int, secure: bool) -> ServerSocket:
        loop = asyncio.get_running_loop()
        sock = ServerSocket(self, name, server_pid, target_port, secure)
        await loop.create_datagram_endpoint(lambda: sock, local_addr=(host, port))
        self.sockets[name] = sock
        log.info("%s server listening on udp/%s:%d", name, host, sock.local_port)
        if self._housekeeping is None:
            self._housekeeping = loop.create_task(self._housekeeping_loop())
        return sock

    def close(self) -> None:
        if self._housekeeping:
            self._housekeeping.cancel()
        for task in list(self._tasks):
            task.cancel()
        for sock in self.sockets.values():
            if sock.transport:
                sock.transport.close()

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    # --- receive path ----------------------------------------------------

    def datagram_received(self, sock: ServerSocket, data: bytes, addr: Address) -> None:
        if self.capture:
            self.capture.write(False, data)
        try:
            packets = self.codec.decode_all(data)
        except Exception as exc:  # noqa: BLE001 - malformed input must never kill the server
            log.warning("[%s] undecodable datagram from %s:%d (%d bytes): %s", sock.name, *addr, len(data), exc)
            return
        for pkt in packets:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("[%s] <- %s:%d %s", sock.name, addr[0], addr[1], pkt.short())
            try:
                self._handle_packet(sock, pkt, addr)
            except Exception:  # noqa: BLE001
                log.exception("[%s] error handling %s from %s:%d", sock.name, pkt.type.name, *addr)

    def _handle_packet(self, sock: ServerSocket, pkt: Packet, addr: Address) -> None:
        if pkt.type == PacketType.SYN:
            self._on_syn(sock, pkt, addr)
            return
        if pkt.type == PacketType.NATPING:
            self._on_natping(sock, pkt, addr)
            return
        client = self.clients_by_id.get(pkt.signature)
        if client is None:
            log.debug("[%s] %s from %s:%d with unknown signature %08X", sock.name, pkt.type.name, *addr, pkt.signature)
            return
        if client.addr != addr:
            if client.addr[0] != addr[0]:
                # connection ids are guessable enough that a move to another host
                # must not redirect a player's traffic
                log.debug("[%s] %s for %s from foreign address %s:%d ignored", sock.name, pkt.type.name,
                          client, *addr)
                return
            log.info("%s moved to port %d", client, addr[1])
            self._rebind(client, addr)
        now = time.monotonic()
        client.last_seen = now
        conn = client.connections.get((sock.name, pkt.src.port))
        if pkt.type == PacketType.CONNECT:
            if not pkt.has(Flag.ACK):
                self.spawn(self._on_connect(sock, client, pkt))
            return
        if conn is None:
            log.debug("[%s] %s for unknown connection %s:%d", sock.name, pkt.type.name, client, pkt.src.port)
            return
        conn.last_seen = now
        if pkt.type == PacketType.DATA:
            self._on_data(conn, pkt)
        elif pkt.type == PacketType.PING:
            self._reply(conn, pkt, PacketType.PING, signature=conn.signature)
        elif pkt.type == PacketType.DISCONNECT:
            self._reply(conn, pkt, PacketType.DISCONNECT, signature=conn.signature)
            self.spawn(self._on_disconnect(conn))

    def _new_connection_id(self) -> int:
        if self.profile.first_connection_id is not None:
            # sequential, as acb-rdv did (0x12345678, 0x12345679, ...)
            while self._next_conn_id in self.clients_by_id or self._next_conn_id == 0:
                self._next_conn_id = (self._next_conn_id + 1) & 0xFFFFFFFF
            conn_id = self._next_conn_id
            self._next_conn_id = (self._next_conn_id + 1) & 0xFFFFFFFF
            return conn_id
        while True:
            conn_id = random.getrandbits(32)
            if conn_id and conn_id not in self.clients_by_id:
                return conn_id

    def _rebind(self, client: Client, addr: Address) -> None:
        self.clients_by_addr.pop(client.addr, None)
        client.addr = addr
        self.clients_by_addr[addr] = client

    def _reply(self, conn: Connection, pkt: Packet, ptype: PacketType, *, signature: int,
               conn_sig: int = 0, payload: bytes = b"", trailer: bool = False) -> None:
        reply = Packet(pkt.dst, pkt.src, ptype, Flag.ACK, pkt.session_id, signature, pkt.seq,
                       conn_sig=conn_sig, part=pkt.part, payload=payload, connect_trailer=trailer)
        conn.socket.send(self.codec.encode(reply), conn.client.addr, reply)

    def _on_syn(self, sock: ServerSocket, pkt: Packet, addr: Address) -> None:
        client = self.clients_by_addr.get(addr)
        if client is None:
            client = Client(addr, self._new_connection_id())
            self.clients_by_addr[addr] = client
            self.clients_by_id[client.conn_id] = client
        client.last_seen = time.monotonic()
        # a SYN (re)starts the connection on this vport
        conn = Connection(sock, client, pkt.src, pkt.dst)
        client.connections[(sock.name, pkt.src.port)] = conn
        reply = Packet(pkt.dst, pkt.src, PacketType.SYN, Flag.ACK, pkt.session_id, pkt.signature,
                       pkt.seq, conn_sig=client.conn_id)
        sock.send(self.codec.encode(reply), addr, reply)

    async def _on_connect(self, sock: ServerSocket, client: Client, pkt: Packet) -> None:
        conn = client.connections.get((sock.name, pkt.src.port))
        if conn is None:
            conn = Connection(sock, client, pkt.src, pkt.dst)
            client.connections[(sock.name, pkt.src.port)] = conn
        conn.signature = pkt.conn_sig
        conn.session_id = pkt.session_id
        conn.connected = True
        client.server_pid = sock.server_pid
        client.server_port = sock.target_port
        payload = b""
        if pkt.payload:
            payload = await self._connect_payload(client, conn, pkt)
            if payload is None:
                return
        self._reply(conn, pkt, PacketType.CONNECT, signature=pkt.conn_sig, conn_sig=client.conn_id,
                    payload=payload, trailer=not payload)

    async def _connect_payload(self, client: Client, conn: Connection, pkt: Packet) -> bytes | None:
        try:
            ticket_len = struct.unpack_from("<I", pkt.payload, 0)[0]
            pos = 4 + ticket_len
            (blob_len,) = struct.unpack_from("<I", pkt.payload, pos)
            blob = pkt.payload[pos + 4 : pos + 4 + blob_len - 16]
            pid, cid, challenge = struct.unpack_from("<III", rc4(client.session_key, blob))
        except (struct.error, IndexError):
            log.warning("%s sent a malformed CONNECT payload", client)
            return None
        if conn.port != self.profile.player_port_number:
            # the Tracking user's connection; the original server used a fixed challenge
            return struct.pack("<II", 4, TRACKING_CHALLENGE)
        if self.on_connect is not None and not await self.on_connect(client, conn, pid, cid):
            conn.connected = False
            return None
        log.info("CONNECT %s pid=%d cid=%d", client, pid, cid)
        return struct.pack("<II", 4, (challenge + 1) & 0xFFFFFFFF)

    def _on_data(self, conn: Connection, pkt: Packet) -> None:
        if pkt.has(Flag.ACK):
            conn.acknowledge(pkt.seq, pkt.part)
            return
        if pkt.src.stream != self.profile.rmc_stream:
            return
        if pkt.has(Flag.NEED_ACK):
            self._reply(conn, pkt, PacketType.DATA, signature=conn.signature)
        conn.session_id = pkt.session_id
        if pkt.has(Flag.RELIABLE):
            if pkt.seq in conn.seen:
                cached = conn.seen[pkt.seq]
                log.debug("%s duplicate seq %d%s", conn, pkt.seq, " (replaying response)" if cached else "")
                for datagram in cached or ():
                    conn.socket.send(datagram, conn.client.addr)
                return
            conn.seen[pkt.seq] = None
            while len(conn.seen) > 256:
                conn.seen.popitem(last=False)
        if pkt.part != 0:
            conn.fragments[pkt.seq] = (pkt.part, pkt.payload)
            return
        payload = pkt.payload
        if conn.fragments:
            ordered = [data for _, (_, data) in sorted(conn.fragments.items())]
            conn.fragments.clear()
            payload = b"".join(ordered) + payload
        try:
            message = RMCMessage.decode(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s sent an undecodable RMC message: %s", conn, exc)
            return
        if message.is_request:
            # acb-rdv numbers server pushes after the highest client call id seen
            conn.client.call_counter = max(conn.client.call_counter, message.call_id)
            self.spawn(self._dispatch(conn, pkt, message))
        else:
            self._on_response(conn, message)

    async def _dispatch(self, conn: Connection, pkt: Packet, message: RMCMessage) -> None:
        async with conn.lock:
            response = await self.dispatcher.dispatch(conn, message)
            if response is None:
                return
            body, after = response
            datagrams = conn.send_rmc(body, session_id=pkt.session_id, part=pkt.part,
                                      compressed=pkt.compressed, src=pkt.dst, dst=pkt.src)
            if pkt.has(Flag.RELIABLE) and pkt.seq in conn.seen:
                conn.seen[pkt.seq] = datagrams
        for action in after:
            try:
                result = action()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("post-response action failed for %s", conn)

    def _on_response(self, conn: Connection, message: RMCMessage) -> None:
        future = conn.client.pending_calls.pop(message.call_id, None)
        status = "ok" if message.success else f"error 0x{message.error:08X}"
        log.info("%s answered push proto=0x%X method=%d call=%d: %s",
                 conn.client, message.protocol, message.method, message.call_id, status)
        if future is not None and not future.done():
            future.set_result(message)

    def _on_natping(self, sock: ServerSocket, pkt: Packet, addr: Address) -> None:
        """Echo a NAT probe, then probe back (acb-rdv ``PrudpHandler`` NATPING case)."""
        if len(pkt.payload) < 13:
            return
        flag = pkt.payload[0]
        rvcid, stamp = struct.unpack_from("<IQ", pkt.payload, 1)
        if stamp in self._ignored_nat_times:
            self._ignored_nat_times.discard(stamp)
            return
        client = self.clients_by_id.get(pkt.signature)
        if client is not None:
            rvcid = client.rvcid
        log.info("NATPING from %s:%d (flag=%d) - echoing", addr[0], addr[1], flag)
        echo = 0 if flag == 1 else 1
        reply = Packet(pkt.src, pkt.dst, pkt.type, pkt.flags, pkt.session_id, pkt.signature, pkt.seq,
                       part=pkt.part, payload=struct.pack("<BIQ", echo, rvcid, stamp), compressed=False)
        sock.send(self.codec.encode(reply), addr, reply)
        fresh = time.time_ns() & 0xFFFFFFFFFFFFFFFF
        self._ignored_nat_times.add(fresh)
        if len(self._ignored_nat_times) > 1024:
            self._ignored_nat_times.pop()
        probe = Packet(pkt.src, pkt.dst, pkt.type, pkt.flags, pkt.session_id, pkt.signature, pkt.seq,
                       part=pkt.part, payload=struct.pack("<BIQ", 1 - echo, rvcid, fresh), compressed=False)
        sock.send(self.codec.encode(probe), addr, probe)

    async def _on_disconnect(self, conn: Connection) -> None:
        client = conn.client
        client.connections.pop((conn.socket.name, conn.port), None)
        log.info("DISCONNECT %s", conn)
        if conn.socket.secure and conn.port == self.profile.player_port_number:
            await self.remove_client(client, "disconnect")

    # --- client table ----------------------------------------------------

    def clients(self) -> Iterable[Client]:
        return list(self.clients_by_id.values())

    async def remove_client(self, client: Client, reason: str) -> None:
        if self.clients_by_id.get(client.conn_id) is not client:
            return
        del self.clients_by_id[client.conn_id]
        if self.clients_by_addr.get(client.addr) is client:
            del self.clients_by_addr[client.addr]
        for future in client.pending_calls.values():
            if not future.done():
                future.cancel()
        client.pending_calls.clear()
        log.info("removed %s (%s)", client, reason)
        for hook in self.on_client_removed:
            try:
                await hook(client, reason)
            except Exception:  # noqa: BLE001
                log.exception("client removal hook failed for %s", client)

    # --- server-initiated calls -------------------------------------------

    def push(self, client: Client, protocol: int, method: int, body: bytes, *,
             expect_response: bool = False) -> asyncio.Future | None:
        """Send an RMC request to a client over its player connection."""
        conn = client.player_connection()
        if conn is None:
            log.warning("cannot push proto=0x%X method=%d to %s: no player connection", protocol, method, client)
            return None
        client.call_counter = (client.call_counter + 1) & 0xFFFFFFFF
        call_id = client.call_counter
        future: asyncio.Future | None = None
        if expect_response:
            future = asyncio.get_running_loop().create_future()
            client.pending_calls[call_id] = future
        message = RMCMessage.request(protocol, method, call_id, body).encode()
        src = VPort(self.profile.rmc_stream, self.profile.server_port_number)
        dst = VPort(self.profile.rmc_stream, self.profile.player_port_number)
        conn.send_rmc(message, src=src, dst=dst)
        return future

    # --- housekeeping ------------------------------------------------------

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            for client in self.clients():
                if now - client.last_seen > self.idle_timeout:
                    await self.remove_client(client, "timeout")
                    continue
                for conn in list(client.connections.values()):
                    self._retransmit(conn, now)

    def _retransmit(self, conn: Connection, now: float) -> None:
        for key, pending in list(conn.unacked.items()):
            if now - pending.sent_at < self.retransmit_timeout * pending.attempts:
                continue
            if pending.attempts > self.max_retransmits:
                log.debug("%s gave up on seq=%d part=%d", conn, *key)
                del conn.unacked[key]
                continue
            pending.attempts += 1
            pending.sent_at = now
            conn.socket.send(pending.data, conn.client.addr)
