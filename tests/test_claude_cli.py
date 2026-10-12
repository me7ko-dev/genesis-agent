"""Claude с абонамента на оператора през `claude -p` (2026-10-12).

`claude` тук е фалшив: subprocess.run се подменя, тестът вижда аргументите,
средата и stdin. Истинското пускане е мерено ръчно (genesis -p, ЕГН задача:
65 s, 24 passed) — CI няма вход в Claude.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from genesis_agent import brain, claude_cli


class FakeClaude:
    """Подмяна на subprocess.run: пази заявката, връща зададения stdout."""

    def __init__(self, payload: dict | None = None, *, stdout: str | None = None,
                 stderr: str = "", rc: int = 0):
        self.payload = payload if payload is not None else {
            "type": "result", "subtype": "success", "is_error": False, "result": "готово",
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1200,
                      "cache_read_input_tokens": 300, "output_tokens": 7}}
        self.stdout = stdout
        self.stderr = stderr
        self.rc = rc
        self.calls: list[dict] = []

    def __call__(self, argv, **kw):
        system = ""
        if "--system-prompt-file" in argv:
            system = Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
        raw = kw.get("input", b"").decode("ascii")          # stream-json, само ASCII
        message = json.loads(raw)["message"]
        blocks = [b["text"] for b in message["content"]]
        self.calls.append({"argv": list(argv), "env": kw.get("env"), "cwd": kw.get("cwd"),
                           "stdin": "\n\n".join(blocks), "blocks": blocks, "system": system})
        out = self.stdout if self.stdout is not None else json.dumps(self.payload, ensure_ascii=False)
        return subprocess.CompletedProcess(argv, self.rc, out.encode("utf-8"), self.stderr.encode("utf-8"))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("GENESIS_CLAUDE_CLI", str(exe))
    f = FakeClaude()
    monkeypatch.setattr(claude_cli.subprocess, "run", f)
    return f


# ── разговорът към един вход ─────────────────────────────────────────────────

def test_a_single_question_goes_as_is_and_the_system_prompt_separately():
    system, prompt = claude_cli.render([
        {"role": "system", "content": "Ти си Genesis."},
        {"role": "user", "content": "Колко е 2+2?"}])
    assert system == "Ти си Genesis."
    assert prompt == "Колко е 2+2?"


def test_a_conversation_keeps_every_turn_and_the_tool_calls_in_order():
    system, prompt = claude_cli.render([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "направи a.py"},
        {"role": "assistant", "content": "пиша",
         "tool_calls": [{"function": {"name": "WRITE_FILE", "arguments": '{"path": "a.py"}'}}]},
        {"role": "tool", "content": "✓ записан"},
        {"role": "system", "content": "[Резултат] ok"},
        {"role": "user", "content": "пусни го"}])
    assert system == "SYS"
    order = [prompt.index(s) for s in ("направи a.py", "WRITE_FILE", "✓ записан", "[Резултат] ok", "пусни го")]
    assert order == sorted(order)
    assert "САМО следващия си отговор" in prompt


# ── средата: абонаментът, не ключът ──────────────────────────────────────────

def test_the_child_never_sees_an_api_key_that_would_bill_instead_of_the_subscription():
    env = claude_cli.child_env({
        "PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "t",
        "CLAUDE_CODE_USE_BEDROCK": "1", "GROQ_API_KEY": "g", "GROQ_API_KEY_2": "g2",
        "GITHUB_TOKEN": "gh", "CLAUDE_CODE_OAUTH_TOKEN": "oauth"})
    assert env == {"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_OAUTH_TOKEN": "oauth"}


# ── едно извикване ───────────────────────────────────────────────────────────

def test_claude_runs_as_a_pure_text_model_with_our_prompt(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    text, usage = claude_cli.complete([
        {"role": "system", "content": "Ти си Genesis."},
        {"role": "user", "content": "здравей"}], model="opus")
    assert text == "готово"
    call = fake.calls[0]
    argv = call["argv"]
    for flag in ("-p", "--safe-mode", "--no-session-persistence", "--output-format"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""          # без инструментите на Claude Code
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert "--bare" not in argv                           # --bare не чете абонамента
    assert all(a.isascii() for a in argv[1:] if not a.startswith(("/", "C:")))  # cmd.exe
    assert call["system"] == "Ти си Genesis."
    assert call["stdin"] == "здравей"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert not Path(call["cwd"]).exists()                 # празна временна папка, махната
    assert usage == {"prompt_tokens": 1502, "completion_tokens": 7,
                     "cached_read_tokens": 300, "cached_write_tokens": 1200}


@pytest.mark.parametrize(("result", "code"), [
    ("You've hit your session limit · resets 3:45pm", "HTTP_429"),
    ("You've hit your weekly limit · resets Mon", "HTTP_429"),
    ("Not logged in · Please run /login", "HTTP_401"),
    ("OAuth token has expired", "HTTP_401"),
    ("API Error: 529 overloaded", "HTTP_503"),
    ("something else broke", "HTTP_500"),
])
def test_a_failed_run_says_why_in_the_chains_language(fake, result, code):
    fake.payload = {"type": "result", "subtype": "success", "is_error": True, "result": result}
    with pytest.raises(RuntimeError, match=code):
        claude_cli.complete([{"role": "user", "content": "x"}])


def test_an_error_subtype_without_result_uses_the_errors_list(fake):
    fake.payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                    "errors": ["Login expired · Please run /login"]}
    with pytest.raises(RuntimeError, match="HTTP_401"):
        claude_cli.complete([{"role": "user", "content": "x"}])


def test_output_that_is_not_json_is_classified_from_stderr(fake):
    fake.stdout, fake.stderr, fake.rc = "", "Error: Not logged in", 1
    with pytest.raises(RuntimeError, match="HTTP_401"):
        claude_cli.complete([{"role": "user", "content": "x"}])


def test_a_warning_line_before_the_json_does_not_hide_the_answer(fake):
    fake.stdout = "warning: something\n" + json.dumps(fake.payload)
    assert claude_cli.complete([{"role": "user", "content": "x"}])[0] == "готово"


def test_the_answer_is_the_result_line_not_the_stream_before_it(fake):
    """stream-json пише init и съобщението на модела преди резултата."""
    fake.stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "чернова"}]}}),
        json.dumps(fake.payload),
        ""])
    assert claude_cli.complete([{"role": "user", "content": "x"}])[0] == "готово"


def test_a_stream_without_a_result_line_is_classified_from_stderr(fake):
    fake.stdout = json.dumps({"type": "system", "subtype": "init"})
    fake.stderr, fake.rc = "Error: Not logged in", 1
    with pytest.raises(RuntimeError, match="HTTP_401"):
        claude_cli.complete([{"role": "user", "content": "x"}])


def test_each_turn_is_its_own_block_so_the_next_call_reuses_the_cache(fake):
    """Кешът съвпада само по граници на блокове (мерено 2026-10-12): с един
    общ текст всеки ход записваше целия разговор наново. Блоковете на хода
    трябва да са начало на блоковете на следващия."""
    history = [{"role": "system", "content": "SYS"},
               {"role": "user", "content": "направи a.py"},
               {"role": "assistant", "content": "[WRITE_FILE: a.py]\nx = 1\n[END_WRITE]"},
               {"role": "system", "content": "[Резултат] записан"}]
    claude_cli.complete(history)
    claude_cli.complete(history + [{"role": "assistant", "content": "[RUN_CMD: pytest]"},
                                   {"role": "system", "content": "[Резултат] 3 passed"}])
    first, second = fake.calls[0]["blocks"], fake.calls[1]["blocks"]
    assert len(first) == 4 and second[:len(first)] == first and len(second) == 6


def test_without_the_cli_the_chain_moves_on(monkeypatch):
    monkeypatch.setenv("GENESIS_CLAUDE_CLI", "/nowhere/claude")
    with pytest.raises(RuntimeError, match="^skip:"):
        claude_cli.complete([{"role": "user", "content": "x"}])


# ── в Brain ──────────────────────────────────────────────────────────────────

@pytest.fixture
def clean_cooldowns(monkeypatch):
    monkeypatch.setattr(brain, "_EXHAUSTED", {})
    monkeypatch.delenv("GENESIS_MODEL_GATEWAY", raising=False)


def test_a_pinned_subscription_answers_and_its_usage_is_counted(fake, clean_cooldowns):
    b = brain.Brain(pin_model=("claude_cli", "sonnet"), use_local=False)
    reply = b.complete([{"role": "user", "content": "здравей"}])
    assert reply.raw_text == "готово"
    assert b.current["provider"] == "claude_cli"
    assert b._last_usage["completion_tokens"] == 7


def test_native_tool_history_reaches_claude_as_text(fake, clean_cooldowns):
    b = brain.Brain(pin_model=("claude_cli", "sonnet"), use_local=False)
    b.complete([{"role": "user", "content": "a"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "1", "type": "function",
                                 "function": {"name": "READ_FILE", "arguments": '{"path": "a.py"}'}}]},
                {"role": "tool", "tool_call_id": "1", "content": "print(1)"},
                {"role": "user", "content": "b"}])
    stdin = fake.calls[0]["stdin"]
    assert "READ_FILE" in stdin and "print(1)" in stdin


def test_the_subscription_limit_pauses_claude_for_the_whole_account(fake, clean_cooldowns):
    fake.payload = {"type": "result", "subtype": "success", "is_error": True,
                    "result": "You've hit your session limit · resets 3:45pm"}
    b = brain.Brain(pin_model=("claude_cli", "sonnet"), use_local=False)
    with pytest.raises(RuntimeError, match="HTTP_429"):
        b._call_claude_cli("sonnet", [{"role": "user", "content": "x"}])
    calls = len(fake.calls)
    with pytest.raises(RuntimeError, match="HTTP_429"):
        b._call_claude_cli("opus", [{"role": "user", "content": "x"}])
    assert len(fake.calls) == calls          # вторият модел не пробва изобщо


def test_the_online_service_never_uses_the_operators_subscription(fake, clean_cooldowns, monkeypatch):
    monkeypatch.setenv("GENESIS_MODEL_GATEWAY", "http://gateway:8090")
    b = brain.Brain(pin_model=("claude_cli", "sonnet"), use_local=False)
    with pytest.raises(RuntimeError, match="^skip:"):
        b._call_claude_cli("sonnet", [{"role": "user", "content": "x"}])
    assert fake.calls == []


# ── в терминала ──────────────────────────────────────────────────────────────

def test_the_model_menu_offers_the_subscription_when_the_cli_is_there(fake):
    import genesis_terminal_agent as gta
    assert gta.provider_ready("claude_cli") == (True, "")
    assert gta.fetch_models("claude_cli") == ["sonnet", "opus", "haiku"]


def test_the_model_menu_says_what_is_missing_without_the_cli(monkeypatch):
    import genesis_terminal_agent as gta
    monkeypatch.setenv("GENESIS_CLAUDE_CLI", "/nowhere/claude")
    ready, why = gta.provider_ready("claude_cli")
    assert not ready and "claude" in why


def test_no_key_guard_lets_the_subscription_answer(fake, monkeypatch):
    import genesis_terminal_agent as gta
    monkeypatch.setattr(gta, "current_provider", "claude_cli")
    monkeypatch.setattr("genesis_agent.brain.any_model_reachable", lambda: False)
    assert gta._no_model_guard() is False


def test_the_raw_reply_log_is_written_only_when_asked(fake, monkeypatch, tmp_path):
    claude_cli.complete([{"role": "user", "content": "x"}])        # без настройка — нищо
    log = tmp_path / "replies.jsonl"
    monkeypatch.setenv("GENESIS_CLAUDE_CLI_LOG", str(log))
    claude_cli.complete([{"role": "user", "content": "x"}])
    assert [json.loads(line)["text"] for line in log.read_text(encoding="utf-8").splitlines()] == ["готово"]


def test_light_work_falls_back_to_haiku_only_when_the_subscription_is_chosen(fake, monkeypatch):
    monkeypatch.delenv("GENESIS_START_PROVIDER", raising=False)
    monkeypatch.delenv("GENESIS_CLAUDE_CLI_ACTIVE", raising=False)
    assert not any(c["provider"] == "claude_cli" for c in brain.Brain(light=True, use_local=False).chain)
    monkeypatch.setenv("GENESIS_CLAUDE_CLI_ACTIVE", "1")
    chain = brain.Brain(light=True, use_local=False).chain
    assert chain[-1] == {"provider": "claude_cli", "model": "haiku", "size_b": 0, "supports_tools": False}


def test_a_missing_key_is_not_printed_as_a_dropped_model(capsys):
    brain._print_skip("groq", "m", "skip: no GROQ_API_KEY configured", 0.0)
    brain._print_skip("groq", "m", "HTTP_429: slow down", 0.1)
    out = capsys.readouterr().out
    assert "GROQ_API_KEY" not in out and "HTTP_429" in out
