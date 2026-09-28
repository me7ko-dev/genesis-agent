"""genesis_agent.web_check — бележката след запис на .html/.css/.js.

Случаите идват от живата задача от 2026-09-28 (сайт за къща за гости): това,
което там беше сгрешено, трябва да се види; обичайната чиста страница не бива
да носи бележка, иначе моделът се учи да я прескача."""
from __future__ import annotations

import shutil

import pytest

import genesis_skills as gs
from genesis_agent.web_check import check_css, check_html, web_note

CLEAN = """<!DOCTYPE html>
<html lang="bg">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Бяла мечка</title>
  <meta name="description" content="Къща за гости в Широка лъка">
  <meta property="og:image" content="https://byalamechka.bg/og.jpg">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <a href="#main" class="skip">Към съдържанието</a>
  <main id="main">
    <p>Стая „Мечка“ — 65 € на нощ<p>Закуска включена
    <img src="bear.svg" alt="Мечка">
    <svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>
    <form><label for="name">Име</label><input id="name" required>
      <button type="submit">Изпрати</button></form>
  </main>
  <script src="script.js" defer></script>
</body>
</html>
"""


@pytest.fixture
def site(tmp_path):
    for name in ("styles.css", "script.js", "bear.svg"):
        (tmp_path / name).write_text("", encoding="utf-8")
    return tmp_path


def test_a_clean_page_has_no_findings(site) -> None:
    assert check_html(site / "index.html", CLEAN) == []


def test_what_the_live_run_got_wrong_is_reported(site) -> None:
    page = (CLEAN.replace("https://byalamechka.bg/og.jpg", "https://example.com/og-image.jpg")
                 .replace("65 €", "120 лв"))
    found = " | ".join(check_html(site / "index.html", page))
    assert "example.com" in found
    assert "лева" in found and "евро" in found


def test_every_leva_price_is_listed_at_once(site) -> None:
    """С една цена в бележката моделът оправяше по една стая на рунд."""
    page = CLEAN.replace("65 €", "120 лв").replace("Закуска включена", "Суита — 180 лв.")
    found = " | ".join(check_html(site / "index.html", page))
    assert "2 цени в лева" in found
    assert "120 лв" in found and "180 лв" in found


def test_fixing_the_findings_is_confirmed_once(site) -> None:
    """Без изрично „чисто“ моделът продължаваше да проверява до тавана."""
    page = site / "index.html"
    page.write_text(CLEAN.replace("65 €", "120 лв"), encoding="utf-8")
    assert "лева" in web_note(page)
    page.write_text(CLEAN, encoding="utf-8")
    assert "вече е чисто" in web_note(page)
    assert web_note(page) == ""


def test_a_missing_local_file_and_a_dead_anchor(site) -> None:
    page = CLEAN.replace('href="styles.css"', 'href="style.css"').replace('href="#main"', 'href="#top"')
    found = " | ".join(check_html(site / "index.html", page))
    assert "style.css" in found
    assert "#top" in found


def test_accessibility_basics(site) -> None:
    page = (CLEAN.replace('<html lang="bg">', "<html>").replace(' alt="Мечка"', "")
                 .replace('<label for="name">Име</label>', ""))
    found = " | ".join(check_html(site / "index.html", page))
    assert "lang" in found
    assert "без alt" in found
    assert "name" in found


def test_an_unclosed_element_is_reported(site) -> None:
    page = CLEAN.replace("</main>", "")
    found = " | ".join(check_html(site / "index.html", page))
    assert "<main>" in found


def test_duplicate_ids(site) -> None:
    page = CLEAN.replace('<main id="main">', '<main id="main"><div id="main"></div>')
    assert any("повтарящи се id" in f for f in check_html(site / "index.html", page))


def test_a_price_in_euro_carries_no_leva_note(site) -> None:
    assert not any("лева" in f for f in check_html(site / "index.html", CLEAN))


def test_css_brace_balance() -> None:
    assert check_css("a { color: red; }\n/* { */ b::after { content: '}'; }") == []
    assert check_css("a { color: red;\nb { color: blue; }")


@pytest.mark.skipif(shutil.which("node") is None, reason="няма node")
def test_a_js_syntax_error_is_reported(tmp_path) -> None:
    bad = tmp_path / "script.js"
    bad.write_text("document.addEventListener('DOMContentLoaded', () => {\n  const x = ;\n});\n",
                   encoding="utf-8")
    assert "JS не се парсва" in web_note(bad)
    bad.write_text("const x = 1;\n", encoding="utf-8")
    assert "вече е чисто" in web_note(bad)


def test_other_files_are_left_alone(tmp_path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("120 лв", encoding="utf-8")
    assert web_note(p) == ""


def test_write_file_carries_the_note(tmp_path) -> None:
    gs.set_workspace(tmp_path)
    try:
        page = CLEAN.replace("https://byalamechka.bg/og.jpg", "https://example.com/og.jpg")
        out = gs._tool_write_file(str(tmp_path / "index.html"), page)
    finally:
        gs.set_workspace(gs._PROJECT_ROOT)
    assert "✓ записани" in out
    assert "[уеб проверка]" in out and "example.com" in out
