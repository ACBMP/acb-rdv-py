"""NAT traversal: connection candidates, probe relaying and relay allocation.

Sources:

* acb-rdv (``NATTraversalService``): on ``RequestProbeInitiation`` the server
  tells each online target peer to probe the caller's server-observed URL.
* earlier NAT experiments:

  - ``RequestProbeInitiationExt`` (method 3);
  - probing is made bidirectional: the caller is also told to probe every
    candidate;
  - the candidate list is expanded with every URL the target registered;
  - the server-observed ("reflected") address is added to a host's
    candidates;
  - a blind UDP relay is offered as an extra candidate, including in
    ``RetrieveGameSessionFromContact`` results.

New here:

* targets can be resolved by ``RVCID`` when a URL carries no ``PID`` (the
  capture shows clients doing this);
* every step can be switched off (:class:`NatConfig`);
* every pushed probe is tagged with where its URL came from, and the client's
  answer is logged and counted. That is the data needed to settle which of
  these paths the game actually uses;
* relay URLs carry the ``PID``/``RVCID`` of the peer reached through them
  (see :mod:`quazal.nat.relay`), so the game can match the relayed probe
  replies;
* URLs a player registered at a known "AC Relay" server (a client-side relay
  run by a game patch, ``docs/AC_RELAY_PROTOCOL.md``) are passed through
  unchanged and tagged ``acrelay``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..context import ServerContext, player
from ..ddl import StationURL
from .relay import Allocation, RelayService

if TYPE_CHECKING:
    from ..prudp.transport import Client

log = logging.getLogger("quazal.nat")

NAT_PROTOCOL = 0x03
INITIATE_PROBE = 2


@dataclass
class NatConfig:
    push_to_target: bool = True       # acb-rdv: target probes the caller back
    push_to_caller: bool = True       # caller is told to probe every candidate
    expand_caller: bool = True        # the target also probes the caller's own registered URLs
                                      # (e.g. its AC Relay URL) - needed when only the joiner has a relay
    expand_known: bool = True         # add the target's registered URLs
    reflect_candidates: bool = True   # add the server-observed address to host candidates
    relay: bool = False               # offer a relay candidate (needs a RelayService); off by
                                      # default - AC Relay replaces it (docs/NAT_TRAVERSAL.md)
    relay_in_contacts: bool = True    # relay candidate in RetrieveGameSessionFromContact
    push_response_timeout: float = 5.0
    # AC Relay (client-side relay) servers whose URLs are tagged "acrelay":
    # IPv4 addresses or hostnames, comma-separated (a TOML list works too)
    ac_relay_servers: str | list[str] = ""


def resolve_hosts(spec: str | list[str]) -> frozenset[str]:
    """IPv4 addresses for a comma-separated (or list) host spec; unresolvable names are logged and skipped."""
    names = spec.split(",") if isinstance(spec, str) else list(spec)
    out = set()
    for name in (n.strip() for n in names):
        if not name:
            continue
        try:
            out.add(socket.gethostbyname(name))
        except OSError as exc:
            log.warning("AC Relay server %r does not resolve: %s", name, exc)
    return frozenset(out)


class NatService:
    def __init__(self, ctx: ServerContext, config: NatConfig | None = None, relay: RelayService | None = None):
        self.ctx = ctx
        self.config = config or NatConfig()
        self.relay = relay if self.config.relay else None
        self.stats: Counter[tuple[str, str]] = Counter()
        self.ac_relays = resolve_hosts(self.config.ac_relay_servers)
        if self.ac_relays:
            log.info("AC Relay servers: %s", ", ".join(sorted(self.ac_relays)))

    # --- candidates ---------------------------------------------------------

    @staticmethod
    def self_url(client: "Client") -> StationURL:
        """The client's server-observed URL in acb-rdv's full form (``StationUrl(ClientInfo)``)."""
        return StationURL("prudp", address=client.addr[0], port=client.addr[1], CID=1, PID=client.pid,
                          sid=1, RVCID=client.rvcid, stream=3, type=2)

    def is_ac_relay(self, url: StationURL) -> bool:
        """Whether ``url`` points at a configured AC Relay server (and not at this server's own relay)."""
        if url.address not in self.ac_relays:
            return False
        return self.relay is None or not self.relay.is_allocated(url.address, url.port)

    def origin(self, url: StationURL, default: str) -> str:
        return "acrelay" if self.is_ac_relay(url) else default

    def tagged_candidates(self, host: "Client", viewer: "Client | None") -> list[tuple[StationURL, str]]:
        # registered URLs are passed through as registered, whatever their
        # address: an AC Relay URL names the relay, not the host's own IP
        tagged = [(u, self.origin(u, "known")) for u in player(host).registered_urls]
        if self.config.reflect_candidates and not any(
            u.address == host.addr[0] and u.port == host.addr[1] for u, _ in tagged
        ):
            tagged.append((self.self_url(host), "reflected"))
        if self.relay is not None and viewer is not None:
            allocation = self.relay.get(host.pid, viewer.pid)
            if allocation is not None:
                tagged.append((self.relay_url_to(allocation, host), "relay"))
        return tagged

    def caller_candidates(self, caller: "Client") -> list[tuple[StationURL, str]]:
        """The caller's own URLs (login and session registrations) for the target to probe.

        Rebuilt as plain probe URLs naming the caller, so a reply through any of
        them (an AC Relay URL in particular) matches the caller's RVCID.
        """
        state = player(caller)
        out: list[tuple[StationURL, str]] = []
        seen: set[tuple[str, int]] = set()
        for u in [*state.registered_urls, *state.urls]:
            key = (u.address, u.port)
            if not u.address or not u.port or key in seen or key == (caller.addr[0], caller.addr[1]):
                continue
            seen.add(key)
            # v6: an AC Relay IPv6 URL; address is the virtual IPv4 the
            # client derives from it, useless without the param
            v6 = u.params.get("v6")
            url = StationURL("prudp", address=u.address, port=u.port, PID=caller.pid, RVCID=caller.rvcid, v6=v6)
            out.append((url, "ipv6" if v6 else self.origin(u, "caller-known")))
        return out

    def host_candidates(self, host: "Client", viewer: "Client | None") -> list[StationURL]:
        return [u for u, _ in self.tagged_candidates(host, viewer)]

    def resolve(self, url: StationURL) -> "Client | None":
        if url.pid:
            return self.ctx.online(url.pid)
        if url.rvcid:
            return self.ctx.online_by_rvcid(url.rvcid)
        return None

    @staticmethod
    def relay_url_to(allocation: Allocation, peer: "Client") -> StationURL:
        """The relay URL for reaching ``peer``; it goes to the other player of the pair."""
        return allocation.url_to(peer.pid, peer.rvcid)

    async def relay_allocation(self, a: "Client", b: "Client") -> Allocation | None:
        if self.relay is None:
            return None
        return await self.relay.allocate(a.pid, b.pid)

    async def contact_candidates(self, caller: "Client", host: "Client") -> list[StationURL]:
        """Extra URLs for a RetrieveGameSessionFromContact result (relay injection)."""
        if not self.config.relay_in_contacts:
            return []
        allocation = await self.relay_allocation(caller, host)
        return [self.relay_url_to(allocation, host)] if allocation is not None else []

    # --- probing -------------------------------------------------------------

    def _push_probe(self, target: "Client", url: StationURL, tag: str, about: "Client | None") -> None:
        w = self.ctx.writer().string(str(url))
        future = self.ctx.push(target, NAT_PROTOCOL, INITIATE_PROBE, w.bytes(), expect_response=True)
        self.stats[(tag, "sent")] += 1
        peer = f" (peer {about})" if about is not None else ""
        log.info("InitiateProbe[%s] -> %s: %s%s", tag, target, url, peer)
        if future is not None:
            self.ctx.server.spawn(self._await_probe(future, target, url, tag))

    async def _await_probe(self, future: asyncio.Future, target: "Client", url: StationURL, tag: str) -> None:
        try:
            answer = await asyncio.wait_for(future, self.config.push_response_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.stats[(tag, "no answer")] += 1
            log.info("InitiateProbe[%s] to %s for %s: no answer", tag, target, url)
            return
        outcome = "ok" if answer.success else f"error 0x{answer.error:08X}"
        self.stats[(tag, outcome)] += 1
        log.info("InitiateProbe[%s] to %s for %s: %s", tag, target, url, outcome)

    async def request_probes(self, caller: "Client", urls: list[StationURL], my_url: StationURL | None) -> None:
        cfg = self.config
        tagged: list[tuple[StationURL, str]] = [(u, self.origin(u, "client")) for u in urls]
        targets: list["Client"] = []
        for url in urls:
            target = self.resolve(url)
            if target is not None and target is not caller and target not in targets:
                targets.append(target)
        if not targets:
            log.info("%s asked to probe %d URL(s) of no online peer", caller, len(urls))

        if cfg.expand_known:
            seen = {str(u) for u, _ in tagged}
            for target in targets:
                for url, tag in self.tagged_candidates(target, caller):
                    if str(url) not in seen:
                        seen.add(str(url))
                        tagged.append((url, tag))

        if cfg.push_to_caller:
            for url, tag in tagged:
                self._push_probe(caller, url, tag, None)

        caller_url = my_url or self.self_url(caller)
        pushed_relays = {str(u) for u, tag in tagged if tag == "relay"} if cfg.push_to_caller else set()
        for target in targets:
            allocation = await self.relay_allocation(caller, target)
            if allocation is not None and cfg.push_to_caller:
                to_target = self.relay_url_to(allocation, target)
                if str(to_target) not in pushed_relays:
                    pushed_relays.add(str(to_target))
                    self._push_probe(caller, to_target, "relay", target)
            if cfg.push_to_target:
                self._push_probe(target, caller_url, "caller", caller)
                if allocation is not None:
                    self._push_probe(target, self.relay_url_to(allocation, caller), "relay", caller)
                if cfg.expand_caller:
                    for url, tag in self.caller_candidates(caller):
                        if (url.address, url.port) != (caller_url.address, caller_url.port):
                            self._push_probe(target, url, tag, caller)

    def report(self) -> str:
        lines = ["InitiateProbe pushes by candidate origin and client answer:"]
        for (tag, outcome), n in sorted(self.stats.items()):
            lines.append(f"  {tag:10} {outcome:20} {n}")
        if self.ac_relays:
            lines.append(f"AC Relay servers: {', '.join(sorted(self.ac_relays))}")
            registered = [(c, u) for c in self.ctx.players.values() for u in player(c).registered_urls
                          if self.is_ac_relay(u)]
            lines.append(f"registered AC Relay URLs: {len(registered)}")
            lines.extend(f"  {c}: {u}" for c, u in registered)
        else:
            lines.append("AC Relay servers: none configured")
        if self.relay is None:
            lines.append("server relay: off")
        if self.relay is not None:
            lines.append(f"relay allocations: {len(self.relay.allocations)}")
            lines.extend("  " + a.summary() for a in self.relay.allocations.values())
        return "\n".join(lines)
