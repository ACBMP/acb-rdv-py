import pytest

from quazal.crypto import rc4
from quazal.kerberos import DEFAULT_INNER_TICKET, build_ticket, open_ticket
from quazal.profile import GameProfile
from quazal.prudp.packet import ChecksumError, Flag, Packet, PacketType, VPort
from quazal.rmc.message import RMCMessage

CODEC = GameProfile("acb").codec()


def test_rc4_known_vector():
    assert rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"


def test_ticket_round_trip():
    key = bytes(range(16))
    ticket = build_ticket(10058, 0x1000, key)
    assert open_ticket(10058, ticket) == (key, 0x1000, DEFAULT_INNER_TICKET)
    with pytest.raises(ValueError):
        open_ticket(10059, ticket)


@pytest.mark.parametrize("compressed", [True, False])
def test_data_packet_round_trip(compressed):
    pkt = Packet(VPort(3, 15), VPort(3, 1), PacketType.DATA, Flag.RELIABLE | Flag.NEED_ACK, 88, 0x12345678, 7,
                 part=0, payload=b"hello world" * 20, compressed=compressed)
    data = CODEC.encode(pkt)
    back = CODEC.decode(data, verify=True)
    assert back.payload == pkt.payload and back.compressed == compressed and back.seq == 7
    assert CODEC.encode(back) == data


def test_checksum_is_verified():
    data = bytearray(CODEC.encode(Packet(VPort(3, 1), VPort(3, 15), PacketType.PING, Flag.ACK, 1, 2, 3)))
    data[-1] ^= 0xFF
    with pytest.raises(ChecksumError):
        CODEC.decode(bytes(data), verify=True)


def test_has_size_allows_several_packets_per_datagram():
    a = Packet(VPort(3, 15), VPort(3, 1), PacketType.DATA, Flag.NEED_ACK | Flag.HAS_SIZE, 1, 2, 3, payload=b"a")
    b = Packet(VPort(3, 15), VPort(3, 1), PacketType.PING, Flag.NEED_ACK | Flag.HAS_SIZE, 1, 2, 4)
    packets = CODEC.decode_all(CODEC.encode(a) + CODEC.encode(b), verify=True)
    assert [p.type for p in packets] == [PacketType.DATA, PacketType.PING]


def test_auth_connect_trailer_form():
    pkt = Packet(VPort(3, 1), VPort(3, 15), PacketType.CONNECT, Flag.ACK, 253, 0x5A2E0015, 1, connect_trailer=True)
    data = CODEC.encode(pkt)
    assert data[10:15] == b"\x00\x00\x00\x00\x0f"
    assert CODEC.decode(data).connect_trailer


def test_rmc_framing():
    req = RMCMessage.request(0x2A, 7, 99, b"xyz")
    assert RMCMessage.decode(req.encode()) == req
    ok = RMCMessage.response(0x1B5B, 3, 5, b"body")
    assert ok.encode()[4] == 0x7F  # extended protocol id marker
    assert RMCMessage.decode(ok.encode()) == ok
    err = RMCMessage.failure(0x0A, 2, 8, 0x80030065)
    back = RMCMessage.decode(err.encode())
    assert not back.success and back.error == 0x80030065 and back.call_id == 8
