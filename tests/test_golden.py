"""Replay recorded acb-rdv requests through the new handlers and compare responses.

The capture (``ACB_CAPTURE``, a ``packetLog.bin``) and the database of the
server it was taken from (``ACB_CAPTURE_DB``) hold real traffic and data, so
they are not part of the repository; the tests skip without them.
Only handlers whose answers depend on nothing but the request and the stored
data are compared byte for byte.
"""

from __future__ import annotations

import collections

import pytest

from quazal.prudp.transport import Client, Connection, ServerSocket
from quazal.prudp.packet import VPort
from quazal.rmc.message import RMCMessage
from quazal.storage import MemoryStorage, Privilege, Reward, User

from acb_rdv.server import ACB_PROFILE

from .capture_replay import CAPTURE, DATABASE, capture_rows, exchanges, login_pids

pytestmark = pytest.mark.skipif(not (CAPTURE.exists() and DATABASE.exists()),
                                reason="capture/database not available")

# the server's configuration at the time of the capture
CAPTURE_ADDRESS = "10.8.0.21"
EXACT = {
    (0x0A, 1), (0x0A, 2), (0x0A, 3), (0x0B, 1), (0x1D, 3), (0x23, 1), (0x21, 1), (0x24, 3), (0x24, 4),
    (0x6F, 1), (0x1F, 2), (0x1F, 8), (0x1F, 10), (0x71, 5), (0x27, 2), (0x76, 1), (0x6C, 2), (0x78, 5),
    (0x24, 1), (0x6D, 1), (0x17, 3), (0x18, 4),
}


@pytest.fixture(scope="module")
def recorded():
    ex = exchanges()
    return ex, login_pids(ex)


def make_storage() -> MemoryStorage:
    storage = MemoryStorage()
    for pid, name, password, email, country, lang in capture_rows(
        "select pid, name, password, email, country_code, pref_lang from users"
    ):
        # acb-rdv filled the Ubi account id from the password column; mirror it
        storage.add_user(User(pid, name, ubi_id=password, email=email, country_code=country, pref_lang=lang),
                         password)
    storage.privilege_list = [Privilege(i, d) for i, d, loc in capture_rows("select * from privileges")
                              if loc == "en-US"]
    storage.reward_list = [Reward(*row) for row in capture_rows("select code, name, description, val from rewards")]
    storage.tags = [t for (t,) in capture_rows("select tag from tags")]
    return storage


def make_server(storage):
    from acb_rdv.config import Config
    from acb_rdv.server import AcbServer

    cfg = Config(public_address=CAPTURE_ADDRESS, backend="memory", status_port=0,
                 relay_port_start=0, game_settings=str(DATABASE.parent / "gamesettings_c1380_d873_s6285.cxb"))
    return AcbServer(cfg, storage=storage)


async def test_recorded_responses_match(recorded):
    ex, pids = recorded
    storage = make_storage()
    server = make_server(storage)
    settings_size = (DATABASE.parent / "gamesettings_c1380_d873_s6285.cxb").stat().st_size
    sock = ServerSocket(server.prudp, "rdv", 0x1000, 21031, secure=True)
    clients: dict[int, Client] = {}
    results = collections.Counter()
    mismatches = []
    for e in ex:
        key = (e.request.protocol, e.request.method)
        if key not in EXACT or not e.response.success:
            continue
        if key == (0x18, 4) and len(e.response.body) != settings_size + 5:
            continue  # served an older version of the settings file
        pid = pids.get(e.conn_id)
        client = clients.get(e.conn_id)
        if client is None:
            client = clients[e.conn_id] = Client(("10.8.0.99", 7917), e.conn_id, server_pid=0x1000,
                                                 server_port=21031)
        if key != (0x0A, 2) and pid is not None and client.user is None:
            client.user = await storage.get_user_by_pid(pid)
        if key in ((0x0A, 1), (0x24, 3)) or e.port == 14:
            client.tracking_user = User(105, "Tracking")
        conn = Connection(sock, client, VPort(3, e.port), VPort(3, 1))
        answer = await server.dispatcher.dispatch(conn, e.request)
        assert answer is not None
        got = RMCMessage.decode(answer[0])
        if got.success and got.body == e.response.body:
            results[key] += 1
        else:
            mismatches.append((key, e.response.body.hex(), got.body.hex(), got.error))
    assert sum(results.values()) > 500, results
    assert not mismatches, mismatches[:5]
    exercised = {f"0x{p:X}/{m}": n for (p, m), n in sorted(results.items())}
    print("byte-identical responses:", exercised)
    assert len(results) >= 16, exercised


def test_capture_packets_round_trip():
    """Every recorded datagram decodes with a valid checksum and re-encodes identically.

    The only tolerated difference is zlib output for a handful of client packets
    compressed by the game's own zlib build.
    """
    from quazal.capture import read_capture

    codec = ACB_PROFILE.codec()
    total = differing = 0
    for rec in read_capture(CAPTURE):
        pkt = codec.decode(rec.data, verify=True)
        assert pkt.raw_size == len(rec.data)
        total += 1
        if codec.encode(pkt) != rec.data:
            assert not rec.sent and pkt.compressed, pkt.short()
            differing += 1
    assert total > 70_000 and differing < 20, (total, differing)
