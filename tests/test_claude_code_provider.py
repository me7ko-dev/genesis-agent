"""genesis_agent.brain — `claude_code`: the operator's Claude subscription through `claude -p`."""
import json
import subprocess

import pytest

from genesis_agent import brain as b


def _reply(result="Готово.", **extra):
    return json.dumps({"type": "result", "is_error": False, "result": result,
                       "usage": {"input_tokens": 12, "output_tokens": 5,
                                 "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0},
                       **extra})


def _fake_run(monkeypatch, stdout, rc=0):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        sp = argv[argv.index("--system-prompt-file") + 1]
        with open(sp, encoding="utf-8") as f:
            seen["system"] = f.read()
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="warn\n")

    monkeypatch.setattr(b.subprocess, "run", run)
    monkeypatch.setattr(b, "_claude_exe", lambda: "claude.exe")
    return seen


def test_a_call_is_a_bare_model_call_on_the_subscription(monkeypatch):
    """Без инструментите на Claude Code, без MCP/plugin-и, без ключове в средата:
    вложен в Claude Code сесия CLI-ят иначе тръгва през нейния прокси, а
    ANTHROPIC_API_KEY би го прехвърлил на платен API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
    seen = _fake_run(monkeypatch, "noise\n" + _reply("[RUN_CMD: pytest -q]"))
    text, calls = b.Brain(use_local=False)._call(
        "claude_code", "claude-sonnet-5-5",
        [{"role": "system", "content": "Ти си Genesis."}, {"role": "user", "content": "направи x.py"}])
    assert text == "[RUN_CMD: pytest -q]" and calls is None
    argv, env = seen["argv"], seen["kw"]["env"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert not any(k.upper().startswith(("CLAUDE", "ANTHROPIC")) for k in env)
    assert seen["system"] == "Ти си Genesis." and "направи x.py" in seen["kw"]["input"]


def test_usage_is_recorded_like_the_anthropic_branch(monkeypatch):
    _fake_run(monkeypatch, _reply())
    brain = b.Brain(use_local=False)
    brain._call("claude_code", "sonnet", [{"role": "user", "content": "hi"}])
    assert brain._last_usage == {"prompt_tokens": 12, "completion_tokens": 5,
                                 "cached_read_tokens": 800, "cached_write_tokens": 0}


def test_the_transcript_keeps_order_and_later_notes_in_place():
    system, convo = b._claude_transcript([
        {"role": "system", "content": "S1"}, {"role": "user", "content": "задача"},
        {"role": "assistant", "content": "[WRITE_FILE: a.py]x=1[END_WRITE]"},
        {"role": "user", "content": "[WRITE_FILE: a.py] ✓"},
        {"role": "system", "content": "Пробвай кода."}])
    assert system == "S1"
    assert convo.index("задача") < convo.index("[WRITE_FILE: a.py]x=1") < convo.index("Пробвай кода.")
    assert convo.rstrip().endswith("ASSISTANT.")


@pytest.mark.parametrize("result,code", [("Claude AI usage limit reached|1759450000", "HTTP_429"),
                                         ("API Error: 500 overloaded", "HTTP_502")])
def test_errors_become_chain_errors(monkeypatch, result, code):
    _fake_run(monkeypatch, json.dumps({"type": "result", "is_error": True, "result": result}))
    with pytest.raises(RuntimeError, match=code):
        b.Brain(use_local=False)._call("claude_code", "sonnet", [{"role": "user", "content": "hi"}])


def test_no_cli_is_a_skip_not_a_crash(monkeypatch):
    monkeypatch.setattr(b, "_claude_exe", lambda: None)
    with pytest.raises(RuntimeError, match="skip"):
        b.Brain(use_local=False)._call("claude_code", "sonnet", [{"role": "user", "content": "hi"}])


def test_only_model_leaves_no_silent_fallback(monkeypatch):
    """Опит/bench: отговаря ТОЗИ модел или никой — иначе сравнението лъже."""
    monkeypatch.setenv("GENESIS_ONLY_MODEL", "claude_code/claude-sonnet-5-5")
    brain = b.Brain(pin_model=("groq", "openai/gpt-oss-120b"))
    assert [(c["provider"], c["model"]) for c in brain.chain] == [("claude_code", "claude-sonnet-5-5")]
    assert brain.local is None and not brain.chain[0]["supports_tools"]
    assert len(b.Brain(light=True).chain) > 1  # паметта и резюметата остават на бързите


def test_model_check_does_not_probe_the_cli():
    from genesis_agent import model_check
    assert model_check.probe("claude_code", "sonnet", None)["status"] == "skip"
