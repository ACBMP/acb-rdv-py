import json
import socket

import httpx
import pytest

from quazal.httpserver import serve

from onlineconfig import config as config_mod
from onlineconfig.service import CONFIG_PATH, OnlineConfigService, default_games

ACB_ID = "f26ad290e85146a685acbbb5f6ed7672"
ACR_ID = "31ba374e5cb44188b8372a39ebd93c4b"


@pytest.fixture
async def base_url():
    cfg = config_mod.load(env={"ONLINECONFIG_ACR_ADDRESS": "10.0.0.9"})
    service = OnlineConfigService(cfg.games, "127.0.0.1")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = await serve("online config", "127.0.0.1", port, service.handle)
    yield f"http://127.0.0.1:{port}"
    server.close()


async def fetch(base_url, online_id):
    async with httpx.AsyncClient() as http:
        return await http.get(base_url + CONFIG_PATH, params={"onlineConfigID": online_id})


async def test_every_game_gets_its_own_sandbox(base_url):
    acb = await fetch(base_url, ACB_ID)
    acr = await fetch(base_url, ACR_ID)
    assert acb.status_code == acr.status_code == 200
    assert acb.headers["server"] == "Microsoft-IIS/7.5"
    acb_entries = {e["Name"]: e["Values"][0] for e in json.loads(acb.content)}
    acr_entries = {e["Name"]: e["Values"][0] for e in json.loads(acr.content)}
    assert acb_entries["SandboxUrl"] == "prudp:/address=127.0.0.1;port=21030"
    assert acb_entries["SandboxUrlPS3"].endswith("serviceid=UP0001-NPUB90483_00")
    assert acr_entries["SandboxUrl"] == "prudp:/address=10.0.0.9;port=21040"  # per-game address
    assert "SandboxUrlPS3" not in acr_entries
    assert b"prudp:\\/address" in acb.content  # slashes escaped like IIS


async def test_aliases_and_unknown_ids(base_url):
    # AN-Flask's status check asks for onlineConfigID=ACB
    assert (await fetch(base_url, "ACB")).status_code == 200
    assert (await fetch(base_url, ACB_ID.upper())).status_code == 200
    assert (await fetch(base_url, "nope")).status_code == 404
    async with httpx.AsyncClient() as http:
        assert (await http.get(base_url + "/other")).status_code == 404


def test_matches_general_server_output():
    """Same body as the original service for ACR (which has no PS3 entry)."""
    from onlineconfig.service import render

    acr = next(g for g in default_games() if g.key == "ACR")
    ip = "198.51.100.7"
    expected = ('[{"Name":"SandboxUrl","Values":["prudp:\\/address=198.51.100.7;port=21040"]},'
                '{"Name":"SandboxUrlWS","Values":["198.51.100.7:21040"]},'
                '{"Name":"uplay_DownloadServiceUrl","Values":["198.51.100.7\\/UplayServices\\/UplayFacade\\/'
                'DownloadServicesRESTXML.svc\\/REST\\/XML\\/?url="]},')
    assert render(acr.entries(ip)).decode().startswith(expected)


def test_config_file_adds_games(tmp_path):
    f = tmp_path / "oc.toml"
    f.write_text('public_address = "1.2.3.4"\nport = 8080\n[games.ACR]\nport = 21056\n'
                 '[games.AC3]\nids = ["abc", "AC3"]\nport = 21050\n')
    cfg = config_mod.load(f, env={"ONLINECONFIG_AC3_PORT": "21051"})
    ports = {g.key: g.port for g in cfg.games}
    assert ports == {"ACB": 21030, "ACR": 21056, "AC3": 21051} and cfg.port == 8080
    with pytest.raises(ValueError, match="used by"):
        OnlineConfigService(default_games() + default_games()[:1], "1.2.3.4")
