"""Одит 2026-10-27 на веригата в brain.py. Всеки тест пада без поправката си
(пуснато срещу стария код): мрежата е `_post_with_deadline`, SDK-то на
Anthropic е фалшив модул — ключове, cooldown-и, budget_log и самата верига
минават през истинския код."""
from __future__ import annotations

import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

from genesis_agent import anthropic_spend, brain, budget


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


def _budget_entries() -> list[dict]:
    try:
        lines = budget.LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _left(key: str) -> float:
    return brain._EXHAUSTED[key] - time.time()


# ── 1: OpenRouter 403 „moderation/flagged“ е за модела, не за ключа ──────────

_MODERATED = ('{"error":{"message":"Your chosen model requires moderation and '
              'your input was flagged","code":403}}')


def test_a_moderation_403_cools_only_the_model_not_the_openrouter_key(monkeypatch) -> None:
    """Одит 2026-10-27: 403 „requires moderation … flagged“ е отказ на ЕДИН
    модел за ЕДНА заявка, а брояше за мъртъв ключ: OPENROUTER_API_KEY спираше
    за 300 s и вторият OpenRouter модел не се питаше изобщо."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "o1")
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("openrouter", "A"), ("openrouter", "B")))
    seen = _route(monkeypatch, lambda p: FakeResp(403, _MODERATED) if p["model"] == "A"
                  else ok("ok B"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "ok B"
    assert seen == ["A", "B"]
    assert "key::OPENROUTER_API_KEY" not in brain._EXHAUSTED
    assert "openrouter::A" in brain._EXHAUSTED


def test_a_plain_403_still_kills_the_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "o1")
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("openrouter", "A"), ("openrouter", "B")))
    seen = _route(monkeypatch, lambda p: FakeResp(403, '{"error":{"message":"forbidden"}}'))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert "Error" in r.raw_text
    assert seen == ["A"]
    assert "key::OPENROUTER_API_KEY" in brain._EXHAUSTED


def test_rotation_cooldown_treats_a_moderation_403_as_a_model_cooldown() -> None:
    """Същото правило при въртене на ключове (_call_rotating/_call_vertex):
    модерацията не спира ключа — грешката стига до complete(), което спира
    само модела (като 503)."""
    assert brain._rotation_cooldown("key::OPENROUTER_API_KEY#1", "A",
                                    f"HTTP_403: {_MODERATED}") is False
    assert "key::OPENROUTER_API_KEY#1" not in brain._EXHAUSTED
    assert brain._rotation_cooldown("key::OPENROUTER_API_KEY#2", "A", "HTTP_403: forbidden")
    assert "key::OPENROUTER_API_KEY#2" in brain._EXHAUSTED


# ── фалшив anthropic SDK ───────────────────────────────────────────────────────

class _Stream:
    """Поток с снимка след message_start: входът вече е платен."""

    def __init__(self, behaviour: dict, calls: list, model: str) -> None:
        self._behaviour = behaviour
        calls.append(model)
        self.current_message_snapshot = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=behaviour.get("snapshot_in", 100),
                                  output_tokens=0, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
            model=model)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def get_final_message(self):
        if self._behaviour.get("raise"):
            raise self._behaviour["raise"]
        return self._behaviour["final"]


def _fake_anthropic(monkeypatch, behaviour: dict) -> tuple[types.ModuleType, list]:
    calls: list = []
    api = SimpleNamespace(stream=lambda **kw: _Stream(behaviour, calls, kw.get("model", "")))
    fake = types.ModuleType("anthropic")

    class APIStatusError(Exception):
        def __init__(self, msg, status_code=500, headers=None):
            super().__init__(msg)
            self.status_code = status_code
            self.response = SimpleNamespace(headers=headers or {})

    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError",
                 "RateLimitError"):
        setattr(fake, name, type(name, (APIStatusError,), {}))
    fake.APIStatusError = APIStatusError
    fake.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake.Anthropic = lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=api), messages=api)
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake, calls


def _pinned_anthropic(monkeypatch) -> tuple[brain.Brain, list]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a1")
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "B")))
    seen = _route(monkeypatch, lambda p: ok("groq B"))
    return brain.Brain(use_local=False, pin_model=("anthropic", "claude-sonnet-5-5")), seen


# ── 2: Anthropic 429 носи retry-after ─────────────────────────────────────────

@pytest.mark.parametrize("headers, text", [
    ({"retry-after": "3"}, "Error code: 429 - rate_limit_error"),
    ({}, "Error code: 429 - rate limited, please try again in 2s"),
])
def test_an_anthropic_429_cooldown_follows_the_retry_hint(monkeypatch, headers, text) -> None:
    """Одит 2026-10-27: native 429 вдигаше `HTTP_429: …` без `[retry=N]` и
    платеният модел спираше за 300 s при подсказка за 3 s."""
    behaviour: dict = {}
    fake, _calls = _fake_anthropic(monkeypatch, behaviour)
    behaviour["raise"] = fake.RateLimitError(text, 429, headers)
    b, seen = _pinned_anthropic(monkeypatch)
    r = b.complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "groq B" and seen == ["B"]
    assert _left("anthropic::claude-sonnet-5-5") <= brain._RETRY_HINT_MIN_S + 1


# ── 3: странни 200 тела → следващ модел, не изключение от complete() ──────────

def _reply(content, **message) -> dict:
    return {"choices": [{"message": {"content": content, **message}, "finish_reason": "stop"}]}


_JUNK = {
    "usage string + empty": {**_reply(""), "usage": "lots"},
    "usage list + empty": {**_reply(""), "usage": [1]},
    "reasoning int + empty": _reply("", reasoning=5),
    "reasoning list + empty": _reply("", reasoning=["x"]),
    "tool_calls int + length": {"choices": [{"message": {"content": "", "tool_calls": 5},
                                             "finish_reason": "length"}]},
    "tool_calls str + stop": _reply("", tool_calls="abc"),
    "tool_calls [str] + length": {"choices": [{"message": {"content": "", "tool_calls": ["abc"]},
                                               "finish_reason": "length"}]},
}


@pytest.mark.parametrize("body", list(_JUNK.values()), ids=list(_JUNK))
def test_a_junk_200_body_moves_the_chain_to_the_next_model(monkeypatch, body) -> None:
    """Одит 2026-10-27: usage „lots“, reasoning 5, tool_calls 5 излизаха от
    complete() като AttributeError/TypeError — ходът падаше, без да е питан
    следващият модел."""
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setenv("NVIDIA_API_KEY", "n1")
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "B")))
    seen = _route(monkeypatch, lambda p: FakeResp(200, body) if p["model"] == "A" else ok("ok B"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "ok B"
    assert r.tool_calls is None
    assert seen == ["A", "B"]


@pytest.mark.parametrize("cached", ["abc", "12.0", [1]])
def test_junk_cached_tokens_do_not_lose_a_good_reply(monkeypatch, cached) -> None:
    """`int("12.0")` в _openai_usage гърмеше върху иначе валиден отговор."""
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setenv("NVIDIA_API_KEY", "n1")
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "B")))
    body = {**_reply("hi"), "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                      "prompt_tokens_details": {"cached_tokens": cached}}}
    seen = _route(monkeypatch, lambda p: FakeResp(200, body) if p["model"] == "A" else ok("ok B"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "hi" and seen == ["A"]
    assert r.usage["prompt_tokens"] == 5
    assert not r.usage.get("cached_read_tokens")


# ── 4: _sanitize_for_textmode с развалени tool_calls от историята ─────────────

@pytest.mark.parametrize("tcs", [[{"id": "1", "type": "function", "function": None}], ["abc"],
                                 [{"id": "1"}]])
def test_sanitize_for_textmode_survives_odd_tool_calls(tcs) -> None:
    """Одит 2026-10-27: `tc.get('function', {}).get('name')` падаше с
    AttributeError при function: null или запис-низ."""
    out = brain.Brain._sanitize_for_textmode([{"role": "assistant", "content": "", "tool_calls": tcs}])
    assert out[0]["role"] == "assistant"


@pytest.mark.parametrize("tcs", [[{"id": "1", "type": "function", "function": None}], ["abc"]])
def test_odd_history_tool_calls_still_reach_a_text_mode_model(monkeypatch, tcs) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), tools=False))
    seen = _route(monkeypatch, lambda p: ok("fine"))
    hist = [{"role": "user", "content": "do"},
            {"role": "assistant", "content": "", "tool_calls": tcs},
            {"role": "tool", "tool_call_id": "1", "content": "r"},
            {"role": "user", "content": "go"}]
    assert brain.Brain(use_local=False).complete(hist).raw_text == "fine"
    assert seen == ["A"]


# ── 5: скъсан платен поток стига до budget_log ─────────────────────────────────

def test_an_interrupted_anthropic_stream_is_recorded_in_the_budget(monkeypatch) -> None:
    """Одит 2026-10-27: скъсана връзка след message_start се броеше в тавана
    (anthropic_spend), но не и в budget_log — `genesis budget` показваше
    по-малко от сметката."""
    behaviour: dict = {"snapshot_in": 100_000}
    fake, calls = _fake_anthropic(monkeypatch, behaviour)
    behaviour["raise"] = fake.APIConnectionError("Connection reset mid-stream")
    b, _seen = _pinned_anthropic(monkeypatch)
    r = b.complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "groq B" and calls == ["claude-sonnet-5-5"]
    paid = [e for e in _budget_entries() if e["provider"] == "anthropic"]
    assert [(e["model"], e["prompt_tokens"]) for e in paid] == [("claude-sonnet-5-5", 100_000)]
    assert paid[0]["cost_usd"] == pytest.approx(anthropic_spend.total_usd())
    assert anthropic_spend.total_usd() > 0


def test_a_mid_stream_500_retry_logs_each_charge_exactly_once(monkeypatch) -> None:
    """500 посред потока се повтаря веднъж на същия модел: две плащания в
    тавана → два реда в budget_log, не един и не три."""
    behaviour: dict = {"snapshot_in": 1_000}
    fake, calls = _fake_anthropic(monkeypatch, behaviour)
    behaviour["raise"] = fake.APIStatusError("Error code: 500 - internal", 500)
    b, _seen = _pinned_anthropic(monkeypatch)
    assert b.complete([{"role": "user", "content": "hi"}]).raw_text == "groq B"
    assert calls == ["claude-sonnet-5-5"] * 2
    paid = [e for e in _budget_entries() if e["provider"] == "anthropic"]
    assert len(paid) == 2
    assert sum(e["cost_usd"] for e in paid) == pytest.approx(anthropic_spend.total_usd())
