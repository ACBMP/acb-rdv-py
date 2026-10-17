"""Rich presence (0x6D), acb-rdv ``RichPresenceService``.

The presence blob is an ASCII hex string of little-endian u32 words:
``count, then (property id, variant type, value)`` per property. acb-rdv parsed
and re-serialised it; here the client's blob is stored and returned as sent
(which is identical for well-formed Int32 properties) and parsed only for
display.
"""

from __future__ import annotations

import logging
import struct

from ..context import ServerContext, player
from ..ddl import Bool, QBuffer, Structure, U32
from ..rmc import Call, Protocol, method

log = logging.getLogger("quazal.presence")

# unknown field the original server always sent
PRESENCE_UNK = 0x69
VARIANT_INT32 = 1


class PresenceElement(Structure):
    pid: U32 = 0
    override_status: Bool = True
    unknown: U32 = PRESENCE_UNK
    properties: QBuffer = b""


def parse_presence(blob: bytes) -> list[tuple[int, int, int]]:
    """Decode the hex blob into ``(id, variant type, value)`` triples (Int32 only)."""
    try:
        raw = bytes.fromhex(blob.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return []
    words = [struct.unpack_from("<I", raw, i)[0] for i in range(0, len(raw) - len(raw) % 4, 4)]
    if not words:
        return []
    props = []
    pos = 1
    for _ in range(words[0]):
        if pos + 2 >= len(words) or words[pos + 1] != VARIANT_INT32:
            break
        props.append((words[pos], words[pos + 1], words[pos + 2]))
        pos += 3
    return props


class RichPresenceProtocol(Protocol):
    protocol_id = 0x6D

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "SetPresence")
    async def set_presence(self, call: Call):
        call.args.u32()  # pid
        blob = call.args.qbuffer()
        state = player(call.client)
        # acb-rdv answered players without properties with an empty buffer
        state.presence = [blob] if parse_presence(blob) else []
        return None

    @method(2, "GetPresence")
    async def get_presence(self, call: Call):
        pids = call.args.u32_list()
        elements = []
        for pid in pids:
            client = self.ctx.online(pid)
            blob = b""
            if client is not None:
                presence = player(client).presence
                blob = presence[0] if presence else b""
            elements.append(PresenceElement(pid, True, PRESENCE_UNK, blob))
        return self.ctx.writer().list(elements, lambda w, e: e.write(w))
