"""genesis_agent.browser — резолвиране на елемент и пазачите около действията.

Playwright не се стартира тук: `_resolve_element` работи изцяло върху
`_last_elements` (изхода от последното сканиране), а `navigate`/`read`/`click`/
`type_text` имат проверки, които се задействат ПРЕДИ докосване на страница.
Точно това е частта, която решава ВЪРХУ КОЙ елемент ще се действа — и оттам
какво вижда sandbox-ът, когато преценява дали действието е позволено.
"""
from __future__ import annotations

import pytest

from genesis_agent import browser as b


def _el(i: int, text: str = "", name: str = "", tag: str = "button",
        type_: str = "") -> dict:
    return {"i": i, "tag": tag, "text": text, "name": name, "type": type_}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(b, "_last_elements", [])
    monkeypatch.setattr(b, "_page", None)
    yield


class TestResolveByIndex:
    def test_a_digit_target_selects_that_index(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0, "A"), _el(1, "B")])
        assert b._resolve_element("1")["text"] == "B"

    def test_an_index_that_is_not_on_the_page_resolves_to_nothing(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0, "A")])
        assert b._resolve_element("7") is None

    def test_an_index_never_falls_through_to_text_matching(self, monkeypatch) -> None:
        """A number that misses must fail, not quietly match something by text."""
        monkeypatch.setattr(b, "_last_elements", [_el(0, "7 items")])
        assert b._resolve_element("9") is None


class TestBlankElementsAreNotCandidates:
    """An element with neither text nor name produced hay=" " — a single space
    — and `" " in target` is true for EVERY multi-word query. So any target
    with a space in it hit the first blank element on the page.

    Not merely a wrong click: click() takes its label from
    `el["text"] or el["name"] or target`, so for a blank element the label
    becomes the text of the QUERY, and sandbox.assess_browser_click judges that
    instead of the real button. A blank "Pay" button under the query
    "continue to next page" passed as an ordinary click.
    """

    def test_a_multi_word_target_does_not_hit_a_blank_element(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements",
                            [_el(0), _el(1, "Search", "q")])
        assert b._resolve_element("search button")["i"] == 1

    def test_a_page_of_only_blank_elements_resolves_to_nothing(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0), _el(1), _el(2)])
        assert b._resolve_element("any words here") is None

    def test_the_real_button_wins_even_when_the_blank_one_comes_first(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements",
                            [_el(0), _el(1, "Купи сега", "buy")])
        assert b._resolve_element("кликни Купи сега")["i"] == 1


class TestResolveByText:
    def test_an_exact_label_matches(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0, "Search", "q")])
        assert b._resolve_element("Search")["i"] == 0

    def test_matching_ignores_case(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0, "Search", "q")])
        assert b._resolve_element("SEARCH")["i"] == 0

    def test_an_exact_match_beats_a_partial_one_earlier_in_the_list(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements",
                            [_el(0, "Search products"), _el(1, "Search")])
        assert b._resolve_element("search")["i"] == 1

    def test_text_and_name_are_compared_separately_not_glued(
        self, monkeypatch
    ) -> None:
        """Glued as "купи сега buy", the label is no longer a substring of
        "кликни Купи сега", so the visible text stopped matching as soon as an
        element had a name."""
        monkeypatch.setattr(b, "_last_elements", [_el(0, "Купи сега", "buy")])
        assert b._resolve_element("кликни Купи сега")["i"] == 0

    def test_a_very_short_label_does_not_stick_to_every_sentence(
        self, monkeypatch
    ) -> None:
        """"ok" inside "click the checkout button" is a coincidence, not a match."""
        monkeypatch.setattr(b, "_last_elements",
                            [_el(0, "ok"), _el(1, "checkout", "pay")])
        assert b._resolve_element("click the checkout button")["i"] == 1

    def test_no_match_resolves_to_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_last_elements", [_el(0, "Search", "q")])
        assert b._resolve_element("nothing like this") is None

    def test_field_kind_only_considers_input_elements(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_last_elements", [
            _el(0, "email", "email", tag="button"),
            _el(1, "email", "email", tag="input"),
        ])
        assert b._resolve_element("email", kind="field")["i"] == 1

    def test_field_kind_resolves_to_nothing_when_no_field_matches(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(b, "_last_elements",
                            [_el(0, "email", "email", tag="button")])
        assert b._resolve_element("email", kind="field") is None


class TestNavigateScheme:
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x",
                                     "javascript:alert(1)", "data:text/html,x"])
    def test_non_http_schemes_are_refused_without_opening_a_page(self, url) -> None:
        out = b.navigate(url)
        assert "Отказано" in out
        assert b._page is None


class TestActionsWithoutAPage:
    def test_read_says_to_open_a_page_first(self) -> None:
        assert "първо използвай" in b.read()

    def test_click_says_to_open_a_page_first(self) -> None:
        assert "първо използвай" in b.click("1")

    def test_type_says_to_open_a_page_first(self) -> None:
        assert "първо използвай" in b.type_text("1 | text")


class TestTypeTextArgument:
    def test_a_missing_separator_is_reported_not_guessed(self, monkeypatch) -> None:
        monkeypatch.setattr(b, "_page", object())
        assert "Формат:" in b.type_text("just some text")
