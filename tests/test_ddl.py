import datetime as dt

import pytest

from quazal.ddl import (
    U8, U32, AnyDataHolder, ListOf, MapOf, Property, Reader, StationURL, StreamError, String, Structure, Writer,
    field, qdatetime_from, qdatetime_to,
)


def test_primitives_round_trip():
    w = Writer().u8(1).u16(0x1234).u32(0xDEADBEEF).u64(1 << 40).bool(True).f32(1.5).string("héllo").buffer(b"ab").qbuffer(b"c")
    r = Reader(w.bytes())
    assert (r.u8(), r.u16(), r.u32(), r.u64(), r.bool(), r.f32(), r.string(), r.buffer(), r.qbuffer()) == (
        1, 0x1234, 0xDEADBEEF, 1 << 40, True, 1.5, "héllo", b"ab", b"c")
    assert r.eof()


def test_string_wire_format_matches_acb_rdv():
    # u16 length including the terminator, one byte per character, NUL
    assert Writer().string("ab").bytes() == b"\x03\x00ab\x00"
    # acb-rdv writes a null string as an empty one
    assert Writer().string(None).bytes() == b"\x01\x00\x00"


def test_u64_upper_bits():
    # acb-rdv's ReadU64 lost the upper 32 bits; ours must not
    assert Reader(Writer().u64(0x0123456789ABCDEF).bytes()).u64() == 0x0123456789ABCDEF


def test_truncated_read_raises():
    with pytest.raises(StreamError):
        Reader(b"\x01").u32()


class Inner(Structure):
    a: U8 = 0
    name: String = ""


class Outer(Structure):
    id: U32 = 0
    inner: Inner = field(default_factory=Inner)
    items: ListOf(Property) = field(default_factory=list)
    table: MapOf(U32, String) = field(default_factory=dict)


def test_structure_nesting_round_trip():
    value = Outer(7, Inner(3, "x"), [Property(1, 2), Property(3, 4)], {5: "five"})
    data = value.encode()
    assert data[:4] == b"\x07\x00\x00\x00"
    assert Outer.decode(data) == value


def test_station_url_parse_and_canonical():
    url = StationURL.parse("prudps:/address=10.0.0.2;port=7917;type=3;RVCID=12;sid=14;natf=1")
    assert url.address == "10.0.0.2" and url.port == 7917 and url.rvcid == 12
    # passthrough keeps order and unknown keys
    assert str(url) == "prudps:/address=10.0.0.2;port=7917;type=3;RVCID=12;sid=14;natf=1"
    # canonical is acb-rdv's StationUrl.ToString: known keys, fixed order, no zeros
    assert str(url.canonical()) == "prudps:/address=10.0.0.2;port=7917;sid=14;RVCID=12;type=3"
    assert str(StationURL.parse("prudp:/address=a;port=1;PID=0;CID=2").canonical()) == "prudp:/address=a;port=1;CID=2"
    # the AC Relay v6 param survives (RegisterEx / GameSession.RegisterURLs canonicalise)
    v6 = StationURL.parse("prudp:/address=255.251.89.225;port=37445;v6=20010db800000000000000000000d722;RVCID=5")
    assert str(v6.canonical()) == "prudp:/address=255.251.89.225;port=37445;RVCID=5;v6=20010db800000000000000000000d722"

    # RVCID >= 2**31 is written signed, as the game does (its atoi clamps the
    # unsigned form, failing JoinSessionImpl's same-RVCID check), and reads back unsigned
    built = StationURL("prudp", address="203.0.113.48", port=1529, RVCID=3912695145)
    assert str(built) == "prudp:/address=203.0.113.48;port=1529;RVCID=-382272151"
    assert built.rvcid == 3912695145
    assert StationURL.parse("prudp:/address=1.2.3.4;port=1;RVCID=-382272151").rvcid == 3912695145


def test_qdatetime():
    t = dt.datetime(2001, 9, 27, 13, 46, 5)
    assert qdatetime_to(qdatetime_from(t)) == t


def test_any_data_holder_sizes():
    w = Writer()
    AnyDataHolder("TextMessage", b"12345").write(w)
    r = Reader(w.bytes())
    assert r.string() == "TextMessage"
    assert (r.u32(), r.u32()) == (9, 5)
