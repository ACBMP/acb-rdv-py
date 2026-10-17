"""Kerberos-style tickets (acb-rdv ``AuthenticationService/Kerberos/KerberosTicket.cs``).

A ticket is ``RC4(key, session key[16] | u32 server pid | u32 len | inner
ticket) + HMAC-MD5(key, encrypted part)`` where ``key`` is derived from the
user's pid and the (dummy) password. The server never has to open the inner
ticket, so a fixed blob captured from the original service is used.
"""

from __future__ import annotations

import struct

from .crypto import derive_key, hmac_md5, rc4

# inner ticket bytes dumped from the original traffic (acb-rdv)
DEFAULT_INNER_TICKET = bytes.fromhex(
    "76214BA62196D3F39A8C7A270DD9B3FA210EEDAF42639295C1165408EE6E691735782E6E"
)


def build_ticket(
    user_pid: int,
    server_pid: int,
    session_key: bytes,
    inner: bytes = DEFAULT_INNER_TICKET,
    password: str = "UbiDummyPwd",
) -> bytes:
    key = derive_key(user_pid, password)
    plain = session_key + struct.pack("<II", server_pid, len(inner)) + inner
    enc = rc4(key, plain)
    return enc + hmac_md5(key, enc)


def open_ticket(user_pid: int, ticket: bytes, password: str = "UbiDummyPwd") -> tuple[bytes, int, bytes]:
    """Verify and decrypt a ticket; returns ``(session key, server pid, inner)``."""
    key = derive_key(user_pid, password)
    enc, mac = ticket[:-16], ticket[-16:]
    if hmac_md5(key, enc) != mac:
        raise ValueError("ticket HMAC mismatch")
    plain = rc4(key, enc)
    server_pid, size = struct.unpack_from("<II", plain, 16)
    return plain[:16], server_pid, plain[24 : 24 + size]
