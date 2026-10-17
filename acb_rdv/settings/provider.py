"""Where the game settings (CXB) served by PersistentStore come from.

* :class:`CxbFileProvider` serves a prebuilt ``.cxb`` file (what acb-rdv did).
* :class:`XmlSettingsProvider` keeps the settings as a directory of XML files
  and encodes them to CXB just in time. The first request after a change
  rebuilds the file, and the result is cached until the XMLs change again. If
  an edit breaks the build (malformed XML, encoder error), the last good CXB
  keeps being served and the error is reported on ``/settings``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .encoders import CxbEncoder, CxbError, XmlFile, find_encoder

log = logging.getLogger("acb.settings")


@dataclass(frozen=True)
class GameSettings:
    data: bytes
    source: str
    built_at: float
    files: tuple[str, ...] = ()

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)


class SettingsError(Exception):
    pass


class SettingsProvider(Protocol):
    async def current(self) -> GameSettings:
        """The CXB to serve now; raises :class:`SettingsError` if none can be produced."""
        ...

    def describe(self) -> str: ...


@dataclass
class _State:
    settings: GameSettings | None = None
    fingerprint: str = ""
    last_error: str = ""
    last_check: float = 0.0
    builds: int = 0


class CxbFileProvider:
    """Serves a ``.cxb`` file, re-read when it changes on disk."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._state = _State()

    async def current(self) -> GameSettings:
        try:
            stat = self.path.stat()
            fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
            if fingerprint != self._state.fingerprint:
                data = self.path.read_bytes()
                self._state.settings = GameSettings(data, str(self.path), time.time(), (self.path.name,))
                self._state.fingerprint = fingerprint
                self._state.last_error = ""
        except OSError as exc:
            self._state.last_error = str(exc)
            if self._state.settings is None:
                raise SettingsError(f"cannot read game settings {self.path}: {exc}") from exc
            log.error("cannot re-read %s, serving the previous copy: %s", self.path, exc)
        return self._state.settings  # type: ignore[return-value]

    def describe(self) -> str:
        return _describe(f"CXB file {self.path}", self._state)


class XmlSettingsProvider:
    """Encodes a directory of XML files to CXB on demand."""

    def __init__(self, directory: str | Path, encoder: CxbEncoder, *, check_interval: float = 2.0):
        self.directory = Path(directory)
        self.encoder = encoder
        self.check_interval = check_interval
        self._state = _State()
        self._lock = asyncio.Lock()

    def _scan(self) -> list[Path]:
        paths = [p for p in self.directory.iterdir() if p.is_file() and p.suffix.lower() == ".xml"]
        # CXBTool orders documents by base name (std::map, byte order)
        return sorted(paths, key=lambda p: p.stem.encode("utf-8"))

    def _fingerprint(self, paths: list[Path]) -> str:
        h = hashlib.sha1()
        for p in paths:
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        return h.hexdigest()

    def _load(self, paths: list[Path]) -> list[XmlFile]:
        files = []
        problems = []
        for p in paths:
            data = p.read_bytes()
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                problems.append(f"{p.name}: {exc}")
            files.append((p.name, data))
        if problems:
            raise SettingsError("malformed XML - " + "; ".join(problems))
        if not files:
            raise SettingsError(f"no .xml files in {self.directory}")
        return files

    async def current(self) -> GameSettings:
        state = self._state
        now = time.monotonic()
        if state.settings is not None and now - state.last_check < self.check_interval:
            return state.settings
        async with self._lock:
            if state.settings is not None and time.monotonic() - state.last_check < self.check_interval:
                return state.settings
            try:
                paths = await asyncio.to_thread(self._scan)
                fingerprint = await asyncio.to_thread(self._fingerprint, paths)
                state.last_check = time.monotonic()
                if fingerprint != state.fingerprint or state.settings is None:
                    await self._build(paths, fingerprint)
            except (OSError, SettingsError, CxbError) as exc:
                state.last_check = time.monotonic()
                state.last_error = str(exc)
                if state.settings is None:
                    raise SettingsError(f"cannot build game settings from {self.directory}: {exc}") from exc
                log.error("game settings rebuild failed, serving the last good build: %s", exc)
        return state.settings  # type: ignore[return-value]

    async def _build(self, paths: list[Path], fingerprint: str) -> None:
        state = self._state
        files = await asyncio.to_thread(self._load, paths)
        started = time.monotonic()
        data = await asyncio.to_thread(self.encoder.encode, files)
        state.settings = GameSettings(data, str(self.directory), time.time(), tuple(n for n, _ in files))
        state.fingerprint = fingerprint
        state.last_error = ""
        state.builds += 1
        log.info("encoded %d XML files from %s to CXB (%d bytes, sha1 %s) with %s in %.0f ms",
                 len(files), self.directory, len(data), state.settings.sha1[:12], self.encoder.name,
                 (time.monotonic() - started) * 1000)

    def describe(self) -> str:
        head = f"XML directory {self.directory}, encoder: {self.encoder.name}, builds: {self._state.builds}"
        return _describe(head, self._state)


def _describe(head: str, state: _State) -> str:
    lines = [head]
    s = state.settings
    if s is not None:
        lines += [f"size: {s.size} bytes", f"sha1: {s.sha1}",
                  f"built: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.built_at))}",
                  f"files ({len(s.files)}): {', '.join(s.files)}"]
    else:
        lines.append("not built yet")
    lines.append(f"last error: {state.last_error or 'none'}")
    return "\n".join(lines)


def make_provider(path: str | Path, library: str = "") -> SettingsProvider:
    """A directory means XML sources (encoded just in time); a file is a ready CXB."""
    path = Path(path)
    if path.is_dir():
        return XmlSettingsProvider(path, find_encoder(library))
    return CxbFileProvider(path)


__all__ = ["GameSettings", "SettingsError", "SettingsProvider", "CxbFileProvider", "XmlSettingsProvider",
           "make_provider"]
