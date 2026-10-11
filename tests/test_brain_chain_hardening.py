"""Brain веригата през истинския HTTP път (одит 2026-10-11): всеки тест подменя
само `_post_with_deadline` (мрежата) и веригата, а ключове, cooldown-и, превод
на съобщенията и отчетът на разхода минават през истинския код."""
from __future__ import annotations

import json
import time

import pytest

from genesis_agent import brain


class FakeResp:
    def __init__(self, status: int, body, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self._body) if isinstance(self._body, str) else self._body


def ok(content=None, tool_calls=None, finish="stop", usage=None) -> FakeResp:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return FakeResp(200, {"choices": [{"message": msg, "finish_reason": finish}],
                          "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}})


def chain(*entries) -> list[dict]:
    return [{"provider": p, "model": m, "size_b": 120, "supports_tools": True} for p, m in entries]


_KEY_ENVS = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
             "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(brain, "_EXHAUSTED", {})
    monkeypatch.setattr(brain, "_KEY_429_MODELS", {})
    monkeypatch.setattr(brain, "ENV_FILES", ())
    monkeypatch.setattr("genesis_agent.paths.ENV_FILES", ())
    monkeypatch.setattr(brain, "_local_available", lambda m: False)
    for base in _KEY_ENVS:
        monkeypatch.delenv(base, raising=False)
        for i in range(2, 11):
            monkeypatch.delenv(f"{base}_{i}", raising=False)
    # Статистиката на доставчиците не бива да пренарежда фалшивите вериги.
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)
    yield


def _route(monkeypatch, handler) -> list:
    seen: list = []

    def post(url, *, headers, json, timeout):
        seen.append((json["model"], headers.get("Authorization", "")[-2:]))
        return handler(json)

    monkeypatch.setattr(brain, "_post_with_deadline", post)
    return seen


# ── F1: няколко ключа (GROQ_API_KEY + _2 + _3) ────────────────────────────────

def test_a_tpm_429_on_one_model_does_not_take_down_every_key_for_every_model(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("groq", "A"), ("groq", "B"), ("nvidia", "C")))
    for k, v in (("GROQ_API_KEY", "g1"), ("GROQ_API_KEY_2", "g2"), ("GROQ_API_KEY_3", "g3"),
                 ("NVIDIA_API_KEY", " n")):
        monkeypatch.setenv(k, v)

    def handler(payload):
        if payload["model"] == "A":
            return FakeResp(429, {"error": {"message": "Rate limit reached for model A (TPM)"}})
        return ok("ok " + payload["model"])

    seen = _route(monkeypatch, handler)
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "ok B"
    assert seen == [("A", "g1"), ("A", "g2"), ("A", "g3"), ("B", "g1")]
    # Ключовете остават здрави за другите модели.
    assert not any(brain._is_exhausted(f"key::GROQ_API_KEY#{i}") for i in (1, 2, 3))


def test_a_second_models_429_on_the_same_key_still_stops_that_key(monkeypatch) -> None:
    b = brain.Brain.__new__(brain.Brain)
    b.timeout = 30

    def fake_http(base_url, key, model, messages, timeout, tools=None, extra=None):
        raise RuntimeError("HTTP_429: rate limited")

    monkeypatch.setattr(b, "_http", fake_http)
    for model in ("A", "B"):
        with pytest.raises(RuntimeError):
            b._call_rotating("https://x", "GROQ_API_KEY", model, [], None, ["g1", "g2"])
    assert brain._is_exhausted("key::GROQ_API_KEY#1")
    assert brain._is_exhausted("key::GROQ_API_KEY#2")


def test_a_401_still_stops_the_whole_rotating_key_at_once(monkeypatch) -> None:
    b = brain.Brain.__new__(brain.Brain)
    b.timeout = 30
    calls: list[str] = []

    def fake_http(base_url, key, model, messages, timeout, tools=None, extra=None):
        calls.append(key)
        if key == "g1":
            raise RuntimeError("HTTP_401: invalid key")
        return "ok", None

    monkeypatch.setattr(b, "_http", fake_http)
    assert b._call_rotating("https://x", "GROQ_API_KEY", "A", [], None, ["g1", "g2"]) == ("ok", None)
    assert brain._is_exhausted("key::GROQ_API_KEY#1")


def test_a_vertex_429_on_one_model_leaves_the_project_for_another_model(monkeypatch) -> None:
    from genesis_agent import vertex_auth
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    monkeypatch.setattr(vertex_auth, "auth_available", lambda: True)
    monkeypatch.setattr(vertex_auth, "token", lambda p: "tok")
    seen: list[str] = []

    def fake_http(self, base_url, key, model, messages, timeout, tools=None, extra=None):
        seen.append(model)
        if model == "A":
            raise RuntimeError("HTTP_429: RESOURCE_EXHAUSTED")
        return "ok", None

    monkeypatch.setattr(brain.Brain, "_http", fake_http)
    b = brain.Brain(use_local=False)
    with pytest.raises(RuntimeError, match="HTTP_429"):
        b._call("vertex", "A", [{"role": "user", "content": "x"}])
    assert b._call("vertex", "B", [{"role": "user", "content": "x"}]) == ("ok", None)
    assert seen == ["A", "B"]


# ── F2: Brain чете .env като paths.read_env_files ────────────────────────────

def test_brain_reads_the_same_keys_as_paths_from_a_bom_and_a_quoted_hash(monkeypatch, tmp_path) -> None:
    from genesis_agent import paths
    env = tmp_path / ".env"
    # PowerShell 5.1 / стар Notepad: UTF-8 с BOM пред първия ключ.
    env.write_bytes('GROQ_API_KEY=gsk_real\nOPENAI_API_KEY="sk-proj #1"\n'
                    'export NVIDIA_API_KEY=nv  # бележка\n'.encode("utf-8-sig"))
    monkeypatch.setattr(brain, "ENV_FILES", (str(env),))
    monkeypatch.setattr(paths, "ENV_FILES", (str(env),))
    b = brain.Brain(use_local=False)
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY"):
        assert b._provider_key(name) == paths.read_env_files(name), name
    assert b._provider_key("GROQ_API_KEY") == "gsk_real"
    assert b._provider_key("OPENAI_API_KEY") == "sk-proj #1"
    assert b._provider_key("NVIDIA_API_KEY") == "nv"


# ── F3: прескочен заради ключа модел не получава свой 300 s cooldown ─────────

def test_a_model_skipped_for_its_keys_cooldown_is_back_when_the_key_is(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain",
                        lambda: chain(("groq", "A"), ("groq", "B"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")
    clock = [1_000_000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])
    monkeypatch.setattr(brain.time, "sleep", lambda s: None)
    brain._EXHAUSTED["key::GROQ_API_KEY"] = clock[0] + 1
    seen = _route(monkeypatch, lambda p: ok("от " + p["model"]))
    b = brain.Brain(use_local=False)
    assert b.complete([{"role": "user", "content": "hi"}]).raw_text == "от C"
    assert not brain._is_exhausted("groq::A") and not brain._is_exhausted("groq::B")
    clock[0] += 1.1
    seen.clear()
    assert b.complete([{"role": "user", "content": "hi"}]).raw_text == "от A"
    assert [m for m, _ in seen] == ["A"]


def test_a_model_skipped_because_all_rotating_keys_cool_down_is_not_marked(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g1")
    monkeypatch.setenv("GROQ_API_KEY_2", "g2")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")
    brain._mark_exhausted("key::GROQ_API_KEY#1")
    brain._mark_exhausted("key::GROQ_API_KEY#2")
    _route(monkeypatch, lambda p: ok("от " + p["model"]))
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == "от C"
    assert "groq::A" not in brain._EXHAUSTED


# ── F4: отрязан на тавана tool_call и платен празен отговор ──────────────────

_TOOLS = [{"type": "function", "function": {"name": "WRITE_FILE", "parameters": {"type": "object"}}}]


def _usage_rows(monkeypatch) -> list:
    rows: list = []
    monkeypatch.setattr("genesis_agent.budget.record_usage",
                        lambda **kw: rows.append((kw["model"], kw["completion_tokens"])))
    return rows


def test_tool_call_arguments_cut_at_max_tokens_go_to_the_next_model(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")
    cut = json.dumps({"path": "app.py", "content": "x = 1\n" * 50})[:120]
    good = json.dumps({"path": "app.py", "content": "x = 1\n"})

    def handler(p):
        args = cut if p["model"] == "A" else good
        tc = [{"id": "c1", "type": "function", "function": {"name": "WRITE_FILE", "arguments": args}}]
        return ok(None, tool_calls=tc, finish="length" if p["model"] == "A" else "tool_calls",
                  usage={"prompt_tokens": 3000, "completion_tokens": 4096 if p["model"] == "A" else 40})

    seen = _route(monkeypatch, handler)
    rows = _usage_rows(monkeypatch)
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "напиши app.py"}], tools=_TOOLS)
    assert [m for m, _ in seen] == ["A", "C"]
    assert json.loads(r.tool_calls[0]["function"]["arguments"])["path"] == "app.py"
    assert rows == [("A", 4096), ("C", 40)]
    assert not brain._is_exhausted("groq::A")   # таванът не е изчерпана квота


def test_complete_tool_call_arguments_with_length_are_still_accepted(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    args = json.dumps({"path": "a.py", "content": "x"})
    tc = [{"id": "c1", "type": "function", "function": {"name": "WRITE_FILE", "arguments": args}}]
    seen = _route(monkeypatch, lambda p: ok(None, tool_calls=tc, finish="length"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "x"}], tools=_TOOLS)
    assert [m for m, _ in seen] == ["A"] and r.tool_calls


def test_an_empty_but_paid_reply_is_recorded_in_the_budget(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")

    def handler(p):
        if p["model"] == "A":
            return FakeResp(200, {"choices": [{"message": {"role": "assistant", "content": "",
                                                           "reasoning": "r" * 9000},
                                               "finish_reason": "length"}],
                                  "usage": {"prompt_tokens": 3500, "completion_tokens": 4096}})
        return ok("от C", usage={"prompt_tokens": 3500, "completion_tokens": 50})

    _route(monkeypatch, handler)
    rows = _usage_rows(monkeypatch)
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == "от C"
    assert rows == [("A", 4096), ("C", 50)]


# ── F5: повреден HTTP 200 не излиза от complete() като изключение ────────────

@pytest.mark.parametrize("body, expected", [
    ({"choices": [{"message": None, "finish_reason": "error",
                   "error": {"code": 502, "message": "upstream error"}}]}, "от C"),
    ({"choices": [{"message": {"role": "assistant",
                               "content": [{"type": "text", "text": "здрасти"}, {"type": "image"}]},
                   "finish_reason": "stop"}]}, "здрасти"),
    ({"choices": [{"message": {"role": "assistant", "content": {"text": "?"}},
                   "finish_reason": "stop"}]}, "от C"),
], ids=["message-null", "content-parts", "content-dict"])
def test_a_malformed_200_falls_through_or_is_read_as_text(monkeypatch, body, expected) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("openrouter", "A"), ("nvidia", "C")))
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")
    _route(monkeypatch, lambda p: FakeResp(200, body) if p["model"] == "A" else ok("от C"))
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == expected


# ── F6: 429 с подсказка от сървъра (retry-after / „try again in 1.2s“) ───────

_TPM = ("Rate limit reached for model openai/gpt-oss-120b on tokens per minute (TPM). "
        "Please try again in 1.2s.")


@pytest.mark.parametrize("headers, message, low, high", [
    ({"retry-after": "2"}, _TPM, 1, 10),
    ({}, _TPM, 1, 10),
    ({}, "Rate limit reached. Please try again in 1m30.5s.", 85, 95),
    ({}, "Rate limit reached. Please try again in 450ms.", 1, 10),
    ({"retry-after": "86400"}, "daily quota", 299, 300),
    ({}, "Rate limit exceeded", 299, 300),
], ids=["header", "body-seconds", "body-minutes", "body-ms", "huge-header", "no-hint"])
def test_a_429_cooldown_follows_the_servers_hint(monkeypatch, headers, message, low, high) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("groq", "B")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    clock = [1_000_000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])

    def handler(p):
        if p["model"] == "A":
            return FakeResp(429, {"error": {"message": message}}, headers=headers)
        return ok("от " + p["model"])

    _route(monkeypatch, handler)
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == "от B"
    left = brain._EXHAUSTED["groq::A"] - clock[0]
    assert low <= left <= high, left


def test_a_short_429_lets_the_start_model_answer_again_seconds_later(monkeypatch) -> None:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("groq", "B")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    clock = [1_000_000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])
    answers = iter([FakeResp(429, {"error": {"message": _TPM}}, headers={"retry-after": "2"}),
                    ok("от A")])
    seen = _route(monkeypatch, lambda p: next(answers) if p["model"] == "A" else ok("от B"))
    b = brain.Brain(use_local=False)
    assert b.complete([{"role": "user", "content": "hi"}]).raw_text == "от B"
    clock[0] += 11
    assert b.complete([{"role": "user", "content": "hi"}]).raw_text == "от A"
    assert [m for m, _ in seen] == ["A", "B", "A"]


# ── F7: излишните 8 s паузи ──────────────────────────────────────────────────

@pytest.fixture
def _slept(monkeypatch) -> list:
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setattr(brain, "_load_light_chain", lambda: chain(("groq", "small")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", " n")
    slept: list = []
    monkeypatch.setattr(brain.time, "sleep", slept.append)
    return slept


def test_a_round_with_every_model_cooling_down_does_not_pause(monkeypatch, _slept) -> None:
    for k in ("groq::A", "nvidia::C"):
        brain._EXHAUSTED[k] = time.time() + 290
    seen = _route(monkeypatch, lambda p: ok("x"))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert seen == [] and _slept == []
    assert "cooldown" in r.raw_text and "неизвестна грешка" not in r.raw_text


def test_a_cooldown_ending_within_the_pause_still_gets_the_second_round(monkeypatch, _slept) -> None:
    clock = [1_000_000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])
    monkeypatch.setattr(brain.time, "sleep", lambda s: (_slept.append(s), clock.__setitem__(0, clock[0] + s)))
    brain._EXHAUSTED["groq::A"] = clock[0] + 3
    brain._EXHAUSTED["nvidia::C"] = clock[0] + 290
    _route(monkeypatch, lambda p: ok("от " + p["model"]))
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == "от A"
    assert _slept == [8]


def test_the_light_route_walks_the_chain_once(monkeypatch, _slept) -> None:
    import requests

    def down(p):
        raise requests.exceptions.ConnectionError("network down")

    seen = _route(monkeypatch, down)
    r = brain.Brain(light=True, use_local=False).complete([{"role": "user", "content": "hi"}])
    assert r.raw_text.startswith("Error:")
    assert [m for m, _ in seen] == ["small", "A", "C"] and _slept == []


def test_a_light_chat_turn_with_the_network_down_waits_one_pause_at_most(monkeypatch, _slept) -> None:
    import requests

    import genesis_terminal_agent as gta

    def down(p):
        raise requests.exceptions.ConnectionError("network down")

    _route(monkeypatch, down)
    monkeypatch.setattr(gta, "_CODING_MODE", False)
    monkeypatch.setattr(gta, "current_provider", "groq")
    monkeypatch.setattr(gta, "current_model_id", "A")
    text, _calls = gta.ask_genesis([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
    assert sum(_slept) <= 8 and "network down" in text
