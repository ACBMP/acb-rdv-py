"""RC4, key derivation, HMAC and zlib helpers (acb-rdv ``Helper.cs``)."""

from __future__ import annotations

import hashlib
import hmac
import zlib
from functools import lru_cache


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4 with a fresh key schedule; encryption and decryption are the same."""
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


@lru_cache(maxsize=4096)
def derive_key(pid: int, password: str = "UbiDummyPwd") -> bytes:
    """Kerberos key: MD5 applied ``65000 + pid % 1024`` times to the password."""
    buf = password.encode("ascii")
    for _ in range(65000 + pid % 1024):
        buf = hashlib.md5(buf).digest()
    return buf


def hmac_md5(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.md5).digest()


def zlib_compress(data: bytes, level: int = 6) -> bytes:
    return zlib.compress(data, level)


def zlib_decompress(data: bytes) -> bytes:
    return zlib.decompress(data)
