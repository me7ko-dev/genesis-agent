"""Преглед на вълна 1 (2026-10-11): `genesis export` и `genesis fix` — какво
задържаше прекалено много или отказваше напразно."""
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


# ── export: образец на ключ не е ключ ───────────────────────────────────────

def test_export_keeps_placeholders_spinkit_css_and_firebase_web_config(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "README.md").write_text("# Сайт\n\n```bash\nexport OPENAI_API_KEY=sk-your-api-key-here\n```\n",
                                    encoding="utf-8")
    (root / ".env.example").write_text("OPENAI_API_KEY=sk-your-key-here\n"
                                       "GROQ_API_KEY=gsk_XXXXXXXXXXXXXXXXXXXXXXXX\n"
                                       "AWS_ACCESS_KEY_ID=AKIA" + "IOSFODNN7EXAMPLE\n", encoding="utf-8")
    (root / "index.html").write_text('<div class="sk-fading-circle"><div class="sk-circle12 sk-circle">'
                                     "</div></div>\n", encoding="utf-8")
    (root / "spinkit.css").write_text(".sk-fading-circle .sk-circle12:before { transform: rotate(330deg); }\n"
                                      ".sk-chasing-dots-container-wrapper { margin: 0; }\n", encoding="utf-8")
    (root / "firebase.js").write_text('const firebaseConfig = {\n  apiKey: "AIza' + "SyB1x2Y3z4W5v6U7t8S9r0Q1p2O3n4M5\",\n"
                                      '  authDomain: "shop-1a2b.firebaseapp.com",\n};\n', encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == [".env.example", "README.md", "firebase.js", "index.html", "spinkit.css"]
    assert withheld == []


def test_export_still_withholds_a_real_key_after_a_placeholder(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    real = "sk-proj-" + "Ab3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1eF" * 2   # сглобен — не е цял в кода
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "config.py").write_text(f'# пример: sk-your-api-key-here\nOPENAI_API_KEY = "{real}"\n',
                                    encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["main.py"]
    assert len(withheld) == 1 and "config.py" in withheld[0] and "ред 2" in withheld[0]


# ── export: вложено хранилище / submodule (gitlink) ─────────────────────────

def test_export_keeps_a_committed_nested_repo(tmp_path):
    root = tmp_path / "outer"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (root / "main.py").write_text("import lib.util\n", encoding="utf-8")
    (lib / "util.py").write_text("X = 1\n", encoding="utf-8")
    _git(lib, "init", "-q")
    _git(lib, "add", "util.py")
    _git(lib, "commit", "-qm", "lib")
    _git(root, "init", "-q")
    _git(root, "add", "main.py", "lib")      # `lib` влиза като gitlink (160000)
    _git(root, "commit", "-qm", "outer")
    keep, withheld = deliver.project_files(root)
    assert _rels(root, keep) == ["lib/util.py", "main.py"]
    assert not any(w.startswith("lib") for w in withheld)


# ── export без git: `**/` пасва и на нула папки, както в git ─────────────────

def test_gitignore_double_star_matches_at_the_top_too(tmp_path):
    root = tmp_path / "p"
    (root / "sub").mkdir(parents=True)
    (root / ".gitignore").write_text("**/*.log\n/only_top.txt\n", encoding="utf-8")
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    for f in ("app.log", "sub/app.log", "only_top.txt", "sub/only_top.txt"):
        (root / f).write_text("x\n", encoding="utf-8")
    keep, _ = deliver.project_files(root)
    assert _rels(root, keep) == [".gitignore", "main.py", "sub/only_top.txt"]


# ── fix: описание в кавички, което започва с `--`, е текст ──────────────────

@pytest.fixture
def fake_repair(monkeypatch):
    seen: dict = {}

    def repair(project, task, **kw):
        seen.update(project=project, task=task, **kw)
        return repo_agent.RepairOutcome(True, "ok")
    monkeypatch.setattr(repo_agent, "repair", repair)
    return seen


def test_fix_quoted_description_starting_with_dashes_is_the_task(fake_repair):
    assert cli.main(["fix", "./proj", "--verbose flag is ignored"]) == 0
    assert fake_repair["task"] == "--verbose flag is ignored"
