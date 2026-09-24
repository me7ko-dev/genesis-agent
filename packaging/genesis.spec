# PyInstaller spec for the native build — run through packaging/build.py,
# which sets the GENESIS_* variables read below. By hand:
#     pyinstaller packaging/genesis.spec --noconfirm
#
# One directory, not one file: a onefile exe unpacks itself into %TEMP% on
# every start (seconds, and the classic antivirus false positive). UPX off for
# the same antivirus reason.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH is injected by PyInstaller
PKG = ROOT / "genesis_agent"

datas = [(str(PKG / "config.yaml"), "genesis_agent")]
# Same set as [tool.setuptools.package-data]: what pip ships, this ships.
datas += [(str(p), "genesis_agent/skills") for p in sorted((PKG / "skills").glob("*.md"))]
datas.append((str(PKG / "skills" / "skills.json"), "genesis_agent/skills"))
# /update in the native build runs the installer, from a copy in %TEMP%.
datas.append((str(ROOT / "scripts" / "install.ps1"), "."))
build_info = os.environ.get("GENESIS_BUILD_INFO")
if build_info:
    datas.append((build_info, "genesis_agent"))
# The phone app's web build (mobile/, `expo export -p web`): `genesis serve`
# hands it to a browser, so an iPhone needs no install at all.
web = os.environ.get("GENESIS_WEB_DIR")
if web and (Path(web) / "index.html").is_file():
    for f in sorted(Path(web).rglob("*")):
        if f.is_file():
            datas.append((str(f), str(Path("genesis_agent/web") / f.relative_to(web).parent)))

# genesis.exe doubles as the sandbox's Python (packaging/genesis_entry.py), so
# it carries the whole standard library, not only what Genesis imports itself:
# code the agent writes may reach for any of it.
_STDLIB_SKIP = {
    "tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip",
    "venv", "pydoc", "pydoc_data", "antigravity", "this", "msilib", "curses",
    "readline", "tty", "pty", "termios", "crypt", "nis", "spwd", "grp", "pwd",
    "posix", "fcntl", "resource", "syslog", "ossaudiodev", "distutils",
    "sre_compile", "sre_constants", "sre_parse",
}


def _stdlib() -> list[str]:
    names: list[str] = []
    for name in sorted(getattr(sys, "stdlib_module_names", ())):
        if name.startswith("_") or name in _STDLIB_SKIP:
            continue
        names += collect_submodules(
            name, filter=lambda m: ".test" not in m and ".idle" not in m)
    return names


hiddenimports = (
    ["genesis_terminal_agent", "genesis_skills"]
    + collect_submodules("genesis_agent")
    # rich loads its unicode width tables by version at runtime.
    + collect_submodules("rich")
    + _stdlib()
)
# The optional extras are carried when the build environment has them
# (CI installs [google,premium,signing,mobile]): Vertex, the paid MAX tier and
# signed skills then work without `pipx inject`, which the native build has
# no equivalent of.
for optional in ("google.auth", "anthropic", "cryptography", "qrcode"):
    try:
        __import__(optional)
    except ImportError:
        continue
    hiddenimports += collect_submodules(optional)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "genesis_entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "_tkinter", "test", "idlelib", "turtledemo", "lib2to3", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

icon = os.environ.get("GENESIS_ICON")
version_file = os.environ.get("GENESIS_VERSION_FILE")
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="genesis",
    console=True,
    upx=False,
    icon=icon if icon and Path(icon).exists() else None,
    version=version_file if version_file and Path(version_file).exists() else None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="genesis",
    upx=False,
)
