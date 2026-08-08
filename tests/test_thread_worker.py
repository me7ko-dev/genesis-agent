"""genesis_agent.thread_worker — the unsupervised 24/7 read-only prep pass
behind the Discord bot's overnight thread work. Zero coverage before this
file. Two things matter most here, both safety properties rather than
features:

  1. redact_secrets() — the last line of defense before a model's "findings"
     (which may have read a .env or similar while investigating) get posted
     to Discord. Wrong here means a real credential leaks into a chat log.
  2. prepare_thread() never raises and never goes outside the read-only tool
     boundary the module's docstring promises (no RUN_CMD/WRITE_FILE/browser
     — this whole mode runs unsupervised specifically because it can't touch
     anything).
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import genesis_agent.thread_worker as tw


class TestRedactSecrets:
    def test_empty_text_is_returned_as_is(self) -> None:
        assert tw.redact_secrets("") == ""

    def test_openai_style_key_is_redacted(self) -> None:
        # 16 chars after "sk-" — enough to trip redact_secrets' own {16,}
        # pattern without also tripping this repo's CI secret-scanner, which
        # flags anything sk-[A-Za-z0-9]{20,} even inside a test fixture.
        out = tw.redact_secrets("found key sk-abc123def456ghij in the file")
        assert "sk-abc123def456ghij" not in out
        assert "<скрито>" in out

    def test_huggingface_token_is_redacted(self) -> None:
        out = tw.redact_secrets("HF_TOKEN is hf_abcdefghijklmnopqrstuvwx")
        assert "hf_abcdefghijklmnopqrstuvwx" not in out

    def test_github_token_is_redacted(self) -> None:
        out = tw.redact_secrets("token: ghp_abcdefghijklmnopqrstuvwx1234")
        assert "ghp_abcdefghijklmnopqrstuvwx1234" not in out

    def test_google_key_is_redacted(self) -> None:
        out = tw.redact_secrets("AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz01234")
        assert "AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz01234" not in out

    def test_key_value_dotenv_style_line_is_redacted(self) -> None:
        out = tw.redact_secrets("OPENAI_API_KEY=notArealSecretValue1")
        assert "notArealSecretValue1" not in out
        assert "OPENAI_API_KEY=<скрито>" in out

    def test_ordinary_prose_is_left_untouched(self) -> None:
        text = "The README says to run pytest before committing."
        assert tw.redact_secrets(text) == text

    def test_short_normal_words_are_not_treated_as_secrets(self) -> None:
        text = "checked the configuration and the deployment script"
        assert tw.redact_secrets(text) == text


class TestPersist:
    def _fake_wm(self):
        calls = []
        wm = types.SimpleNamespace()
        wm.update_thread = lambda tid, **kw: calls.append({"thread_id": tid, **kw})
        return wm, calls

    def test_extracts_next_step_from_a_sledva_line(self) -> None:
        wm, calls = self._fake_wm()
        findings = "НАХОДКИ: the schema uses UUIDs\nСЛЕДВА: add a migration for the new column"
        tw._persist(wm, 42, findings, "")
        assert calls[0]["next_step"] == "add a migration for the new column"
        assert calls[0]["thread_id"] == 42

    def test_extracts_next_step_from_an_english_next_line(self) -> None:
        wm, calls = self._fake_wm()
        findings = "FINDINGS: looked at the code\nNEXT: talk to the API owner"
        tw._persist(wm, 1, findings, "")
        assert calls[0]["next_step"] == "talk to the API owner"

    def test_no_next_line_leaves_next_step_empty(self) -> None:
        wm, calls = self._fake_wm()
        tw._persist(wm, 1, "just some prose with no marker", "")
        assert calls[0]["next_step"] == ""

    def test_old_notes_are_preserved_after_the_new_finding(self) -> None:
        wm, calls = self._fake_wm()
        tw._persist(wm, 1, "НАХОДКИ: new stuff\nСЛЕДВА: do x", "previous session notes")
        assert "new stuff" in calls[0]["notes"]
        assert "previous session notes" in calls[0]["notes"]

    def test_wm_exception_is_swallowed_not_raised(self) -> None:
        wm = types.SimpleNamespace()
        def _boom(tid, **kw):
            raise RuntimeError("db locked")
        wm.update_thread = _boom
        tw._persist(wm, 1, "findings", "")  # must not raise


class _FakeBrain:
    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self._replies = list(replies)

    def complete(self, messages, tools=None):
        if not self._replies:
            raise AssertionError("complete() called more times than scripted")
        return self._replies.pop(0)


def _reply(raw_text="", tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(raw_text=raw_text, tool_calls=tool_calls)


@pytest.fixture
def fake_genesis_skills(monkeypatch):
    mod = types.ModuleType("genesis_skills")
    mod.dispatch_calls = []
    mod.tag_results = []

    def dispatch_tool_call(name, args):
        mod.dispatch_calls.append((name, args))
        return "tool output"

    def parse_and_execute_readonly_tools(text):
        # Only "fires" when the text actually contains a readonly tag, same
        # as the real regex-based parser would — otherwise a final
        # НАХОДКИ/СЛЕДВА reply with no tags in it would be misread as a tool
        # round and the loop would never reach the findings branch.
        if any(f"[{name}" in text for name in ("READ_FILE", "WEB_SEARCH", "LIST_DIR", "RESEARCH")):
            return mod.tag_results
        return []

    mod.dispatch_tool_call = dispatch_tool_call
    mod.parse_and_execute_readonly_tools = parse_and_execute_readonly_tools
    monkeypatch.setitem(sys.modules, "genesis_skills", mod)
    return mod


@pytest.fixture
def fake_wm(monkeypatch):
    calls = []
    monkeypatch.setattr("genesis_agent.workspace_memory.update_thread",
                        lambda tid, **kw: calls.append({"thread_id": tid, **kw}))
    return calls


def _install_brain(monkeypatch, replies):
    monkeypatch.setattr("genesis_agent.brain.Brain", lambda **kw: _FakeBrain(replies))


class TestPrepareThread:
    def test_import_failure_is_reported_not_raised(self, monkeypatch) -> None:
        def _boom(name, *a, **kw):
            if name == "genesis_skills":
                raise ImportError("no such module")
            return real_import(name, *a, **kw)
        import builtins
        real_import = builtins.__import__
        monkeypatch.setattr(builtins, "__import__", _boom)
        out = tw.prepare_thread({"id": 1, "title": "t"})
        assert out.ok is False
        assert "import:" in out.error

    def test_simple_text_reply_is_treated_as_findings_and_persisted(
        self, monkeypatch, fake_genesis_skills, fake_wm
    ) -> None:
        _install_brain(monkeypatch, [_reply(raw_text="НАХОДКИ: x\nСЛЕДВА: y")])
        out = tw.prepare_thread({"id": 7, "title": "investigate the bug"})
        assert out.ok is True
        assert out.thread_id == 7
        assert "НАХОДКИ" in out.findings
        assert fake_wm[0]["next_step"] == "y"

    def test_secrets_in_findings_are_redacted_before_persisting(
        self, monkeypatch, fake_genesis_skills, fake_wm
    ) -> None:
        _install_brain(monkeypatch, [_reply(raw_text="found OPENAI_API_KEY=sk-realvalue12345678")])
        out = tw.prepare_thread({"id": 1, "title": "t"})
        assert "sk-realvalue12345678" not in out.findings
        assert "sk-realvalue12345678" not in fake_wm[0]["notes"]

    def test_native_tool_call_is_dispatched_and_loop_continues(
        self, monkeypatch, fake_genesis_skills, fake_wm
    ) -> None:
        _install_brain(monkeypatch, [
            _reply(raw_text="let me check", tool_calls=[
                {"id": "1", "function": {"name": "READ_FILE", "arguments": '{"path": "x"}'}},
            ]),
            _reply(raw_text="НАХОДКИ: done\nСЛЕДВА: ship it"),
        ])
        out = tw.prepare_thread({"id": 1, "title": "t"}, max_rounds=4)
        assert out.ok is True
        # thread_worker passes the raw JSON-string arguments straight through —
        # parsing them is dispatch_tool_call's own job (see genesis_skills.py).
        assert fake_genesis_skills.dispatch_calls == [("READ_FILE", '{"path": "x"}')]

    def test_text_tag_readonly_result_continues_the_loop(
        self, monkeypatch, fake_genesis_skills, fake_wm
    ) -> None:
        fake_genesis_skills.tag_results = ["[READ_FILE: x] some content"]
        _install_brain(monkeypatch, [
            _reply(raw_text="[READ_FILE: x]"),
            _reply(raw_text="НАХОДКИ: found it\nСЛЕДВА: proceed"),
        ])
        out = tw.prepare_thread({"id": 1, "title": "t"}, max_rounds=4)
        assert out.ok is True

    def test_error_prefixed_reply_fails_cleanly(self, monkeypatch, fake_genesis_skills, fake_wm) -> None:
        _install_brain(monkeypatch, [_reply(raw_text="Error: model unavailable")])
        out = tw.prepare_thread({"id": 1, "title": "t"})
        assert out.ok is False
        assert "Error:" in out.error

    def test_exhausted_rounds_asks_for_a_conclusion(self, monkeypatch, fake_genesis_skills, fake_wm) -> None:
        fake_genesis_skills.tag_results = ["some tool output"]
        replies = [_reply(raw_text="[READ_FILE: x]") for _ in range(2)]
        replies.append(_reply(raw_text="НАХОДКИ: what I found\nСЛЕДВА: manual step needed"))
        _install_brain(monkeypatch, replies)
        out = tw.prepare_thread({"id": 1, "title": "t"}, max_rounds=2)
        assert out.ok is True
        assert "what I found" in out.findings

    def test_unexpected_exception_is_caught_and_reported(self, monkeypatch, fake_genesis_skills, fake_wm) -> None:
        def _boom(**kw):
            raise RuntimeError("brain init failed")
        monkeypatch.setattr("genesis_agent.brain.Brain", _boom)
        out = tw.prepare_thread({"id": 1, "title": "t"})
        assert out.ok is False
        assert "brain init failed" in out.error


class TestPrepareOpenThreads:
    def test_list_threads_failure_returns_empty_list(self, monkeypatch) -> None:
        def _boom(*a, **kw):
            raise RuntimeError("db unavailable")
        monkeypatch.setattr("genesis_agent.workspace_memory.list_threads", _boom)
        assert tw.prepare_open_threads() == []

    def test_prepares_each_open_thread(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.workspace_memory.list_threads",
                            lambda status, limit: [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
        seen = []
        monkeypatch.setattr(tw, "prepare_thread",
                            lambda t, **kw: seen.append(t["id"]) or SimpleNamespace(thread_id=t["id"]))
        out = tw.prepare_open_threads(limit=2)
        assert seen == [1, 2]
        assert len(out) == 2
