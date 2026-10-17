"""Unit tests for the NAT helpers: per-recipient relay URLs and AC Relay config."""

from __future__ import annotations

import pytest

from quazal.nat.relay import Allocation
from quazal.nat.service import NatConfig, resolve_hosts

from acb_rdv.config import load


def test_relay_url_names_the_peer_behind_it():
    allocation = Allocation((10001, 10002), "203.0.113.10", 40000)
    to_bob = allocation.url_to(10002, 0xBEEF)
    assert str(to_bob) == ("prudp:/address=203.0.113.10;port=40000;CID=1;PID=10002;sid=1;"
                           f"RVCID={0xBEEF};stream=3;type=2")
    # the URL survives RegisterURLs-style canonicalisation unchanged
    assert to_bob.canonical() == to_bob
    assert allocation.url_to(10001, 7).rvcid == 7
    with pytest.raises(ValueError):
        allocation.url_to(10003, 1)


def test_resolve_hosts():
    assert resolve_hosts("") == frozenset()
    assert resolve_hosts(" 198.51.100.7 , ,localhost") == {"198.51.100.7", "127.0.0.1"}
    assert resolve_hosts(["198.51.100.7"]) == {"198.51.100.7"}
    assert resolve_hosts("no-such-host.invalid") == frozenset()


def test_ac_relay_config(tmp_path):
    assert NatConfig().ac_relay_servers == ""
    assert NatConfig().relay is False  # the server relay is off by default
    cfg = load(env={"ACB_RDV_NAT_AC_RELAY_SERVERS": "198.51.100.7,198.51.100.8", "ACB_RDV_NAT_RELAY": "0"})
    assert cfg.nat.ac_relay_servers == "198.51.100.7,198.51.100.8"
    assert cfg.nat.relay is False
    f = tmp_path / "c.toml"
    f.write_text('[nat]\nac_relay_servers = ["198.51.100.7"]\nrelay = false\n')
    cfg = load(f, env={})
    assert cfg.nat.ac_relay_servers == ["198.51.100.7"] and cfg.nat.relay is False


def test_caller_candidates_name_the_caller():
    from types import SimpleNamespace

    from quazal.context import PlayerState
    from quazal.ddl import StationURL
    from quazal.nat.service import NatService

    state = PlayerState()
    state.urls = [StationURL.parse("prudps:/address=10.8.0.2;port=7917;sid=15;type=3"),
                  StationURL.parse("prudp:/address=198.51.100.20;port=40203"),
                  StationURL.parse("prudps:/address=10.8.0.2;port=7917;sid=15;type=3")]
    state.registered_urls = [StationURL.parse("prudp:/address=198.51.100.20;port=40203;RVCID=5")]
    caller = SimpleNamespace(pid=10001, rvcid=0x1234, addr=("10.8.0.2", 7917), state={"player": state})
    nat = NatService.__new__(NatService)
    nat.ac_relays = frozenset({"198.51.100.20"})
    nat.relay = None
    got = nat.caller_candidates(caller)
    # the server-observed address is pushed separately ("caller"), duplicates dropped
    assert [(str(u), t) for u, t in got] == [
        (f"prudp:/address=198.51.100.20;port=40203;PID=10001;RVCID={0x1234}", "acrelay")]


def test_caller_candidates_keep_the_ipv6_param():
    from types import SimpleNamespace

    from quazal.context import PlayerState
    from quazal.ddl import StationURL
    from quazal.nat.service import NatService

    state = PlayerState()
    v6 = "prudp:/address=247.105.51.108;port=55289;v6=20010db800000000000000000000d722;RVCID=5"
    state.registered_urls = [StationURL.parse(v6)]
    caller = SimpleNamespace(pid=10001, rvcid=0x1234, addr=("10.8.0.2", 7917), state={"player": state})
    nat = NatService.__new__(NatService)
    nat.ac_relays = frozenset()
    nat.relay = None
    got = nat.caller_candidates(caller)
    assert [(str(u), t) for u, t in got] == [
        (f"prudp:/address=247.105.51.108;port=55289;PID=10001;RVCID={0x1234};v6=20010db800000000000000000000d722",
         "ipv6")]
