"""Одит 2026-10-11 (вълна 4): `genesis export` — истински ключове, които
заминаваха към клиента, Firebase изключението за целия файл и вложените
хранилища (submodule), които носеха игнорираното си или изпадаха цели.

Ключовете в теста са сглобени от части, за да не стои цял ключ в изходния код."""
from __future__ import annotations

import os
import subprocess

import pytest

from genesis_agent import deliver

_HEX16 = "0123456789abcdef"


def _git(root, *a):
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(root), *a],
                   check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _rels(root, keep):
    return sorted(p.relative_to(root).as_posix() for p in keep)


def _ship(tmp_path, files: dict[str, str]):
    root = tmp_path / "app"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    return _rels(root, keep), withheld


# ── B1: истински ключове без цифра или главна буква ──────────────────────────

_REAL_KEYS = {
    "deepseek": "sk-" + _HEX16 * 2,
    "openrouter": "sk-" + "or-v1-" + _HEX16 * 4,
    "stripe secret": "sk_" + "live_" + "a1B2c3D4" * 3,
    "stripe restricted": "rk_" + "live_" + "a1B2c3D4" * 3,
    "cerebras": "csk-" + "a1b2c3d4e5" * 4,
    "hugging face": "hf_" + "AbCdEfGhIj" * 3 + "KlMn",
    "aws without a digit": "AKIA" + "QWERTYUIOPASDFGH",
    "gemini 35": "AIza" + "SyB1x2Y3z4W5v6U7t8S9r0Q1p2O3n4M5abc",
}


@pytest.mark.parametrize("name", sorted(_REAL_KEYS))
def test_a_real_key_is_withheld(tmp_path, name):
    shipped, withheld = _ship(tmp_path, {
        "main.py": "print('hi')\n",
        "cfg.py": f'DEBUG = True\nAPI_KEY = "{_REAL_KEYS[name]}"\n'})
    assert shipped == ["main.py"], name
    assert len(withheld) == 1 and withheld[0].startswith("cfg.py") and "ред 2" in withheld[0], name
    assert _REAL_KEYS[name] not in withheld[0]


_PLACEHOLDERS = (
    "sk-your-api-key-here", "sk-" + "your-key-here", "AIza" + "Sy" + "X" * 33,
    "AKIA" + "IOSFODNN7EXAMPLE", "ghp_" + "x" * 36, "hf_" + "x" * 34, "pk_" + "test_" + "a1B2" * 6,
    "sk-" + "proj-" + "REPLACE_ME_" + "a" * 40, "csk-" + "placeholder" + "0" * 30,
)


def test_placeholders_and_class_names_still_ship(tmp_path):
    text = "\n".join(f'KEY_{i} = "{p}"' for i, p in enumerate(_PLACEHOLDERS)) + "\n"
    shipped, withheld = _ship(tmp_path, {
        ".env.example": text,
        "index.html": '<div class="sk-fading-circle sk-circle12 sk-chasing-dots-container"></div>\n'})
    assert shipped == [".env.example", "index.html"] and withheld == []


# ── B2/B3: Firebase web конфигът е публичен — само той ──────────────────────

_FB_KEY = "AIza" + "SyF1r2E3b4A5s6E7k8E9y0P1u2B3l4I5c6x"     # 35 след AIza
_GEMINI = "AIza" + "SyG3m4I5n6I7k8E9y0S1e2C3r4E5t6Z7q8w"
_FB_CONFIG = ('const firebaseConfig = {\n  apiKey: "' + _FB_KEY + '",\n'
              '  authDomain: "shop-1a2b.firebaseapp.com",\n  projectId: "shop-1a2b",\n};\n')


def test_the_firebase_web_config_ships(tmp_path):
    shipped, withheld = _ship(tmp_path, {"fb.js": _FB_CONFIG})
    assert shipped == ["fb.js"] and withheld == []


def test_a_gemini_key_next_to_a_firebase_config_is_withheld(tmp_path):
    shipped, withheld = _ship(tmp_path, {
        "web.js": _FB_CONFIG + f'const GEMINI_API_KEY = "{_GEMINI}";\n',
        "ai.js": _FB_CONFIG + f'const model = new GoogleGenerativeAI({{ apiKey: "{_GEMINI}" }});\n',
        "g.py": f'# Firebase: authDomain е в web.js\nGEMINI = "{_GEMINI}"\n',
        "main.py": "print('hi')\n"})
    assert shipped == ["main.py"]
    by_file = {w.split(" ", 1)[0]: w for w in withheld}
    assert "ред 6" in by_file["web.js"] and "ред 6" in by_file["ai.js"] and "ред 2" in by_file["g.py"]


def test_a_legacy_short_gemini_key_in_a_firebase_file_is_withheld(tmp_path):
    short = "AIza" + "SyB1x2Y3z4W5v6U7t8S9r0Q1p2O3n4M5"     # 32 — само notifier образецът
    shipped, _ = _ship(tmp_path, {"web.js": _FB_CONFIG + f'const GEMINI = "{short}";\n'})
    assert shipped == []


def test_firebase_with_a_template_literal_and_flutterfire_options_ship(tmp_path):
    template = ('const firebaseConfig = {\n  apiKey: "' + _FB_KEY + '",\n'
                '  storageBucket: `${env}-shop.appspot.com`,\n'
                '  authDomain: "shop.firebaseapp.com",\n};\n')
    dart = ("class DefaultFirebaseOptions {\n  static FirebaseOptions get currentPlatform {\n"
            "    return android;\n  }\n\n"
            "  static const FirebaseOptions android = FirebaseOptions(\n"
            f"    apiKey: '{_FB_KEY}',\n    appId: '1:123:android:abc',\n"
            "    messagingSenderId: '123456789',\n    projectId: 'shop-1a2b',\n  );\n}\n")
    shipped, withheld = _ship(tmp_path, {"t.js": template, "lib/firebase_options.dart": dart})
    assert shipped == ["lib/firebase_options.dart", "t.js"] and withheld == []


# ── B4: вложени хранилища и submodule ───────────────────────────────────────

def _repo(path, files: dict[str, str], *, add: tuple[str, ...] = ()):
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (path / rel).write_text(text, encoding="utf-8")
    _git(path, "init", "-q")
    if add:
        _git(path, "add", *add)
        _git(path, "commit", "-qm", "init")


def _with_submodule(tmp_path, at: str):
    up = tmp_path / "upstream"
    _repo(up, {"u.py": "U = 1\n", ".gitignore": "*.log\n"}, add=("u.py", ".gitignore"))
    root = tmp_path / "outer"
    _repo(root, {"m.py": "import u\n"}, add=("m.py",))
    _git(root, "submodule", "add", "-q", str(up), at)
    _git(root, "commit", "-qm", "sub")
    (root / at / "run.log").write_text("token=abc\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("at", ["lib", "vendor/u"])
def test_a_submodule_ships_without_its_ignored_files_or_its_git_pointer(tmp_path, at):
    root = _with_submodule(tmp_path, at)
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == sorted([".gitmodules", f"{at}/.gitignore", f"{at}/u.py", "m.py"])
    assert f"{at}/run.log ({deliver._IGNORED})" in withheld
    assert not any(".git " in w or w.endswith("/.git") for w in withheld)


def test_an_untracked_nested_repo_keeps_its_ignored_files_back(tmp_path):
    root = tmp_path / "outer"
    _repo(root, {"m.py": "x = 1\n"}, add=("m.py",))
    _repo(root / "sub", {"s.py": "S = 1\n", ".gitignore": "server.log\n", "server.log": "x\n"})
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["m.py", "sub/.gitignore", "sub/s.py"]
    assert f"sub/server.log ({deliver._IGNORED})" in withheld


def test_a_nested_repo_that_ignores_everything_is_not_shipped_whole(tmp_path):
    root = tmp_path / "outer"
    _repo(root, {"m.py": "x = 1\n"}, add=("m.py",))
    _repo(root / "sub", {".gitignore": "*\n", "db.sqlite3": "x\n"})
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["m.py"]
    assert f"sub/ ({deliver._IGNORED})" in withheld
