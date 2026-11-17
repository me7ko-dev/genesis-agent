"""Наръчникът на оператора: кратки бележки, които чатът подава по темата."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent import handbook


def _note(folder: Path, name: str, triggers: list[str], body: str, title: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"title: {title or name}", "triggers:"]
    lines += [f"- {t}" for t in triggers]
    lines += ["---", body]
    (folder / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture()
def book(tmp_path, monkeypatch):
    folder = tmp_path / "handbook"
    monkeypatch.setenv("GENESIS_HANDBOOK_DIR", str(folder))
    handbook.reload()
    yield folder
    handbook.reload()


def test_no_folder_gives_nothing(book):
    assert handbook.handbook_context("качи сайта в cloudflare") == ""


def test_note_is_found_by_whole_trigger(book):
    _note(book, "deploy", ["качване в cloudflare"], "Командата е `npx wrangler deploy`.")
    handbook.reload()
    out = handbook.handbook_context("как беше качването в Cloudflare на сайта?")
    assert out.startswith("## Бележка от наръчника: deploy")
    assert "npx wrangler deploy" in out


def test_partial_trigger_is_not_enough(book):
    # „сайт“ сам не е „качване в cloudflare“: едната дума е случайно съвпадение.
    _note(book, "deploy", ["качване в cloudflare"], "Командата е `npx wrangler deploy`.")
    handbook.reload()
    assert handbook.handbook_context("cloudflare ми прати имейл за сметката") == ""


def test_single_word_trigger_matches(book):
    _note(book, "shop", ["магазин"], "Папка Projects/shop.")
    handbook.reload()
    assert "Projects/shop" in handbook.handbook_context("пусни тестовете на магазина")


def test_at_most_two_notes_and_budget(book):
    for i in range(4):
        _note(book, f"n{i}", ["pytest пада"], "ред\n" * 50)
    _note(book, "big", ["pytest пада"], "x" * 9000)
    handbook.reload()
    out = handbook.handbook_context("pytest пада с грешка")
    assert out.count("## Бележка от наръчника:") <= 2
    assert len(out) <= handbook.MAX_CHARS + 200


def test_long_note_is_cut_not_dropped(book):
    _note(book, "big", ["голяма бележка"], "важно в началото\n" + "y" * 9000)
    handbook.reload()
    out = handbook.handbook_context("отвори голямата бележка")
    assert "важно в началото" in out
    assert len(out) <= handbook.MAX_CHARS + 200


def test_more_specific_trigger_wins(book):
    _note(book, "general", ["тест"], "общо")
    _note(book, "specific", ["тест на магазин"], "точно")
    handbook.reload()
    out = handbook.handbook_context("пусни тест на магазин")
    assert out.index("specific") < out.index("general")


def test_broken_note_does_not_stop_the_rest(book):
    book.mkdir(parents=True)
    (book / "bad.md").write_text("---\ntriggers: [незатворен\n---\nтяло", encoding="utf-8")
    (book / "nofm.md").write_text("само текст без глава", encoding="utf-8")
    _note(book, "ok", ["ключ за groq"], "В ~/.genesis/.env")
    handbook.reload()
    assert "~/.genesis/.env" in handbook.handbook_context("къде е ключът за groq")


def test_search_lists_matches_for_the_tool(book):
    _note(book, "deploy", ["качване в cloudflare"], "wrangler", title="Качване в Cloudflare")
    _note(book, "egn", ["егн"], "правила")
    handbook.reload()
    hits = handbook.search("cloudflare качване")
    assert [h["name"] for h in hits] == ["deploy"]
    assert hits[0]["title"] == "Качване в Cloudflare"
    assert handbook.read("deploy").strip() == "wrangler"
    assert handbook.read("../../secret") == ""


def test_injected_note_is_not_operator_text(book):
    # Паметта учи предпочитания само от казаното от оператора (одит 2026-10-07).
    from genesis_agent.workspace_memory import operator_text
    _note(book, "deploy", ["качване в cloudflare"], "никога не качвай без тест")
    handbook.reload()
    said = "направи качване в cloudflare"
    assert operator_text(f"{said}\n\n{handbook.handbook_context(said)}") == said


def test_note_goes_into_a_conversation_once(book):
    _note(book, "deploy", ["качване в cloudflare"], "wrangler")
    handbook.reload()
    said = "направи качване в cloudflare"
    text, names = handbook.fresh_notes(said, [])
    assert names == ["deploy"]
    assert handbook.fresh_notes("пак качване в cloudflare", [f"{said}\n\n{text}"]) == ("", [])
