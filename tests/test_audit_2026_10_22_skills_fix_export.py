"""Одит 2026-10-22 (skills / `genesis fix` / `genesis export`): ключ, който
излиза в GENESIS_REPORT.md, макар файлът му да е задържан; „✅ Поправено“ с
код 0, когато моделът е отслабил или пропуснал самия тест; ключ след първите
256 KB на файла; познати форми токени, които минаваха; едно умение с
развален frontmatter, което гасеше цялото знание в чата.

Ключовете са сглобени от части, за да не стои цял ключ в изходния код."""
from __future__ import annotations

import base64
import json
import os
import sys
import zipfile

import pytest

from genesis_agent import deliver, repo_agent
from genesis_agent import skill_loader as sl

_OPENAI = "sk-" + "proj-" + "Ab3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5oQ7rS9uW1yZ3"
_PYTEST_CMD = f'"{sys.executable}" -m pytest -q'


def _write(root, files: dict[str, str]):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _zip_text(d) -> str:
    with zipfile.ZipFile(d.zip_path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


# ── 1: отчетът носеше ключа на задържания файл ───────────────────────────────

def test_a_withheld_key_never_reaches_the_report(tmp_path) -> None:
    # (а) pytest показва ключа от задържания .env в изхода на падащия тест.
    shop = _write(tmp_path / "shop", {
        ".env": f"OPENAI_API_KEY={_OPENAI}\nDB_PASSWORD=Tr0ub4dor-3xyz-Qq\n",
        "config.py": (
            "from pathlib import Path\n"
            "def load():\n"
            "    out = {}\n"
            "    for line in (Path(__file__).parent / '.env').read_text().splitlines():\n"
            "        k, _, v = line.partition('=')\n"
            "        out[k] = v\n"
            "    return out\n"),
        "tests/test_config.py": (
            "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
            "from config import load\n\n"
            "def test_key_is_live():\n"
            "    assert load()['OPENAI_API_KEY'].startswith('sk-live')\n\n"
            "def test_db_password():\n"
            "    assert load()['DB_PASSWORD'] == 'x'\n"),
        "pyproject.toml": "[project]\nname='shop'\nversion='0'\n"})
    d = deliver.export(shop, tmp_path / "shop.zip", run_tests=True)
    assert ".env" in d.withheld and d.tests_passed is False
    shipped = _zip_text(d)
    assert "❌ падат" in d.report
    assert _OPENAI not in shipped and _OPENAI not in d.report
    assert "Tr0ub4dor-3xyz-Qq" not in shipped

    # (б) README с ключа е задържан, но разделът му „Quick start“ влизаше в отчета.
    app = _write(tmp_path / "app", {
        "main.py": "print('hi')\n",
        "README.md": ("# App\n\n## Quick start\n\n```bash\npip install -r requirements.txt\n"
                      f"export OPENAI_API_KEY={_OPENAI}\npython main.py\n```\n")})
    d = deliver.export(app, tmp_path / "app.zip", run_tests=False)
    assert any(w.startswith("README.md") for w in d.withheld)
    assert "python main.py" in d.report            # разделът остава, само без ключа
    assert _OPENAI not in _zip_text(d) and _OPENAI not in d.report


# ── 2: „поправено“ с отслабен или пропуснат тест ─────────────────────────────

_CALC = "def add(a, b):\n    return a - b\n"
_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


@pytest.mark.parametrize("edit, fixed", [
    ({"path": "tests/test_calc.py", "old": "assert add(2, 2) == 4", "new": "assert add(2, 2) == 0"},
     False),
    ({"path": "tests/test_calc.py", "old": "def test_add():",
      "new": "import pytest\n\n\n@pytest.mark.skip(reason='flaky')\ndef test_add():"}, False),
    ({"path": "calc.py", "old": "return a - b", "new": "return a + b"}, True),
], ids=["assert-changed", "skip-added", "real-fix"])
def test_weakening_the_test_is_not_a_fix(tmp_path, monkeypatch, edit, fixed) -> None:
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    root = _write(tmp_path / "proj", {
        "calc.py": _CALC, "tests/test_calc.py": _TEST,
        "pyproject.toml": "[project]\nname='p'\nversion='0'\n"})
    step = {"n": 0}

    class _Brain:
        def __init__(self, *a, **k) -> None:
            pass

        def complete(self, messages, tools=None):
            step["n"] += 1
            if step["n"] == 1:
                calls = [{"id": "1", "function": {"name": "READ_FILE",
                                                  "arguments": json.dumps({"path": edit["path"]})}}]
            elif step["n"] == 2:
                calls = [{"id": "2", "function": {"name": "EDIT_FILE", "arguments": json.dumps(edit)}}]
            else:
                return type("R", (), {"raw_text": "Готово.", "tool_calls": None})()
            return type("R", (), {"raw_text": "", "tool_calls": calls})()

    monkeypatch.setattr(repo_agent, "Brain", _Brain)
    out = repo_agent.repair(str(root), "add връща грешен резултат", test_command=_PYTEST_CMD,
                            max_rounds=4, on_status=lambda m: None)
    assert out.tests_after is not None and out.tests_after.passed, "редакцията трябва да е минала"
    assert out.success is fixed, out.summary
    if not fixed:
        assert "Поправено" not in out.summary and "тест" in out.summary


# ── 3: ключ след първите 256 KB ──────────────────────────────────────────────

def test_a_key_after_the_first_256_kb_is_withheld(tmp_path) -> None:
    img = base64.b64encode(os.urandom(300_000)).decode()
    nb = {"cells": [
        {"cell_type": "code", "source": ["plot()"],
         "outputs": [{"output_type": "display_data", "data": {"image/png": img}}]},
        {"cell_type": "code", "source": [f'client = OpenAI(api_key="{_OPENAI}")'], "outputs": []}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    root = _write(tmp_path / "proj", {
        "main.py": "print('hi')\n",
        "analysis.ipynb": json.dumps(nb),
        "big.py": "x = 1\n" * 60_000 + "KEY = '''\n-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE\n'''\n"})
    assert (root / "analysis.ipynb").stat().st_size > deliver._SCAN_BYTES
    keep, withheld = deliver.project_files(root)
    assert [p.name for p in keep] == ["main.py"]
    assert any(w.startswith("analysis.ipynb (съдържа ключ sk-p") for w in withheld)
    assert "big.py" in withheld


# ── 4: GitLab, npm, Telegram, SendGrid и парола в адрес ──────────────────────

_TG = "7412589630:" + "AAHf3kZx9Qw2LmN8pRt5vYb1cDe6GhJk4Ls"


def test_common_credential_shapes_are_withheld(tmp_path) -> None:
    root = _write(tmp_path / "proj", {
        "main.py": "print('hi')\n",
        "bot.py": f'TELEGRAM_TOKEN = "{_TG}"\n',
        "notify.py": f'URL = "https://api.telegram.org/bot{_TG}/sendMessage"\n',
        "gitlab_ci.py": 'GITLAB_TOKEN = "glpat-' + 'Xy7Kp2Qm9Rt4Vw1Zb8Nc' + '"\n',
        "publish.js": 'const NPM_TOKEN = "npm_' + 'aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5' + '";\n',
        "mail.py": ('SENDGRID_API_KEY = "SG.' + 'aBcDeFgHiJkLmNoPqRsTuV' + '.'
                    + 'wXyZaBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLm' + '"\n'),
        "requirements.txt": ("requests==2.32.3\n"
                             "git+https://deploy:" + "Zq8vR2mXk7LpT4wN" + "@gitlab.example.com/acme/lib.git\n"),
        ".gitmodules": ('[submodule "vendor/x"]\n\tpath = vendor/x\n'
                        "\turl = https://ci-bot:" + "Hk29fLq7Xw3Rt8Vz" + "@git.acme.bg/acme/x.git\n"),
        # Образците и препратките пак заминават.
        "docker-compose.yml": ("services:\n  web:\n    environment:\n"
                               "      DATABASE_URL: postgres://${DB_USER}:${DB_PASS}@db/app\n"
                               "      CACHE_URL: redis://user:password@cache:6379\n"
                               "      DEV_DB: postgres://postgres:postgres@db/dev\n"),
        "README.md": ("Пример: `https://user:<password>@host/x`, "
                      "`TELEGRAM_TOKEN=123456789:" + "AA" + "x" * 33 + "`\n"),
    })
    keep, withheld = deliver.project_files(root)
    shipped = sorted(p.relative_to(root).as_posix() for p in keep)
    assert shipped == ["README.md", "docker-compose.yml", "main.py"], withheld
    by_file = {w.split(" ", 1)[0]: w for w in withheld}
    assert set(by_file) == {"bot.py", "notify.py", "gitlab_ci.py", "publish.js", "mail.py",
                            "requirements.txt", ".gitmodules"}
    assert "ред 2" in by_file["requirements.txt"] and "Zq8vR2mXk7LpT4wN" not in by_file["requirements.txt"]


# ── 5: развален frontmatter на едно умение гасеше знанието ──────────────────

def test_a_skill_with_list_frontmatter_does_not_hide_the_good_one(tmp_path, monkeypatch) -> None:
    d = tmp_path / "skills"
    good = ("---\nname: egn_rules\n---\n## Python Код\n```python\ndef egn_ok(x):\n"
            "    return True\nassert egn_ok(1)\nprint('OK')\n```\n")
    bad = "---\n- broken\n- frontmatter\n---\n## Python Код\n```python\nprint('OK')\n```\n"
    _write(d, {"egn_rules.md": good, "egn_bad.md": bad})
    trig = ["егн проверка контролна цифра"]
    (d / "skills.json").write_text(json.dumps({"skills": [
        {"name": "egn_bad", "file_path": "skills/egn_bad.md", "category": "domain",
         "verified": True, "triggers": trig, "min_score": 3},
        {"name": "egn_rules", "file_path": "skills/egn_rules.md", "category": "domain",
         "verified": True, "triggers": trig, "min_score": 3}]}), encoding="utf-8")
    monkeypatch.setattr(sl, "SKILLS_DIR", d)
    monkeypatch.setattr(sl, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    assert sl.skill_view("egn_bad")["metadata"]["name"] == "egn_bad"
    knowledge = sl.domain_context("егн проверка контролна цифра")
    assert "def egn_ok" in knowledge
