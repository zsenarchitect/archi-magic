"""Tests for scripts/render-pdf-pages.ps1 (Windows.Data.Pdf, no Python or poppler needed by users).

The script runs only under Windows PowerShell 5.1, so these tests skip on other systems.
The tests write a tiny synthetic PDF, render it, and decode the PNG with zlib.
"""
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render-pdf-pages.ps1"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell 5.1 (Windows only)")


def make_pdf(path, pages):
    """Write a PDF. pages: list of (width_pt, height_pt, content_stream_bytes)."""
    n = len(pages)
    kids = " ".join("%d 0 R" % (3 + 2 * i) for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)).encode("ascii")]
    for i, (w, h, content) in enumerate(pages):
        objs.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R >>"
                     % (w, h, 4 + 2 * i)).encode("ascii"))
        objs.append(b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
                    + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def read_png(path):
    """Decode an 8-bit, non-interlaced RGB or RGBA PNG. Returns (width, height, rows of pixel tuples)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    width, height, depth, color, _, _, interlace = ihdr
    assert depth == 8 and interlace == 0 and color in (2, 6)
    bpp = 3 if color == 2 else 4
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        rows.append([tuple(line[x:x + 3]) for x in range(0, stride, bpp)])
        prev = line
    return width, height, rows


def run(*args):
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


# Page 1: 144 x 72 pt (2 x 1 in), left half filled red. Page 2: 72 x 72 pt, blank.
RED_LEFT = b"1 0 0 rg 0 0 72 72 re f"


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "review set.pdf"
    make_pdf(path, [(144, 72, RED_LEFT), (72, 72, b"")])
    return path


def test_check_reports_engine():
    r = run("-Check")
    assert r.returncode == 0, r.stderr
    assert "WINRT_PDF ok" in r.stdout


def test_renders_every_page_with_size_and_content(pdf, tmp_path):
    out = tmp_path / "png"
    r = run("-Pdf", pdf, "-OutDir", out, "-Dpi", 100)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "PDF 2 page(s)"
    assert lines[1].startswith("PAGE 1 200x100 ")
    assert lines[2].startswith("PAGE 2 100x100 ")
    width, height, rows = read_png(out / "review set_p1.png")
    assert (width, height) == (200, 100)
    left, right = rows[50][50], rows[50][150]
    assert left[0] > 200 and left[1] < 60 and left[2] < 60, left
    assert min(right) > 200, right
    assert read_png(out / "review set_p2.png")[:2] == (100, 100)
    assert pdf.exists()


def test_page_list_renders_only_named_pages(pdf, tmp_path):
    out = tmp_path / "png"
    r = run("-Pdf", pdf, "-OutDir", out, "-Pages", "2", "-Dpi", 50)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["review set_p2.png"]


@pytest.mark.parametrize("pages", ["0", "2-1", "a", "1,,2", "3"])
def test_bad_or_missing_pages_exit_2(pdf, tmp_path, pages):
    out = tmp_path / "png"
    r = run("-Pdf", pdf, "-OutDir", out, "-Pages", pages)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert not out.exists() or not any(out.iterdir())


def test_bad_dpi_exits_2(pdf, tmp_path):
    assert run("-Pdf", pdf, "-OutDir", tmp_path / "png", "-Dpi", 10).returncode == 2


def test_missing_and_damaged_pdf_exit_1(tmp_path):
    assert run("-Pdf", tmp_path / "none.pdf", "-OutDir", tmp_path / "png").returncode == 1
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    r = run("-Pdf", bad, "-OutDir", tmp_path / "png")
    assert r.returncode == 1, (r.stdout, r.stderr)


def test_script_is_ascii_only():
    data = SCRIPT.read_bytes()
    assert all(b < 128 for b in data)
    assert not data.startswith(b"\xef\xbb\xbf")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
def test_powershell_7_exits_3(pdf, tmp_path):
    cmd = [shutil.which("pwsh"), "-NoProfile", "-File", str(SCRIPT), "-Pdf", str(pdf), "-OutDir", str(tmp_path / "png")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "WINRT_PDF missing" in r.stderr
