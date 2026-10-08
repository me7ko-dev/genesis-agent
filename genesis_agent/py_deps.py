"""
genesis_agent.py_deps — which third-party packages a Python project imports,
and which of them it never declares.

Two callers, one question. RUN_CMD output with `No module named 'openpyxl'`
gets a hint with the exact pip name (the model otherwise guesses — `pip install
PIL`, `pip install docx` — and loses a round to the wrong package). `genesis
export` lists the imports missing from requirements.txt / pyproject.toml: the
project runs here because the package happens to be installed, and fails on
the recipient's machine on the first line.

Static, via `ast` — nothing is imported or executed.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Import name → the name on PyPI, where the two differ. Only the common ones:
# an unknown name falls back to itself, which is right for most packages.
IMPORT_TO_PIP = {
    "PIL": "pillow", "cv2": "opencv-python", "yaml": "PyYAML", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "docx": "python-docx", "pptx": "python-pptx",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "fitz": "pymupdf",
    "jwt": "PyJWT", "serial": "pyserial", "Crypto": "pycryptodome", "telegram": "python-telegram-bot",
    "magic": "python-magic", "multipart": "python-multipart", "jose": "python-jose",
    "google.generativeai": "google-generativeai", "attr": "attrs", "OpenSSL": "pyOpenSSL",
    "win32api": "pywin32", "win32com": "pywin32", "pythoncom": "pywin32", "usb": "pyusb",
    "Levenshtein": "python-Levenshtein", "slugify": "python-slugify", "ldap": "python-ldap",
    "skimage": "scikit-image", "zmq": "pyzmq", "git": "GitPython", "discord": "discord.py",
    "googleapiclient": "google-api-python-client", "psycopg2": "psycopg2-binary",
    "google.oauth2": "google-auth", "google.auth": "google-auth", "google.protobuf": "protobuf",
}
# Внасянето е едно, дистрибуциите — няколко равностойни (одит 2026-10-07:
# `psycopg2-binary` в requirements се отчиташе като „липсващ psycopg2“).
ALSO_PROVIDED_BY = {
    "psycopg2": {"psycopg2", "psycopg2-binary"},
    "cv2": {"opencv-python", "opencv-python-headless", "opencv-contrib-python",
            "opencv-contrib-python-headless"},
    "PIL": {"pillow", "pil"},
    "yaml": {"pyyaml", "ruamel-yaml"},
    "Crypto": {"pycryptodome", "pycryptodomex", "pycrypto"},
    "jwt": {"pyjwt", "jwt"},
}
# Пространства от имена: `google` сам по себе не е пакет — `pip install google`
# е друг, безполезен пакет. Името е от първите две части (`google.cloud.storage`
# → `google-cloud-storage`); без втора част — никаква подсказка, не грешна.
_NAMESPACES = {"google", "azure", "zope", "jaraco", "backports"}

_SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "build",
              "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages"}
# Test-only tooling that every runner already has, or that belongs in a dev
# extra rather than in what the recipient installs to run the thing.
_DEV_ONLY = {"pytest", "_pytest", "hypothesis", "mypy", "ruff"}


def pip_name(module: str) -> str:
    """Името в PyPI; "" за голо пространство от имена (`google`)."""
    parts = module.split(".")
    top = parts[0]
    if module in IMPORT_TO_PIP:
        return IMPORT_TO_PIP[module]
    if top in _NAMESPACES:
        if len(parts) < 2:
            return ""
        three = len(parts) > 2 and (parts[1] == "cloud" or top == "azure")
        return "-".join(parts[:3] if three else parts[:2])
    return IMPORT_TO_PIP.get(top) or top


def _accepted(module: str) -> set[str]:
    """Всички нормализирани имена, които покриват това внасяне."""
    top = module.split(".")[0]
    names = {normalize(n) for n in ALSO_PROVIDED_BY.get(top, set())}
    pip = pip_name(module)
    if pip:
        names.add(normalize(pip))
    if top not in _NAMESPACES:
        names.add(normalize(top))
    return names


def normalize(name: str) -> str:
    """PEP 503: `Python_Dateutil` and `python-dateutil` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


# Стандартни в по-нов Python, отколкото може да върви Genesis (≥3.10): на 3.10
# `No module named 'tomllib'` не е липсващ пакет — `pip install tomllib` е
# грешният съвет (CI на 3.10, 2026-10-07). Нито се броят за незаписани.
_NEWER_STDLIB = frozenset({"tomllib", "__future__"})


def _stdlib() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)  # 3.10+
    return frozenset(names or ()) | _NEWER_STDLIB


_MAX_FILES = 3000


def _py_files(root: Path):
    """.py файловете на проекта — без да се слиза в node_modules/.venv (одит
    2026-10-07: rglob ги обхождаше целите, ~1 s на всеки падащ RUN_CMD)."""
    import os
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                seen += 1
                if seen > _MAX_FILES:
                    return
                yield Path(dirpath) / name


def local_modules(root: Path) -> set[str]:
    """Top-level names the project provides itself (root and src/ layouts)."""
    names: set[str] = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.suffix == ".py":
                names.add(p.stem)
            elif p.is_dir() and p.name not in _SKIP_DIRS and (
                    (p / "__init__.py").is_file() or any(p.glob("*.py"))):
                names.add(p.name)
    # tests/helpers.py imported as `helpers` from tests/test_x.py
    for p in _py_files(root):
        names.add(p.stem)
    return names


def third_party_imports(root: Path) -> dict[str, list[str]]:
    """Top-level third-party module → the files importing it (relative)."""
    root = Path(root)
    std, local = _stdlib(), local_modules(root)
    found: dict[str, list[str]] = {}
    for p in _py_files(root):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module.split(".")[0] in _NAMESPACES:
                    # `from google.cloud import storage` → google.cloud.storage
                    mods.update(f"{node.module}.{a.name}" for a in node.names)
                else:
                    mods.add(node.module)
        for m in mods:
            top = m.split(".")[0]
            if top in std or top in local or top in _DEV_ONLY:
                continue
            parts = m.split(".")
            if m in IMPORT_TO_PIP:
                key = m
            elif top in _NAMESPACES:
                key = ".".join(parts[:3] if len(parts) > 2 and (parts[1] == "cloud" or top == "azure")
                               else parts[:2])
            else:
                key = top
            found.setdefault(key, []).append(p.relative_to(root).as_posix())
    return dict(sorted(found.items()))


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared(root: Path) -> set[str] | None:
    """Normalized names from requirements*.txt and pyproject.toml, or None
    when the project declares its dependencies nowhere."""
    root = Path(root)
    names: set[str] = set()
    sources = 0
    for req in sorted(root.glob("requirements*.txt")):
        sources += 1
        try:
            lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = _REQ_NAME.match(line)
            if m:
                names.add(normalize(m.group(1)))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib  # type: ignore[import-not-found]
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except ImportError:
            data = None
        except (OSError, ValueError):
            data = {}
        if data is not None:
            project = data.get("project", {})
            deps = list(project.get("dependencies", []))
            for extra in project.get("optional-dependencies", {}).values():
                deps += list(extra)
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            deps += [k for k in poetry if k.lower() != "python"]
            if deps or "project" in data or poetry:
                sources += 1
            for d in deps:
                m = _REQ_NAME.match(str(d))
                if m:
                    names.add(normalize(m.group(1)))
    return names if sources else None


def undeclared(root: Path) -> list[tuple[str, str, list[str]]]:
    """(import name, pip name, files) for each third-party import the project
    does not declare. Empty when it declares nothing at all and imports only
    the standard library."""
    have = declared(root) or set()
    out = []
    for mod, files in third_party_imports(root).items():
        pip = pip_name(mod)
        if pip and not (_accepted(mod) & have):
            out.append((mod, pip, files))
    return out


def missing_module_hint(output: str, root: Path, python: str = "python") -> str:
    """A hint for `No module named 'X'` when X is a third-party package that
    simply is not installed — with the right pip name."""
    m = re.search(r"No module named '([\w.]+)'", output)
    if not m:
        return ""
    module = m.group(1)
    top = module.split(".")[0]
    if top in _stdlib() or top in local_modules(Path(root)):
        return ""
    pip = pip_name(module)
    if not pip:
        return ""
    renamed = f" (пакетът се казва `{pip}`, не `{top}`)" if normalize(pip) != normalize(top) else ""
    req = Path(root) / "requirements.txt"
    note = ("и го добави в requirements.txt" if req.is_file() else
            "и създай requirements.txt с него, за да тръгне и на друга машина")
    return (f"[подсказка] `{top}` не е инсталиран{renamed}: "
            f"`{python} -m pip install {pip}` {note}.")
