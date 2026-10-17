"""AnApiStorage against the real AN-API ``/rdv`` blueprint (backed by mongomock).

Needs the AN-API checkout next to this repository and its dependencies::

    uv run --no-project --with httpx --with pytest --with pytest-asyncio --with flask \\
        --with flask_pymongo --with 'pymongo<4.9' --with mongomock --with flasgger \\
        --with flask-cors --with markdown --with requests pytest tests/test_anapi_contract.py
"""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

AN_API = Path(__file__).resolve().parents[2] / "AN-API" / "src"
flask = pytest.importorskip("flask")
mongomock = pytest.importorskip("mongomock")
pytest.importorskip("flask_pymongo")
if not AN_API.exists():
    pytest.skip("AN-API checkout not found", allow_module_level=True)
sys.path.insert(0, str(AN_API))

from werkzeug.serving import make_server  # noqa: E402

from quazal.client import QuazalClient  # noqa: E402
from quazal.ddl import Reader, Writer  # noqa: E402
from quazal.storage import AuthFailure, ModeStats, GameInvite, RelationshipType, RelResult, StoredMessage  # noqa: E402

from acb_rdv.backend.anapi import AnApiStorage  # noqa: E402

KEY = "svc"


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def anapi():
    from app import create_app
    from app.extensions import mongo
    from app.routes import rdv

    rdv._indexes_ready = False
    app = create_app()
    app.config.update(RDV_SERVICE_KEY=KEY, RDV_LEGACY_PASSWORD="ACB", RDV_NOTIFY_URL="", RDV_NOTIFY_KEY="")
    client = mongomock.MongoClient()
    mongo.cx, mongo.db = client, client["public"]
    mongo.db.players.insert_many([
        {"name": "Alice", "api_key": "tok-a", "revoked": False, "nation": "pl"},
        {"name": "Bob", "api_key": "tok-b", "revoked": False, "pid": 10058},
    ])
    port = free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", mongo.db
    server.shutdown()


@pytest.fixture
async def storage(anapi):
    s = AnApiStorage(anapi[0], KEY)
    yield s
    await s.close()


async def test_accounts(storage):
    alice = await storage.authenticate("Alice", "tok-a")
    assert alice.pid > 10058 and alice.country_code == "PL"
    assert (await storage.authenticate("Bob", "ACB")).pid == 10058  # legacy launcher password
    with pytest.raises(AuthFailure) as err:
        await storage.authenticate("Alice", "nope")
    assert err.value.reason == "bad_password"
    with pytest.raises(AuthFailure):
        await storage.authenticate("Nobody", "x")
    assert (await storage.get_user_by_pid(10058)).name == "Bob"
    assert (await storage.get_user_by_name("Alice")).pid == alice.pid
    assert await storage.get_user_by_pid(1) is None


async def test_relationships_messages_invites(storage):
    alice = (await storage.authenticate("Alice", "tok-a")).pid
    await storage.remove_relationship(alice, 10058)
    assert await storage.add_friend_request(alice, 10058, 0) == RelResult.SUCCEEDED
    assert [r.requester for r in await storage.get_relationships(10058, RelationshipType.PENDING)] == [alice]
    assert await storage.accept_friend(alice, 10058, 0) == RelResult.SUCCEEDED
    assert len(await storage.get_relationships(alice, RelationshipType.FRIEND)) == 1
    assert await storage.remove_relationship(alice, 10058)

    await storage.add_message(StoredMessage(0, 10058, 1, 0, alice, 5, 0, 0, "hi", "Alice", "yo"))
    pending = await storage.pending_messages(10058)
    assert pending[-1].body == "yo"
    assert (await storage.messages_by_ids([pending[-1].id]))[0].subject == "hi"
    await storage.mark_delivered([pending[-1].id])
    assert pending[-1].id not in [m.id for m in await storage.pending_messages(10058)]

    await storage.add_game_invite(GameInvite(0, alice, 10058, 1, 9, "join"))
    assert [i.session_id for i in await storage.take_game_invites(10058)] == [9]
    assert await storage.take_game_invites(10058) == []


async def test_static_data_and_telemetry(storage, anapi):
    assert (await storage.privileges("en-US"))[0].description == "Allow to play online"
    assert len(await storage.rewards()) == 6
    assert "GAME_START" in await storage.telemetry_tags()
    await storage.save_telemetry(1, "GAME_START", "a=b", 3)
    await storage.flush_telemetry()
    assert anapi[1].rdv_telemetry.find_one({"tag": "GAME_START"})["attributes"] == "a=b"


async def test_ladders_and_player_records(storage, anapi):
    alice = (await storage.authenticate("Alice", "tok-a")).pid
    anapi[1].players.update_one({"name": "Bob"}, {"$set": {
        "hidden": False, "mhmmr": 1300.5, "mhgames": {"total": 12, "won": 7, "lost": 5},
        "mhstats": {"totalscore": 9000, "highscore": 1500, "kills": 40, "deaths": 30}}})
    anapi[1].players.update_one({"name": "Alice"}, {"$set": {"hidden": False, "mhmmr": 1600,
                                                             "mhgames": {"total": 3}}})
    ladder = await storage.mmr_ladder("mh")
    # the website's order: Bob is placed, Alice (3 games) is not despite the higher MMR
    assert [(e.pid, e.name, e.mmr, e.games) for e in ladder] == [(10058, "Bob", 1300.5, 12),
                                                                 (alice, "Alice", 1600.0, 3)]
    import httpx
    async with httpx.AsyncClient() as http:
        public = (await http.get(f"{anapi[0]}/boards/ranking/mh")).json()
    assert public and all("api_key" not in p for p in public)

    records = {r.name: r for r in await storage.player_records()}
    assert records["Bob"].pid == 10058
    assert records["Bob"].modes["mh"] == ModeStats(1300.5, 12, 7, 5, 9000, 1500, 40, 30)
    assert set(records["Alice"].modes) == {"mh"}


async def test_game_login_through_anapi(anapi, tmp_path):
    from acb_rdv.config import Config
    from acb_rdv.server import AcbServer

    auth, secure = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    cfg = Config(public_address="127.0.0.1", bind_host="127.0.0.1", auth_port=auth, secure_port=secure,
                 status_port=0, relay_port_start=0, anapi_url=anapi[0],
                 anapi_service_key=KEY, game_settings=str(tmp_path / "none.cxb"))
    server = AcbServer(cfg)
    await server.start()
    client = await QuazalClient.open("127.0.0.1")
    try:
        denied = await client.login(auth, "Alice", "wrong-token")
        assert not denied.success
        reply = await client.login(auth, "Alice", "tok-a")
        assert reply.success
        await client.connect_secure(secure)
        account = await client.call(0x1D, 3)
        r = Reader(account.body)
        r.string()
        assert r.string() == "Alice"
        friends = await client.call(0x14, 4, Writer().string("Bob").u32(0).string("Alice").bytes())
        assert friends.success
    finally:
        client.close()
        await server.stop()
