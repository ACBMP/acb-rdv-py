"""Friends protocol (0x14), acb-rdv ``FriendsService``."""

from __future__ import annotations

import asyncio
import logging

from ..context import ServerContext
from ..ddl import U8, U32, ResultRange, String, Structure
from ..errors import QError
from ..rmc import Call, Protocol, RMCError, method
from ..storage import RelationshipType, RelResult
from . import notifications as notif

log = logging.getLogger("quazal.friends")

# delays before the logon notifications (acb-rdv SendLogonFriendInvites / SendLogonGameInvites)
LOGON_FRIEND_INVITES_DELAY = 2.0
LOGON_GAME_INVITES_DELAY = 3.0


class FriendData(Structure):
    pid: U32 = 0
    name: String = ""
    relationship: U8 = 0
    details: U32 = 0
    status: String = ""


class RelationshipData(Structure):
    pid: U32 = 0
    name: String = ""
    relationship: U8 = 0
    details: U32 = 0
    status: U8 = 0


class FriendsProtocol(Protocol):
    protocol_id = 0x14

    def __init__(self, ctx: ServerContext, *, game_invites=None):
        self.ctx = ctx
        # callable(client) that delivers stored game invites on logon (set by the game session protocol)
        self.game_invites = game_invites
        ctx.logout_hooks.append(self._notify_offline)

    async def _user_or_error(self, pid: int):
        user = await self.ctx.storage.get_user_by_pid(pid)
        if user is None:
            raise RMCError(QError.Friends_UserNotFound)
        return user

    @method(4, "AddFriendByNameWithDetails")
    async def add_friend_by_name(self, call: Call):
        invitee_name = call.args.string()
        details = call.args.u32()
        inviter_name = call.args.string()
        client = call.client
        if invitee_name == inviter_name or invitee_name == client.name:
            raise RMCError(QError.Friends_CannotAddYourselfAsFriend)
        invitee = await self.ctx.storage.get_user_by_name(invitee_name)
        if invitee is None:
            raise RMCError(QError.Friends_UserNotFound)
        result = await self.ctx.storage.add_friend_request(client.pid, invitee.pid, details)
        if result == RelResult.USER_BLOCKED:
            raise RMCError(QError.Friends_CannotAddBlacklistedPlayer)

        def notify_invitee() -> None:
            target = self.ctx.online(invitee.pid)
            if target is not None:
                notif.friend_invite_received(self.ctx, target, client.pid, client.name)

        call.after(notify_invitee)
        return RelationshipData(invitee.pid, invitee_name, 0, 0, 0)

    @method(5, "AcceptFriendship")
    async def accept_friendship(self, call: Call):
        inviter_pid = call.args.u32()
        details = call.args.u32() if call.args.remaining() >= 4 else 0
        client = call.client
        inviter = await self._user_or_error(inviter_pid)
        result = await self.ctx.storage.accept_friend(inviter.pid, client.pid, details)

        def notify() -> None:
            target = self.ctx.online(inviter.pid)
            if target is not None:
                notif.friend_invite_accepted(self.ctx, target, client.pid, client.name)
                notif.friend_status_changed(self.ctx, client, target.pid, target.name, True)

        call.after(notify)
        return self.ctx.writer().bool(result == RelResult.SUCCEEDED)

    @method(6, "DeclineFriendship")
    async def decline_friendship(self, call: Call):
        inviter_pid = call.args.u32()
        client = call.client
        await self._user_or_error(inviter_pid)
        removed = await self.ctx.storage.remove_relationship(inviter_pid, client.pid)

        def notify() -> None:
            target = self.ctx.online(inviter_pid)
            if target is not None:
                notif.friend_removed(self.ctx, target, client.pid, client.name)

        call.after(notify)
        return self.ctx.writer().bool(removed)

    @method(7, "BlackList")
    async def blacklist(self, call: Call):
        pid = call.args.u32()
        details = call.args.u32()
        client = call.client
        await self._user_or_error(pid)
        result = await self.ctx.storage.add_blacklist(client.pid, pid, details)

        def notify() -> None:
            target = self.ctx.online(pid)
            if target is not None and result == RelResult.SUCCEEDED:
                notif.friend_removed(self.ctx, target, client.pid, client.name)

        call.after(notify)
        return self.ctx.writer().bool(result == RelResult.SUCCEEDED)

    @method(9, "ClearRelationship")
    async def clear_relationship(self, call: Call):
        pid = call.args.u32()
        client = call.client
        removed = await self.ctx.storage.remove_relationship(client.pid, pid)

        def notify() -> None:
            target = self.ctx.online(pid)
            if target is not None and removed:
                notif.friend_removed(self.ctx, target, client.pid, client.name)

        call.after(notify)
        return self.ctx.writer().bool(removed)

    @method(12, "GetDetailedList")
    async def get_detailed_list(self, call: Call):
        rel_type = call.args.u8()
        reversed_ = call.args.bool()
        client = call.client
        try:
            wanted = RelationshipType(rel_type)
        except ValueError:
            return self.ctx.writer().u32(0)
        relationships = await self.ctx.storage.get_relationships(client.pid, wanted)
        friends: list[tuple[FriendData, bool]] = []
        to_notify = []
        for rel in relationships:
            other_pid = rel.other(client.pid)
            other = await self.ctx.storage.get_user_by_pid(other_pid)
            if other is None:
                continue
            online = self.ctx.online(other_pid)
            invite_notif = rel.type == RelationshipType.PENDING and other_pid == rel.requester
            friends.append((FriendData(other.pid, other.name, rel.type, rel.details,
                                       "Online" if online else "Offline"), invite_notif))
            if online is not None and rel.type == RelationshipType.FRIEND:
                to_notify.append(online)

        def after() -> None:
            # tell online friends this player is here
            for friend in to_notify:
                notif.friend_status_changed(self.ctx, friend, client.pid, client.name, True)
            # the client asks for pending (reversed) and then blocked lists at logon;
            # pending friend and game invites are replayed after those requests
            if wanted == RelationshipType.PENDING and reversed_:
                pending = [f for f, invite in friends if invite]
                self.ctx.server.spawn(self._logon_friend_invites(client, pending))
            if wanted == RelationshipType.BLOCKED and self.game_invites is not None:
                self.ctx.server.spawn(self._logon_game_invites(client))

        call.after(after)
        w = self.ctx.writer()
        w.list([f for f, _ in friends], lambda w, f: f.write(w))
        return w

    async def _logon_friend_invites(self, client, pending: list[FriendData]) -> None:
        await asyncio.sleep(LOGON_FRIEND_INVITES_DELAY)
        for friend in pending:
            notif.friend_invite_received(self.ctx, client, friend.pid, friend.name)

    async def _logon_game_invites(self, client) -> None:
        await asyncio.sleep(LOGON_GAME_INVITES_DELAY)
        await self.game_invites(client)

    @method(13, "GetRelationships")
    async def get_relationships(self, call: Call):
        rng = ResultRange.read(call.args)
        client = call.client
        relationships = await self.ctx.storage.get_relationships(client.pid, RelationshipType.FRIEND)
        out = []
        for rel in relationships:
            other_pid = rel.other(client.pid)
            other = await self.ctx.storage.get_user_by_pid(other_pid)
            if other is None:
                continue
            online = self.ctx.online(other_pid) is not None
            out.append(RelationshipData(other.pid, other.name, rel.type, rel.details, 1 if online else 0))
            if len(out) == rng.size:
                break
        w = self.ctx.writer()
        w.list(out, lambda w, r: r.write(w))
        return w

    async def _notify_offline(self, client, reason: str) -> None:
        """Tell online friends a player went away (acb-rdv DISCONNECT / timeout paths)."""
        if reason == "replaced by a new login":
            return
        try:
            relationships = await self.ctx.storage.get_relationships(client.pid, RelationshipType.FRIEND)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load friends of %s to announce logout: %s", client, exc)
            return
        for rel in relationships:
            friend = self.ctx.online(rel.other(client.pid))
            if friend is not None:
                notif.friend_status_changed(self.ctx, friend, client.pid, client.name, False)
