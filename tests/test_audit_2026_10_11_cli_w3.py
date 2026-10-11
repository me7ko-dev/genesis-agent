"""Преглед на вълна 2 (2026-10-11): `genesis export` пускаше истински ключове
и игнорирани файлове на submodule; `genesis fix` приемаше описание за път."""
from __future__ import annotations

import os
import subprocess

import pytest

from genesis_agent import cli, deliver, repo_agent


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _rels(root, keep):
    return sorted(p.relative_to(root).as_posix() for p in keep)


# ── export: DeepSeek ключът е само с малки букви ────────────────────────────

def test_export_withholds_a_lowercase_hex_sk_key(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    real = "sk-" + "3f9a0c1b7d2e4f6a" * 2              # DeepSeek: 32 малки hex
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "llm.py").write_text(f'DEEPSEEK_API_KEY = "{real}"\n', encoding="utf-8")
    (root / "spin.css").write_text(".sk-circle12 { x: 1 } .sk-fading-circle { y: 2 }\n", encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["main.py", "spin.css"]
    assert len(withheld) == 1 and "llm.py" in withheld[0]


# ── export: Firebase изключението е за apiKey в конфига, не за файла ────────

_FIREBASE = ('const firebaseConfig = {\n  apiKey: "AIza' + 'SyB1x2Y3z4W5v6U7t8S9r0Q1p2O3n4M5",\n'
             '  authDomain: "shop-1a2b.firebaseapp.com",\n};\n')


def test_export_withholds_a_gemini_key_next_to_a_firebase_config(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    gemini = "AIza" + "SyQ9w8E7r6T5y4U3i2O1p0A9s8D7f6G5"
    (root / "web.js").write_text(_FIREBASE + f'const GEMINI_API_KEY = "{gemini}";\n', encoding="utf-8")
    (root / "ai.js").write_text(_FIREBASE + f'const ai = new GoogleGenerativeAI({{ apiKey: "{gemini}" }});\n',
                                encoding="utf-8")
    (root / "fb.js").write_text(_FIREBASE, encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["fb.js"]
    assert sorted(w.split(" ")[0] for w in withheld) == ["ai.js", "web.js"]
    assert all("ред 5" in w for w in withheld)


# ── export: игнорираното в комитнат submodule остава извън архива ───────────

def test_export_respects_the_gitignore_of_a_committed_nested_repo(tmp_path):
    root = tmp_path / "outer"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (root / "main.py").write_text("import lib.util\n", encoding="utf-8")
    (lib / "util.py").write_text("X = 1\n", encoding="utf-8")
    (lib / ".gitignore").write_text("*.log\ndb.sqlite3\n", encoding="utf-8")
    _git(lib, "init", "-q")
    _git(lib, "add", "util.py", ".gitignore")
    _git(lib, "commit", "-qm", "lib")
    (lib / "server.log").write_text("token=abc\n", encoding="utf-8")
    (lib / "db.sqlite3").write_bytes(b"SQLite format 3\0")
    (lib / "new.py").write_text("Y = 2\n", encoding="utf-8")       # неследен, но не игнориран
    _git(root, "init", "-q")
    _git(root, "add", "main.py", "lib")      # `lib` влиза като gitlink (160000)
    _git(root, "commit", "-qm", "outer")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["lib/.gitignore", "lib/new.py", "lib/util.py", "main.py"]
    assert any(w.startswith("lib/server.log") for w in withheld)
    assert any(w.startswith("lib/db.sqlite3") for w in withheld)


def test_export_uninitialised_submodule_dir_is_not_an_error(tmp_path):
    root = tmp_path / "outer"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    (lib / "util.py").write_text("X = 1\n", encoding="utf-8")
    _git(lib, "init", "-q")
    _git(lib, "add", "util.py")
    _git(lib, "commit", "-qm", "lib")
    _git(root, "init", "-q")
    _git(root, "add", "main.py", "lib")
    _git(root, "commit", "-qm", "outer")
    # Без `.git` вътре (както при неклониран submodule): решава външният git.
    import shutil
    shutil.rmtree(lib / ".git")
    keep, _ = deliver.project_files(root)
    assert _rels(root, keep) == ["lib/util.py", "main.py"]


# ── fix: описание с `--` и интервал преди пътя е грешка, не път ─────────────

@pytest.fixture
def fake_repair(monkeypatch):
    seen: dict = {}

    def repair(project, task, **kw):
        seen.update(project=project, task=task, **kw)
        return repo_agent.RepairOutcome(True, "ok")
    monkeypatch.setattr(repo_agent, "repair", repair)
    return seen


def test_fix_quoted_dashes_description_before_the_path_is_refused(fake_repair, capsys):
    assert cli.main(["fix", "--verbose flag is ignored", "./proj"]) == 2
    assert "Непозната опция" in capsys.readouterr().out
    assert fake_repair == {}


def test_fix_quoted_dashes_description_after_the_path_still_works(fake_repair):
    assert cli.main(["fix", "./proj", "--verbose flag is ignored"]) == 0
    assert fake_repair["project"] == "./proj" and fake_repair["task"] == "--verbose flag is ignored"
