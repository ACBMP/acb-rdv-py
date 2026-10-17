"""Assembles the ACB RendezVous server from the Quazal library."""

from __future__ import annotations

import asyncio
import logging

from quazal.capture import CaptureWriter
from quazal.context import ServerContext
from quazal.nat.relay import RelayService
from quazal.nat.service import NatService
from quazal.profile import GameProfile
from quazal.protocols.authentication import AuthenticationProtocol
from quazal.protocols.friends import FriendsProtocol
from quazal.protocols.game_session import GameSessionManager, GameSessionProtocol
from quazal.protocols.messaging import MessageDeliveryProtocol, MessagingProtocol
from quazal.protocols.nat import NatTraversalProtocol
from quazal.protocols.presence import RichPresenceProtocol
from quazal.protocols.secure import SecureProtocol
from quazal.prudp.transport import PRUDPServer
from quazal import httpserver as http
from quazal.rmc import Dispatcher
from quazal.storage import MemoryStorage, UbiStorage, User

from .config import Config
from .http.status import StatusAPI
from .leaderboards import Leaderboards
from .matchmaking import AcbSessionPolicy
from .news import make_source
from .settings import SettingsError, make_provider
from .protocols.contacts import ContactsExtensionsProtocol
from .protocols.hermes import HermesPlayerStatisticsProtocol
from .protocols.ubi import (
    GameInfoProtocol,
    LocalizationProtocol,
    NewsProtocol,
    PersistentStoreProtocol,
    PrivilegesProtocol,
    TrackingExtProtocol,
    TrackingProtocol,
    UbiAccountManagementProtocol,
    UbiNewsProtocol,
    UplayWinProtocol,
    VirginProtocol,
)

log = logging.getLogger("acb.server")

ACB_PROFILE = GameProfile(name="Assassin's Creed Brotherhood")


class AcbServer:
    def __init__(self, config: Config, storage: UbiStorage | None = None):
        self.config = config
        self.address = config.resolved_address()
        self.storage: UbiStorage = storage if storage is not None else self._make_storage()
        self.dispatcher = Dispatcher(ACB_PROFILE.string_encoding)
        capture = CaptureWriter(config.packet_capture) if config.packet_capture else None
        self.prudp = PRUDPServer(ACB_PROFILE, self.dispatcher, idle_timeout=config.idle_timeout, capture=capture)
        self.ctx = ServerContext(self.prudp, self.storage, public_address=self.address,
                                 secure_port=config.secure_port, secure_pid=config.secure_pid)
        self.prudp.on_connect = self._on_connect

        self.relay = None
        if config.nat.relay and config.relay_port_start and config.relay_port_end >= config.relay_port_start:
            self.relay = RelayService(self.address, config.relay_port_start, config.relay_port_end,
                                      bind_host=config.bind_host, idle_timeout=config.relay_idle_timeout)
        self.nat = NatService(self.ctx, config.nat, self.relay)
        self.settings = make_provider(config.game_settings, config.cxb_library)
        self.news = make_source(config.news, config.anapi_url, config.news_sender)
        self.sessions = GameSessionManager(AcbSessionPolicy(config.allow_private_sessions))
        game_sessions = GameSessionProtocol(self.ctx, self.sessions, self.nat)
        self.dispatcher.register(
            NatTraversalProtocol(self.ctx, self.nat),
            AuthenticationProtocol(self.ctx),
            SecureProtocol(self.ctx),
            FriendsProtocol(self.ctx, game_invites=game_sessions.deliver_stored_invites),
            MessagingProtocol(self.ctx),
            PersistentStoreProtocol(self.ctx, self.settings),
            MessageDeliveryProtocol(self.ctx),
            UbiAccountManagementProtocol(self.ctx),
            NewsProtocol(self.ctx, self.news),
            UbiNewsProtocol(self.ctx),
            PrivilegesProtocol(self.ctx, self.storage),
            TrackingProtocol(self.ctx, self.storage),
            LocalizationProtocol(self.ctx),
            game_sessions,
            HermesPlayerStatisticsProtocol(self.ctx, Leaderboards(self.storage, config)),
            RichPresenceProtocol(self.ctx),
            TrackingExtProtocol(self.ctx),
            GameInfoProtocol(self.ctx, config.game_storage_file_size),
            ContactsExtensionsProtocol(self.ctx, game_sessions, self.nat),
            VirginProtocol(self.ctx),
            UplayWinProtocol(self.ctx, self.storage),
        )
        self._http: list[asyncio.Server] = []

    def _make_storage(self) -> UbiStorage:
        if self.config.backend == "memory":
            log.warning("using in-memory storage: accounts are created on first login and nothing is saved")
            return AutoCreateMemoryStorage()
        if self.config.backend != "anapi":
            raise ValueError(f"unknown backend {self.config.backend!r}")
        if not self.config.anapi_service_key:
            raise ValueError("anapi_service_key is required for the AN-API backend")
        from .backend.anapi import AnApiStorage

        return AnApiStorage(self.config.anapi_url, self.config.anapi_service_key)

    async def _on_connect(self, client, conn, pid: int, cid: int) -> bool:
        """Secure-server CONNECT: only the player who logged in from this endpoint may connect.

        acb-rdv looked the pid up and accepted anyone; since ticket keys derive
        from a public dummy password, that let a crafted CONNECT take over any
        account.
        """
        if client.user is not None and client.pid == pid:
            return True
        log.warning("%s: CONNECT for pid %d without a matching login - refused", client, pid)
        return False

    async def start(self) -> None:
        cfg = self.config
        await self.prudp.listen("auth", cfg.bind_host, cfg.auth_port, server_pid=cfg.secure_pid,
                                target_port=cfg.secure_port, secure=False)
        await self.prudp.listen("rdv", cfg.bind_host, cfg.secure_port, server_pid=cfg.secure_pid,
                                target_port=cfg.secure_port, secure=True)
        if self.relay is not None:
            self.relay.start()
        await self._prime_settings()
        if cfg.status_port:
            api = StatusAPI(self, cfg.internal_key)
            self._http.append(await http.serve("status API", cfg.status_host, cfg.status_port, api.handle))
        log.info("ACB RendezVous server up; advertising %s", self.address)

    async def _prime_settings(self) -> None:
        """Build/load the game settings now so problems show at start, not at a player's login."""
        try:
            settings = await self.settings.current()
        except SettingsError as exc:
            log.error("game settings unavailable - players will get an empty file: %s", exc)
            return
        log.info("game settings ready: %d bytes from %s", settings.size, settings.source)
        if settings.size > self.config.game_storage_file_size:
            log.warning("game settings (%d bytes) are larger than the size GameInfo reports (%d); "
                        "raise game_storage_file_size if clients misbehave", settings.size,
                        self.config.game_storage_file_size)

    async def stop(self) -> None:
        for srv in self._http:
            srv.close()
        self.prudp.close()
        if self.relay is not None:
            self.relay.close()
        for owner in (self.storage, self.news):
            close = getattr(owner, "close", None)
            if close is not None:
                await close()


class AutoCreateMemoryStorage(MemoryStorage):
    """Development storage: any name/password logs in, with a pid assigned on first use."""

    async def authenticate(self, name: str, password: str) -> User:
        user = await self.get_user_by_name(name)
        if user is None:
            user = self.add_user(User(pid=10000 + len(self.users) + 1, name=name), password)
        return user
