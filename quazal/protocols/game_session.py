"""Game sessions (0x2A), acb-rdv ``GameSessionService`` and ``Globals/Session.cs``.

Sessions live in memory. The generic parts (slots, participants, host
migration, invitations, URL registration) are here; what a search query
matches and which attributes a result exposes is decided by a game-supplied
:class:`SessionPolicy`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from ..context import ServerContext, player
from ..ddl import DateTime, ListOf, Property, ResultRange, Station, StationURL, String, Structure, U32
from ..errors import QError
from ..rmc import Call, Protocol, RMCError, method
from ..storage import GameInvite
from . import notifications as notif

if TYPE_CHECKING:
    from ..nat.service import NatService
    from ..prudp.transport import Client

log = logging.getLogger("quazal.gamesession")


class Param(IntEnum):
    """Standard game session attributes (games add their own from 0x64)."""

    MAX_PUBLIC_SLOTS = 3
    MAX_PRIVATE_SLOTS = 4
    CURRENT_PUBLIC_SLOTS = 5
    CURRENT_PRIVATE_SLOTS = 6
    IS_PRIVATE = 7


class GameSessionKey(Structure):
    type_id: U32 = 0
    session_id: U32 = 0


class GameSession(Structure):
    type_id: U32 = 0
    attributes: ListOf(Property) = field(default_factory=list)


class GameSessionUpdate(Structure):
    key: GameSessionKey = field(default_factory=GameSessionKey)
    attributes: ListOf(Property) = field(default_factory=list)


class GameSessionQuery(Structure):
    type_id: U32 = 0
    query_id: U32 = 0
    params: ListOf(Property) = field(default_factory=list)

    def get(self, param: int) -> int | None:
        return next((p.value for p in self.params if p.id == param), None)


class GameSessionSearchResult(Structure):
    key: GameSessionKey = field(default_factory=GameSessionKey)
    host_pid: U32 = 0
    host_urls: ListOf(Station) = field(default_factory=list)
    attributes: ListOf(Property) = field(default_factory=list)


class GameSessionInvitation(Structure):
    key: GameSessionKey = field(default_factory=GameSessionKey)
    recipients: ListOf(U32) = field(default_factory=list)
    message: String = ""


class GameSessionInvitationReceived(Structure):
    key: GameSessionKey = field(default_factory=GameSessionKey)
    sender_pid: U32 = 0
    message: String = ""
    creation_time: DateTime = 0


class GameSessionInvitationSent(Structure):
    key: GameSessionKey = field(default_factory=GameSessionKey)
    recipient_pid: U32 = 0
    message: String = ""
    creation_time: DateTime = 0


@dataclass
class Session:
    key: GameSessionKey
    attributes: list[Property]
    host_pid: int
    host_urls: list[StationURL] = field(default_factory=list)
    public_pids: list[int] = field(default_factory=list)
    private_pids: list[int] = field(default_factory=list)
    migrating: bool = False

    @property
    def id(self) -> int:
        return self.key.session_id

    def get(self, param: int) -> Property | None:
        return next((p for p in self.attributes if p.id == param), None)

    def value(self, param: int, default: int | None = None) -> int | None:
        prop = self.get(param)
        return prop.value if prop is not None else default

    def set(self, param: int, value: int) -> None:
        prop = self.get(param)
        if prop is None:
            self.attributes.append(Property(param, value))
        else:
            prop.value = value

    def participants(self) -> int:
        return len(self.public_pids) + len(self.private_pids)

    def has_participant(self, pid: int) -> bool:
        return pid in self.public_pids or pid in self.private_pids

    def update_current_slots(self) -> None:
        self.public_pids = list(dict.fromkeys(self.public_pids))
        self.private_pids = list(dict.fromkeys(self.private_pids))
        self.set(Param.CURRENT_PUBLIC_SLOTS, len(self.public_pids))
        self.set(Param.CURRENT_PRIVATE_SLOTS, len(self.private_pids))

    def add_participants(self, public: list[int], private: list[int]) -> None:
        self.public_pids.extend(public)
        self.private_pids.extend(private)
        self.update_current_slots()

    def remove_participants(self, pids: list[int]) -> None:
        self.public_pids = [p for p in self.public_pids if p not in pids]
        self.private_pids = [p for p in self.private_pids if p not in pids]
        self.update_current_slots()

    def is_joinable(self) -> bool:
        is_private = self.value(Param.IS_PRIVATE)
        if is_private is None:
            return False
        maximum = self.value(Param.MAX_PUBLIC_SLOTS if is_private == 0 else Param.MAX_PRIVATE_SLOTS)
        current = self.value(Param.CURRENT_PUBLIC_SLOTS if is_private == 0 else Param.CURRENT_PRIVATE_SLOTS)
        if maximum is None or current is None:
            return False
        return maximum > current


class SessionPolicy:
    """Game-specific session rules. The defaults accept everything."""

    def prepare_new_session(self, session: Session, client: "Client") -> None:
        """Adjust a new session's attributes; raise :class:`RMCError` to refuse it."""

    def matches(self, manager: "GameSessionManager", session: Session, query: GameSessionQuery,
                client: "Client") -> bool:
        return session.host_pid != client.pid

    def result_attributes(self, session: Session) -> list[Property]:
        return list(session.attributes)

    def before_search(self, client: "Client") -> None:
        """Called before a search is evaluated (e.g. to track matchmaking state)."""


class GameSessionManager:
    def __init__(self, policy: SessionPolicy | None = None):
        self.policy = policy or SessionPolicy()
        self.sessions: dict[int, Session] = {}
        self.next_id = 1

    def find(self, session_id: int) -> Session | None:
        return self.sessions.get(session_id)

    def hosted_by(self, pid: int) -> Session | None:
        return next((s for s in self.sessions.values() if s.host_pid == pid), None)

    def with_participant(self, pid: int) -> Session | None:
        return next((s for s in self.sessions.values() if s.has_participant(pid)), None)

    def build(self, request: GameSession, host: "Client") -> Session:
        """A new, not yet registered session hosted by ``host`` (id assigned by :meth:`add`)."""
        return Session(GameSessionKey(request.type_id, 0), list(request.attributes), host.pid,
                       host_urls=list(player(host).urls))

    def add(self, session: Session) -> None:
        """Register a built session, replacing any other session of the same host."""
        old = self.hosted_by(session.host_pid)
        if old is not None:
            log.info("removing old session %d of host %d", old.id, session.host_pid)
            self.remove(old.id)
        session.key.session_id = self.next_id
        self.next_id += 1
        self.sessions[session.id] = session

    def remove(self, session_id: int) -> None:
        self.sessions.pop(session_id, None)

    def remove_hosted_by(self, pid: int) -> None:
        for sid in [s.id for s in self.sessions.values() if s.host_pid == pid]:
            self.remove(sid)

    def drop_player(self, pid: int, reason: str) -> None:
        """Remove a departed player from every session; delete sessions left empty."""
        for session in list(self.sessions.values()):
            if session.has_participant(pid):
                session.remove_participants([pid])
            elif session.host_pid != pid:
                continue
            if session.participants() == 0:
                self.remove(session.id)
                log.info("session %d deleted (%s of player %d)", session.id, reason, pid)


class GameSessionProtocol(Protocol):
    protocol_id = 0x2A

    def __init__(self, ctx: ServerContext, manager: GameSessionManager, nat: "NatService | None" = None,
                 *, allow_private_sessions: bool = True):
        self.ctx = ctx
        self.manager = manager
        self.nat = nat
        self.allow_private_sessions = allow_private_sessions
        ctx.login_hooks.append(self._on_login)
        ctx.logout_hooks.append(self._on_logout)

    async def _on_login(self, client: "Client") -> None:
        self.manager.remove_hosted_by(client.pid)

    async def _on_logout(self, client: "Client", reason: str) -> None:
        if reason != "replaced by a new login":
            self.manager.drop_player(client.pid, reason)

    # --- helpers --------------------------------------------------------

    def search_result(self, session: Session, viewer: "Client", extra: list[StationURL] | None = None
                      ) -> GameSessionSearchResult | None:
        host = self.ctx.online(session.host_pid)
        if host is None:
            return None
        urls = self.nat.host_candidates(host, viewer) if self.nat else list(player(host).registered_urls)
        for url in extra or ():
            if url not in urls:
                urls.append(url)
        return GameSessionSearchResult(GameSessionKey(session.key.type_id, session.key.session_id),
                                       session.host_pid, urls, self.manager.policy.result_attributes(session))

    def _join(self, pid: int, session_id: int) -> None:
        target = self.ctx.online(pid)
        if target is None:
            log.info("AddParticipants: player %d is not online", pid)
            return
        state = player(target)
        if state.in_game_session:
            state.abandoning_session = True
            state.abandoned_session_id = state.game_session_id
        state.game_session_id = session_id
        state.in_game_session = True

    # --- methods ----------------------------------------------------------

    @method(1, "CreateSession")
    async def create_session(self, call: Call):
        request = GameSession.read(call.args)
        client = call.client
        session = self.manager.build(request, client)
        self.manager.policy.prepare_new_session(session, client)
        session.set(Param.CURRENT_PUBLIC_SLOTS, 0)
        session.set(Param.CURRENT_PRIVATE_SLOTS, 0)
        self.manager.add(session)
        player(client).game_session_id = session.id
        log.info("%s created session %d", client, session.id)
        return GameSessionKey(request.type_id, session.id)

    @method(2, "UpdateSession")
    async def update_session(self, call: Call):
        update = GameSessionUpdate.read(call.args)
        session = self.manager.find(update.key.session_id)
        if session is None:
            raise RMCError(QError.GameSession_InvalidSessionKey)
        for prop in update.attributes:
            session.set(prop.id, prop.value)
        return None

    @method(4, "MigrateSession")
    async def migrate_session(self, call: Call):
        key = GameSessionKey.read(call.args)
        session = self.manager.find(key.session_id)
        if session is None:
            raise RMCError(QError.GameSession_InvalidSessionKey)
        client = call.client
        session.migrating = True
        session.host_pid = client.pid
        state = player(client)
        state.game_session_id = session.id
        state.in_game_session = True
        log.info("%s is migrating session %d", client, session.id)
        return key

    @method(5, "LeaveSession")
    async def leave_session(self, call: Call):
        GameSessionKey.read(call.args)
        state = player(call.client)
        state.game_session_id = 0
        state.in_game_session = False
        state.pre_session_search_count = 0
        state.play_now_query = False
        return None

    @method(6, "GetSession")
    async def get_session(self, call: Call):
        key = GameSessionKey.read(call.args)
        session = self.manager.find(key.session_id)
        if session is None:
            raise RMCError(QError.GameSession_InvalidSessionKey)
        if not session.is_joinable():
            is_private = session.value(Param.IS_PRIVATE)
            if is_private is None:
                raise RMCError(QError.GameSession_Unknown)
            raise RMCError(QError.GameSession_NoPublicSlotLeft if is_private == 0
                           else QError.GameSession_NoPrivateSlotLeft)
        result = self.search_result(session, call.client)
        if result is None:
            raise RMCError(QError.GameSession_InvalidPID)
        return result

    @method(7, "SearchSessions")
    async def search_sessions(self, call: Call):
        query = GameSessionQuery.read(call.args)
        client = call.client
        self.manager.policy.before_search(client)
        candidates = []
        for session in reversed(list(self.manager.sessions.values())):
            if not self.manager.policy.matches(self.manager, session, query, client):
                continue
            if self.ctx.online(session.host_pid) is None:
                log.info("removing session %d: host %d is offline", session.id, session.host_pid)
                self.manager.remove(session.id)
                continue
            candidates.append(session)
        joinable = sorted((s for s in candidates if s.is_joinable()), key=lambda s: s.participants(), reverse=True)
        results = [r for r in (self.search_result(s, client) for s in joinable) if r is not None]
        log.info("%s search: %d result(s)", client, len(results))
        return self.ctx.writer().list(results, lambda w, r: r.write(w))

    @method(8, "AddParticipants")
    async def add_participants(self, call: Call):
        key = GameSessionKey.read(call.args)
        public = call.args.u32_list()
        private = call.args.u32_list()
        session = self.manager.find(key.session_id)
        if session is None:
            raise RMCError(QError.GameSession_InvalidSessionKey)
        session.add_participants(public, private)
        for pid in public + private:
            self._join(pid, key.session_id)
        return None

    @method(9, "RemoveParticipants")
    async def remove_participants(self, call: Call):
        key = GameSessionKey.read(call.args)
        pids = call.args.u32_list()
        session = self.manager.find(key.session_id)
        if session is None:
            raise RMCError(QError.GameSession_InvalidSessionKey)
        session.remove_participants(pids)
        return None

    @method(12, "SendInvitation")
    async def send_invitation(self, call: Call):
        invitation = GameSessionInvitation.read(call.args)
        client = call.client
        for pid in invitation.recipients:
            target = self.ctx.online(pid)
            if target is not None:
                notif.game_invite_sent(self.ctx, target, client.pid, invitation.key.type_id,
                                       invitation.key.session_id, invitation.message)
                continue
            await self.ctx.storage.add_game_invite(GameInvite(0, client.pid, pid, invitation.key.type_id,
                                                              invitation.key.session_id, invitation.message))
        return None

    @method(14, "GetInvitationsReceived")
    async def get_invitations_received(self, call: Call):
        call.args.u32()
        ResultRange.read(call.args)
        # acb-rdv always answered with an empty list; stored invites are replayed as
        # notifications at logon instead (see FriendsProtocol)
        return self.ctx.writer().u32(0)

    @method(17, "AcceptInvitation")
    async def accept_invitation(self, call: Call):
        invitation = GameSessionInvitationReceived.read(call.args)
        client = call.client

        def notify() -> None:
            inviter = self.ctx.online(invitation.sender_pid)
            if inviter is not None:
                notif.game_invite_accepted(self.ctx, inviter, client.pid, invitation.key.session_id)

        call.after(notify)
        return None

    @method(18, "DeclineInvitation")
    async def decline_invitation(self, call: Call):
        invitation = GameSessionInvitationReceived.read(call.args)
        client = call.client

        def notify() -> None:
            inviter = self.ctx.online(invitation.sender_pid)
            if inviter is not None:
                notif.game_invite_declined(self.ctx, inviter, client.pid, invitation.key.session_id)

        call.after(notify)
        return None

    @method(19, "CancelInvitation")
    async def cancel_invitation(self, call: Call):
        GameSessionInvitationSent.read(call.args)
        return None

    @method(21, "RegisterURLs")
    async def register_urls(self, call: Call):
        urls = []
        for raw in call.args.string_list():
            try:
                urls.append(StationURL.parse(raw).canonical())
            except ValueError:
                log.warning("%s registered malformed URL %r", call.client, raw)
        client = call.client
        state = player(client)
        if state.game_session_id == 0:
            raise RMCError(QError.GameSession_PlayerIsNotSessionParticipant)
        session = self.manager.find(state.game_session_id)
        if session is None:
            state.game_session_id = 0
            state.in_game_session = False
            raise RMCError(QError.GameSession_Unknown)
        state.registered_urls = urls
        if client.pid == session.host_pid:
            session.host_urls = list(urls)
        if session.migrating:
            log.info("session %d migrated to host %d", session.id, session.host_pid)
            session.migrating = False
        relayed = [u for u in urls if self.nat is not None and self.nat.is_ac_relay(u)]
        log.info("%s registered %d URL(s) for session %d%s", client, len(urls), session.id,
                 f" ({len(relayed)} via AC Relay: {', '.join(map(str, relayed))})" if relayed else "")
        return None

    @method(23, "AbandonSession")
    async def abandon_session(self, call: Call):
        key = GameSessionKey.read(call.args)
        client = call.client
        state = player(client)
        if state.abandoning_session:
            state.abandoning_session = False
            session = self.manager.find(state.abandoned_session_id)
        else:
            session = self.manager.find(key.session_id)
        if session is None:
            log.info("%s abandoned unknown session %d", client, key.session_id)
            return None
        state.game_session_id = 0
        state.in_game_session = False
        removed = client.pid in session.public_pids or client.pid in session.private_pids
        session.remove_participants([client.pid])
        if session.participants() == 0 and removed:
            self.manager.remove(session.id)
            log.info("session %d deleted on abandon by %d", session.id, client.pid)
        return None

    # --- used by FriendsProtocol at logon ----------------------------------

    async def deliver_stored_invites(self, client: "Client") -> None:
        invites = await self.ctx.storage.take_game_invites(client.pid)
        for invite in invites:
            session = self.manager.find(invite.session_id)
            if session is None or not session.has_participant(invite.inviter):
                continue
            if self.ctx.online(invite.inviter) is None:
                continue
            notif.game_invite_sent(self.ctx, client, invite.inviter, invite.session_type, invite.session_id,
                                   invite.message)
