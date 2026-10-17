"""RMC message framing (acb-rdv ``RMC/RMC.cs``, ``RMC/RMCP.cs``).

::

    u32 size of everything that follows
    u8  protocol id | 0x80 for requests   (0x7F / 0xFF: a u16 protocol id follows)
    request:  u32 call id, u32 method id, parameters
    response: u8 1, u32 call id, u32 method id | 0x8000, result     (success)
              u8 0, u32 error code, u32 call id                     (failure)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..ddl import Reader, Writer

METHOD_RESPONSE_BIT = 0x8000


class RMCError(Exception):
    """Raised by handlers to answer a call with a Quazal error code."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message or f"RMC error 0x{code:08X}")
        self.code = code


@dataclass
class RMCMessage:
    protocol: int
    is_request: bool
    call_id: int = 0
    method: int = 0
    success: bool = True
    error: int = 0
    body: bytes = b""

    def encode(self) -> bytes:
        w = Writer()
        if self.protocol < 0x7F:
            w.u8(self.protocol | (0x80 if self.is_request else 0))
        else:
            w.u8(0xFF if self.is_request else 0x7F)
            w.u16(self.protocol)
        if self.is_request:
            w.u32(self.call_id).u32(self.method).write(self.body)
        elif self.success:
            w.u8(1).u32(self.call_id).u32(self.method | METHOD_RESPONSE_BIT).write(self.body)
        else:
            w.u8(0).u32(self.error).u32(self.call_id)
        inner = w.bytes()
        return struct.pack("<I", len(inner)) + inner

    @classmethod
    def decode(cls, data: bytes) -> "RMCMessage":
        r = Reader(data)
        size = r.u32()
        if size > r.remaining():
            raise ValueError(f"RMC size {size} exceeds payload ({r.remaining()} bytes)")
        r = Reader(r.read(size))
        b = r.u8()
        is_request = bool(b & 0x80)
        protocol = b & 0x7F
        if protocol == 0x7F:
            protocol = r.u16()
        if is_request:
            call_id = r.u32()
            method = r.u32()
            return cls(protocol, True, call_id, method, body=r.rest())
        if r.u8() == 1:
            call_id = r.u32()
            method = r.u32() & ~METHOD_RESPONSE_BIT
            return cls(protocol, False, call_id, method, True, 0, r.rest())
        error = r.u32()
        call_id = r.u32()
        return cls(protocol, False, call_id, 0, False, error)

    @classmethod
    def request(cls, protocol: int, method: int, call_id: int, body: bytes = b"") -> "RMCMessage":
        return cls(protocol, True, call_id, method, body=body)

    @classmethod
    def response(cls, protocol: int, method: int, call_id: int, body: bytes = b"") -> "RMCMessage":
        return cls(protocol, False, call_id, method, True, 0, body)

    @classmethod
    def failure(cls, protocol: int, method: int, call_id: int, error: int) -> "RMCMessage":
        return cls(protocol, False, call_id, method, False, error)

    def short(self) -> str:
        kind = "req" if self.is_request else ("ok" if self.success else f"err 0x{self.error:08X}")
        return f"RMC {kind} proto=0x{self.protocol:X} method={self.method} call={self.call_id} ({len(self.body)}B)"
