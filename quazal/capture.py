"""Packet capture in acb-rdv's ``packetLog.bin`` format.

Each record is ``u8 version (1) | u8 sent | u32 length | datagram``. The
format carries no endpoints or timestamps; it is kept so captures from the new
server can be compared with ones taken from acb-rdv.
"""

from __future__ import annotations

import struct
import threading
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple


class Record(NamedTuple):
    sent: bool
    data: bytes


def read_capture(path: str | Path) -> Iterator[Record]:
    with open(path, "rb") as f:
        yield from read_records(f)


def read_records(f: BinaryIO) -> Iterator[Record]:
    while True:
        head = f.read(6)
        if len(head) < 6:
            return
        version, sent, length = struct.unpack("<BBI", head)
        if version != 1:
            raise ValueError(f"unsupported capture record version {version}")
        data = f.read(length)
        if len(data) < length:
            return
        yield Record(bool(sent), data)


class CaptureWriter:
    def __init__(self, path: str | Path):
        self._f = open(path, "ab", buffering=0)
        self._lock = threading.Lock()

    def write(self, sent: bool, data: bytes) -> None:
        with self._lock:
            self._f.write(struct.pack("<BBI", 1, 1 if sent else 0, len(data)) + data)

    def close(self) -> None:
        self._f.close()
