"""``onlineconfigservice.ubi.com`` replacement for every game at once.

Each game asks ``GET /OnlineConfigService.svc/GetOnlineConfig?onlineConfigID=<id>``
at start-up; the id identifies the game, and the answer's ``SandboxUrl`` sends
it to that game's RendezVous auth server, in the original service's IIS-style
reply. Every game can have its own address, extra entries can be added per
game, and short aliases (``ACB``, ``ACR``) are accepted too, which AN-Flask's
status check uses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from quazal.httpserver import Request, Response

log = logging.getLogger("onlineconfig")

CONFIG_PATH = "/OnlineConfigService.svc/GetOnlineConfig"

IIS_HEADERS = [
    ("Cache-Control", "private"),
    ("Content-Type", "application/json; charset=utf-8"),
    ("Server", "Microsoft-IIS/7.5"),
    ("X-AspNet-Version", "2.0.50727"),
    ("X-Powered-By", "ASP.NET"),
]


@dataclass
class Game:
    key: str                               # short name, e.g. "ACB"
    ids: list[str]                         # onlineConfigID values that select this game
    port: int                              # the game's RendezVous auth port
    address: str = ""                      # defaults to the service's public address
    ps3_service_id: str = ""               # adds SandboxUrlPS3 when set
    extra: dict[str, str] = field(default_factory=dict)  # more entries; #IP# / #PORT# are substituted

    def entries(self, default_address: str) -> list[tuple[str, str]]:
        ip = self.address or default_address
        port = self.port
        entries = [("SandboxUrl", f"prudp:/address={ip};port={port}")]
        if self.ps3_service_id:
            entries.append(("SandboxUrlPS3", f"prudp:/address={ip};port={port};serviceid={self.ps3_service_id}"))
        entries += [
            ("SandboxUrlWS", f"{ip}:{port}"),
            ("uplay_DownloadServiceUrl", f"{ip}/UplayServices/UplayFacade/DownloadServicesRESTXML.svc/REST/XML/?url="),
            ("uplay_DynContentBaseUrl", f"{ip}/u/Uplay/"),
            ("uplay_DynContentSecureBaseUrl", f"{ip}/"),
            ("uplay_LinkappBaseUrl", f"{ip}/u/Uplay/Packages/linkapp/1.1/"),
            ("uplay_PackageBaseUrl", f"{ip}/u/Uplay/Packages/1.0.1/"),
            ("uplay_WebServiceBaseUrl", f"{ip}/UplayServices/UplayFacade/ProfileServicesFacadeRESTXML.svc/REST/"),
        ]
        for name, value in self.extra.items():
            entries.append((name, value.replace("#IP#", ip).replace("#PORT#", str(port))))
        return entries


def default_games() -> list[Game]:
    """The built-in games."""
    return [
        # the PS3 entry comes from acb-rdv's additional-fixes branch
        Game("ACB", ["f26ad290e85146a685acbbb5f6ed7672", "ACB"], 21030, ps3_service_id="UP0001-NPUB90483_00"),
        Game("ACR", ["31ba374e5cb44188b8372a39ebd93c4b", "ACR"], 21040),
    ]


def render(entries: list[tuple[str, str]]) -> bytes:
    items = [{"Name": name, "Values": [value]} for name, value in entries]
    raw = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    # IIS escaped every slash; the game's parser has only ever seen it that way
    return raw.replace("/", "\\/").encode("utf-8")


class OnlineConfigService:
    def __init__(self, games: list[Game], public_address: str):
        self.public_address = public_address
        self.games = {g.key: g for g in games}
        self.by_id: dict[str, Game] = {}
        for game in games:
            for game_id in game.ids:
                key = game_id.lower()
                if key in self.by_id:
                    raise ValueError(f"onlineConfigID {game_id!r} is used by {self.by_id[key].key} and {game.key}")
                self.by_id[key] = game
        self._bodies = {g.key: render(g.entries(public_address)) for g in games}

    def body_for(self, online_config_id: str) -> bytes | None:
        game = self.by_id.get(online_config_id.lower())
        return self._bodies[game.key] if game else None

    async def handle(self, request: Request) -> Response:
        if request.method != "GET" or request.path != CONFIG_PATH:
            log.info("[%s] %s %s -> 404", request.peer[0], request.method, request.target)
            return Response(404, b"", [("Cache-Control", "private")])
        online_id = (request.query.get("onlineConfigID") or [""])[0]
        game = self.by_id.get(online_id.lower())
        if game is None:
            log.warning("[%s] unknown onlineConfigID %r", request.peer[0], online_id)
            return Response(404, b"", [("Cache-Control", "private")])
        log.info("[%s] online config for %s", request.peer[0], game.key)
        return Response(200, self._bodies[game.key], list(IIS_HEADERS))

    def describe(self) -> str:
        return ", ".join(f"{g.key}->{g.address or self.public_address}:{g.port}" for g in self.games.values())
