"""A deliberately small HTTP/1.1 server on asyncio streams.

The online config request and status/webhook endpoints need nothing more
than request line, headers and an optional body, so this avoids a web
framework dependency.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("quazal.http")

MAX_BODY = 64 * 1024


@dataclass
class Request:
    method: str
    target: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    peer: tuple[str, int]


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""


REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
           500: "Internal Server Error"}

Handler = Callable[[Request], Awaitable[Response]]


async def _read_request(reader: asyncio.StreamReader, peer) -> Request | None:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0], parts[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    body = b""
    length = int(headers.get("content-length", "0") or 0)
    if 0 < length <= MAX_BODY:
        body = await reader.readexactly(length)
    split = urlsplit(target)
    return Request(method, target, split.path, parse_qs(split.query), headers, body, peer)


def _serialize(resp: Response) -> bytes:
    lines = [f"HTTP/1.1 {resp.status} {resp.reason or REASONS.get(resp.status, '')}"]
    names = {k.lower() for k, _ in resp.headers}
    headers = list(resp.headers)
    if "content-length" not in names:
        headers.append(("Content-Length", str(len(resp.body))))
    if "connection" not in names:
        headers.append(("Connection", "close"))
    lines += [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + resp.body


async def serve(name: str, host: str, port: int, handler: Handler) -> asyncio.Server:
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            request = await asyncio.wait_for(_read_request(reader, peer), timeout=10)
            if request is None:
                response = Response(400)
            else:
                try:
                    response = await handler(request)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] handler error for %s", name, request.target)
                    response = Response(500)
            writer.write(_serialize(response))
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(on_client, host, port)
    log.info("%s listening on tcp/%s:%d", name, host, port)
    return server


def text(body: str, status: int = 200) -> Response:
    return Response(status, body.encode("utf-8"), [("Content-Type", "text/plain; charset=utf-8")])


def json_response(body: str, status: int = 200) -> Response:
    return Response(status, body.encode("utf-8"), [("Content-Type", "application/json; charset=utf-8")])
