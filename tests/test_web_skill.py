"""Умението web_site_2026 — доказателството, че основата му е „проверена“.

Страница, сглобена САМО от блоковете на умението (HTML скелет + CSS + JS),
трябва да мине и проверката на файловете (web_check), и браузърната проверка
(page_check) без нито една находка. Тъкмо това казва „verified“ в skills.json;
падне ли този тест, ръководството учи модела на грешка."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from genesis_agent import page_check
from genesis_agent.config import SKILLS_DIR
from genesis_agent.skill_loader import domain_context, reload_skills_index
from genesis_agent.web_check import web_note

SKILL = SKILLS_DIR / "web_site_2026.md"
BENCH = Path(__file__).resolve().parent.parent / "bench" / "projects"


def _block(lang: str) -> str:
    m = re.search(rf"```{lang}\n(.*?)\n```", SKILL.read_text(encoding="utf-8"), re.DOTALL)
    assert m, f"няма {lang} блок"
    return m.group(1)


@pytest.fixture
def site(tmp_path) -> Path:
    (tmp_path / "index.html").write_text(_block("html"), encoding="utf-8")
    (tmp_path / "styles.css").write_text(_block("css"), encoding="utf-8")
    (tmp_path / "script.js").write_text(_block("js"), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_index():
    reload_skills_index()
    yield


def test_the_files_pass_web_check(site) -> None:
    for name in ("index.html", "styles.css", "script.js"):
        assert web_note(site / name) == "", name


@pytest.mark.skipif(not page_check.browser_python(), reason="няма Python с playwright")
def test_the_page_passes_the_browser_check(site) -> None:
    findings, why = page_check.run(site / "index.html")
    assert findings is not None, why
    assert findings == []


def test_the_palette_self_test_passes(tmp_path) -> None:
    script = tmp_path / "selftest.py"
    script.write_text(_block("python"), encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stderr


class TestWhenTheGuideIsGiven:
    @pytest.mark.parametrize("query", [
        "Направи ми сайт за пекарна в Пловдив",
        "Направи красив, модерен уеб сайт като за 2026 година — лендинг страница за къща за гости",
        "Build a landing page website for a coffee shop",
    ])
    def test_site_requests_get_it(self, query) -> None:
        ctx = domain_context(query)
        assert "web_site_2026" in ctx.splitlines()[0]
        assert "## Описание" not in ctx  # то е за хората, не за модела
        assert "light-dark(" in ctx

    @pytest.mark.parametrize("query", [
        "Свали цените на горивата от сайта и ги запиши в CSV",   # „сайта“ ≠ „сайт“
        "Направи уеб API с Flask за задачи",
    ])
    def test_other_web_work_does_not(self, query) -> None:
        assert "web_site_2026" not in domain_context(query)

    def test_no_bench_task_gets_it(self) -> None:
        """fuel-prices съдържа „HTML страница“ — затова тези думи не са тригери."""
        for task in sorted(BENCH.glob("*/task.txt")):
            assert "web_site_2026" not in domain_context(task.read_text("utf-8")), task.parent.name
