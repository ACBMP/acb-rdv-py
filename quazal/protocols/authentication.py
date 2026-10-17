"""Authentication protocol (0x0A), acb-rdv ``AuthenticationService``.

* ``Login`` (1) is only used by the "Tracking" telemetry user and is answered
  with a ticket dumped from the original service.
* ``LoginEx`` (2) carries ``UbiAuthenticationLoginCustomData`` with the
  player's name and password; the backend decides whether they are valid.
* ``RequestTicket`` (3) issues the ticket for the secure server.
"""

from __future__ import annotations

import logging

from ..context import TRACKING_USER, ServerContext, player
from ..ddl import AnyDataHolder, Reader
from ..errors import RESULT_SUCCESS, QError
from ..kerberos import build_ticket
from ..rmc import Call, Protocol, RMCError, method
from ..storage import AuthFailure, BackendUnavailable

log = logging.getLogger("quazal.auth")

# Tracking user tickets dumped from the original traffic (acb-rdv)
TRACKING_LOGIN_TICKET = bytes.fromhex(
    "B6E1A2EDD87ECB5FEA36FF9775B3D8CBB857D015A7A1AB03573FC169D956B87341CEE9B26003D6F06C5BEE67FA6F7E5B"
    "EFB7BB6269AF697AD869589A604BA4A74F41BF80C7141A5F350FFD96"
)
TRACKING_REQUEST_TICKET = bytes.fromhex(
    "D922673D4AA9B2727A1A8539DF82624CBB57D015A7A1AB03EC222FBDC000CD75C611F3695680F9457179CD88A3131591"
    "6B30CAA278C279EBB292A643953E34186AB4D25EA0612AFEFBA3E494"
)
# the Login response advertises server id 2 (acb-rdv RVConnectionData default)
TRACKING_SERVER_ID = 2

LOGIN_CUSTOM_DATA = "UbiAuthenticationLoginCustomData"

_AUTH_ERRORS = {
    "unknown_user": QError.RendezVous_InvalidUsername,
    "bad_password": QError.RendezVous_InvalidPassword,
    "revoked": QError.RendezVous_AccountDisabled,
}


def secure_url(address: str, port: int, server_pid: int) -> str:
    return f"prudps:/address={address};port={port};CID=1;PID={server_pid};sid=1;stream=3;type=2"


def read_login_custom_data(call: Call) -> tuple[str, str, str, str]:
    """``AnyDataHolder`` with ``UbiAuthenticationLoginCustomData``: username, online key, password."""
    holder = AnyDataHolder.read(call.args)
    if holder.name != LOGIN_CUSTOM_DATA:
        raise RMCError(QError.Core_InvalidArgument, f"unsupported login data class {holder.name!r}")
    r = Reader(holder.payload, encoding=call.args.encoding)
    return holder.name, r.string(), r.string(), r.string()


class AuthenticationProtocol(Protocol):
    protocol_id = 0x0A

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    def _connection_data(self, w, url: str) -> None:
        # RVConnectionData: regular url, special protocol list (u8 items), special url
        w.string(url).u32(0).string("")

    @method(1, "Login", requires_login=False)
    async def login(self, call: Call):
        username = call.args.string()
        if username != TRACKING_USER.name:
            log.warning("%s: Login for %r - only the Tracking user logs in this way", call.client, username)
            raise RMCError(QError.RendezVous_InvalidUsername)
        call.client.tracking_user = TRACKING_USER
        w = self.ctx.writer()
        w.u32(RESULT_SUCCESS).u32(TRACKING_USER.pid).buffer(TRACKING_LOGIN_TICKET)
        self._connection_data(w, secure_url(self.ctx.public_address, self.ctx.secure_port, TRACKING_SERVER_ID))
        w.string("")  # server build name
        return w

    @method(2, "LoginEx", requires_login=False)
    async def login_ex(self, call: Call):
        call.args.string()  # user name as typed; the custom data carries the real credentials
        _, username, _online_key, password = read_login_custom_data(call)
        try:
            user = await self.ctx.storage.authenticate(username, password)
        except AuthFailure as fail:
            log.info("%s: LoginEx for %r rejected (%s)", call.client, username, fail.reason)
            raise RMCError(_AUTH_ERRORS.get(fail.reason, QError.UbiAuthentication_Unknown)) from None
        except BackendUnavailable as exc:
            log.error("LoginEx for %r: backend unavailable: %s", username, exc)
            raise RMCError(QError.RendezVous_ConnectionFailure) from None

        client = call.client
        stale = self.ctx.logged_in(client, user)
        if stale is not None:
            log.info("%s logged in again from %s; dropping the old connection", user.name, client.addr)
            call.after(lambda: self.ctx.server.remove_client(stale, "replaced by a new login"))
        state = player(client)
        state.urls.clear()
        state.registered_urls.clear()
        for hook in self.ctx.login_hooks:
            await hook(client)
        log.info("%s logged in", client)

        ticket = build_ticket(user.pid, client.server_pid, client.session_key,
                              password=self.ctx.server.profile.kerberos_password)
        w = self.ctx.writer()
        w.u32(RESULT_SUCCESS).u32(user.pid).buffer(ticket)
        self._connection_data(w, secure_url(self.ctx.public_address, client.server_port, client.server_pid))
        w.string("")  # return message
        return w

    @method(3, "RequestTicket", requires_login=False)
    async def request_ticket(self, call: Call):
        source_pid = call.args.u32()
        call.args.u32()  # target pid (the secure server)
        client = call.client
        if source_pid == TRACKING_USER.pid:
            ticket = TRACKING_REQUEST_TICKET
        elif client.user is not None and client.pid == source_pid:
            ticket = build_ticket(source_pid, client.server_pid, client.session_key,
                                  password=self.ctx.server.profile.kerberos_password)
        else:
            raise RMCError(QError.RendezVous_NotAuthenticated)
        return self.ctx.writer().u32(RESULT_SUCCESS).buffer(ticket)
