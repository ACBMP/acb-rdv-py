"""Notification events (acb-rdv ``Notifications/NotificationManager.cs``)."""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import ServerContext
    from ..prudp.transport import Client


class NotificationType(IntEnum):
    FRIENDS = 1
    SESSION_LAUNCH = 2
    PARTICIPATION = 3
    OWNERSHIP_CHANGE = 4
    FRIEND_STATUS_CHANGE = 5
    FORCE_DISCONNECT = 6
    GAME_SESSION = 7


class FriendEvent(IntEnum):
    DECLINED = 0
    ACCEPTED = 1
    REQUESTED = 2


class GameSessionEvent(IntEnum):
    INVITE_RECEIVED = 5
    INVITE_DECLINED = 7
    INVITE_ACCEPTED = 8


def friend_removed(ctx: "ServerContext", receiver: "Client", sender_pid: int, sender_name: str) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.FRIENDS, 1, sender_pid, FriendEvent.DECLINED, 0, sender_name)


def friend_invite_accepted(ctx: "ServerContext", receiver: "Client", sender_pid: int, sender_name: str) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.FRIENDS, 1, sender_pid, FriendEvent.ACCEPTED, 0, sender_name)


def friend_invite_received(ctx: "ServerContext", receiver: "Client", sender_pid: int, sender_name: str) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.FRIENDS, 1, sender_pid, FriendEvent.REQUESTED, 0, sender_name)


def friend_status_changed(ctx: "ServerContext", receiver: "Client", sender_pid: int, sender_name: str,
                          online: bool) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.FRIEND_STATUS_CHANGE, 1, sender_pid, 1 if online else 0,
               0, sender_name)


def game_invite_sent(ctx: "ServerContext", receiver: "Client", sender_pid: int, session_type: int,
                     session_id: int, message: str) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.GAME_SESSION, GameSessionEvent.INVITE_RECEIVED, 0,
               session_id, session_type, message)


def game_invite_declined(ctx: "ServerContext", receiver: "Client", sender_pid: int, session_id: int) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.GAME_SESSION, GameSessionEvent.INVITE_DECLINED, 0, session_id)


def game_invite_accepted(ctx: "ServerContext", receiver: "Client", sender_pid: int, session_id: int) -> None:
    ctx.notify(receiver, sender_pid, NotificationType.GAME_SESSION, GameSessionEvent.INVITE_ACCEPTED, 0, session_id)
