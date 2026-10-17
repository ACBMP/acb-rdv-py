"""UDP relay for peers that cannot hole-punch each other.

Each allocation binds one port from a fixed pool and blindly forwards
datagrams between the first two distinct endpoints that talk to it. No PRUDP
parsing is needed - both peers only need a route to a stable public address.
Allocations are keyed by the unordered pid pair, so both directions share one,
and are reaped after a period without traffic.

The URL that advertises an allocation is *per recipient*: the one given to
player A names peer B (``PID`` and ``RVCID`` of B) and vice versa, see
:meth:`Allocation.url_to`. The game matches a probe reply to its probe entry by
IP string + ``RVCID`` (``Quazal::URLProbeList::FindProbe``); a reply that
arrives through the relay comes from the relay's address and carries the
other peer's ``RVCID``, so a relay URL without that ``RVCID`` never gets a ping
and the game's lowest-ping URL choice can never pick it.

The counters on :class:`Allocation` exist for the NAT verification phase: they
show whether a relay candidate was ever used, and by whom.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from ..ddl import StationURL

log = logging.getLogger("quazal.relay")

Address = tuple[str, int]


@dataclass
class Allocation:
    pair: tuple[int, int]
    address: str
    port: int
    transport: asyncio.DatagramTransport | None = None
    peer_a: Address | None = None
    peer_b: Address | None = None
    created: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    packets: dict[Address, int] = field(default_factory=dict)
    bytes: dict[Address, int] = field(default_factory=dict)
    forwarded: int = 0
    dropped: int = 0

    def url_to(self, pid: int, rvcid: int) -> StationURL:
        """The relay URL for reaching peer ``pid``, given to the *other* player of the pair.

        It carries the peer's ``PID`` and ``RVCID`` (needed for the game's probe
        matching) and otherwise mirrors the form of the peer's own
        server-observed URL (``NatService.self_url``, acb-rdv's
        ``StationUrl(ClientInfo)``): ``CID=1;sid=1;stream=3;type=2``.
        """
        if pid not in self.pair:
            raise ValueError(f"pid {pid} is not part of relay pair {self.pair}")
        return StationURL("prudp", address=self.address, port=self.port, CID=1, PID=pid, sid=1,
                          RVCID=rvcid, stream=3, type=2)

    def summary(self) -> str:
        peers = ", ".join(f"{a[0]}:{a[1]}={self.packets.get(a, 0)}p/{self.bytes.get(a, 0)}B"
                          for a in (self.peer_a, self.peer_b) if a)
        return (f"relay port {self.port} pids {self.pair}: forwarded {self.forwarded}, dropped {self.dropped}"
                f"{'; ' + peers if peers else '; never used'}")


class _RelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, allocation: Allocation):
        self.allocation = allocation

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.allocation.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        a = self.allocation
        src: Address = (addr[0], addr[1])
        a.last_activity = time.monotonic()
        a.packets[src] = a.packets.get(src, 0) + 1
        a.bytes[src] = a.bytes.get(src, 0) + len(data)
        if a.peer_a is None:
            a.peer_a = src
            log.info("relay %d (pids %s): first peer %s:%d", a.port, a.pair, *src)
        elif src != a.peer_a and a.peer_b is None:
            a.peer_b = src
            log.info("relay %d (pids %s): second peer %s:%d - forwarding", a.port, a.pair, *src)
        if src == a.peer_a:
            target = a.peer_b
        elif src == a.peer_b:
            target = a.peer_a
        else:
            target = None
        if target is None or a.transport is None:
            a.dropped += 1
            return
        a.transport.sendto(data, target)
        a.forwarded += 1

    def error_received(self, exc: Exception) -> None:
        log.debug("relay %d socket error: %s", self.allocation.port, exc)


class RelayService:
    def __init__(self, public_address: str, port_start: int = 40000, port_end: int = 40199, *,
                 bind_host: str = "0.0.0.0", idle_timeout: float = 60.0, sweep_interval: float = 10.0):
        self.public_address = public_address
        self.bind_host = bind_host
        self.idle_timeout = idle_timeout
        self.sweep_interval = sweep_interval
        self.free_ports: deque[int] = deque(range(port_start, port_end + 1))
        self.allocations: dict[tuple[int, int], Allocation] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None

    @staticmethod
    def pair(pid_a: int, pid_b: int) -> tuple[int, int]:
        return (pid_a, pid_b) if pid_a < pid_b else (pid_b, pid_a)

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())

    def get(self, pid_a: int, pid_b: int) -> Allocation | None:
        return self.allocations.get(self.pair(pid_a, pid_b))

    async def allocate(self, pid_a: int, pid_b: int) -> Allocation | None:
        """Return the allocation for this pid pair, binding a new port if needed."""
        key = self.pair(pid_a, pid_b)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self.allocations.get(key)
            if existing is not None:
                return existing
            loop = asyncio.get_running_loop()
            for _ in range(len(self.free_ports)):
                port = self.free_ports.popleft()
                allocation = Allocation(key, self.public_address, port)
                try:
                    await loop.create_datagram_endpoint(lambda a=allocation: _RelayProtocol(a),
                                                        local_addr=(self.bind_host, port))
                except OSError as exc:
                    log.warning("relay port %d unavailable: %s", port, exc)
                    self.free_ports.append(port)
                    continue
                self.allocations[key] = allocation
                log.info("allocated relay port %d for pids %s", port, key)
                return allocation
            log.warning("relay port pool exhausted; no relay for pids %s", key)
            return None

    def is_allocated(self, address: str, port: int) -> bool:
        """Whether ``address:port`` is one of this relay's live allocations."""
        return address == self.public_address and any(a.port == port for a in self.allocations.values())

    def release(self, key: tuple[int, int], reason: str) -> None:
        allocation = self.allocations.pop(key, None)
        self._locks.pop(key, None)
        if allocation is None:
            return
        if allocation.transport is not None:
            allocation.transport.close()
        self.free_ports.append(allocation.port)
        log.info("released %s (%s)", allocation.summary(), reason)

    def release_pid(self, pid: int) -> None:
        for key in [k for k in self.allocations if pid in k]:
            self.release(key, f"pid {pid} left")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval)
            now = time.monotonic()
            for key, allocation in list(self.allocations.items()):
                if now - allocation.last_activity >= self.idle_timeout:
                    self.release(key, "idle")

    def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for key in list(self.allocations):
            self.release(key, "shutdown")
