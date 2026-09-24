#!/usr/bin/env python3
"""Build the native app: genesis.exe on Windows (a `genesis` binary elsewhere).

    pip install ".[google,premium,signing,mobile]" pyinstaller
    (cd mobile && npm ci && npm run export:web)   # optional: the phone web app
    python packaging/build.py            # build + smoke test + zip
    python packaging/build.py --no-zip   # leave dist/genesis/ only

Output: dist/genesis/ (the app), dist/genesis-<os>-x64.zip and its .sha256 —
the two files scripts/install.ps1 downloads from the GitHub release.

The smoke test runs the BUILT binary, not the source: a module PyInstaller
missed, a data file left out or a broken Python mode only shows up there,
and only on the user's machine otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "native"
DIST = ROOT / "dist"
APP = DIST / "genesis"
WEB = ROOT / "mobile" / "dist-web"
DEFAULT_TAG = "latest"  # version_info.LATEST_RELEASE
DEFAULT_URL = "https://github.com/me7ko-dev/genesis-agent"


def _version() -> str:
    text = (ROOT / "genesis_agent" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no __version__ in genesis_agent/__init__.py")


def _commit() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if sha:
        return sha
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def asset_name() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return f"genesis-{system}-x64.zip"


# ── icon: drawn here, so the repository carries no binary ──────────────────

def _png(size: int) -> bytes:
    """A terminal prompt `>_` on a rounded violet square, 4x4 supersampled."""
    def seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5

    def rounded(px: float, py: float) -> bool:
        r, lo, hi = 0.2, 0.04, 0.96
        cx, cy = min(max(px, lo + r), hi - r), min(max(py, lo + r), hi - r)
        return (px - cx) ** 2 + (py - cy) ** 2 <= r * r and lo <= px <= hi and lo <= py <= hi

    strokes = [(0.28, 0.30, 0.50, 0.50), (0.50, 0.50, 0.28, 0.70), (0.56, 0.70, 0.74, 0.70)]
    n = 4
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            bg = fg = 0
            for sy in range(n):
                for sx in range(n):
                    px, py = (x + (sx + 0.5) / n) / size, (y + (sy + 0.5) / n) / size
                    if not rounded(px, py):
                        continue
                    bg += 1
                    if any(seg(px, py, *s) <= 0.055 for s in strokes):
                        fg += 1
            k = n * n
            t = y / max(size - 1, 1)
            base = (int(109 - 60 * t), int(40 + 20 * t), int(217 - 40 * t))
            a = bg / k
            f = fg / max(bg, 1)
            rgb = [int(c * (1 - f) + 255 * f) for c in base]
            row += bytes([*rgb, int(255 * a)])
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def write_icon(path: Path) -> None:
    images = [(s, _png(s)) for s in (16, 32, 48, 256)]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    path.write_bytes(bytes(out))


def write_version_file(path: Path, version: str, commit: str) -> None:
    nums = [int(p) for p in version.split(".")[:3] if p.isdigit()] + [0, 0, 0, 0]
    v = tuple(nums[:4])
    product = f"{version} ({commit[:7]})" if commit else version
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Genesis Agent'),
      StringStruct('FileDescription', 'Genesis Agent - autonomous coding agent'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'genesis'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'genesis.exe'),
      StringStruct('ProductName', 'Genesis Agent'),
      StringStruct('ProductVersion', '{product}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


# ── smoke test of the built binary ─────────────────────────────────────────

SMOKE_SCRIPT = """
import sys, json, csv, sqlite3, asyncio, zoneinfo, ctypes, decimal, unittest
import urllib.request, xml.etree.ElementTree, email, http.server, concurrent.futures
import requests, yaml, rich
assert sys.argv[1:] == ["a", "b"], sys.argv
print("script-ok")
"""

SERVE_CHECK = """
from genesis_agent import remote_server as rs
key = bytes(32)
env = rs.Cipher(key).seal({"op": "status"}, rs._REQ_AAD)
assert rs.Cipher(key).open(env, rs._REQ_AAD) == {"op": "status"}
import qrcode
qrcode.QRCode().add_data("x")
print("serve-ok", "web" if rs._web_root() else "no-web")
"""

SANDBOX_CHECK = """
from genesis_agent import sandbox
r = sandbox.run_python("import sqlite3; print('sandbox', 6 * 7)")
assert r.ok and "sandbox 42" in r.stdout, (r.stdout, r.stderr)
from genesis_agent.config import SKILLS_DIR
assert any(SKILLS_DIR.glob("*.md")), SKILLS_DIR
import genesis_agent.paths as p
assert p.FROZEN and p.install_dir() is not None
print("sandbox-ok")
"""


def smoke(exe: Path) -> None:
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "GENESIS_HOME": home, "PYTHONIOENCODING": "utf-8"}
        script = Path(home) / "check.py"
        script.write_text(SMOKE_SCRIPT, encoding="utf-8")

        def run(*args: str, expect: str) -> None:
            r = subprocess.run([str(exe), *args], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env, cwd=home,
                               timeout=300, check=False)
            out = r.stdout + r.stderr
            ok = r.returncode == 0 and expect in out
            print(f"  {'ok ' if ok else 'FAIL'} {' '.join(args)[:60]}")
            if not ok:
                raise SystemExit(f"smoke test failed (rc={r.returncode}):\n{out[-3000:]}")

        run("--version", expect="genesis-agent")
        run("--help", expect="genesis setup")
        run("skills", expect="")
        run("-c", "import sys; print('code-ok', sys.argv[1:])", "x", expect="code-ok ['x']")
        run(str(script), "a", "b", expect="script-ok")
        run("-m", "json.tool", "--help", expect="usage")
        run("-c", "raise SystemExit(0)", expect="")
        run("-c", SANDBOX_CHECK, expect="sandbox-ok")
        run("-c", "import genesis_terminal_agent", expect="")
        run("-c", SERVE_CHECK, expect="serve-ok web" if WEB.joinpath("index.html").is_file() else "serve-ok")
        run("serve", "--help", expect="genesis serve")


def package(version: str) -> Path:
    archive = DIST / asset_name()
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(APP.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(APP).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (DIST / (archive.name + ".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    size = archive.stat().st_size / 1e6
    print(f"{archive.name}  {size:.1f} MB  sha256 {digest[:16]}…  (version {version})")
    return archive


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", default=os.environ.get("GENESIS_RELEASE_TAG", DEFAULT_TAG),
                    help="release tag /update follows (default %(default)s)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    ns = ap.parse_args()

    version, commit = _version(), _commit()
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    info = BUILD / "_build_info.json"
    info.write_text(json.dumps({"url": ns.url, "commit": commit, "ref": ns.tag,
                                "version": version, "platform": platform.platform()}),
                    encoding="utf-8")
    icon, version_file = BUILD / "genesis.ico", BUILD / "version_info.txt"
    write_icon(icon)
    write_version_file(version_file, version, commit)

    if WEB.joinpath("index.html").is_file():
        print(f"phone web app: {WEB}")
    else:
        print("phone web app: not built (cd mobile && npm ci && npm run export:web) — "
              "genesis serve will show a landing page instead")
    env = {**os.environ, "GENESIS_BUILD_INFO": str(info), "GENESIS_ICON": str(icon),
           "GENESIS_WEB_DIR": str(WEB),
           "GENESIS_VERSION_FILE": str(version_file)}
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "genesis.spec"),
                    "--noconfirm", "--clean", "--distpath", str(DIST),
                    "--workpath", str(BUILD / "work")], cwd=ROOT, env=env, check=True)

    exe = APP / ("genesis.exe" if sys.platform == "win32" else "genesis")
    if not ns.no_smoke:
        print(f"smoke test: {exe}")
        smoke(exe)
    if not ns.no_zip:
        package(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
