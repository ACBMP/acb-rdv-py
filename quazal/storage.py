"""Persistence interfaces used by the protocol implementations.

Game servers provide an implementation (the ACB server talks to AN-API);
:class:`MemoryStorage` is a complete in-process implementation for tests and
local development.
"""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field, replace
from typing import Protocol


class RelationshipType(enum.IntEnum):
    FRIEND = 1
    PENDING = 2
    BLOCKED = 3


class RelResult(enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ALREADY_PRESENT = "already_present"
    USER_BLOCKED = "user_blocked"


class AuthFailure(Exception):
    """Login rejected; ``reason`` is ``unknown_user``, ``bad_password`` or ``revoked``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BackendUnavailable(Exception):
    """The persistence backend could not be reached."""


@dataclass
class User:
    pid: int
    name: str
    ubi_id: str = ""
    email: str = ""
    country_code: str = "US"
    pref_lang: str = "en"


@dataclass
class Relationship:
    requester: int
    requestee: int
    type: RelationshipType
    details: int = 0

    def other(self, pid: int) -> int:
        return self.requestee if self.requester == pid else self.requester


@dataclass
class StoredMessage:
    id: int
    recipient_pid: int
    recipient_type: int
    parent_id: int
    sender_pid: int
    reception_time: int
    lifetime: int
    flags: int
    subject: str
    sender_name: str
    body: str
    delivered: bool = False


@dataclass
class GameInvite:
    id: int
    inviter: int
    invitee: int
    session_type: int
    session_id: int
    message: str


@dataclass
class Privilege:
    id: int
    description: str


@dataclass
class Reward:
    code: str
    name: str
    description: str
    value: int


@dataclass
class ModeStats:
    """A player's record in one mode, as AN-API keeps it (``{mode}mmr``, ``{mode}games``, ``{mode}stats``)."""

    mmr: float = 0.0
    games: int = 0
    won: int = 0
    lost: int = 0
    total_score: int = 0
    high_score: int = 0
    kills: int = 0
    deaths: int = 0


@dataclass
class PlayerRecord:
    """A visible player with their per-mode records; ``pid`` is 0 if they never logged in to the game."""

    pid: int
    name: str
    modes: dict[str, ModeStats]


@dataclass
class LadderEntry:
    """A player on a mode's MMR ladder; ``pid`` is 0 for players who never logged in to the game."""

    pid: int
    name: str
    mmr: float
    games: int


class Storage(Protocol):
    # accounts
    async def authenticate(self, name: str, password: str) -> User: ...
    async def get_user_by_pid(self, pid: int) -> User | None: ...
    async def get_user_by_name(self, name: str) -> User | None: ...

    # relationships
    async def add_friend_request(self, requester: int, requestee: int, details: int) -> RelResult: ...
    async def accept_friend(self, requester: int, requestee: int, details: int) -> RelResult: ...
    async def add_blacklist(self, requester: int, requestee: int, details: int) -> RelResult: ...
    async def remove_relationship(self, a: int, b: int) -> bool: ...
    async def get_relationships(self, pid: int, type: RelationshipType) -> list[Relationship]: ...

    # messages
    async def add_message(self, msg: StoredMessage) -> bool: ...
    async def pending_messages(self, recipient_pid: int) -> list[StoredMessage]: ...
    async def messages_by_ids(self, ids: list[int]) -> list[StoredMessage]: ...
    async def mark_delivered(self, ids: list[int]) -> None: ...

    # offline game invites
    async def add_game_invite(self, invite: GameInvite) -> bool: ...
    async def take_game_invites(self, invitee: int) -> list[GameInvite]: ...


class StatsStorage(Protocol):
    """Player records behind the in-game leaderboards (AN-API's ladders and player list)."""

    # a mode's MMR ladder in the website's order: placed players first, then by MMR
    async def mmr_ladder(self, mode: str) -> list[LadderEntry]: ...
    async def player_records(self) -> list[PlayerRecord]: ...


class UbiStorage(Storage, StatsStorage, Protocol):
    """Ubisoft-service data (privileges, Uplay rewards, telemetry, Hermes statistics)."""

    async def privileges(self, locale: str) -> list[Privilege]: ...
    async def rewards(self) -> list[Reward]: ...
    async def telemetry_tags(self) -> list[str]: ...
    async def save_telemetry(self, tracking_id: int, tag: str, attributes: str, delta_time: int) -> None: ...


DEFAULT_TAGS = [
    "LINKAPP_VIEW", "UPLAY_START", "UPLAY_STOP", "GAME_START", "GAME_STOP", "FPSCLIENT_START",
    "FPSCLIENT_STOP", "LEVEL_START", "LEVEL_STOP", "OBJECTIVE_START", "OBJECTIVE_STOP",
    "UPLAY_BROWSE", "FPSCLIENT_ABORT", "MATCHMAKING_STATS", "SEQUENCE_STOP", "PLAYER_STATS",
    "AWARD_UNLOCK", "PLAYER_DEATH", "GAME_SAVE", "INSTALL_START", "INSTALL_STOP", "MENU_ENTER",
    "MENU_EXIT", "MENU_OPTIONCHANGE", "MM_RES", "PLAYER_KILL", "PLAYER_SAVED", "UNINSTALL_START",
    "UNINSTALL_STOP", "VIDEO_START", "VIDEO_STOP",
]


@dataclass
class MemoryStorage:
    """In-memory storage with the relationship semantics of acb-rdv's ``DbHelper``."""

    users: dict[int, User] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    messages: dict[int, StoredMessage] = field(default_factory=dict)
    invites: list[GameInvite] = field(default_factory=list)
    privilege_list: list[Privilege] = field(default_factory=lambda: [Privilege(1, "Allow to play online")])
    reward_list: list[Reward] = field(default_factory=list)
    tags: list[str] = field(default_factory=lambda: list(DEFAULT_TAGS))
    telemetry: list[tuple[int, str, str, int]] = field(default_factory=list)
    records: dict[int, dict[str, ModeStats]] = field(default_factory=dict)  # pid -> mode -> record
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def add_user(self, user: User, password: str = "") -> User:
        self.users[user.pid] = user
        self.passwords[user.name] = password
        return user

    async def authenticate(self, name: str, password: str) -> User:
        user = await self.get_user_by_name(name)
        if user is None:
            raise AuthFailure("unknown_user")
        if self.passwords.get(name) != password:
            raise AuthFailure("bad_password")
        return user

    async def get_user_by_pid(self, pid: int) -> User | None:
        return self.users.get(pid)

    async def get_user_by_name(self, name: str) -> User | None:
        return next((u for u in self.users.values() if u.name == name), None)

    def _between(self, a: int, b: int) -> list[Relationship]:
        return [r for r in self.relationships if {r.requester, r.requestee} == {a, b}]

    async def add_friend_request(self, requester: int, requestee: int, details: int) -> RelResult:
        existing = self._between(requester, requestee)
        for rel in existing:
            if rel.type == RelationshipType.BLOCKED:
                return RelResult.USER_BLOCKED
            if rel.type == RelationshipType.PENDING:
                return RelResult.FAILED
        if existing:
            return RelResult.ALREADY_PRESENT
        self.relationships.append(Relationship(requester, requestee, RelationshipType.PENDING, details))
        return RelResult.SUCCEEDED

    async def accept_friend(self, requester: int, requestee: int, details: int) -> RelResult:
        existing = self._between(requester, requestee)
        if any(r.type == RelationshipType.FRIEND for r in existing):
            return RelResult.ALREADY_PRESENT
        # only the invited player can accept (acb-rdv left this as a TODO)
        if (len(existing) == 1 and existing[0].type == RelationshipType.PENDING
                and existing[0].requester == requester):
            existing[0].type = RelationshipType.FRIEND
            existing[0].details = details
            return RelResult.SUCCEEDED
        return RelResult.ALREADY_PRESENT

    async def add_blacklist(self, requester: int, requestee: int, details: int) -> RelResult:
        existing = self._between(requester, requestee)
        if any(r.type == RelationshipType.BLOCKED for r in existing):
            return RelResult.USER_BLOCKED
        if len(existing) == 1:
            rel = existing[0]
            rel.requester, rel.requestee = requester, requestee
            rel.type, rel.details = RelationshipType.BLOCKED, details
            return RelResult.SUCCEEDED
        # acb-rdv only blocks players it already has a relationship with
        return RelResult.USER_BLOCKED

    async def remove_relationship(self, a: int, b: int) -> bool:
        before = len(self.relationships)
        self.relationships = [r for r in self.relationships if {r.requester, r.requestee} != {a, b}]
        return len(self.relationships) != before

    async def get_relationships(self, pid: int, type: RelationshipType) -> list[Relationship]:
        if type == RelationshipType.BLOCKED:
            return [replace(r) for r in self.relationships if r.requester == pid and r.type == type]
        return [replace(r) for r in self.relationships if pid in (r.requester, r.requestee) and r.type == type]

    async def add_message(self, msg: StoredMessage) -> bool:
        msg = replace(msg, id=next(self._ids), delivered=False)
        self.messages[msg.id] = msg
        return True

    async def pending_messages(self, recipient_pid: int) -> list[StoredMessage]:
        return [m for m in self.messages.values() if m.recipient_pid == recipient_pid and not m.delivered]

    async def messages_by_ids(self, ids: list[int]) -> list[StoredMessage]:
        return [self.messages[i] for i in ids if i in self.messages]

    async def mark_delivered(self, ids: list[int]) -> None:
        for i in ids:
            if i in self.messages:
                self.messages[i].delivered = True

    async def add_game_invite(self, invite: GameInvite) -> bool:
        self.invites.append(replace(invite, id=next(self._ids)))
        return True

    async def take_game_invites(self, invitee: int) -> list[GameInvite]:
        taken = [i for i in self.invites if i.invitee == invitee]
        self.invites = [i for i in self.invites if i.invitee != invitee]
        return taken

    async def privileges(self, locale: str) -> list[Privilege]:
        return list(self.privilege_list)

    async def rewards(self) -> list[Reward]:
        return list(self.reward_list)

    async def telemetry_tags(self) -> list[str]:
        return list(self.tags)

    async def save_telemetry(self, tracking_id: int, tag: str, attributes: str, delta_time: int) -> None:
        self.telemetry.append((tracking_id, tag, attributes, delta_time))

    async def mmr_ladder(self, mode: str) -> list[LadderEntry]:
        entries = [LadderEntry(pid, self.users[pid].name, m[mode].mmr, m[mode].games)
                   for pid, m in self.records.items() if mode in m and m[mode].games > 0 and pid in self.users]
        entries.sort(key=lambda e: (e.games < 10, -e.mmr))  # as AN-API's /boards/ranking
        return entries

    async def player_records(self) -> list[PlayerRecord]:
        return [PlayerRecord(pid, self.users[pid].name, dict(m)) for pid, m in self.records.items() if pid in self.users]
