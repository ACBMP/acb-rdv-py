"""NAT traversal protocol (0x03).

* 1 ``RequestProbeInitiation(list<StationURL> targets)``
* 2 ``InitiateProbe(StationURL)`` - server to client only
* 3 ``RequestProbeInitiationExt(list<StationURL> targets, StationURL myUrl)``

The work is done by :class:`~quazal.nat.service.NatService`.
"""

from __future__ import annotations

import logging

from ..context import ServerContext
from ..ddl import StationURL
from ..nat.service import NatService
from ..rmc import Call, Protocol, method

log = logging.getLogger("quazal.nat")


def _parse(urls: list[str]) -> list[StationURL]:
    out = []
    for raw in urls:
        try:
            out.append(StationURL.parse(raw))
        except ValueError:
            log.warning("ignoring malformed probe URL %r", raw)
    return out


class NatTraversalProtocol(Protocol):
    protocol_id = 0x03

    def __init__(self, ctx: ServerContext, nat: NatService):
        self.ctx = ctx
        self.nat = nat

    @method(1, "RequestProbeInitiation")
    async def request_probe_initiation(self, call: Call):
        urls = _parse(call.args.string_list())
        log.info("%s RequestProbeInitiation %s", call.client, ", ".join(map(str, urls)))
        await self.nat.request_probes(call.client, urls, None)
        return None

    @method(3, "RequestProbeInitiationExt")
    async def request_probe_initiation_ext(self, call: Call):
        urls = _parse(call.args.string_list())
        my_url = _parse([call.args.string()])
        log.info("%s RequestProbeInitiationExt %s (self %s)", call.client, ", ".join(map(str, urls)),
                 my_url[0] if my_url else "?")
        await self.nat.request_probes(call.client, urls, my_url[0] if my_url else None)
        return None
