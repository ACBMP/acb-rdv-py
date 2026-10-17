"""Shared state for protocol implementations: online players, pushes, notifications."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .ddl import StationURL, Writer
from .storage import Storage, User

if TYPE_CHECKING:
    from .prudp.transport import Client, PRUDPServer

log = logging.getLogger("quazal.context")

GLOBAL_NOTIFICATION_PROTOCOL = 0x0E

TRACKING_USER = User(pid=105, name="Tracking", ubi_id="", email="tracking@ubi.com")


@dataclass
class PlayerState:
    """Per-player RendezVous state (acb-rdv ``ClientInfo`` fields)."""

    urls: list[StationURL] = field(default_factory=list)            # from Secure.RegisterEx
    registered_urls: list[StationURL] = field(default_factory=list)  # from GameSession.RegisterURLs
    tracking_urls: list[StationURL] = field(default_factory=list)
    locale: str = ""
    game_session_id: int = 0
    in_game_session: bool = False
    abandoned_session_id: int = 0
    abandoning_session: bool = False
    pre_session_search_count: int = 0
    play_now_query: bool = False
    presence: list[Any] = field(default_factory=list)


def player(client: "Client") -> PlayerState:
    state = client.state.get("player")
    if state is None:
        state = client.state["player"] = PlayerState()
    return state


class ServerContext:
    """What protocols share: the transport, storage and the online-player index."""

    def __init__(self, server: "PRUDPServer", storage: Storage, *, public_address: str,
                 secure_port: int, secure_pid: int = 0x1000):
        self.server = server
        self.storage = storage
        self.public_address = public_address
        self.secure_port = secure_port
        self.secure_pid = secure_pid
        self.players: dict[int, "Client"] = {}
        # coroutines run on every successful login (e.g. dropping stale sessions)
        self.login_hooks: list[Callable[["Client"], Awaitable[None]]] = []
        # coroutines run when a logged-in player goes away
        self.logout_hooks: list[Callable[["Client", str], Awaitable[None]]] = []
        self.encoding = server.profile.string_encoding
        server.on_client_removed.append(self._client_removed)

    # --- online players ----------------------------------------------------

    def online(self, pid: int) -> "Client | None":
        return self.players.get(pid)

    def online_by_name(self, name: str) -> "Client | None":
        return next((c for c in self.players.values() if c.name == name), None)

    def online_by_rvcid(self, rvcid: int) -> "Client | None":
        return next((c for c in self.players.values() if c.rvcid == rvcid), None)

    def logged_in(self, client: "Client", user: User) -> "Client | None":
        """Record a login; returns a stale client that held the same pid, if any."""
        previous = self.players.get(user.pid)
        client.user = user
        self.players[user.pid] = client
        return previous if previous is not client else None

    async def _client_removed(self, client: "Client", reason: str) -> None:
        if client.user is None:
            return
        if self.players.get(client.pid) is client:
            del self.players[client.pid]
        for hook in self.logout_hooks:
            try:
                await hook(client, reason)
            except Exception:  # noqa: BLE001
                log.exception("logout hook failed for %s", client)

    # --- server to client ---------------------------------------------------

    def writer(self) -> Writer:
        return Writer(self.encoding)

    def push(self, client: "Client", protocol: int, method: int, body: bytes, *, expect_response: bool = False):
        return self.server.push(client, protocol, method, body, expect_response=expect_response)

    def notify(self, client: "Client", source: int, type: int, subtype: int,
               param1: int = 0, param2: int = 0, param3: int = 0, text: str = "") -> None:
        """Send a ``GlobalNotificationEvent`` (acb-rdv ``RMC.SendNotification``)."""
        log.info("notify %s: source=%d type=%d.%d p=(%d,%d,%d) %r", client, source, type, subtype,
                 param1, param2, param3, text)
        body = (
            self.writer().u32(source).u32(type * 1000 + subtype).u32(param1).u32(param2)
            .string(text).u32(param3).bytes()
        )
        self.push(client, GLOBAL_NOTIFICATION_PROTOCOL, 1, body)
