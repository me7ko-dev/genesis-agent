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

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH is injected by PyInstaller
PKG = ROOT / "genesis_agent"

datas = [(str(PKG / "config.yaml"), "genesis_agent")]
# Same set as [tool.setuptools.package-data]: what pip ships, this ships.
datas += [(str(p), "genesis_agent/skills") for p in sorted((PKG / "skills").glob("*.md"))]
datas.append((str(PKG / "skills" / "skills.json"), "genesis_agent/skills"))
# The browser check runs this file with the system Python (the one with
# playwright), so it has to exist as a .py file, not only as bytecode.
datas.append((str(PKG / "page_check_runner.py"), "genesis_agent"))
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

# Глас и картинки (2026-10-15): [voice] = sounddevice + edge-tts, [vision] =
# Pillow. voice.py/vision.py ги внасят мързеливо (find_spec + import във
# функция), затова тук се изброяват изрично — анализът на PyInstaller не
# трябва да е единственото, което ги „вижда“. Без тях genesis.exe нямаше
# /listen, /voice on и смаляване на картинки, а операторът на Windows
# ползва точно него.
#   sounddevice — DLL-ът на PortAudio е в _sounddevice_data/portaudio-binaries;
#     прибира го hook-sounddevice от pyinstaller-hooks-contrib (идва с
#     pyinstaller==6.22.3), а проверката под Analysis спира билда без него.
#   edge_tts — чист Python върху aiohttp; TLS контекстът му е
#     certifi.where(), затова cacert.pem трябва да е вътре (hook-certifi,
#     а certifi е и изрично тук), иначе всеки глас гърми на TLS.
#   PIL — hook-PIL.Image прибира плъгините; тук са тези, които vision.py
#     приема (PNG/JPEG/WebP/GIF), за да не зависят от hook-а.
# faster-whisper ([voice-local]) НЕ влиза: ctranslate2 + модел от 0.5–3 GB.
MEDIA_IMPORTS = {
    "sounddevice": ["sounddevice", "_sounddevice", "_sounddevice_data", "_cffi_backend"],
    "edge_tts": ["edge_tts", "aiohttp", "certifi"],
    "PIL": ["PIL.Image", "PIL.ImageOps", "PIL.PngImagePlugin", "PIL.JpegImagePlugin",
            "PIL.WebPImagePlugin", "PIL.GifImagePlugin"],
}
media: list[str] = []
for top, names in MEDIA_IMPORTS.items():
    try:
        __import__(top)
    except ImportError:
        continue
    except OSError:
        # sounddevice на Linux без libportaudio2: пакетът е тук, DLL-ът не.
        pass
    media.append(top)
    hiddenimports += names
if "edge_tts" in media:
    # aiohttp.worker е за gunicorn — edge-tts ползва само клиента.
    hiddenimports += collect_submodules("edge_tts")
    hiddenimports += collect_submodules("aiohttp", filter=lambda m: m != "aiohttp.worker")
    datas += collect_data_files("certifi")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "genesis_entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "_tkinter", "test", "idlelib", "turtledemo", "lib2to3", "playwright",
              # [voice-local] — дори ако е инсталиран там, където се билдва.
              "faster_whisper", "ctranslate2"],
    noarchive=False,
)
# Без DLL-а `import sounddevice` в genesis.exe гърми с „PortAudio library not
# found“ — по-добре билдът да спре тук, отколкото микрофонът при оператора.
if sys.platform == "win32" and "sounddevice" in media and not any(
        "libportaudio" in Path(dest).name.lower() for dest, *_ in a.binaries):
    raise SystemExit("genesis.spec: sounddevice е тук, но libportaudio*.dll не е събран")
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
