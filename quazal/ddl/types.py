"""Declarative DDL structures.

A :class:`Structure` subclass is a dataclass whose field annotations name the
wire codec of each field::

    class GameSessionKey(Structure):
        type_id: U32 = 0
        session_id: U32 = 0

    class GameSession(Structure):
        type_id: U32 = 0
        attributes: ListOf(Property) = field(default_factory=list)

Fields are (de)serialised in declaration order. Structures nest, and
``ListOf``/``MapOf`` wrap any codec. Irregular types can override
``read``/``write`` directly.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import field  # re-exported for structure definitions
from typing import Annotated, Any, Callable, ClassVar, get_args, get_origin, get_type_hints

from .stream import Reader, Writer

__all__ = [
    "Codec", "Structure", "field", "U8", "U16", "U32", "U64", "S32", "F32", "F64",
    "Bool", "String", "Buffer", "QBuffer", "DateTime", "ListOf", "MapOf",
    "qdatetime_from", "qdatetime_to", "qdatetime_now", "StationURL", "StationURLCodec",
    "Station", "AnyDataHolder", "ResultRange", "Property",
]


class Codec:
    """Reads and writes one value of some wire type."""

    def __init__(self, name: str, read: Callable[[Reader], Any], write: Callable[[Writer, Any], Any]):
        self.name = name
        self._read = read
        self._write = write

    def read(self, r: Reader) -> Any:
        return self._read(r)

    def write(self, w: Writer, v: Any) -> None:
        self._write(w, v)

    def __repr__(self) -> str:
        return f"<Codec {self.name}>"


_u8 = Codec("u8", Reader.u8, Writer.u8)
_u16 = Codec("u16", Reader.u16, Writer.u16)
_u32 = Codec("u32", Reader.u32, Writer.u32)
_u64 = Codec("u64", Reader.u64, Writer.u64)
_s32 = Codec("s32", Reader.s32, Writer.s32)
_f32 = Codec("f32", Reader.f32, Writer.f32)
_f64 = Codec("f64", Reader.f64, Writer.f64)
_bool = Codec("bool", Reader.bool, Writer.bool)
_string = Codec("string", Reader.string, Writer.string)
_buffer = Codec("buffer", Reader.buffer, Writer.buffer)
_qbuffer = Codec("qbuffer", Reader.qbuffer, Writer.qbuffer)

U8 = Annotated[int, _u8]
U16 = Annotated[int, _u16]
U32 = Annotated[int, _u32]
U64 = Annotated[int, _u64]
S32 = Annotated[int, _s32]
F32 = Annotated[float, _f32]
F64 = Annotated[float, _f64]
Bool = Annotated[bool, _bool]
String = Annotated[str, _string]
Buffer = Annotated[bytes, _buffer]
QBuffer = Annotated[bytes, _qbuffer]
# Quazal DateTime is a bit-packed u64, kept raw on the Python side
DateTime = Annotated[int, _u64]


def codec_for(hint: Any) -> Codec:
    if get_origin(hint) is Annotated:
        for meta in get_args(hint)[1:]:
            if isinstance(meta, Codec):
                return meta
    if isinstance(hint, type) and issubclass(hint, Structure):
        return hint.codec()
    if isinstance(hint, Codec):
        return hint
    raise TypeError(f"no DDL codec for annotation {hint!r}")


def ListOf(item: Any) -> Any:
    inner = codec_for(item)
    codec = Codec(
        f"list<{inner.name}>",
        lambda r: r.list(inner.read),
        lambda w, v: w.list(v, inner.write),
    )
    return Annotated[list, codec]


def MapOf(key: Any, value: Any) -> Any:
    kc, vc = codec_for(key), codec_for(value)

    def read(r: Reader) -> dict:
        return {kc.read(r): vc.read(r) for _ in range(r.u32())}

    def write(w: Writer, v: dict) -> None:
        w.u32(len(v))
        for k, val in v.items():
            kc.write(w, k)
            vc.write(w, val)

    return Annotated[dict, Codec(f"map<{kc.name},{vc.name}>", read, write)]


class Structure:
    """Base class for DDL structures; subclasses become dataclasses."""

    _fields_: ClassVar[tuple[tuple[str, Codec], ...]] = ()
    _codec_: ClassVar[Codec | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        dataclasses.dataclass(cls)
        hints = get_type_hints(cls, include_extras=True)
        cls._fields_ = tuple((f.name, codec_for(hints[f.name])) for f in dataclasses.fields(cls))
        cls._codec_ = None

    @classmethod
    def codec(cls) -> Codec:
        if cls.__dict__.get("_codec_") is None:
            cls._codec_ = Codec(cls.__name__, cls.read, lambda w, v: v.write(w))
        return cls._codec_  # type: ignore[return-value]

    @classmethod
    def read(cls, r: Reader):
        return cls(**{name: codec.read(r) for name, codec in cls._fields_})

    def write(self, w: Writer) -> None:
        for name, codec in self._fields_:
            codec.write(w, getattr(self, name))

    @classmethod
    def decode(cls, data: bytes, encoding: str | None = None):
        r = Reader(data) if encoding is None else Reader(data, encoding=encoding)
        return cls.read(r)

    def encode(self, encoding: str | None = None) -> bytes:
        w = Writer() if encoding is None else Writer(encoding)
        self.write(w)
        return w.bytes()


# --- Quazal DateTime -----------------------------------------------------

def qdatetime_from(t: _dt.datetime) -> int:
    return (
        (t.year << 26)
        | (t.month << 22)
        | (t.day << 17)
        | (t.hour << 12)
        | (t.minute << 6)
        | t.second
    )


def qdatetime_to(raw: int) -> _dt.datetime | None:
    try:
        return _dt.datetime(
            raw >> 26,
            (raw >> 22) & 0xF,
            (raw >> 17) & 0x1F,
            (raw >> 12) & 0x1F,
            (raw >> 6) & 0x3F,
            raw & 0x3F,
        )
    except ValueError:
        return None


def qdatetime_now() -> int:
    return qdatetime_from(_dt.datetime.now())


# --- StationURL ----------------------------------------------------------

def _signed32(v: Any) -> int:
    """RVCID as the game writes it: a signed 32-bit int.

    Quazal reads URL ints with atoi, which clamps "3912695145" to 0x7FFFFFFF
    on Windows; a client then sees two RVCIDs in one host's URL list and
    JoinSessionImpl fails (0xe0000016) before sending a packet.
    """
    n = int(v) & 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


class StationURL:
    """``scheme:/key=value;key=value`` endpoint description.

    Parameters keep their original order and any keys this code does not know
    about (``natf``, ``natm``, ``upnp``, ``pmp``, ...) so a URL can be passed
    through unchanged. :meth:`canonical` re-renders it the way acb-rdv did:
    the known keys only, in a fixed order, dropping zero values.
    """

    CANONICAL_ORDER = ("address", "port", "CID", "PID", "sid", "RVCID", "stream", "type")

    __slots__ = ("scheme", "params")

    def __init__(self, scheme: str = "prudp", params: dict[str, str] | None = None, **kw: Any):
        self.scheme = scheme
        self.params: dict[str, str] = dict(params or {})
        for k, v in kw.items():
            if v is not None:
                self.params[k] = str(_signed32(v)) if k == "RVCID" else str(v)

    @classmethod
    def parse(cls, url: str) -> "StationURL":
        scheme, sep, rest = url.partition(":/")
        if not sep:
            raise ValueError(f"not a station URL: {url!r}")
        params: dict[str, str] = {}
        for part in rest.split(";"):
            if not part:
                continue
            key, _, value = part.partition("=")
            params[key] = value
        return cls(scheme, params)

    def _int(self, key: str) -> int:
        try:
            return int(self.params.get(key, "0"))
        except ValueError:
            return 0

    @property
    def address(self) -> str:
        return self.params.get("address", "")

    @property
    def port(self) -> int:
        return self._int("port")

    @property
    def pid(self) -> int:
        return self._int("PID")

    @property
    def cid(self) -> int:
        return self._int("CID")

    @property
    def rvcid(self) -> int:
        # unsigned, like Client.rvcid, whichever form the URL carries
        return self._int("RVCID") & 0xFFFFFFFF

    def with_params(self, **kw: Any) -> "StationURL":
        return StationURL(self.scheme, self.params, **kw)

    def canonical(self) -> "StationURL":
        params = {}
        for key in self.CANONICAL_ORDER:
            if key not in self.params:
                continue
            if key not in ("address", "port") and self._int(key) == 0:
                continue
            params[key] = self.params[key]
        # AC Relay IPv6 URL: address is a virtual IPv4 that only the v6 param
        # makes reachable, so it must survive (hex string, not an int)
        if self.params.get("v6"):
            params["v6"] = self.params["v6"]
        return StationURL(self.scheme, params)

    def __str__(self) -> str:
        body = ";".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.scheme}:/{body}"

    def __repr__(self) -> str:
        return f"StationURL({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StationURL) and str(self) == str(other)

    def __hash__(self) -> int:
        return hash(str(self))


StationURLCodec = Codec(
    "station_url",
    lambda r: StationURL.parse(r.string()),
    lambda w, v: w.string(str(v)),
)
Station = Annotated[StationURL, StationURLCodec]


# --- Common structures ---------------------------------------------------

class AnyDataHolder:
    """``string class name, u32 outer size, u32 inner size, payload``."""

    __slots__ = ("name", "payload")

    def __init__(self, name: str, payload: bytes):
        self.name = name
        self.payload = payload

    @classmethod
    def read(cls, r: Reader) -> "AnyDataHolder":
        name = r.string()
        outer = r.u32()
        inner = r.u32()
        if outer != inner + 4:
            # trust the inner size, as the original servers do
            pass
        return cls(name, r.read(inner))

    def write(self, w: Writer) -> None:
        w.string(self.name)
        w.u32(len(self.payload) + 4)
        w.u32(len(self.payload))
        w.write(self.payload)

    def __repr__(self) -> str:
        return f"AnyDataHolder({self.name!r}, {len(self.payload)} bytes)"


class ResultRange(Structure):
    offset: U32 = 0
    size: U32 = 0


class Property(Structure):
    """Generic ``{u32 id, u32 value}`` pair used by game session attributes."""

    id: U32 = 0
    value: U32 = 0
