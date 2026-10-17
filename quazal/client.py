"""A minimal Quazal client, for tests and smoke-testing a deployed server.

It speaks just enough PRUDP v0 to log in the way the game does::

    client = await QuazalClient.open("127.0.0.1")
    pid, ticket_session_key = await client.login(auth_port, "Player", "password")
    await client.connect_secure(secure_port)
    reply = await client.call(0x1D, 3)   # UbiAccountManagement.GetAccount
"""

from __future__ import annotations

import asyncio
import random
import struct
from dataclasses import dataclass, field

from .crypto import hmac_md5, rc4
from .ddl import AnyDataHolder, Reader, Writer
from .errors import RESULT_SUCCESS
from .kerberos import DEFAULT_INNER_TICKET, open_ticket
from .profile import GameProfile
from .prudp.packet import Flag, Packet, PacketCodec, PacketType, VPort
from .rmc.message import RMCMessage


class ClientError(Exception):
    pass


@dataclass
class _Conn:
    port: int
    server_sig: int = 0
    my_sig: int = 0
    session_id: int = 0
    seq: int = 1
    fragments: list[bytes] = field(default_factory=list)


class QuazalClient(asyncio.DatagramProtocol):
    def __init__(self, host: str, profile: GameProfile | None = None, timeout: float = 3.0):
        self.host = host
        self.profile = profile or GameProfile("test")
        self.codec: PacketCodec = self.profile.codec()
        self.timeout = timeout
        self.transport: asyncio.DatagramTransport | None = None
        self.inbox: asyncio.Queue[tuple[Packet, tuple]] = asyncio.Queue()
        self.responses: dict[int, asyncio.Future] = {}
        self.pushes: asyncio.Queue[RMCMessage] = asyncio.Queue()
        self.acks: dict[tuple[int, int], asyncio.Event] = {}
        self.call_id = 0
        self.conn: _Conn | None = None
        self.server_port = 0
        self.pid = 0
        self.session_key = b""
        self.answer_pushes = True
        self.raw: list[Packet] = []

    @classmethod
    async def open(cls, host: str, local_port: int = 0, **kw) -> "QuazalClient":
        client = cls(host, **kw)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: client, local_addr=("127.0.0.1" if host == "127.0.0.1" else "0.0.0.0", local_port))
        return client

    @property
    def local_address(self) -> tuple[str, int]:
        return self.transport.get_extra_info("sockname")[:2]  # type: ignore[union-attr]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def close(self) -> None:
        if self.transport:
            self.transport.close()

    # --- receive -----------------------------------------------------------------

    def datagram_received(self, data: bytes, addr) -> None:
        for pkt in self.codec.decode_all(data):
            self.raw.append(pkt)
            if pkt.type == PacketType.DATA:
                self._on_data(pkt, addr)
            else:
                self.inbox.put_nowait((pkt, addr))

    def _on_data(self, pkt: Packet, addr) -> None:
        if pkt.has(Flag.ACK):
            ev = self.acks.get((pkt.seq, pkt.part))
            if ev:
                ev.set()
            return
        if pkt.has(Flag.NEED_ACK):
            ack = Packet(pkt.dst, pkt.src, PacketType.DATA, Flag.ACK, pkt.session_id,
                         self.conn.server_sig if self.conn else 0, pkt.seq, part=pkt.part)
            self.transport.sendto(self.codec.encode(ack), addr)  # type: ignore[union-attr]
        conn = self.conn
        if pkt.part != 0:
            if conn:
                conn.fragments.append(pkt.payload)
            return
        payload = pkt.payload
        if conn and conn.fragments:
            payload = b"".join(conn.fragments) + payload
            conn.fragments = []
        msg = RMCMessage.decode(payload)
        if msg.is_request:
            self.pushes.put_nowait(msg)
            if self.answer_pushes and msg.protocol != 0x0E:
                self._send_data(RMCMessage.response(msg.protocol, msg.method, msg.call_id).encode(),
                                reliable=False)
            return
        fut = self.responses.pop(msg.call_id, None)
        if fut and not fut.done():
            fut.set_result(msg)

    async def _expect(self, ptype: PacketType) -> Packet:
        while True:
            pkt, _ = await asyncio.wait_for(self.inbox.get(), self.timeout)
            if pkt.type == ptype:
                return pkt

    # --- send ----------------------------------------------------------------------

    def _vports(self) -> tuple[VPort, VPort]:
        stream = self.profile.rmc_stream
        return VPort(stream, self.conn.port), VPort(stream, self.profile.server_port_number)  # type: ignore[union-attr]

    def _send(self, pkt: Packet) -> None:
        self.transport.sendto(self.codec.encode(pkt), (self.host, self.server_port))  # type: ignore[union-attr]

    def _send_data(self, body: bytes, reliable: bool = True) -> int:
        conn = self.conn
        assert conn is not None
        src, dst = self._vports()
        seq = conn.seq
        conn.seq += 1
        flags = (Flag.RELIABLE | Flag.NEED_ACK) if reliable else Flag.NEED_ACK
        self._send(Packet(src, dst, PacketType.DATA, flags, conn.session_id, conn.server_sig, seq, payload=body))
        return seq

    async def handshake(self, server_port: int, vport: int = 15, payload: bytes = b"") -> Packet:
        self.server_port = server_port
        self.conn = _Conn(vport, my_sig=random.getrandbits(32), session_id=random.randrange(1, 255))
        src, dst = self._vports()
        self._send(Packet(src, dst, PacketType.SYN, Flag.NEED_ACK, 0, 0, 0))
        syn = await self._expect(PacketType.SYN)
        self.conn.server_sig = syn.conn_sig
        self._send(Packet(src, dst, PacketType.CONNECT, Flag.RELIABLE | Flag.NEED_ACK, self.conn.session_id,
                          self.conn.server_sig, 1, conn_sig=self.conn.my_sig, payload=payload))
        self.conn.seq = 2
        return await self._expect(PacketType.CONNECT)

    async def call(self, protocol: int, method: int, body: bytes = b"") -> RMCMessage:
        self.call_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.responses[self.call_id] = fut
        self._send_data(RMCMessage.request(protocol, method, self.call_id, body).encode())
        return await asyncio.wait_for(fut, self.timeout)

    async def ping(self) -> Packet:
        src, dst = self._vports()
        self._send(Packet(src, dst, PacketType.PING, Flag.NEED_ACK, self.conn.session_id, self.conn.server_sig,  # type: ignore[union-attr]
                          0))
        return await self._expect(PacketType.PING)

    async def disconnect(self) -> Packet:
        src, dst = self._vports()
        conn = self.conn
        assert conn is not None
        self._send(Packet(src, dst, PacketType.DISCONNECT, Flag.RELIABLE | Flag.NEED_ACK, conn.session_id,
                          conn.server_sig, conn.seq))
        return await self._expect(PacketType.DISCONNECT)

    # --- the game's login sequence ---------------------------------------------------

    async def login(self, auth_port: int, username: str, password: str) -> RMCMessage:
        await self.handshake(auth_port)
        custom = Writer().string(username).string("").string(password).bytes()
        w = Writer().string(username)
        AnyDataHolder("UbiAuthenticationLoginCustomData", custom).write(w)
        reply = await self.call(0x0A, 2, w.bytes())
        if not reply.success:
            return reply
        r = Reader(reply.body)
        if r.u32() != RESULT_SUCCESS:
            raise ClientError("unexpected LoginEx result")
        self.pid = r.u32()
        ticket_reply = await self.call(0x0A, 3, Writer().u32(self.pid).u32(0x1000).bytes())
        tr = Reader(ticket_reply.body)
        tr.u32()
        ticket = tr.buffer()
        self.session_key, _server_pid, _inner = open_ticket(self.pid, ticket, self.profile.kerberos_password)
        return reply

    async def connect_secure(self, secure_port: int, challenge: int = 0x1234) -> Packet:
        blob = rc4(self.session_key, struct.pack("<III", self.pid, 1, challenge))
        payload = (struct.pack("<I", len(DEFAULT_INNER_TICKET)) + DEFAULT_INNER_TICKET
                   + struct.pack("<I", len(blob) + 16) + blob + hmac_md5(self.session_key, blob))
        return await self.handshake(secure_port, payload=payload)
