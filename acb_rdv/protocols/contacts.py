"""Contacts extensions (0x72): join a friend's game session.

acb-rdv ``ContactsService``, plus a relay candidate. Clients only probe the
candidates listed in this response, so the relay URL has to be offered here.
"""

from __future__ import annotations

import logging

from quazal.context import ServerContext, player
from quazal.ddl import Bool, String, Structure, U32, field
from quazal.nat.service import NatService
from quazal.protocols.game_session import GameSessionManager, GameSessionProtocol, GameSessionSearchResult
from quazal.rmc import Call, Protocol, method

log = logging.getLogger("acb.contacts")


class GameSessionByPlayerInfo(Structure):
    friend_name: String = ""
    friend_pid: U32 = 0
    unk1: Bool = True
    unk2: Bool = True
    result: GameSessionSearchResult = field(default_factory=GameSessionSearchResult)


class ContactsExtensionsProtocol(Protocol):
    protocol_id = 0x72
    name = "ContactsExtensions"

    def __init__(self, ctx: ServerContext, sessions: GameSessionProtocol, nat: NatService | None):
        self.ctx = ctx
        self.sessions = sessions
        self.manager: GameSessionManager = sessions.manager
        self.nat = nat

    @method(3, "RetrieveGameSessionFromContact")
    async def retrieve_game_session_from_contact(self, call: Call):
        call.args.u32()  # session type
        names = call.args.string_list()
        client = call.client
        results = []
        for name in names:
            friend = self.ctx.online_by_name(name)
            if friend is None:
                continue
            session = self.manager.find(player(friend).game_session_id) or self.manager.with_participant(friend.pid)
            if session is None:
                continue
            host = self.ctx.online(session.host_pid)
            if host is None or not session.is_joinable():
                continue
            state = player(client)
            state.game_session_id = session.id
            state.in_game_session = True
            extra = await self.nat.contact_candidates(client, host) if self.nat else []
            result = self.sessions.search_result(session, client, extra)
            if result is None:
                continue
            log.info("%s joins %s's session %d via host %s", client, name, session.id, host)
            results.append(GameSessionByPlayerInfo(friend.name, friend.pid, True, True, result))
        return self.ctx.writer().list(results, lambda w, r: r.write(w))
