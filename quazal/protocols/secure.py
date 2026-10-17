"""Secure connection protocol (0x0B), acb-rdv ``SecureService``.

``RegisterEx`` (4) is the player's registration: the client lists its local
URLs and the server answers with the address it actually sees the client at.
The game adds that reflected URL to the candidates it registers for its game
sessions, so this is effectively the server's STUN service.
``Register`` (1) is the Tracking user's variant.
"""

from __future__ import annotations

import logging

from ..context import ServerContext, player
from ..ddl import AnyDataHolder, StationURL
from ..errors import RESULT_SUCCESS
from ..rmc import Call, Protocol, method

log = logging.getLogger("quazal.secure")

# connection id the original server returned for Register (Tracking user)
TRACKING_CONNECTION_ID = 78


def parse_urls(urls: list[str]) -> list[StationURL]:
    out = []
    for url in urls:
        try:
            out.append(StationURL.parse(url))
        except ValueError:
            log.warning("ignoring malformed station URL %r", url)
    return out


class SecureProtocol(Protocol):
    protocol_id = 0x0B

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "Register")
    async def register(self, call: Call):
        urls = parse_urls(call.args.string_list())
        client = call.client
        state = player(client)
        state.tracking_urls = [u.canonical() for u in urls]
        if state.tracking_urls:
            url = state.tracking_urls[0].with_params(sid=14, type=3).canonical()
        else:
            url = StationURL("prudp", address=client.addr[0], port=client.addr[1], sid=14, type=3)
        return self.ctx.writer().u32(RESULT_SUCCESS).u32(TRACKING_CONNECTION_ID).string(str(url))

    @method(4, "RegisterEx")
    async def register_ex(self, call: Call):
        urls = parse_urls(call.args.string_list())
        AnyDataHolder.read(call.args)  # login custom data again; not needed here
        client = call.client
        state = player(client)
        state.urls.extend(u.canonical() for u in urls)
        public = StationURL("prudps", address=client.addr[0], port=client.addr[1], sid=15, type=3)
        state.urls.append(public)
        log.info("%s registered %d local URL(s); public %s", client, len(urls), public)
        return self.ctx.writer().u32(RESULT_SUCCESS).u32(client.rvcid).string(str(public))
