"""Storage backed by AN-API (``/rdv`` blueprint), which owns the MongoDB data.

The RDV server holds no database credentials: accounts, relationships,
messages, offline invites, privileges, rewards and telemetry all go through
AN-API over loopback, authenticated with a shared service key. Users and the
static tables are cached briefly; telemetry is queued and sent in batches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from quazal.storage import (
    AuthFailure,
    BackendUnavailable,
    GameInvite,
    LadderEntry,
    ModeStats,
    PlayerRecord,
    Privilege,
    Relationship,
    RelationshipType,
    RelResult,
    Reward,
    StoredMessage,
    User,
)

log = logging.getLogger("acb.anapi")

USER_TTL = 300.0
STATIC_TTL = 600.0
RANKINGS_TTL = 60.0
TELEMETRY_BATCH = 100
TELEMETRY_INTERVAL = 5.0
TELEMETRY_QUEUE_LIMIT = 10_000


def _user(doc: dict) -> User:
    return User(
        pid=int(doc["pid"]),
        name=doc["name"],
        ubi_id=doc.get("ubi_id") or "",
        email=doc.get("email") or "",
        country_code=doc.get("country_code") or "US",
        pref_lang=doc.get("pref_lang") or "en",
    )


def _message(doc: dict) -> StoredMessage:
    return StoredMessage(
        id=int(doc["id"]), recipient_pid=int(doc["recipient_pid"]), recipient_type=int(doc.get("recipient_type", 0)),
        parent_id=int(doc.get("parent_id", 0)), sender_pid=int(doc.get("sender_pid", 0)),
        reception_time=int(doc.get("reception_time", 0)), lifetime=int(doc.get("lifetime", 0)),
        flags=int(doc.get("flags", 0)), subject=doc.get("subject", ""), sender_name=doc.get("sender_name", ""),
        body=doc.get("body", ""), delivered=bool(doc.get("delivered", False)),
    )


def _game_name(name: str) -> str:
    return str(name).replace(" ", "_")  # the game shows spaces as underscores


def _mode_stats(doc: dict, mode: str) -> ModeStats | None:
    games = doc.get(f"{mode}games") or {}
    if not games.get("total"):
        return None
    stats = doc.get(f"{mode}stats") or {}
    return ModeStats(float(doc.get(f"{mode}mmr") or 0), int(games.get("total", 0)), int(games.get("won", 0)),
                     int(games.get("lost", 0)), int(stats.get("totalscore", 0)), int(stats.get("highscore", 0)),
                     int(stats.get("kills", 0)), int(stats.get("deaths", 0)))


class AnApiStorage:
    def __init__(self, base_url: str, service_key: str, *, timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.root = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.root + "/rdv", timeout=timeout,
                                      headers={"X-Service-Key": service_key}, transport=transport)
        self._users: dict[Any, tuple[float, User | None]] = {}
        self._static: dict[str, tuple[float, Any]] = {}
        self._telemetry: list[dict] = []
        self._flusher: asyncio.Task | None = None

    async def close(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
        await self.flush_telemetry()
        await self.http.aclose()

    # --- plumbing -------------------------------------------------------------

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = await self.http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{method} {url}: {exc}") from exc
        if resp.status_code >= 500:
            raise BackendUnavailable(f"{method} {url}: HTTP {resp.status_code}")
        return resp

    async def _json(self, method: str, url: str, **kw) -> Any:
        resp = await self._request(method, url, **kw)
        if resp.status_code >= 400:
            raise BackendUnavailable(f"{method} {url}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _cache_user(self, user: User | None, *keys) -> User | None:
        entry = (time.monotonic() + USER_TTL, user)
        for k in keys:
            self._users[k] = entry
        return user

    def _cached(self, key) -> tuple[bool, User | None]:
        entry = self._users.get(key)
        if entry is None or entry[0] < time.monotonic():
            return False, None
        return True, entry[1]

    async def _static_list(self, key: str, url: str, field: str, **kw) -> list:
        entry = self._static.get(key)
        if entry is not None and entry[0] > time.monotonic():
            return entry[1]
        data = (await self._json("GET", url, **kw))[field]
        self._static[key] = (time.monotonic() + STATIC_TTL, data)
        return data

    # --- accounts --------------------------------------------------------------

    async def authenticate(self, name: str, password: str) -> User:
        resp = await self._request("POST", "/login", json={"name": name, "password": password})
        if resp.status_code == 403:
            raise AuthFailure("revoked")
        if resp.status_code in (401, 404):
            reason = "unknown_user"
            try:
                reason = resp.json().get("error", reason)
            except ValueError:
                pass
            raise AuthFailure(reason)
        if resp.status_code >= 400:
            raise BackendUnavailable(f"login: HTTP {resp.status_code}")
        user = _user(resp.json())
        return self._cache_user(user, ("pid", user.pid), ("name", user.name))  # type: ignore[return-value]

    async def get_user_by_pid(self, pid: int) -> User | None:
        hit, user = self._cached(("pid", pid))
        if hit:
            return user
        resp = await self._request("GET", f"/users/{pid}")
        user = _user(resp.json()) if resp.status_code == 200 else None
        return self._cache_user(user, ("pid", pid), *([("name", user.name)] if user else []))

    async def get_user_by_name(self, name: str) -> User | None:
        hit, user = self._cached(("name", name))
        if hit:
            return user
        resp = await self._request("GET", "/users/by-name", params={"name": name})
        user = _user(resp.json()) if resp.status_code == 200 else None
        return self._cache_user(user, ("name", name), *([("pid", user.pid)] if user else []))

    # --- relationships ------------------------------------------------------------

    async def _rel(self, action: str, requester: int, requestee: int, details: int) -> RelResult:
        data = await self._json("POST", f"/relationships/{action}",
                                json={"requester": requester, "requestee": requestee, "details": details})
        return RelResult(data["result"])

    async def add_friend_request(self, requester: int, requestee: int, details: int) -> RelResult:
        return await self._rel("request", requester, requestee, details)

    async def accept_friend(self, requester: int, requestee: int, details: int) -> RelResult:
        return await self._rel("accept", requester, requestee, details)

    async def add_blacklist(self, requester: int, requestee: int, details: int) -> RelResult:
        return await self._rel("block", requester, requestee, details)

    async def remove_relationship(self, a: int, b: int) -> bool:
        return bool((await self._json("POST", "/relationships/remove", json={"a": a, "b": b}))["removed"])

    async def get_relationships(self, pid: int, type: RelationshipType) -> list[Relationship]:
        data = await self._json("GET", f"/relationships/{pid}", params={"type": int(type)})
        return [Relationship(int(r["requester"]), int(r["requestee"]), RelationshipType(int(r["type"])),
                             int(r.get("details", 0))) for r in data["relationships"]]

    # --- messages -------------------------------------------------------------------

    async def add_message(self, msg: StoredMessage) -> bool:
        body = {k: v for k, v in msg.__dict__.items() if k not in ("id", "delivered")}
        await self._json("POST", "/messages", json=body)
        return True

    async def pending_messages(self, recipient_pid: int) -> list[StoredMessage]:
        data = await self._json("GET", f"/messages/pending/{recipient_pid}")
        return [_message(m) for m in data["messages"]]

    async def messages_by_ids(self, ids: list[int]) -> list[StoredMessage]:
        if not ids:
            return []
        data = await self._json("POST", "/messages/fetch", json={"ids": ids})
        return [_message(m) for m in data["messages"]]

    async def mark_delivered(self, ids: list[int]) -> None:
        if ids:
            await self._json("POST", "/messages/delivered", json={"ids": ids})

    # --- invites --------------------------------------------------------------------

    async def add_game_invite(self, invite: GameInvite) -> bool:
        await self._json("POST", "/invites", json={
            "inviter": invite.inviter, "invitee": invite.invitee, "session_type": invite.session_type,
            "session_id": invite.session_id, "message": invite.message,
        })
        return True

    async def take_game_invites(self, invitee: int) -> list[GameInvite]:
        data = await self._json("POST", f"/invites/take/{invitee}")
        return [GameInvite(int(i.get("id", 0)), int(i["inviter"]), int(i["invitee"]), int(i["session_type"]),
                           int(i["session_id"]), i.get("message", "")) for i in data["invites"]]

    # --- Ubisoft data --------------------------------------------------------------

    async def privileges(self, locale: str) -> list[Privilege]:
        rows = await self._static_list(f"privileges:{locale}", "/privileges", "privileges", params={"locale": locale})
        return [Privilege(int(p["id"]), p["description"]) for p in rows]

    async def rewards(self) -> list[Reward]:
        rows = await self._static_list("rewards", "/rewards", "rewards")
        return [Reward(r["code"], r["name"], r["description"], int(r["value"])) for r in rows]

    async def telemetry_tags(self) -> list[str]:
        return list(await self._static_list("tags", "/telemetry/tags", "tags"))

    async def save_telemetry(self, tracking_id: int, tag: str, attributes: str, delta_time: int) -> None:
        if len(self._telemetry) >= TELEMETRY_QUEUE_LIMIT:
            self._telemetry.pop(0)
        self._telemetry.append({"tracking_id": tracking_id, "tag": tag, "attributes": attributes,
                                "delta_time": delta_time, "time": time.time()})
        if self._flusher is None:
            self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())
        if len(self._telemetry) >= TELEMETRY_BATCH:
            await self.flush_telemetry()

    async def flush_telemetry(self) -> None:
        if not self._telemetry:
            return
        batch, self._telemetry = self._telemetry, []
        try:
            await self._json("POST", "/telemetry", json={"events": batch})
        except BackendUnavailable as exc:
            log.warning("telemetry flush failed (%d events kept): %s", len(batch), exc)
            self._telemetry = (batch + self._telemetry)[-TELEMETRY_QUEUE_LIMIT:]

    # --- leaderboards (public AN-API routes) ---------------------------------------

    async def _public(self, key: str, path: str) -> Any:
        entry = self._static.get(key)
        if entry is not None and entry[0] > time.monotonic():
            return entry[1]
        try:
            resp = await self.http.get(f"{self.root}{path}")
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{path}: {exc}") from exc
        if resp.status_code != 200:
            raise BackendUnavailable(f"{path}: HTTP {resp.status_code}")
        data = resp.json()
        self._static[key] = (time.monotonic() + RANKINGS_TTL, data)
        return data

    async def mmr_ladder(self, mode: str) -> list[LadderEntry]:
        """``/boards/ranking/<mode>``, cached briefly."""
        docs = await self._public(f"ladder:{mode}", f"/boards/ranking/{mode}")
        return [LadderEntry(int(p.get("pid") or 0), _game_name(p["name"]), float(p.get(f"{mode}mmr") or 0),
                            int((p.get(f"{mode}games") or {}).get("total", 0))) for p in docs]

    async def player_records(self) -> list[PlayerRecord]:
        """``/players/`` (visible players), cached briefly."""
        out = []
        for doc in await self._public("players", "/players/"):
            modes = {}
            for key in doc:
                if key.endswith("games") and isinstance(doc[key], dict):
                    mode = key[:-len("games")]
                    if (stats := _mode_stats(doc, mode)) is not None:
                        modes[mode] = stats
            out.append(PlayerRecord(int(doc.get("pid") or 0), _game_name(doc["name"]), modes))
        return out

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(TELEMETRY_INTERVAL)
            await self.flush_telemetry()
