"""genesis_agent.research — grounded_research(), the backend for the
[RESEARCH: ...] tool tag. Zero coverage before this file. web_search.search
and brain.Brain are both faked throughout: what's under test is the
cross-verification logic itself (per-source grounded extraction, then a
compare pass only when there's more than one source to compare), not real
search results or a real model.
"""
from __future__ import annotations

from types import SimpleNamespace

import genesis_agent.research as rs


class _FakeBrain:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.calls.append(messages)
        if not self._replies:
            raise AssertionError("complete() called more times than replies were scripted")
        return SimpleNamespace(raw_text=self._replies.pop(0), code="", tool_calls=None)


def _install_fake_brain(monkeypatch, replies: list[str]) -> _FakeBrain:
    brain = _FakeBrain(replies)
    monkeypatch.setattr("genesis_agent.brain.Brain", lambda: brain)
    return brain


def test_empty_question_short_circuits_without_searching(monkeypatch) -> None:
    called = []
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: called.append(True))
    assert rs.grounded_research("   ") == "[RESEARCH] Празен въпрос."
    assert called == []


def test_search_exception_is_reported_not_raised(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("DNS failure")
    monkeypatch.setattr("genesis_agent.web_search.search", _boom)
    out = rs.grounded_research("what is the GIL")
    assert "Грешка при търсене" in out
    assert "DNS failure" in out


def test_no_results_is_reported_clearly(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: [])
    out = rs.grounded_research("an obscure question")
    assert "Няма намерени резултати" in out


def test_results_with_no_snippets_are_reported_as_empty(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search",
                        lambda *a, **kw: [{"title": "t", "url": "u", "snippet": ""}])
    out = rs.grounded_research("q")
    assert "без съдържание" in out


def test_single_source_skips_the_compare_pass(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: [
        {"title": "Docs", "url": "https://x.test", "snippet": "the GIL is a mutex"},
    ])
    brain = _install_fake_brain(monkeypatch, ["the GIL is a global lock — cited: 'the GIL is a mutex'"])
    out = rs.grounded_research("what is the GIL")
    assert "само 1 източник, без cross-check" in out
    assert "the GIL is a global lock" in out
    assert len(brain.calls) == 1  # only the extraction call, no compare call


def test_multiple_sources_triggers_a_compare_pass(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: [
        {"title": "A", "url": "https://a.test", "snippet": "snippet a"},
        {"title": "B", "url": "https://b.test", "snippet": "snippet b"},
    ])
    brain = _install_fake_brain(monkeypatch, [
        "answer from A",
        "answer from B",
        "consensus: both agree",
    ])
    out = rs.grounded_research("q", top_n=2)
    assert "consensus: both agree" in out
    assert "проверено през 2 източника" in out
    assert len(brain.calls) == 3  # 2 extractions + 1 compare


def test_sources_with_empty_snippet_are_skipped_but_others_still_counted(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: [
        {"title": "A", "url": "https://a.test", "snippet": "real content"},
        {"title": "B", "url": "https://b.test", "snippet": ""},  # skipped
        {"title": "C", "url": "https://c.test", "snippet": "more content"},
    ])
    brain = _install_fake_brain(monkeypatch, [
        "answer from A",
        "answer from C",
        "consensus reached",
    ])
    out = rs.grounded_research("q", top_n=3)
    assert "проверено през 2 източника" in out
    assert len(brain.calls) == 3  # 2 extractions (B skipped) + 1 compare


def test_per_source_extraction_prompt_includes_question_title_and_url(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: [
        {"title": "PEP 8", "url": "https://peps.python.org/pep-0008", "snippet": "use 4 spaces"},
    ])
    brain = _install_fake_brain(monkeypatch, ["4 spaces per indent level"])
    rs.grounded_research("how many spaces for indentation")
    prompt = brain.calls[0][-1]["content"]
    assert "how many spaces for indentation" in prompt
    assert "PEP 8" in prompt
    assert "https://peps.python.org/pep-0008" in prompt
    assert "use 4 spaces" in prompt


class TestUnreachableModelIsNotDressedUpAsVerified:
    """Brain.complete() does not raise when the provider chain is exhausted —
    it returns an object whose raw_text starts with "Error:". Unchecked, that
    string flowed straight into the report and came out labelled "(проверено
    през N източника)": a cross-check claim with nothing behind it, and the
    weak-model path in autonomous_loop injects exactly this text into the
    prompt as researched context (fixed 2026-08-12). The whole module exists
    for the difference between verified and merely verified-looking.
    """

    _SOURCES = [
        {"title": "A", "url": "https://a.example", "snippet": "text a"},
        {"title": "B", "url": "https://b.example", "snippet": "text b"},
    ]

    def test_all_extractions_failing_is_reported_as_no_content(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: self._SOURCES)
        _install_fake_brain(monkeypatch, ["Error: цялата верига е изчерпана"] * 2)
        out = rs.grounded_research("what is the GIL")
        assert "проверено през" not in out
        assert "Error:" not in out

    def test_failed_compare_pass_does_not_claim_cross_check(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: self._SOURCES)
        _install_fake_brain(monkeypatch, [
            "GIL is a mutex",           # source A extraction — fine
            "GIL serializes bytecode",  # source B extraction — fine
            "Error: цялата верига е изчерпана",  # the compare pass fails
        ])
        out = rs.grounded_research("what is the GIL")

        assert "проверено през" not in out
        assert "НЕ бе извършено" in out
        # The per-source answers that DID succeed are still handed over.
        assert "GIL is a mutex" in out
        assert "GIL serializes bytecode" in out

    def test_a_single_failed_source_is_dropped_not_counted(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: self._SOURCES)
        _install_fake_brain(monkeypatch, [
            "Error: цялата верига е изчерпана",  # source A fails
            "GIL serializes bytecode",           # source B succeeds
        ])
        out = rs.grounded_research("what is the GIL")
        # One usable source left -> the no-cross-check branch, not a claim of two.
        assert "само 1 източник" in out
        assert "Error:" not in out

    def test_healthy_run_still_claims_verification(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **kw: self._SOURCES)
        _install_fake_brain(monkeypatch, ["answer a", "answer b", "consensus: it is a mutex"])
        out = rs.grounded_research("what is the GIL")
        assert "проверено през 2 източника" in out
        assert "consensus: it is a mutex" in out
