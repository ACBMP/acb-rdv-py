"""Helpers to pair requests and responses in an acb-rdv ``packetLog.bin`` capture."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from quazal.capture import read_capture
from quazal.ddl import Reader
from quazal.profile import GameProfile
from quazal.prudp.packet import Flag, PacketType
from quazal.rmc.message import RMCMessage

DATA = Path(__file__).resolve().parents[1] / "data"
CAPTURE = Path(os.environ.get("ACB_CAPTURE", DATA / "packetLog.bin"))
DATABASE = Path(os.environ.get("ACB_CAPTURE_DB", DATA / "database.sqlite"))


@dataclass
class Exchange:
    conn_id: int
    port: int
    request: RMCMessage
    response: RMCMessage


def exchanges(path: Path = CAPTURE) -> list[Exchange]:
    codec = GameProfile("acb").codec()
    sig_to_conn: dict[int, int] = {}
    pending: dict[tuple, RMCMessage] = {}
    fragments: dict[int, list[bytes]] = {}
    out: list[Exchange] = []
    for rec in read_capture(path):
        pkt = codec.decode(rec.data)
        if not rec.sent and pkt.type == PacketType.CONNECT:
            sig_to_conn[pkt.conn_sig] = pkt.signature
        if pkt.type != PacketType.DATA or pkt.has(Flag.ACK) or not pkt.payload:
            continue
        if rec.sent:
            payload = pkt.payload
            if pkt.has(Flag.RELIABLE):
                fragments.setdefault(pkt.signature, []).append(pkt.payload)
                if pkt.part != 0:
                    continue
                payload = b"".join(fragments.pop(pkt.signature))
            msg = RMCMessage.decode(payload)
            if msg.is_request:
                continue
            conn_id = sig_to_conn.get(pkt.signature)
            req = pending.pop((conn_id, msg.protocol, msg.call_id), None)
            if req is not None and (msg.method == req.method or not msg.success):
                out.append(Exchange(conn_id, pkt.dst.port, req, msg))
        else:
            msg = RMCMessage.decode(pkt.payload)
            if msg.is_request:
                pending[(pkt.signature, msg.protocol, msg.call_id)] = msg
    return out


def login_pids(exchanges_: list[Exchange]) -> dict[int, int]:
    pids = {}
    for ex in exchanges_:
        if (ex.request.protocol, ex.request.method) == (0x0A, 2) and ex.response.success:
            r = Reader(ex.response.body)
            r.u32()
            pids[ex.conn_id] = r.u32()
    return pids


def capture_rows(query: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()
