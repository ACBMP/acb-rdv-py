"""Status API (from acb-rdv's ``additional-fixes`` branch) and the AN-API webhook.

* ``GET /``, ``/sessions``, ``/clients`` - plain text, as in acb-rdv (host URLs
  are left out);
* ``GET /nat`` - NAT traversal counters (see ``NatService.report``);
* ``GET /settings`` - where the game settings come from, their size and hash,
  and the last build error;
* ``POST /internal/notify`` - AN-API tells the server about changes made
  outside the game (a friend request sent from the website, for example) so
  online players get the in-game notification. Requires the shared key in
  ``X-Internal-Key``.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING

from quazal.context import player
from quazal.protocols import notifications as notif
from quazal.protocols.messaging import CHECK_MESSAGES_BODY, NOTIFICATION_SUBJECT, TextMessage

from quazal.httpserver import Request, Response, json_response, text

if TYPE_CHECKING:
    from ..server import AcbServer

log = logging.getLogger("acb.status")

MESSAGE_DELIVERY_PROTOCOL = 0x1B


class StatusAPI:
    def __init__(self, server: "AcbServer", internal_key: str = ""):
        self.server = server
        self.internal_key = internal_key

    async def handle(self, request: Request) -> Response:
        path = request.path.lower()
        if request.method == "POST" and path == "/internal/notify":
            return await self.notify(request)
        if request.method != "GET":
            return Response(405)
        if path == "/":
            return text("Status: OK")
        if path == "/sessions":
            return text(self.sessions_text())
        if path == "/clients":
            return text(self.clients_text())
        if path == "/settings":
            return text(self.server.settings.describe())
        if path == "/nat":
            nat = self.server.nat
            return text(nat.report() if nat else "NAT traversal disabled")
        return Response(404)

    def sessions_text(self) -> str:
        sessions = list(self.server.sessions.sessions.values())
        lines = [f"Total Sessions: {len(sessions)}", ""]
        for s in sessions:
            lines += [
                f"[ID: {s.id}]",
                f"[Type: {s.key.type_id}]",
                f"[Host: {s.host_pid}]",
                f"[Public PIDs: ({', '.join(map(str, s.public_pids))})]",
                f"[Private PIDs: ({', '.join(map(str, s.private_pids))})]",
                "[Attributes]",
            ]
            lines += [f"    [{p.id}: {p.value}]" for p in s.attributes]
            lines.append("")
        if not sessions:
            lines.append("No active sessions.")
        return "\n".join(lines) + "\n"

    def clients_text(self) -> str:
        clients = list(self.server.ctx.players.values())
        lines = [f"Total Clients: {len(clients)}", ""]
        if not clients:
            lines.append("No clients online.")
        for c in clients:
            state = player(c)
            lines += [
                f"[PID: {c.pid}]",
                f"[Name: {c.name}]",
                f"[Locale: {state.locale or 'N/A'}]",
                f"[In Game: {state.in_game_session}]",
                f"[Session ID: {state.game_session_id}]",
                "",
            ]
        return "\n".join(lines) + "\n"

    async def notify(self, request: Request) -> Response:
        if not self.internal_key or not hmac.compare_digest(request.headers.get("x-internal-key", ""),
                                                            self.internal_key):
            return Response(401)
        try:
            event = json.loads(request.body or b"{}")
            kind = event["type"]
            to_pid = int(event["to_pid"])
        except (ValueError, KeyError, TypeError):
            return json_response('{"error":"expected {type, to_pid, ...}"}', 400)
        ctx = self.server.ctx
        target = ctx.online(to_pid)
        if target is None:
            return json_response('{"delivered":false}')
        from_pid = int(event.get("from_pid", 0))
        from_name = str(event.get("from_name", ""))
        if kind == "friend_request":
            notif.friend_invite_received(ctx, target, from_pid, from_name)
        elif kind == "friend_accepted":
            # as when accepting in game: the requester learns about it, and both
            # online players see each other as online friends
            notif.friend_invite_accepted(ctx, target, from_pid, from_name)
            accepter = ctx.online(from_pid)
            if accepter is not None:
                notif.friend_status_changed(ctx, target, from_pid, from_name, True)
                notif.friend_status_changed(ctx, accepter, target.pid, target.name, True)
        elif kind == "friend_removed":
            notif.friend_removed(ctx, target, from_pid, from_name)
        elif kind == "message":
            # the same "Notification" text message players send each other
            msg = TextMessage(recipient_id=to_pid, sender_id=from_pid, sender_name=from_name,
                              subject=NOTIFICATION_SUBJECT, body=CHECK_MESSAGES_BODY)
            w = ctx.writer()
            msg.holder(ctx.encoding).write(w)
            ctx.push(target, MESSAGE_DELIVERY_PROTOCOL, 1, w.bytes())
        else:
            return json_response('{"error":"unknown event type"}', 400)
        log.info("delivered %s notification to %s", kind, target)
        return json_response('{"delivered":true}')
