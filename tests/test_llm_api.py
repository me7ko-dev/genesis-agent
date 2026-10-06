"""`genesis api` — локалният текстов вход. Без мрежа: Brain е фалшив."""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from genesis_agent import llm_api


class FakeBrain:
    def __init__(self, replies: list[str], delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.calls: list[dict] = []
        self.chain = [{"provider": "groq", "model": "fake-fast"},
                      {"provider": "nvidia", "model": "fake-big"}]
        self.current: dict | None = self.chain[0]

    def complete(self, messages, tools=None, avoid=None):
        self.calls.append({"messages": messages, "tools": tools, "avoid": avoid})
        if self.delay:
            time.sleep(self.delay)
        text = self.replies.pop(0) if self.replies else "Добър ден."
        self.current = self.chain[1] if avoid else self.chain[0]
        return type("R", (), {"raw_text": text, "code": "", "tool_calls": None,
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture
def api(monkeypatch):
    brain = FakeBrain([])
    monkeypatch.setattr(llm_api, "_make_brain", lambda: brain)
    httpd = llm_api.ApiServer(0, request_timeout=2.0)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        conn.request(method, path, body=data, headers=headers or {})
        r = conn.getresponse()
        text = r.read().decode("utf-8")
        conn.close()
        return r.status, (json.loads(text) if text else None), dict(r.getheaders())

    call.brain = brain  # type: ignore[attr-defined]
    call.port = port  # type: ignore[attr-defined]
    call.httpd = httpd  # type: ignore[attr-defined]
    yield call
    httpd.shutdown()
    httpd.server_close()


CHAT = {"messages": [{"role": "system", "content": "Ти си баба Мара."},
                     {"role": "user", "content": "Здравей!"}]}


def test_health(api) -> None:
    status, body, _ = api("GET", "/v1/health")
    assert status == 200
    assert body["app"] == "genesis" and body["api"] == 1 and body["ok"] is True
    assert body["ready"] is True
    assert body["models"] == ["groq/fake-fast", "nvidia/fake-big"]
    assert body["providers"] == ["groq", "nvidia"]


def test_chat_returns_openai_shape_and_never_passes_tools(api) -> None:
    api.brain.replies = ["Добре дошъл, чедо."]
    status, body, _ = api("POST", "/v1/chat/completions", CHAT)
    assert status == 200
    assert body["object"] == "chat.completion"
    assert body["model"] == "groq/fake-fast"
    choice = body["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "Добре дошъл, чедо."}
    assert choice["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 15
    (call,) = api.brain.calls
    assert call["tools"] is None
    assert call["messages"] == CHAT["messages"]


def test_text_parts_content_is_joined(api) -> None:
    msg = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Здра"},
                                                     {"type": "text", "text": "вей"}]}]}
    status, _, _ = api("POST", "/v1/chat/completions", msg)
    assert status == 200
    assert api.brain.calls[0]["messages"] == [{"role": "user", "content": "Здравей"}]


def test_wrong_host_is_rejected(api) -> None:
    status, body, _ = api("GET", "/v1/health", headers={"Host": f"evil.example:{api.port}"})
    assert status == 403 and "Host" in body["error"]["message"]
    status, _, _ = api("GET", "/v1/health", headers={"Host": f"localhost:{api.port}"})
    assert status == 200


def test_foreign_origin_is_rejected_local_origin_reflected(api) -> None:
    status, _, headers = api("POST", "/v1/chat/completions", CHAT,
                             headers={"Origin": "https://evil.example"})
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers
    assert api.brain.calls == []
    status, _, _ = api("GET", "/v1/health", headers={"Origin": "http://127.0.0.1.evil.example"})
    assert status == 403
    status, _, headers = api("POST", "/v1/chat/completions", CHAT,
                             headers={"Origin": "http://localhost:5173"})
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173"


def test_preflight(api) -> None:
    hdrs = {"Origin": "http://127.0.0.1:4173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"}
    status, _, headers = api("OPTIONS", "/v1/chat/completions", headers=hdrs)
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:4173"
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]
    status, _, headers = api("OPTIONS", "/v1/chat/completions",
                             headers={**hdrs, "Origin": "null"})
    assert status == 403


def test_body_and_message_limits(api) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    conn.putrequest("POST", "/v1/chat/completions")
    conn.putheader("Content-Length", str(llm_api.MAX_BODY + 1))
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 413
    conn.close()
    many = {"messages": [{"role": "user", "content": "x"}] * (llm_api.MAX_MESSAGES + 1)}
    status, _, _ = api("POST", "/v1/chat/completions", many)
    assert status == 400
    assert api.brain.calls == []


@pytest.mark.parametrize("payload", [
    None, [], {"messages": []}, {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": 5}]}, {**CHAT, "stream": True}])
def test_bad_requests_are_400(api, payload) -> None:
    raw = b"not json" if payload is None else json.dumps(payload).encode()
    status, body, _ = api("POST", "/v1/chat/completions", raw=raw)
    assert status == 400 and body["error"]["message"]


def test_unknown_path_and_method(api) -> None:
    assert api("GET", "/v1/models")[0] == 404
    assert api("GET", "/v1/chat/completions")[0] == 405


def test_brain_error_is_502(api) -> None:
    api.brain.replies = ["Error: цялата верига е изчерпана | последна: HTTP_429"]
    status, body, _ = api("POST", "/v1/chat/completions", CHAT)
    assert status == 502
    assert "изчерпана" in body["error"]["message"]


def test_slow_model_is_504(api) -> None:
    api.brain.delay = 3.0  # над request_timeout=2.0
    status, body, _ = api("POST", "/v1/chat/completions", CHAT)
    assert status == 504 and body["error"]["message"]


def test_no_configured_model_is_503(api) -> None:
    api.brain.chain = []
    status, _, _ = api("POST", "/v1/chat/completions", CHAT)
    assert status == 503


def test_json_mode_extracts_object_and_adds_instruction(api) -> None:
    api.brain.replies = ['Ето:\n```json\n{"реплика": "Здрасти", "настроение": 3}\n```']
    status, body, _ = api("POST", "/v1/chat/completions",
                          {**CHAT, "response_format": {"type": "json_object"}})
    assert status == 200
    assert json.loads(body["choices"][0]["message"]["content"]) == {
        "реплика": "Здрасти", "настроение": 3}
    sent = api.brain.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[0]["content"].startswith("Ти си баба Мара.")
    assert llm_api.JSON_INSTRUCTION in sent[0]["content"]
    assert len(sent) == 2


def test_json_mode_retries_once_on_another_model(api) -> None:
    api.brain.replies = ["Не знам какво е JSON.", '{"ok": true}']
    status, body, _ = api("POST", "/v1/chat/completions",
                          {**CHAT, "response_format": {"type": "json_object"}})
    assert status == 200
    assert body["model"] == "nvidia/fake-big"
    assert api.brain.calls[1]["avoid"] == ("groq", "fake-fast")
    api.brain.replies = ["нищо", "пак нищо"]
    status, _, _ = api("POST", "/v1/chat/completions",
                       {**CHAT, "response_format": {"type": "json_object"}})
    assert status == 502


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Ето отговора: {"a": {"b": [1, 2]}} — това е.', {"a": {"b": [1, 2]}}),
    ('```json\n{"a": "}"}\n```', {"a": "}"}),
    ('{счупен} и после {"a": 2}', {"a": 2}),
])
def test_extract_json_object(text, expected) -> None:
    assert json.loads(llm_api.extract_json_object(text) or "null") == expected


def test_extract_json_object_none() -> None:
    assert llm_api.extract_json_object("само текст [1, 2]") is None


def test_json_instruction_without_system_message() -> None:
    out = llm_api.with_json_instruction([{"role": "user", "content": "x"}])
    assert out[0] == {"role": "system", "content": llm_api.JSON_INSTRUCTION}


def test_make_brain_is_text_only_fast_and_free(monkeypatch) -> None:
    from genesis_agent import brain as brain_mod
    monkeypatch.setenv("GENESIS_QUALITY", "max")
    monkeypatch.setattr(brain_mod, "_load_keys", lambda: {
        "GROQ_API_KEY": "k", "NVIDIA_API_KEY": "k", "ANTHROPIC_API_KEY": "k"})
    monkeypatch.setattr(brain_mod, "_load_chain", lambda: [
        {"provider": p, "model": f"m{i}", "size_b": 70, "supports_tools": True}
        for i, p in enumerate(["groq", "openrouter", "nvidia"] * 4)])
    monkeypatch.setattr(brain_mod, "_load_light_chain", lambda: [
        {"provider": "groq", "model": "light", "size_b": 20, "supports_tools": False}])
    b = llm_api._make_brain()
    assert b.local is None
    assert b.timeout == llm_api.MODEL_TIMEOUT_S
    assert b.premium == []
    assert [c["model"] for c in b.chain[:2]] == ["m0", "light"]  # стартовият, после леките
    assert len(b.chain) == llm_api.MAX_MODELS
    assert {c["provider"] for c in b.chain} == {"groq", "nvidia"}  # без ключ → вън


def test_serve_rejects_bad_port(capsys) -> None:
    assert llm_api.serve(["--port", "abc"]) == 2
    assert llm_api.serve(["--bogus"]) == 2
    assert llm_api.serve(["--help"]) == 0
    assert "genesis api" in capsys.readouterr().out


def test_cli_usage_lists_api() -> None:
    from genesis_agent.cli import USAGE
    assert "genesis api" in USAGE
