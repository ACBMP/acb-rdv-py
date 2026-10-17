"""Game settings providers and the CXB library ABI."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from acb_rdv.settings import (
    CxbError,
    CxbFileProvider,
    LibraryCxbEncoder,
    SettingsError,
    XmlSettingsProvider,
    find_encoder,
)

ROOT = Path(__file__).resolve().parents[1]
CXBTOOL = Path(os.environ.get("CXBTOOL_SRC", ROOT.parent / "CXBTool"))
REFERENCE_CXB = Path(os.environ.get("ACB_REFERENCE_CXB", ROOT / "data" / "gamesettings_c1380_d873_s6285.cxb"))


class FakeEncoder:
    name = "fake"

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, files):
        self.calls.append([n for n, _ in files])
        return b"CXB:" + b"|".join(data for _, data in files)


def write(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body, encoding="latin-1")


async def test_xml_provider_orders_caches_and_rebuilds(tmp_path):
    for name in ("b.xml", "a_b.xml", "A.xml", "notes.txt"):
        write(tmp_path, name, f"<r n='{name}'/>")
    enc = FakeEncoder()
    provider = XmlSettingsProvider(tmp_path, enc, check_interval=0)
    first = await provider.current()
    # CXBTool's order: base name, byte order (uppercase first); non-XML ignored
    assert enc.calls == [["A.xml", "a_b.xml", "b.xml"]]
    assert (await provider.current()) is first and len(enc.calls) == 1  # unchanged -> cached
    write(tmp_path, "b.xml", "<r n='changed'/>")
    os.utime(tmp_path / "b.xml", ns=(1, 1))
    second = await provider.current()
    assert len(enc.calls) == 2 and b"changed" in second.data


async def test_xml_provider_keeps_last_good_build(tmp_path):
    write(tmp_path, "a.xml", "<ok/>")
    provider = XmlSettingsProvider(tmp_path, FakeEncoder(), check_interval=0)
    good = await provider.current()
    write(tmp_path, "a.xml", "<broken>")
    os.utime(tmp_path / "a.xml", ns=(2, 2))
    assert (await provider.current()) is good
    assert "malformed XML - a.xml" in provider.describe()


async def test_xml_provider_without_a_good_build_raises(tmp_path):
    write(tmp_path, "a.xml", "<broken>")
    with pytest.raises(SettingsError, match="malformed"):
        await XmlSettingsProvider(tmp_path, FakeEncoder()).current()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SettingsError, match="no .xml files"):
        await XmlSettingsProvider(empty, FakeEncoder()).current()


async def test_cxb_file_provider_reloads(tmp_path):
    f = tmp_path / "s.cxb"
    f.write_bytes(b"one")
    provider = CxbFileProvider(f)
    assert (await provider.current()).data == b"one"
    f.write_bytes(b"two!")
    assert (await provider.current()).data == b"two!"
    with pytest.raises(SettingsError):
        await CxbFileProvider(tmp_path / "missing.cxb").current()


def test_find_encoder_reports_missing_library(monkeypatch):
    monkeypatch.delenv("ACB_CXB_LIBRARY", raising=False)
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    with pytest.raises(CxbError, match="no CXB library"):
        find_encoder()


# --- the C ABI, through the test-only reference implementation -----------------------

@pytest.fixture(scope="module")
def reference_library(tmp_path_factory) -> Path:
    """Compile tests/native/cxb_reference.cpp against CXBTool's encoder."""
    parser = CXBTOOL / "src" / "cxb_parser.cpp"
    if not parser.exists() or shutil.which("c++") is None or shutil.which("pkg-config") is None:
        pytest.skip("CXBTool sources or a C++ toolchain not available")
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "pugixml", "lzo2"], capture_output=True, text=True)
    if flags.returncode != 0:
        pytest.skip("pugixml/lzo2 development files not available")
    out = tmp_path_factory.mktemp("cxb") / "libcxb.so"
    cmd = ["c++", "-std=c++17", "-O2", "-shared", "-fPIC", "-I", str(CXBTOOL / "src"),
           str(ROOT / "tests" / "native" / "cxb_reference.cpp"), str(parser), "-o", str(out),
           *flags.stdout.split()]
    build = subprocess.run(cmd, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return out


@pytest.fixture(scope="module")
def reference_xml(tmp_path_factory) -> Path:
    """The reference settings exported to XML with the CXBTool CLI (test data preparation only)."""
    tool = CXBTOOL / "build" / "CXBTool"
    if not tool.exists() or not REFERENCE_CXB.exists():
        pytest.skip("CXBTool build or reference settings not available")
    out = tmp_path_factory.mktemp("xml")
    subprocess.run([str(tool), "export", str(REFERENCE_CXB), str(out)], check=True, capture_output=True)
    return out


def test_library_abi(reference_library):
    enc = LibraryCxbEncoder(reference_library)
    data = enc.encode([("globalparams.xml", b'<?xml version="1.0" encoding="ISO-8859-1"?><a v="\xe9"/>')])
    assert data.startswith(b"globalparams\x00")
    with pytest.raises(CxbError, match="broken.xml"):
        enc.encode([("broken.xml", b"<a>")])


async def test_xml_through_library_reproduces_reference_settings(reference_library, reference_xml):
    provider = XmlSettingsProvider(reference_xml, LibraryCxbEncoder(reference_library))
    settings = await provider.current()
    assert settings.data == REFERENCE_CXB.read_bytes()
    assert len(settings.files) == 28
