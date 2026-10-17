"""Little-endian byte stream reader/writer for Quazal DDL encoding.

Wire conventions (from acb-rdv ``QuazalWV/Helper.cs``):

* integers and floats are little-endian;
* ``bool`` is one byte;
* ``string`` is ``u16 length`` (including the NUL terminator), the characters,
  then a NUL byte. acb-rdv writes one byte per character, so the default
  encoding here is Latin-1; games that really use UTF-8 can switch it per
  stream;
* ``buffer`` is ``u32 length`` + bytes, ``qbuffer`` is ``u16 length`` + bytes;
* lists are ``u32 count`` followed by the items.
"""

from __future__ import annotations

import struct
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

DEFAULT_ENCODING = "latin-1"


class StreamError(ValueError):
    """Raised when a stream is truncated or malformed."""


class Reader:
    __slots__ = ("data", "pos", "encoding")

    def __init__(self, data: bytes, pos: int = 0, encoding: str = DEFAULT_ENCODING):
        self.data = bytes(data)
        self.pos = pos
        self.encoding = encoding

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise StreamError(f"read of {n} bytes at {self.pos} exceeds {len(self.data)}")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def rest(self) -> bytes:
        chunk = self.data[self.pos :]
        self.pos = len(self.data)
        return chunk

    def _unpack(self, fmt: str, size: int):
        return struct.unpack_from(fmt, self.read(size))[0]

    def u8(self) -> int:
        return self._unpack("<B", 1)

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def u32(self) -> int:
        return self._unpack("<I", 4)

    def u64(self) -> int:
        return self._unpack("<Q", 8)

    def s32(self) -> int:
        return self._unpack("<i", 4)

    def f32(self) -> float:
        return self._unpack("<f", 4)

    def f64(self) -> float:
        return self._unpack("<d", 8)

    def bool(self) -> bool:
        return self.u8() != 0

    def string(self) -> str:
        length = self.u16()
        if length == 0:
            return ""
        raw = self.read(length)
        # the terminator is part of the length; tolerate a missing NUL
        if raw[-1:] == b"\x00":
            raw = raw[:-1]
        return raw.decode(self.encoding, errors="replace")

    def buffer(self) -> bytes:
        return self.read(self.u32())

    def qbuffer(self) -> bytes:
        return self.read(self.u16())

    def list(self, item: Callable[["Reader"], T]) -> list[T]:
        return [item(self) for _ in range(self.u32())]

    def string_list(self) -> list[str]:
        return self.list(Reader.string)

    def u32_list(self) -> list[int]:
        return self.list(Reader.u32)


class Writer:
    __slots__ = ("buf", "encoding")

    def __init__(self, encoding: str = DEFAULT_ENCODING):
        self.buf = bytearray()
        self.encoding = encoding

    def bytes(self) -> bytes:
        return bytes(self.buf)

    def __len__(self) -> int:
        return len(self.buf)

    def write(self, data: bytes) -> "Writer":
        self.buf += data
        return self

    def u8(self, v: int) -> "Writer":
        self.buf += struct.pack("<B", v & 0xFF)
        return self

    def u16(self, v: int) -> "Writer":
        self.buf += struct.pack("<H", v & 0xFFFF)
        return self

    def u32(self, v: int) -> "Writer":
        self.buf += struct.pack("<I", v & 0xFFFFFFFF)
        return self

    def u64(self, v: int) -> "Writer":
        self.buf += struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)
        return self

    def s32(self, v: int) -> "Writer":
        self.buf += struct.pack("<i", v)
        return self

    def f32(self, v: float) -> "Writer":
        self.buf += struct.pack("<f", v)
        return self

    def f64(self, v: float) -> "Writer":
        self.buf += struct.pack("<d", v)
        return self

    def bool(self, v: bool) -> "Writer":
        return self.u8(1 if v else 0)

    def string(self, v: str | None) -> "Writer":
        # acb-rdv writes a null string as an empty one (length 1, NUL)
        raw = (v or "").encode(self.encoding, errors="replace")
        self.u16(len(raw) + 1)
        self.buf += raw
        self.buf += b"\x00"
        return self

    def buffer(self, v: bytes) -> "Writer":
        self.u32(len(v))
        self.buf += v
        return self

    def qbuffer(self, v: bytes) -> "Writer":
        self.u16(len(v))
        self.buf += v
        return self

    def list(self, items: Iterable[T], item: Callable[["Writer", T], object]) -> "Writer":
        items = list(items)
        self.u32(len(items))
        for it in items:
            item(self, it)
        return self

    def string_list(self, items: Iterable[str]) -> "Writer":
        return self.list(items, Writer.string)

    def u32_list(self, items: Iterable[int]) -> "Writer":
        return self.list(items, Writer.u32)
