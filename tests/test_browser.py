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


# ── 2026-11-23: BROWSE и системният Edge/Chrome ─────────────────────────────
# На лаптопа вграденият Chromium липсва (`playwright install` не е пускан) и
# BROWSE падаше с „Executable doesn't exist“, докато търсенето в браузъра вече
# вървеше през Edge. Истински браузър тук няма: playwright.sync_api е подменен.
class _FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[object, object]] = []

    def route(self, pattern, handler) -> None:
        self.routes.append((pattern, handler))


class _FakePage:
    def __init__(self) -> None:
        self.context = _FakeContext()

    def set_default_timeout(self, ms) -> None:
        pass


class _FakeBrowser:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.context_kwargs: list[dict] = []

    def new_page(self, **kw):
        self.context_kwargs.append(kw)
        return self.page

    def close(self) -> None:
        pass


class _FakePlaywright:
    """chromium.launch успява само за каналите в `installed` („“ = вграденият)."""

    def __init__(self, installed: set[str]) -> None:
        self.installed = installed
        self.launches: list[dict] = []
        self.stopped = False
        self.chromium = self
        self.browser = _FakeBrowser()

    def launch(self, **kw):
        self.launches.append(kw)
        if kw.get("channel", "") not in self.installed:
            raise RuntimeError("Executable doesn't exist")
        return self.browser

    def start(self):
        return self

    def stop(self) -> None:
        self.stopped = True


def _fake_playwright(monkeypatch, installed: set[str]) -> _FakePlaywright:
    import sys
    import types
    pw = _FakePlaywright(installed)
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: pw  # type: ignore[attr-defined]
    pkg = types.ModuleType("playwright")
    pkg.sync_api = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    monkeypatch.setattr(b, "_pw", None)
    monkeypatch.setattr(b, "_browser", None)
    monkeypatch.delenv("GENESIS_BROWSER_CHANNEL", raising=False)
    monkeypatch.delenv("GENESIS_BROWSER_VISIBLE", raising=False)
    return pw


class TestSystemBrowserFallback:
    def test_without_bundled_chromium_browse_starts_system_edge(self, monkeypatch) -> None:
        pw = _fake_playwright(monkeypatch, {"msedge"})
        page = b._ensure_page()
        assert page is pw.browser.page
        assert [k.get("channel", "") for k in pw.launches] == ["", "msedge"]
        assert all(k["headless"] is True for k in pw.launches)

    def test_bundled_chromium_is_still_preferred(self, monkeypatch) -> None:
        pw = _fake_playwright(monkeypatch, {"", "msedge", "chrome"})
        b._ensure_page()
        assert pw.launches == [{"headless": True}]

    def test_the_order_is_the_one_the_search_browser_uses(self, monkeypatch) -> None:
        pw = _fake_playwright(monkeypatch, {"chrome"})
        monkeypatch.setenv("GENESIS_BROWSER_CHANNEL", "chrome")
        b._ensure_page()
        assert [k.get("channel", "") for k in pw.launches] == ["chrome"]

    def test_the_system_browser_gets_the_same_guards_and_an_empty_profile(
        self, monkeypatch
    ) -> None:
        """Каналът сменя само изпълнимия файл: пазачът на вътрешните адреси е
        на контекста и профилът не е този на потребителя (launch, не
        launch_persistent_context; без user_data_dir/storage_state)."""
        pw = _fake_playwright(monkeypatch, {"msedge"})
        page = b._ensure_page()
        assert page.context.routes == [(b._INTERNAL_LOOKING_URL, b._route_guard)]
        assert pw.browser.context_kwargs == [{}]
        assert all(set(k) <= {"headless", "channel"} for k in pw.launches)

    def test_no_browser_at_all_is_a_clear_error_and_can_be_retried(self, monkeypatch) -> None:
        pw = _fake_playwright(monkeypatch, set())
        with pytest.raises(RuntimeError, match="няма браузър за BROWSE"):
            b._ensure_page()
        assert [k.get("channel", "") for k in pw.launches] == ["", "msedge", "chrome"]
        assert pw.stopped and b._pw is None and b._page is None
        pw.installed.add("msedge")          # Edge се появи → следващият BROWSE тръгва
        assert b._ensure_page() is pw.browser.page
