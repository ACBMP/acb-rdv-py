"""Server configuration: a TOML file, overridable with ``ACB_RDV_*`` environment variables."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from quazal.nat.service import NatConfig


@dataclass
class Config:
    # address advertised to clients (IPv4 or hostname, resolved at start like acb-rdv)
    public_address: str = "127.0.0.1"
    bind_host: str = "0.0.0.0"
    auth_port: int = 21030
    secure_port: int = 21031
    secure_pid: int = 0x1000
    status_port: int = 21032               # 0 disables the status API
    status_host: str = "0.0.0.0"
    internal_key: str = ""                 # shared with AN-API for POST /internal/notify
    # game settings served to clients: a directory of XML files (the settings
    # submodule, encoded to CXB just in time by the CXB library) or a ready .cxb
    game_settings: str = "gamesettings"
    cxb_library: str = ""                  # path to libcxb; empty searches ACB_CXB_LIBRARY and the system
    # file size GameInfo.GetFileListOnGameStorage reports; 58484 was dumped from
    # the original service and is what acb-rdv always sent
    game_storage_file_size: int = 58484
    allow_private_sessions: bool = True
    # in-game news panel: "anapi" for AN-API's /news/ posts (the launcher
    # news), "" for none, or the path of a TOML file (see news.example.toml)
    news: str = "anapi"
    news_sender: str = ""                  # sender name shown on AN-API news
    # in-game ranking tabs (the game's Free/Coop/Team/Overall "Templar" boards):
    # the AN-API MMR modes shown on each, comma-separated; several are combined
    ranking_free: str = "asb"
    ranking_coop: str = "e"
    ranking_team: str = "mh"
    ranking_overall: str = "asb,mh,e"
    ranking_combine: str = "mean"          # how combined tabs merge MMRs: mean, sum or max
    ranking_placement_games: int = 10      # combined tabs: below this in every mode ranks after placed players
    # statistic tabs and gamer cards: AN-API recorded stats summed over these modes
    stats_modes: str = "asb,mh,e"
    # XP (board 1, stat 1) reported to everyone; 650000 is what the old server sent
    stats_default_xp: int = 650000
    # acb-rdv dropped a player 20-30 s after its last packet (20 s expiry, 10 s sweep)
    idle_timeout: float = 30.0
    # storage: "anapi" or "memory" (development only)
    backend: str = "anapi"
    anapi_url: str = "http://127.0.0.1:8000"   # AN-API gunicorn (src/gunicorn_config.py)
    anapi_service_key: str = ""
    # the server's own relay for NAT-blocked pairs; only used with nat.relay = true (off by default)
    relay_port_start: int = 40000
    relay_port_end: int = 40199
    relay_idle_timeout: float = 60.0
    nat: NatConfig = field(default_factory=NatConfig)
    packet_capture: str = ""               # path of a packetLog.bin-style capture, empty to disable
    log_level: str = "INFO"

    def resolved_address(self) -> str:
        addr = socket.gethostbyname(self.public_address)
        if addr == "0.0.0.0":
            raise ValueError("public_address must not resolve to 0.0.0.0")
        return addr


def _coerce(value: str, like):
    if isinstance(like, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(value, 0)
    if isinstance(like, float):
        return float(value)
    return value


def load(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    cfg = Config()
    data: dict = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    nat_data = data.pop("nat", {})
    for f in fields(Config):
        if f.name in data:
            setattr(cfg, f.name, data[f.name])
    for f in fields(NatConfig):
        if f.name in nat_data:
            setattr(cfg.nat, f.name, nat_data[f.name])
    env = os.environ if env is None else env
    for f in fields(Config):
        key = f"ACB_RDV_{f.name.upper()}"
        if key in env and f.name != "nat":
            setattr(cfg, f.name, _coerce(env[key], getattr(cfg, f.name)))
    for f in fields(NatConfig):
        key = f"ACB_RDV_NAT_{f.name.upper()}"
        if key in env:
            setattr(cfg.nat, f.name, _coerce(env[key], getattr(cfg.nat, f.name)))
    return cfg
