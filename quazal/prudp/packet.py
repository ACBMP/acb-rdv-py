"""PRUDP v0 ("old Quazal") packet codec.

Header layout (acb-rdv ``PRUDP/PrudpPacket.cs``)::

    u8  source vport        (stream type << 4 | port)
    u8  destination vport
    u8  type | flags << 3
    u8  session id
    u32 signature
    u16 sequence id
    [u32 connection signature]   SYN and CONNECT only
    [u8  fragment id]            DATA only
    [u16 payload size]           only with FLAG_HAS_SIZE
    ... payload ...
    u8  checksum

The payload of everything except SYN and NAT-stream packets is prefixed with
a compression byte (0 = raw, otherwise the zlib compression ratio) and then,
for the OldRVSec stream, RC4-encrypted with a key that is reset per packet.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field

from ..crypto import rc4, zlib_compress, zlib_decompress


class StreamType(enum.IntEnum):
    UNUSED = 0
    DO = 1
    RV = 2
    OLD_RV_SEC = 3
    SB_MGMT = 4
    NAT = 5
    SESSION_DISCOVERY = 6
    NAT_ECHO = 7
    ROUTING = 8
    GAME = 9
    RV_SECURE = 10
    RELAY = 11


class PacketType(enum.IntEnum):
    SYN = 0
    CONNECT = 1
    DATA = 2
    DISCONNECT = 3
    PING = 4
    NATPING = 5


class Flag(enum.IntFlag):
    NONE = 0
    ACK = 1
    RELIABLE = 2
    NEED_ACK = 4
    HAS_SIZE = 8
    UNKNOWN = 16


@dataclass(frozen=True)
class VPort:
    stream: int
    port: int

    @classmethod
    def from_byte(cls, b: int) -> "VPort":
        return cls(b >> 4, b & 0xF)

    def to_byte(self) -> int:
        return ((self.stream & 0xF) << 4) | (self.port & 0xF)

    def __str__(self) -> str:
        try:
            name = StreamType(self.stream).name
        except ValueError:
            name = str(self.stream)
        return f"{name}:{self.port}"


# Server CONNECT acknowledgements without a payload (auth server) carry a
# zero connection signature followed by this unexplained byte.
AUTH_CONNECT_TRAILER = b"\x0f"


@dataclass
class Packet:
    src: VPort
    dst: VPort
    type: PacketType
    flags: Flag = Flag.NONE
    session_id: int = 0
    signature: int = 0
    seq: int = 0
    conn_sig: int = 0
    part: int = 0
    payload: bytes = b""
    compressed: bool = True
    # set when a payload-less CONNECT used the auth-server trailer form
    connect_trailer: bool = False
    raw_size: int = field(default=0, compare=False)

    def has(self, flag: Flag) -> bool:
        return bool(self.flags & flag)

    def short(self) -> str:
        f = "".join(c if self.flags & fl else " " for c, fl in (("R", Flag.RELIABLE), ("A", Flag.ACK), ("W", Flag.NEED_ACK), ("S", Flag.HAS_SIZE)))
        return f"{self.type.name}({f}) {self.src}->{self.dst} seq={self.seq} sig={self.signature:08X}"


class ChecksumError(ValueError):
    pass


class PacketCodec:
    """Encodes/decodes packets for one game's PRUDP settings."""

    def __init__(
        self,
        rc4_keys: dict[int, bytes] | None = None,
        checksum_settings: dict[int, int] | None = None,
        plain_streams: frozenset[int] = frozenset({StreamType.NAT}),
        compression_level: int = 6,
    ):
        self.rc4_keys = dict(rc4_keys or {})
        self.checksum_settings = dict(checksum_settings or {})
        self.plain_streams = plain_streams
        self.compression_level = compression_level

    # --- checksum -------------------------------------------------------

    def checksum(self, data: bytes) -> int:
        setting = self.checksum_settings.get(data[0] >> 4, 0)
        words = len(data) // 4
        total = sum(struct.unpack_from(f"<{words}I", data)) & 0xFFFFFFFF if words else 0
        leftover = len(data) & 3
        pos = len(data) - leftover
        extra = 0
        if leftover >= 2:
            extra += data[pos] + data[pos + 1]
            pos += 2
            leftover -= 2
        extra += setting + (data[pos] if leftover else 0)
        return (sum(total.to_bytes(4, "little")) + extra) & 0xFF

    # --- payload transform ----------------------------------------------

    def _transforms(self, p: Packet) -> bool:
        return p.type != PacketType.SYN and p.src.stream not in self.plain_streams

    def encode_payload(self, p: Packet) -> bytes:
        if not p.payload or not self._transforms(p):
            return p.payload
        if p.compressed:
            packed = zlib_compress(p.payload, self.compression_level)
            ratio = -(-len(p.payload) // len(packed))  # ceil, as the original does
            body = bytes([ratio & 0xFF]) + packed
        else:
            body = b"\x00" + p.payload
        key = self.rc4_keys.get(p.src.stream)
        return rc4(key, body) if key else body

    def decode_payload(self, p: Packet, raw: bytes) -> tuple[bytes, bool]:
        if not raw or not self._transforms(p):
            return raw, False
        key = self.rc4_keys.get(p.src.stream)
        body = rc4(key, raw) if key else raw
        if body[0] != 0:
            return zlib_decompress(body[1:]), True
        return body[1:], False

    # --- packets ----------------------------------------------------------

    def encode(self, p: Packet) -> bytes:
        out = bytearray()
        out += bytes((p.src.to_byte(), p.dst.to_byte(), (int(p.type) | (int(p.flags) << 3)) & 0xFF, p.session_id & 0xFF))
        out += struct.pack("<IH", p.signature & 0xFFFFFFFF, p.seq & 0xFFFF)
        if p.type == PacketType.CONNECT and not p.payload and p.connect_trailer:
            out += struct.pack("<I", 0) + AUTH_CONNECT_TRAILER
        elif p.type in (PacketType.SYN, PacketType.CONNECT):
            out += struct.pack("<I", p.conn_sig & 0xFFFFFFFF)
        if p.type == PacketType.DATA:
            out.append(p.part & 0xFF)
        body = self.encode_payload(p)
        if p.flags & Flag.HAS_SIZE:
            out += struct.pack("<H", len(body))
        out += body
        out.append(self.checksum(bytes(out)))
        return bytes(out)

    def decode(self, data: bytes, offset: int = 0, verify: bool = False) -> Packet:
        """Decode one packet starting at ``offset``; ``raw_size`` says how much it used."""
        buf = memoryview(data)[offset:]
        if len(buf) < 11:
            raise ValueError(f"datagram too short ({len(buf)} bytes)")
        src, dst, tf, sid = buf[0], buf[1], buf[2], buf[3]
        signature, seq = struct.unpack_from("<IH", buf, 4)
        ptype = PacketType(tf & 7)
        flags = Flag(tf >> 3)
        pos = 10
        conn_sig = part = 0
        if ptype in (PacketType.SYN, PacketType.CONNECT):
            (conn_sig,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        if ptype == PacketType.DATA:
            part = buf[pos]
            pos += 1
        if flags & Flag.HAS_SIZE:
            (size,) = struct.unpack_from("<H", buf, pos)
            pos += 2
        else:
            size = len(buf) - pos - 1
        if size < 0 or pos + size + 1 > len(buf):
            raise ValueError("payload runs past the end of the datagram")
        raw = bytes(buf[pos : pos + size])
        end = pos + size + 1
        if verify and self.checksum(bytes(buf[: pos + size])) != buf[pos + size]:
            raise ChecksumError(f"bad checksum on {ptype.name}")
        p = Packet(
            src=VPort.from_byte(src),
            dst=VPort.from_byte(dst),
            type=ptype,
            flags=flags,
            session_id=sid,
            signature=signature,
            seq=seq,
            conn_sig=conn_sig,
            part=part,
            raw_size=end,
        )
        if ptype == PacketType.CONNECT and raw == AUTH_CONNECT_TRAILER:
            p.connect_trailer = True
            p.payload, p.compressed = b"", True
        else:
            p.payload, p.compressed = self.decode_payload(p, raw)
        return p

    def decode_all(self, data: bytes, verify: bool = False) -> list[Packet]:
        """Decode every packet in a datagram (several fit only with HAS_SIZE)."""
        packets = []
        offset = 0
        while offset < len(data):
            p = self.decode(data, offset, verify)
            packets.append(p)
            offset += p.raw_size
        return packets
