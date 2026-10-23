"""Одит 2026-10-22 на доставчиците (веригата в brain.py, claude_cli,
model_router). Всеки тест пада без поправката си (пуснато срещу стария код):
мрежата е `_post_with_deadline`, а `claude` и Ollama са фалшиви — ключове,
cooldown-и и самата верига минават през истинския код."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

from genesis_agent import brain, claude_cli, model_router


class FakeResp:
    def __init__(self, status: int, body, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self._body) if isinstance(self._body, str) else self._body


def ok(content=None, tool_calls=None, finish="stop") -> FakeResp:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return FakeResp(200, {"choices": [{"message": msg, "finish_reason": finish}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def chain(*entries, tools: bool = True) -> list[dict]:
    return [{"provider": p, "model": m, "size_b": 120, "supports_tools": tools}
            for p, m in entries]


_KEY_ENVS = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
             "ANTHROPIC_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(brain, "_EXHAUSTED", {})
    monkeypatch.setattr(brain, "_KEY_429_MODELS", {})
    monkeypatch.setattr(brain, "ENV_FILES", ())
    monkeypatch.setattr("genesis_agent.paths.ENV_FILES", ())
    monkeypatch.setattr(brain, "_local_available", lambda m: False)
    monkeypatch.setattr(brain.time, "sleep", lambda s: None)
    monkeypatch.delenv("GENESIS_MODEL_GATEWAY", raising=False)
    for base in _KEY_ENVS:
        monkeypatch.delenv(base, raising=False)
        for i in range(2, 11):
            monkeypatch.delenv(f"{base}_{i}", raising=False)
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)
    yield


def _route(monkeypatch, handler) -> list:
    seen: list = []

    def post(url, *, headers, json, timeout):
        seen.append(json["model"])
        return handler(json)

    monkeypatch.setattr(brain, "_post_with_deadline", post)
    return seen


def _two_keys(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", "n")


# ── 1: отговор само от интервали е празен отговор ────────────────────────────

def test_a_whitespace_only_reply_moves_the_chain_to_the_next_model(monkeypatch) -> None:
    """e2e 2026-10-22: groq върна content="  \\n " → `genesis -p` излизаше с
    rc=1 „няма отговор“, а nvidia след него изобщо не беше питан."""
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    _two_keys(monkeypatch)
    seen = _route(monkeypatch, lambda p: ok("  \n\t ") if p["model"] == "A" else ok("от C"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "от C"
    assert seen == ["A", "C"]
    # Лекият маршрут: празен текст не е отговор — ходът отива на силния модел.
    assert model_router.light_reply_needs_escalation("  \n", None)
    assert not model_router.light_reply_needs_escalation("здравей", None)


# ── 2: празен `result` от claude CLI ─────────────────────────────────────────

def test_an_empty_claude_cli_result_falls_through_to_the_next_model(monkeypatch, tmp_path) -> None:
    """Фалшив `claude` с {"result": ""} (2026-10-22): Brain връщаше raw_text ''
    като успех и groq/B не се пробваше — всеки друг доставчик вдига
    „празен отговор“. Разходът на празния ход пак се записва."""
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("GENESIS_CLAUDE_CLI", str(exe))
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": " \n ",
               "usage": {"input_tokens": 5, "output_tokens": 0}}
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, json.dumps(payload).encode("utf-8"), b""))
    monkeypatch.setattr(brain, "_load_chain", lambda: [
        {"provider": "claude_cli", "model": "sonnet", "size_b": 999, "supports_tools": False},
        {"provider": "groq", "model": "B", "size_b": 120, "supports_tools": True}])
    monkeypatch.setenv("GROQ_API_KEY", "g")
    rows: list = []
    monkeypatch.setattr("genesis_agent.budget.record_usage",
                        lambda **kw: rows.append((kw["model"], kw["prompt_tokens"])))
    seen = _route(monkeypatch, lambda p: ok("от groq"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "от groq"
    assert seen == ["B"]
    assert ("sonnet", 5) in rows


# ── 3: Retry-After: inf / 1e999 ──────────────────────────────────────────────

@pytest.mark.parametrize("hint", ["inf", "Infinity", "1e999", "nan"])
def test_a_non_finite_retry_after_does_not_crash_the_turn(monkeypatch, hint) -> None:
    """r1.py 2026-10-22: float('1e999') е inf, math.ceil(inf) → OverflowError,
    който не е RuntimeError — излизаше от complete() и nvidia/C не се питаше.
    Нефинитна подсказка е като липсваща: обичайният cooldown от 300 s."""
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    _two_keys(monkeypatch)
    clock = [1_000_000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])

    def handler(p):
        if p["model"] == "A":
            return FakeResp(429, {"error": {"message": "rate"}}, headers={"retry-after": hint})
        return ok("от C")

    seen = _route(monkeypatch, handler)
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "от C" and seen == ["A", "C"]
    assert brain._EXHAUSTED["groq::A"] - clock[0] == brain._EXHAUST_COOLDOWN


# ── 4: текстов WRITE_FILE, отрязан на тавана ─────────────────────────────────

def test_a_text_mode_write_cut_at_the_cap_goes_to_the_next_model(monkeypatch) -> None:
    """r4.py 2026-10-22: модел без native tools пише `[WRITE_FILE: p]…[END_WRITE]`
    без код-ограда; отрязан на max_tokens се връщаше като нормален отговор и
    цикълът казваше на модела, че синтаксисът на тага е грешен. Native пътят
    със същия отрязан запис минава към следващия модел."""
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("groq", "A"), ("nvidia", "C"), tools=False))
    _two_keys(monkeypatch)
    cut = "[WRITE_FILE: app.py]import os\n\ndef main():\n    data = load_all_the_th"
    good = "[WRITE_FILE: app.py]print(1)\n[END_WRITE]"
    seen = _route(monkeypatch, lambda p: ok(cut, finish="length") if p["model"] == "A" else ok(good))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "напиши app.py"}])
    assert r.raw_text == good and seen == ["A", "C"]
    assert not brain._is_exhausted("groq::A")   # таванът не е изчерпана квота
    # Отрязан EDIT_FILE — същото; затворен таг с finish_reason=length е цял.
    assert brain._has_open_file_tag("[EDIT_FILE: a.py]\nold\n=======\nne")
    assert not brain._has_open_file_tag(good)


def test_a_text_mode_write_cut_on_every_model_still_reaches_the_loop(monkeypatch) -> None:
    """Ревю 2026-10-22: с поправката горе текстов файл, по-голям от тавана на
    ВСИЧКИ модели, свършваше с „Error: … HTTP_TRUNCATED“ — преди нея отрязаният
    отговор стигаше до цикъла. Правилото на native пътя (ревю 2026-10-11):
    един кръг, после първият отрязан отговор към цикъла, не грешка."""
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("groq", "A"), ("nvidia", "C"), tools=False))
    _two_keys(monkeypatch)
    slept: list = []
    monkeypatch.setattr(brain.time, "sleep", slept.append)
    cut = "[WRITE_FILE: big.py]import os\n\ndef main():\n    data = load_all_the_th"
    seen = _route(monkeypatch, lambda p: ok(cut, finish="length"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "напиши big.py"}])
    assert seen == ["A", "C"] and slept == []
    assert r.raw_text == cut and not r.code
    # Само отворена код-ограда (без файлов таг) остава грешка, както досега.
    fence = "```python\ndef main():\n    data = load_all_the_th"
    seen.clear()
    _route(monkeypatch, lambda p: ok(fence, finish="length"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "напиши big.py"}])
    assert r.raw_text.startswith("Error:") and "HTTP_TRUNCATED" in r.raw_text
    # Старият път на терминала (call_openai_compatible, без верига) ползва
    # _is_truncated: там отрязаният таг си стига до цикъла, както преди.
    assert not brain._is_truncated(cut)


@pytest.mark.parametrize("text, kept", [
    ("[WRITE_FILE: big.py]import os\nx = [", True),
    ("```python\nx = [", False),
])
def test_anthropic_keeps_a_cut_text_tag_for_the_loop(monkeypatch, text, kept) -> None:
    """Същото правило в _call_anthropic (stop_reason=max_tokens): отрязаният
    текстов таг се пази за цикъла, отрязаната само код-ограда — не."""
    final = SimpleNamespace(stop_reason="max_tokens", usage=None,
                            content=[SimpleNamespace(type="text", text=text)])

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def get_final_message(self):
            return final

    api = SimpleNamespace(stream=lambda **kw: Stream())
    fake = types.ModuleType("anthropic")
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError",
                 "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(fake, name, type(name, (Exception,), {}))
    fake.Anthropic = lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=api), messages=api)
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    b = brain.Brain(use_local=False)
    with pytest.raises(RuntimeError, match="HTTP_TRUNCATED"):
        b._call_anthropic("k", "claude-opus-5", [{"role": "user", "content": "x"}], None, "high")
    assert b._cut_reply == ((text, None) if kept else None)


# ── 5: локалните нива — по точен таг ─────────────────────────────────────────

def test_route_for_goal_keeps_an_installed_local_model_not_a_tag_prefix(monkeypatch) -> None:
    """r7.py 2026-10-22: с инсталиран `qwen2.5-coder:14b-instruct-q4_K_M` нивото
    `qwen2.5-coder:14b` излизаше налично по подниз; route_for_goal сменяше
    работещия qwen2.5-coder:3b с него и последната резерва получаваше 404."""
    installed = ["qwen2.5-coder:3b", "qwen2.5-coder:14b-instruct-q4_K_M"]
    tags = FakeResp(200, {"models": [{"name": n} for n in installed]})
    monkeypatch.setattr(brain.requests, "get", lambda *a, **kw: tags)
    monkeypatch.setattr(model_router, "LOCAL_TIERS",
                        ["qwen2.5-coder:3b", "qwen2.5-coder:7b", "qwen2.5-coder:14b"])
    assert model_router.available_tiers() == [True, False, False]
    b = brain.Brain.__new__(brain.Brain)
    b.local = {"provider": "ollama_local", "model": "qwen2.5-coder:3b"}
    b.current = None
    b.route_for_goal("write a recursive descent parser for arithmetic")
    assert b.local["model"] == "qwen2.5-coder:3b"
