"""genesis_agent.page_check — браузърната проверка в края на хода.

Логиката (кога се пуска, колко пъти, какво казва) се тества с фалшив браузър;
истинският Chromium — само където има Python с playwright (на лаптопа да, в CI
обикновено не)."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent import page_check as pc


def _write(path: Path) -> str:
    return f"[WRITE_FILE: {path}] ✓ записани 100 символа"


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    (tmp_path / "styles.css").write_text("h1{}", encoding="utf-8")
    return tmp_path


class TestWhatCountsAsAWebWrite:
    def test_write_and_edit_results(self, site) -> None:
        assert pc.written_web_files(_write(site / "index.html")) == [site / "index.html"]
        edit = f"[EDIT_FILE: {site / 'styles.css'}] ✓ {site / 'styles.css'}: 1 замяна"
        assert pc.written_web_files(edit) == [site / "styles.css"]

    def test_failed_writes_and_other_files_do_not(self, site) -> None:
        assert pc.written_web_files(f"[EDIT_FILE: {site / 'index.html'}] ❌ Anchor-ът не е намерен") == []
        assert pc.written_web_files(_write(site / "app.py")) == []

    def test_css_alone_opens_the_index_next_to_it(self, site) -> None:
        assert pc.pages_for({site / "styles.css"}) == [site / "index.html"]


class TestFinalCheck:
    def test_nothing_written_nothing_checked(self) -> None:
        fc = pc.FinalCheck(runner=lambda p: pytest.fail("no browser without web files"))
        fc.observe("[RUN_CMD: pytest -q]  (rc=0)")
        assert not fc.due()
        assert fc.check() == ("", "")

    def test_findings_go_back_to_the_model(self, site) -> None:
        fc = pc.FinalCheck(runner=lambda p: (["компютър: 2 текста с нисък контраст"], ""))
        fc.observe(_write(site / "index.html"))
        note, line = fc.check()
        assert "нисък контраст" in note and "ще проверя пак" in note
        assert "1 находки" in line
        assert not fc.passed

    def test_after_a_fix_it_checks_again_then_stops(self, site) -> None:
        answers = iter([(["h1 1.6:1"], ""), (["h1 2.9:1"], ""), (["never"], "")])
        fc = pc.FinalCheck(runner=lambda p: next(answers))
        fc.observe(_write(site / "index.html"))
        assert fc.check()[0]
        assert not fc.due()  # моделът още не е поправил нищо
        fc.observe(_write(site / "styles.css"))
        note, _ = fc.check()
        assert "финалния отговор" in note  # последният опит казва да приключи
        fc.observe(_write(site / "styles.css"))
        assert not fc.due()  # MAX_RUNS

    def test_a_clean_page_proves_the_browser_check(self, site) -> None:
        fc = pc.FinalCheck(runner=lambda p: ([], ""))
        fc.observe(_write(site / "index.html"))
        note, line = fc.check()
        assert note == ""
        assert "чисто ✓" in line
        assert fc.passed

    def test_no_browser_is_reported_not_forced_on_the_model(self, site) -> None:
        fc = pc.FinalCheck(runner=lambda p: (None, "няма Python с playwright"))
        fc.observe(_write(site / "index.html"))
        note, line = fc.check()
        assert note == ""
        assert "пропусната" in line and "playwright" in line


class TestAgentLoop:
    """Моделът казва „готово“ → страницата се проверява → находките се връщат
    и той продължава, вместо ходът да свърши."""

    def test_the_loop_sends_findings_back_and_continues(self, site, monkeypatch) -> None:
        from genesis_agent import agent_core as ac

        monkeypatch.setattr("genesis_agent.brain.Brain.compact_chat_history",
                            staticmethod(lambda messages, threshold=16, keep_recent=10: messages))
        answers = iter([(["компютър: h1 1.6:1"], ""), ([], "")])
        monkeypatch.setattr(pc, "run", lambda page, shots=None: next(answers))
        tc = [{"id": "1", "function": {"name": "WRITE_FILE", "arguments": "{}"}}]
        replies = [("", tc, "p", "m"), ("Готово.", None, "p", "m"),
                   ("", tc, "p", "m"), ("Оправих контраста.", None, "p", "m")]

        class Core:
            def complete(self, messages):
                return replies.pop(0)

            def remember(self, *a):
                pass

        class Skills:
            def dispatch_tool_call(self, name, args):
                return _write(site / "index.html")

            def parse_and_execute_tools(self, text):
                return []

        core = Core()
        core.skills = Skills()
        shown = []
        messages = ac.run_tool_loop(core, [{"role": "user", "content": "направи сайт"}],
                                    on_assistant=lambda *a: None,
                                    on_tool_result=lambda name, res, extra: shown.append((name, res)))
        notes = [m["content"] for m in messages if m["role"] == "system"]
        assert any("h1 1.6:1" in n for n in notes)
        assert messages[-1]["content"] == "Оправих контраста."
        assert any(name == "проверка в браузър" and "чисто" in res for name, res in shown)
        assert replies == []


@pytest.mark.skipif(not pc.browser_python(), reason="няма Python с playwright")
def test_a_real_browser_measures_a_bad_page(tmp_path) -> None:
    """Същите грешки като в пробата от 2026-09-28, в истински Chromium."""
    (tmp_path / "index.html").write_text("""<!doctype html><html lang="bg"><head>
<meta name="viewport" content="width=device-width, initial-scale=1"><title>t</title>
<style>
  body { margin: 0; font-family: sans-serif }
  .hero { position: relative; height: 300px }
  .hero-bg { position: absolute; inset: 0; background: linear-gradient(135deg, #a8e6cf, #7fd5b2) }
  .hero h1 { position: relative; color: #fff; margin: 0; padding: 40px }
  .wide { width: 700px }
</style></head><body>
<header><a href="#">Бяла мечка</a></header>
<section class="hero"><div class="hero-bg"></div><h1>Добре дошли</h1></section>
<p class="wide">Широк ред</p>
<svg viewBox="0 0 100 20" width="300"><text x="0" y="15">Карта (SVG placeholder)</text></svg>
<div style="width: 260px; height: 170px; background: #f1ede6">🛏️</div>
</body></html>""", encoding="utf-8")
    findings, why = pc.run(tmp_path / "index.html")
    assert findings is not None, why
    text = " | ".join(findings)
    assert "нисък контраст" in text and "Добре дошли" in text
    assert "по-широка от екрана" in text
    assert "placeholder" in text
    assert "почти без съдържание" in text
