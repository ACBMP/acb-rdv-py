"""Online config service settings.

Defaults cover ACB and ACR (``default_games``). A TOML file can change them or
add games::

    public_address = "203.0.113.10"
    port = 80

    [games.ACR]
    port = 21040
    address = "203.0.113.11"          # a game can live on another host

    [games.AC3]
    ids = ["<onlineConfigID>", "AC3"]
    port = 21050

Environment variables override the file: ``ONLINECONFIG_PUBLIC_ADDRESS``,
``ONLINECONFIG_HOST``, ``ONLINECONFIG_PORT`` and per game
``ONLINECONFIG_<GAME>_PORT`` / ``ONLINECONFIG_<GAME>_ADDRESS``.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .service import Game, default_games


@dataclass
class Config:
    public_address: str = "127.0.0.1"
    host: str = "0.0.0.0"
    port: int = 80
    games: list[Game] = field(default_factory=default_games)
    log_level: str = "INFO"

    def resolved_address(self) -> str:
        return socket.gethostbyname(self.public_address)


def load(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    data: dict = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = Config()
    for key in ("public_address", "host", "port", "log_level"):
        if key in data:
            setattr(cfg, key, data[key])
    games = {g.key: g for g in cfg.games}
    for key, spec in data.get("games", {}).items():
        game = games.get(key)
        if game is None:
            if "ids" not in spec or "port" not in spec:
                raise ValueError(f"new game {key!r} needs 'ids' and 'port'")
            game = games[key] = Game(key, list(spec["ids"]), int(spec["port"]))
        for attr in ("ids", "port", "address", "ps3_service_id", "extra"):
            if attr in spec:
                setattr(game, attr, spec[attr])
    cfg.games = list(games.values())

    if "ONLINECONFIG_PUBLIC_ADDRESS" in env:
        cfg.public_address = env["ONLINECONFIG_PUBLIC_ADDRESS"]
    if "ONLINECONFIG_HOST" in env:
        cfg.host = env["ONLINECONFIG_HOST"]
    if "ONLINECONFIG_PORT" in env:
        cfg.port = int(env["ONLINECONFIG_PORT"])
    if "ONLINECONFIG_LOG_LEVEL" in env:
        cfg.log_level = env["ONLINECONFIG_LOG_LEVEL"]
    for game in cfg.games:
        prefix = f"ONLINECONFIG_{game.key.upper()}_"
        if prefix + "PORT" in env:
            game.port = int(env[prefix + "PORT"])
        if prefix + "ADDRESS" in env:
            game.address = env[prefix + "ADDRESS"]
    return cfg
