"""genesis_agent.translator — identifiers must survive the round trip.

Both prompts already tell the model "keep code identifiers, function names and
technical terms unchanged". Observed live on 2026-08-12, in a real Bulgarian
chat turn, is what the 1B translator does with that instruction:

    EN: "The genesis_agent directory contains 55 Python files."
    BG: "Името на агента генезис съдържа 55 python файла."

A directory name the user then has to type came back translated. Everywhere
else this project turns an instruction the model keeps ignoring into a
mechanism, so: mask the protected spans, translate, put them back verbatim.

No network here — `_call` is replaced with a stub that mimics a translator
(and, in one case, one that loses a placeholder).
"""
from __future__ import annotations

import pytest

from genesis_agent import translator as tr


@pytest.fixture
def echo_call(monkeypatch):
    """Captures what the model was asked to translate and returns it as-is,
    so assertions are about masking/restoring rather than about a model."""
    seen: list[str] = []

    def _fake(_model, prompt, timeout=60):
        body = prompt.split('text: "', 1)[1].rsplit('"', 1)[0]
        seen.append(body)
        return body
    monkeypatch.setattr(tr, "_call", _fake)
    return seen


class TestMasking:
    @pytest.mark.parametrize("token", [
        "genesis_agent",
        "repo_map.find_files()",
        "skill_loader.py",
        "`inline_code`",
        "MAX_ROUNDS",
        "path/to/file",
    ])
    def test_protected_tokens_are_masked(self, token) -> None:
        masked, kept = tr._mask(f"look at {token} please")
        assert token not in masked
        assert kept == [token]

    def test_ordinary_words_are_left_alone(self) -> None:
        masked, kept = tr._mask("the directory contains 55 Python files")
        assert masked == "the directory contains 55 Python files"
        assert kept == []

    def test_unmask_restores_every_token(self) -> None:
        masked, kept = tr._mask("run repo_map.find_files() in genesis_agent")
        assert tr._unmask(masked, kept) == "run repo_map.find_files() in genesis_agent"

    def test_unmask_reports_a_lost_placeholder(self) -> None:
        _masked, kept = tr._mask("run genesis_agent")
        assert tr._unmask("моделът изяде плейсхолдъра", kept) is None


class TestEnToBg:
    def test_the_identifier_never_reaches_the_model(self, echo_call) -> None:
        tr.translate_en_to_bg("The genesis_agent directory contains 55 files.")
        assert "genesis_agent" not in echo_call[0]

    def test_the_identifier_comes_back_verbatim(self, echo_call) -> None:
        out = tr.translate_en_to_bg("The genesis_agent directory contains 55 files.")
        assert "genesis_agent" in out

    def test_code_fences_are_still_untouched(self, echo_call) -> None:
        text = "before\n```python\nx = genesis_agent.run()\n```\nafter"
        out = tr.translate_en_to_bg(text)
        assert "```python\nx = genesis_agent.run()\n```" in out
        # The fence is not sent to the translator at all.
        assert not any("x = " in s for s in echo_call)

    def test_a_dropped_placeholder_keeps_the_original_text(self, monkeypatch) -> None:
        """Fail-open, the same bargain agent_core already makes: a skipped
        translation beats a sentence with the filename missing from it."""
        monkeypatch.setattr(tr, "_call", lambda *_a, **_k: "нещо съвсем друго")
        original = "Open genesis_agent now."
        assert tr.translate_en_to_bg(original) == original


class TestBgToEn:
    def test_identifiers_survive_the_prompt_direction_too(self, echo_call) -> None:
        out = tr.translate_bg_to_en("Отвори genesis_agent и пусни repo_map.find_files()")
        assert "genesis_agent" in out
        assert "repo_map.find_files()" in out
        assert "genesis_agent" not in echo_call[0]

    def test_a_dropped_placeholder_keeps_the_original(self, monkeypatch) -> None:
        monkeypatch.setattr(tr, "_call", lambda *_a, **_k: "something else entirely")
        original = "Отвори genesis_agent"
        assert tr.translate_bg_to_en(original) == original
