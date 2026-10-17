"""ACB game session attributes and matchmaking (acb-rdv ``Globals/Session.cs``,
``Data/Enums/SessionParam.cs`` and ``GameSessionService.CreateSession``)."""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import TYPE_CHECKING

from quazal.context import player
from quazal.errors import QError
from quazal.protocols.game_session import GameSessionManager, GameSessionQuery, Session, SessionPolicy
from quazal.rmc import RMCError

if TYPE_CHECKING:
    from quazal.prudp.transport import Client

log = logging.getLogger("acb.matchmaking")


class SessionParam(IntEnum):
    SEARCH_STRATEGY = 0
    MAX_PUBLIC_SLOTS = 3
    MAX_PRIVATE_SLOTS = 4
    CURRENT_PUBLIC_SLOTS = 5
    CURRENT_PRIVATE_SLOTS = 6
    IS_PRIVATE = 7
    FREE_PUBLIC_SLOTS = 0x32
    FREE_PRIVATE_SLOTS = 0x33
    CXB_CRC_SUM = 0x64
    MAP_ID = 0x65
    HOST_LEVEL_RANGE = 0x66
    LANGUAGE_ID = 0x67
    REGION_ID = 0x68
    MIN_LEVEL_RANGE = 0x69
    MAX_LEVEL_RANGE = 0x6A
    UNK_6B = 0x6B
    GAME_MODE = 0x6C
    QUERY_MAX_SLOTS_TAKEN = 0x6D
    GAME_TYPE = 0x6E
    ACCESSIBILITY = 0x6F
    DLC_VALUE = 0x70
    IS_BEING_PLAYED = 0x71
    NAT_TYPE = 0x72
    PUNKBUSTER_ACTIVE = 0x73


class GameMode(IntEnum):
    MENU = 1
    CHEST_CAPTURE = 2
    ALLIANCE = 3
    WANTED = 4
    INTRO = 5
    ASSASSINATE = 6
    ESCORT = 7
    ADVANCED_ALLIANCE = 8
    MANHUNT = 12
    ADVANCED_WANTED = 13


class GameType(IntEnum):
    RANKED = 0
    PUBLIC = 1
    PRIVATE = 2


class NatType(IntEnum):
    UNDEFINED = 0
    OPEN = 1
    MODERATE = 2
    STRICT = 3


# attributes hidden from search results
HIDDEN_ATTRIBUTES = {
    SessionParam.IS_PRIVATE,
    SessionParam.MIN_LEVEL_RANGE,
    SessionParam.MAX_LEVEL_RANGE,
    SessionParam.PUNKBUSTER_ACTIVE,
}

# searches outside a session after which a query counts as "PLAY NOW"
PLAY_NOW_SEARCHES = 2


class AcbSessionPolicy(SessionPolicy):
    def __init__(self, allow_private_sessions: bool = True):
        self.allow_private_sessions = allow_private_sessions

    def prepare_new_session(self, session: Session, client: "Client") -> None:
        mode = session.value(SessionParam.GAME_MODE)
        gtype = session.value(SessionParam.GAME_TYPE)
        if not self.allow_private_sessions and gtype == GameType.PRIVATE and mode != GameMode.INTRO:
            log.info("%s: private session refused (private sessions are disabled)", client)
            raise RMCError(QError.GameSession_Unknown)
        # the level range is expected by searches; fill it in when missing
        if session.get(SessionParam.MIN_LEVEL_RANGE) is None:
            session.set(SessionParam.MIN_LEVEL_RANGE, 0)
        if session.get(SessionParam.MAX_LEVEL_RANGE) is None:
            session.set(SessionParam.MAX_LEVEL_RANGE, 6)
        session.set(SessionParam.ACCESSIBILITY, 0)
        # every host is advertised as open (acb-rdv's "blind NAT type update")
        if session.get(SessionParam.NAT_TYPE) is None:
            log.info("session of %s has no NAT type attribute", client)
        session.set(SessionParam.NAT_TYPE, NatType.OPEN)

    def before_search(self, client: "Client") -> None:
        state = player(client)
        if not state.in_game_session:
            state.pre_session_search_count += 1
        if state.pre_session_search_count > PLAY_NOW_SEARCHES:
            state.play_now_query = True

    def result_attributes(self, session: Session):
        return [p for p in session.attributes if p.id not in HIDDEN_ATTRIBUTES]

    def matches(self, manager: GameSessionManager, session: Session, query: GameSessionQuery,
                client: "Client") -> bool:
        q_min = query.get(SessionParam.MIN_LEVEL_RANGE)
        q_max = query.get(SessionParam.MAX_LEVEL_RANGE)
        q_mode = query.get(SessionParam.GAME_MODE)
        q_slots_taken = query.get(SessionParam.QUERY_MAX_SLOTS_TAKEN)
        q_type = query.get(SessionParam.GAME_TYPE)
        if q_min is None or q_max is None or q_mode is None or q_type is None:
            log.info("session %d: incomplete query", session.id)
            return False
        if client.pid == session.host_pid:
            return False
        mode = session.value(SessionParam.GAME_MODE)
        gtype = session.value(SessionParam.GAME_TYPE)
        if mode is None or gtype is None:
            log.info("session %d: missing game mode or type", session.id)
            return False
        state = player(client)
        if mode != q_mode or gtype != q_type:
            # PLAY NOW matchmaking may join any non-menu session of the same type
            if not (state.play_now_query and mode != GameMode.MENU and gtype == q_type):
                return False
        slots_param = (SessionParam.CURRENT_PRIVATE_SLOTS if gtype == GameType.PRIVATE
                       else SessionParam.CURRENT_PUBLIC_SLOTS)
        if session.get(slots_param) is None:
            log.info("session %d: missing current slots", session.id)
            return False
        session.update_current_slots()
        current = session.value(slots_param) or 0
        if q_min == q_max or q_slots_taken is None:
            if q_slots_taken is not None:
                return False  # level range restriction
            # first matchmaking query of a player: it gives up any session it hosts
            old = manager.hosted_by(client.pid)
            if old is not None:
                log.info("removing old session %d of %s", old.id, client)
                manager.remove(old.id)
            if current < (session.value(SessionParam.MAX_PUBLIC_SLOTS) or 0):
                state.game_session_id = session.id
                state.in_game_session = True
                return True
            return False
        return current <= q_slots_taken
