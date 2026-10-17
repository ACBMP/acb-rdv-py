"""News shown in the game's UbiNews panel.

The game asks UbiNews.GetChannel for its channel, then News.GetNewsHeaders
for that channel and News.GetNewsMessages for the bodies (see
``protocols/ubi.py``). The items come from a TOML file or from AN-API's
``/news/`` posts (the launcher news).
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time
import tomllib
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("acb.news")

ANAPI_TTL = 60.0


@dataclass
class NewsItem:
    id: int
    subject: str
    body: str
    published: dt.datetime
    sender: str = ""
    link: str = ""


class NewsSource(Protocol):
    async def items(self) -> list[NewsItem]:
        """Newest first."""
        ...


class NoNews:
    async def items(self) -> list[NewsItem]:
        return []


class FileNews:
    """``[[news]]`` tables in a TOML file, reloaded when the file changes.

    ``subject`` and ``body`` are required; ``id``, ``date`` (TOML date or
    datetime), ``sender`` and ``link`` are optional. The file order is the
    display order, so put the newest first.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._items: list[NewsItem] = []

    async def items(self) -> list[NewsItem]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            log.error("news file %s unreadable: %s", self.path, exc)
            return self._items
        if mtime != self._mtime:
            try:
                self._items = self._load()
                log.info("loaded %d news item(s) from %s", len(self._items), self.path)
            except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
                log.error("news file %s invalid, keeping the previous items: %s", self.path, exc)
            self._mtime = mtime
        return self._items

    def _load(self) -> list[NewsItem]:
        with open(self.path, "rb") as f:
            entries = tomllib.load(f).get("news", [])
        mtime = dt.datetime.fromtimestamp(self.path.stat().st_mtime, dt.timezone.utc)
        items = []
        for i, e in enumerate(entries):
            date = e.get("date", mtime)
            if isinstance(date, dt.date) and not isinstance(date, dt.datetime):
                date = dt.datetime(date.year, date.month, date.day)
            items.append(NewsItem(id=int(e.get("id", i + 1)), subject=str(e["subject"]), body=str(e["body"]),
                                  published=date, sender=str(e.get("sender", "")), link=str(e.get("link", ""))))
        return items


class AnApiNews:
    """AN-API's public ``GET /news/`` (the launcher news), HTML flattened to text."""

    def __init__(self, base_url: str, *, sender: str = "", timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self.sender = sender
        self._expires = 0.0
        self._items: list[NewsItem] = []

    async def items(self) -> list[NewsItem]:
        if time.monotonic() < self._expires:
            return self._items
        try:
            resp = await self.http.get("/news/")
            resp.raise_for_status()
            self._items = [self._item(p) for p in resp.json()]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.error("AN-API news unavailable, serving %d cached item(s): %s", len(self._items), exc)
        self._expires = time.monotonic() + ANAPI_TTL
        return self._items

    def _item(self, post: dict) -> NewsItem:
        oid = str(post["_id"])
        # an ObjectId starts with its creation time; the client keys bodies by a positive int
        published = dt.datetime.fromtimestamp(int(oid[:8], 16), dt.timezone.utc)
        return NewsItem(id=zlib.crc32(oid.encode()) & 0x7FFFFFFF, subject=str(post.get("title", "")),
                        body=html_to_text(str(post.get("content", ""))), published=published, sender=self.sender)

    async def close(self) -> None:
        await self.http.aclose()


class _TextExtractor(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.BREAKS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(content: str) -> str:
    if "<" not in content:
        return html.unescape(content).strip()
    parser = _TextExtractor()
    parser.feed(content)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def make_source(setting: str, anapi_url: str, sender: str = "") -> NewsSource:
    """``""`` serves no news, ``"anapi"`` AN-API's posts, anything else is a TOML file path."""
    if not setting:
        return NoNews()
    if setting == "anapi":
        return AnApiNews(anapi_url, sender=sender)
    return FileNews(setting)
