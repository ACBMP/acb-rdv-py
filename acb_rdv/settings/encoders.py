"""XML -> CXB encoders.

A CXB (the game settings file ACB downloads at login) bundles several XML
documents, each minified and LZO2A-compressed. The encoding lives in the C++
CXB library (CXBTool refactored into a library), called in process through
the C ABI in ``native/cxb.h`` (``cxb_abi_version``, ``cxb_encode``,
``cxb_free``, ``cxb_last_error``).

The XML files are passed as ``(name, raw bytes)`` pairs. The bytes go through
untouched (the documents declare their own encoding), and the caller orders
them the way CXBTool does: by base name, byte order.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
from typing import Protocol, Sequence

log = logging.getLogger("acb.settings")

XmlFile = tuple[str, bytes]

# ABI version this code speaks; must match CXB_ABI_VERSION in native/cxb.h
CXB_ABI_VERSION = 1


class CxbError(RuntimeError):
    pass


class CxbEncoder(Protocol):
    name: str

    def encode(self, files: Sequence[XmlFile]) -> bytes:
        """Build a CXB from XML files (blocking; call it off the event loop)."""
        ...


class _CxbFile(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("xml", ctypes.POINTER(ctypes.c_ubyte)),
        ("xml_len", ctypes.c_size_t),
    ]


class LibraryCxbEncoder:
    """In-process encoder over the C ABI (see ``native/cxb.h``)."""

    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        self.name = f"library {self.path}"
        lib = ctypes.CDLL(self.path)
        lib.cxb_abi_version.restype = ctypes.c_int
        lib.cxb_abi_version.argtypes = []
        version = lib.cxb_abi_version()
        if version != CXB_ABI_VERSION:
            raise CxbError(f"{self.path}: CXB ABI version {version}, expected {CXB_ABI_VERSION}")
        lib.cxb_encode.restype = ctypes.c_int
        lib.cxb_encode.argtypes = [ctypes.POINTER(_CxbFile), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(ctypes.c_size_t)]
        lib.cxb_free.restype = None
        lib.cxb_free.argtypes = [ctypes.c_void_p]
        lib.cxb_last_error.restype = ctypes.c_char_p
        lib.cxb_last_error.argtypes = []
        self._lib = lib

    def encode(self, files: Sequence[XmlFile]) -> bytes:
        buffers = [(ctypes.c_ubyte * len(data)).from_buffer_copy(data) for _, data in files]
        array = (_CxbFile * len(files))(*[
            _CxbFile(name.encode("utf-8"), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), len(data))
            for (name, data), buf in zip(files, buffers, strict=True)
        ])
        out = ctypes.POINTER(ctypes.c_ubyte)()
        out_len = ctypes.c_size_t()
        if self._lib.cxb_encode(array, len(files), ctypes.byref(out), ctypes.byref(out_len)) != 0:
            message = self._lib.cxb_last_error() or b"unknown error"
            raise CxbError(message.decode("utf-8", "replace"))
        try:
            return ctypes.string_at(out, out_len.value)
        finally:
            self._lib.cxb_free(out)


def find_encoder(library: str = "") -> CxbEncoder:
    """Load the CXB library from ``library``, ``$ACB_CXB_LIBRARY`` or the system search path."""
    path = library or os.environ.get("ACB_CXB_LIBRARY", "") or ctypes.util.find_library("cxb") or ""
    if not path:
        raise CxbError("no CXB library found: set cxb_library (or ACB_CXB_LIBRARY) to libcxb's path")
    try:
        return LibraryCxbEncoder(path)
    except (OSError, AttributeError) as exc:
        raise CxbError(f"cannot load CXB library {path}: {exc}") from exc
