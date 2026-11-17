"""genesis_agent.research — grounded_research(), бекендът на [RESEARCH: …].

2026-11-17: пренаписан — търсене в няколко търсачки, четене на страниците,
подреждане на откъсите (BM25) и ЕДНО извикване на модела с номерирани
източници (bench_research: 2/20 → 19/20). Търсенето, страниците и моделът са
подменени навсякъде; под тест е логиката: кой текст стига до модела, кои
адреси излизат като източници и че грешка на модела не се представя за отговор.
"""
from __future__ import annotations

from types import SimpleNamespace

import genesis_agent.research as rs


class _FakeBrain:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []
        self.light_calls = 0

    def make(self, light: bool = False, **_kw):
        if light:
            self.light_calls += 1
        return self

    def complete(self, messages, tools=None):
        self.calls.append(messages)
        if not self._replies:
            raise AssertionError("complete() called more times than replies were scripted")
        return SimpleNamespace(raw_text=self._replies.pop(0), code="", tool_calls=None)


def _brain(monkeypatch, replies: list[str]) -> _FakeBrain:
    brain = _FakeBrain(replies)
    monkeypatch.setattr("genesis_agent.brain.Brain", brain.make)
    return brain


def _search(monkeypatch, by_query: dict[str, list[dict]] | list[dict]):
    seen: list[str] = []

    def fake(query, max_results=5, **kw):
        seen.append(query)
        if isinstance(by_query, list):
            return by_query
        return by_query.get(query, [])
    monkeypatch.setattr("genesis_agent.web_search.search", fake)
    return seen


def _pages(monkeypatch, pages: dict[str, str]):
    opened: list[str] = []

    def fake(url):
        opened.append(url)
        return "", pages.get(url, "")
    monkeypatch.setattr(rs, "_fetch_text", fake)
    return opened


MUSALA = [
    {"title": "Мусала — Уикипедия", "url": "https://bg.wikipedia.org/wiki/Мусала", "snippet": "Мусала е връх в Рила."},
    {"title": "Изкачване", "url": "https://planinka.example/musala", "snippet": "Маршрут от Боровец."},
]
MUSALA_PAGES = {
    "https://bg.wikipedia.org/wiki/Мусала": ("Мусала е най-високият връх в България.\n"
                                            "Надморската височина на връх Мусала е 2925 m.\n"
                                            "Името идва от арабски."),
    "https://planinka.example/musala": "Лифтът от Боровец стига до Ястребец. Оттам пътеката е 3 часа.",
}


def test_empty_question_short_circuits_without_searching(monkeypatch) -> None:
    seen = _search(monkeypatch, [])
    assert rs.grounded_research("   ") == "[RESEARCH] Празен въпрос."
    assert seen == []


def test_search_exception_is_reported_not_raised(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("DNS failure")
    monkeypatch.setattr("genesis_agent.web_search.search", _boom)
    out = rs.grounded_research("what is the GIL")
    assert "Грешка при търсене" in out
    assert "DNS failure" in out


def test_no_results_anywhere_is_reported_clearly(monkeypatch) -> None:
    _search(monkeypatch, [])
    _brain(monkeypatch, ["", ""])  # втори кръг: заявки от лекия модел — няма
    out = rs.grounded_research("an obscure question")
    assert "Няма намерени резултати" in out


def test_the_page_text_reaches_the_model_not_only_the_snippet(monkeypatch) -> None:
    """Старият RESEARCH даваше на модела само описанието (≤300 знака) — 18/20
    въпроса бяха „НЕ Е ОТКРИТО“, защото числото е в страницата, не в него."""
    _search(monkeypatch, MUSALA)
    opened = _pages(monkeypatch, MUSALA_PAGES)
    brain = _brain(monkeypatch, ["Височината е 2925 m [1]."])
    out = rs.grounded_research("Каква е надморската височина на връх Мусала?")
    assert set(opened) == {h["url"] for h in MUSALA}
    prompt = brain.calls[0][-1]["content"]
    assert "2925 m" in prompt
    assert len(brain.calls) == 1   # едно извикване, не по едно на източник
    assert "2925 m [1]" in out


def test_sources_are_written_by_code_and_only_cited_ones_are_listed(monkeypatch) -> None:
    _search(monkeypatch, MUSALA)
    _pages(monkeypatch, MUSALA_PAGES)
    _brain(monkeypatch, ["2925 m [1]."])
    out = rs.grounded_research("височина на връх Мусала")
    assert "Източници:\n[1] Мусала — Уикипедия — https://bg.wikipedia.org/wiki/Мусала" in out
    assert "цитирани 1 от" in out


def test_fullwidth_citations_count_as_citations(monkeypatch) -> None:
    """gpt-oss цитира понякога с 【1】 (2026-11-17, Белмекен) — без това всички
    прочетени източници излизаха като цитирани."""
    _search(monkeypatch, MUSALA)
    _pages(monkeypatch, MUSALA_PAGES)
    _brain(monkeypatch, ["2925 m【1】"])
    out = rs.grounded_research("височина на връх Мусала")
    assert "2925 m[1]" in out
    assert "цитирани 1 от" in out


def test_unreachable_model_returns_raw_passages_without_claiming_an_answer(monkeypatch) -> None:
    """Brain.complete() не хвърля при изчерпана верига — връща „Error: …“.
    Тогава откъсите се дават сурови, ясно казано (bug fix 2026-08-12)."""
    _search(monkeypatch, MUSALA)
    _pages(monkeypatch, MUSALA_PAGES)
    _brain(monkeypatch, ["Error: цялата верига е изчерпана"])
    out = rs.grounded_research("височина на връх Мусала")
    assert "Error:" not in out
    assert "НЕ бе направено" in out
    assert "2925 m" in out
    assert "цитирани" not in out


def test_not_found_triggers_a_second_round_with_other_queries(monkeypatch) -> None:
    first = [{"title": "Шум", "url": "https://a.example/x", "snippet": "Белмекен е язовир."}]
    second = [{"title": "Belmeken", "url": "https://b.example/peak", "snippet": "Belmeken peak"}]
    seen = _search(monkeypatch, {"Колко висок е връх Белмекен?": first, "Belmeken peak elevation": second,
                                 "Белмекен връх височина": []})
    _pages(monkeypatch, {"https://a.example/x": "Язовир Белмекен е построен през 1974.",
                         "https://b.example/peak": "Belmeken peak elevation is 2627 m. Белмекен връх."})
    brain = _brain(monkeypatch, [
        f"{rs.NOT_FOUND}. Има само за язовира [1].",          # първи кръг
        "Белмекен връх височина\nBelmeken peak elevation",   # лекият модел: две заявки
        "2627 m [2].",                                         # втори кръг
    ])
    out = rs.grounded_research("Колко висок е връх Белмекен?")
    assert seen[1:] == ["Белмекен връх височина", "Belmeken peak elevation"]
    assert brain.light_calls == 1
    assert "2627 m [2]" in out
    assert "https://b.example/peak" in out


def test_pages_without_relevant_text_are_not_found_not_invented(monkeypatch) -> None:
    _search(monkeypatch, [{"title": "", "url": "https://a.example", "snippet": ""}])
    _pages(monkeypatch, {"https://a.example": ""})
    _brain(monkeypatch, [""])  # вторият кръг: няма заявки
    out = rs.grounded_research("височина на Мусала")
    assert "НЕ Е ОТКРИТО" in out


class TestRankPassages:
    def test_the_passage_with_the_answer_comes_first_and_noise_is_dropped(self) -> None:
        sources = [{"title": "T", "url": "u", "snippet": "",
                    "text": "Времето в Боровец днес е слънчево.\n\n" * 3
                            + "Надморската височина на Мусала е 2925 метра."}]
        got = rs.rank_passages("височината на Мусала", sources, budget=60)
        assert got and "2925" in got[0][1]

    def test_budget_and_per_source_limits(self) -> None:
        text = "\n".join(f"Мусала абзац {i} " + "дума " * 120 for i in range(10))
        sources = [{"title": "T", "url": "u", "snippet": "", "text": text}]
        got = rs.rank_passages("Мусала", sources, budget=10_000, per_source=2)
        assert len(got) == 2

    def test_stemming_matches_word_forms(self) -> None:
        assert rs._terms("височина") == rs._terms("височината")
        assert rs._terms("общини") == rs._terms("общината")

    def test_markdown_links_are_cleaned_to_their_text(self) -> None:
        assert rs._clean("виж [Мусала](https://x.example/a_(b)) ![](img.png)") == "виж Мусала"
