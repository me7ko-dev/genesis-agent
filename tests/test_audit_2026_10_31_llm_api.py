"""Одит 2026-10-31 (шести кръг): `genesis api` (genesis_agent/llm_api.py) —
JSON режимът (фрагмент от счупен обект, NaN/Infinity), срокът на модел и на
заявката, боклук в usage, тяло с BOM, CORS заглавките на openai JS SDK и 413
при голямо тяло. Без мрежа и ключове: Brain е фалшив или говори с фалшив
доставчик на 127.0.0.1. Всеки тест падаше срещу кода преди поправката."""
from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from genesis_agent import llm_api

CHAT = {"messages": [{"role": "system", "content": "Ти си баба Мара."},
                     {"role": "user", "content": "Здравей!"}]}
JSON_CHAT = {**CHAT, "response_format": {"type": "json_object"}}


class FakeBrain:
    def __init__(self, replies: list[str] | None = None, delay: float = 0.0,
                 usage: object = None) -> None:
        self.replies = list(replies or [])
        self.delay = delay
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5} if usage is None else usage
        self.calls: list[dict] = []
        self.chain = [{"provider": "groq", "model": "fake-fast"},
                      {"provider": "nvidia", "model": "fake-big"}]
        self.current: dict | None = self.chain[0]

    def complete(self, messages, tools=None, avoid=None):
        self.calls.append({"messages": messages, "avoid": avoid})
        if self.delay:
            time.sleep(self.delay)
        text = self.replies.pop(0) if self.replies else "Добър ден."
        self.current = self.chain[1] if avoid else self.chain[0]
        return type("R", (), {"raw_text": text, "code": "", "tool_calls": None,
                              "usage": self.usage})


def _serve(monkeypatch, brain: object | None = None, **kw) -> llm_api.ApiServer:
    if brain is not None:
        monkeypatch.setattr(llm_api, "_make_brain", lambda: brain)
    httpd = llm_api.ApiServer(0, **kw)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd


def _stop(httpd: llm_api.ApiServer) -> None:
    httpd.shutdown()
    httpd.server_close()


def _call(httpd: llm_api.ApiServer, method: str, path: str, body: dict | None = None,
          headers: dict | None = None, raw: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=15)
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    conn.request(method, path, body=data, headers=headers or {})
    r = conn.getresponse()
    text = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(text) if text else None), dict(r.getheaders())


def _content(body: dict) -> str:
    return body["choices"][0]["message"]["content"]


def _strict_json(text: str):
    """Като JSON.parse в браузъра: NaN/Infinity не са JSON (RFC 8259)."""
    def no_constant(name: str):
        raise ValueError(f"не е JSON: {name}")
    return json.loads(text, parse_constant=no_constant)


# ── a1: счупен външен обект → вложен фрагмент като „валиден“ отговор ────────

@pytest.mark.parametrize("text", [
    '{"реплика": "Здрасти", "настроение": {"ниво": 3},}',             # запетая накрая
    "{'reply': 'Hi', 'mood': {\"level\": 3}}",                          # единични кавички
    '[{"a": 1}, {"b": 2}]',                                             # масив отгоре
    '{"реплика": "Тя каза "здрасти" и", "настроение": {"вид": "радост", "сила": 3}}',
    '{"реплика": "Здрасти", "настроение": {"вид": "радост", "сила": 3}',  # отрязан
])
def test_a1_a_broken_outer_object_is_not_answered_with_its_inner_fragment(text) -> None:
    assert llm_api.extract_json_object(text) is None


def test_a1_a_trailing_comma_reply_is_retried_on_another_model(monkeypatch) -> None:
    good = {"реплика": "Здрасти, чедо", "настроение": {"ниво": 3}}
    brain = FakeBrain(['{"реплика": "Здрасти, чедо", "настроение": {"ниво": 3},}',
                       json.dumps(good, ensure_ascii=False)])
    httpd = _serve(monkeypatch, brain, request_timeout=5.0)
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", JSON_CHAT)
    finally:
        _stop(httpd)
    assert status == 200
    assert json.loads(_content(body)) == good
    assert body["model"] == "nvidia/fake-big"
    assert len(brain.calls) == 2 and brain.calls[1]["avoid"] == ("groq", "fake-fast")


# ── a8: NaN / Infinity / 1e400 минаваха като „валиден JSON обект“ ───────────

@pytest.mark.parametrize("text", ['{"score": NaN}', '{"score": 1e400}', '{"a": -Infinity}'])
def test_a8_non_rfc_numbers_are_not_a_valid_json_object(text) -> None:
    assert llm_api.extract_json_object(text) is None


def test_a8_a_nan_reply_is_retried_and_the_answer_parses_strictly(monkeypatch) -> None:
    brain = FakeBrain(['{"доверие": NaN}', '{"доверие": 0.5}'])
    httpd = _serve(monkeypatch, brain, request_timeout=5.0)
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", JSON_CHAT)
    finally:
        _stop(httpd)
    assert status == 200
    assert _strict_json(_content(body)) == {"доверие": 0.5}
    assert len(brain.calls) == 2


# ── a3: „8 s на модел“ беше 32 s — капещ отговор държеше цялата верига ──────

class _Provider:
    """Фалшив OpenAI-съвместим доставчик зад GENESIS_MODEL_GATEWAY.
    openrouter праща по интервал на 0.25 s (празните редове, с които държи
    връзката жива — 2026-09-25), nvidia отговаря веднага."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.stop = threading.Event()
        prov = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                return

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                name = self.path.strip("/").split("/")[0]
                prov.calls.append((name, time.monotonic()))
                text = "бавният" if name == "openrouter" else "бързият"
                body = json.dumps({"choices": [{"message": {"role": "assistant",
                                                            "content": text},
                                                "finish_reason": "stop"}]}).encode()
                pad = 24 if name == "openrouter" else 0  # 6 s, над 4 × 1 s
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(pad + len(body)))
                self.end_headers()
                try:
                    for _ in range(pad):
                        if prov.stop.wait(0.25):
                            return
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def _real_chain(monkeypatch, chain: list[tuple[str, str]], keys: dict[str, str]) -> None:
    from genesis_agent import brain as brain_mod
    monkeypatch.setattr(brain_mod, "_load_keys", lambda: dict(keys))
    monkeypatch.setattr(brain_mod, "_load_chain", lambda: [
        {"provider": p, "model": m, "size_b": 70, "supports_tools": True} for p, m in chain])
    monkeypatch.setattr(brain_mod, "_load_light_chain", list)
    monkeypatch.setattr(brain_mod, "_EXHAUSTED", {})
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)


def test_a3_a_trickling_model_is_dropped_after_model_timeout_s(monkeypatch) -> None:
    """Мащаб 1/8: MODEL_TIMEOUT_S=1, срок на заявката 3 s. Преди: Brain държеше
    капещия модел timeout × 4 = 4 s, API-то връщаше 504 на 3 s, а здравият
    втори модел се питаше чак след това."""
    prov = _Provider()
    _real_chain(monkeypatch, [("openrouter", "m0"), ("nvidia", "m1")],
                {"OPENROUTER_API_KEY": "k", "NVIDIA_API_KEY": "k"})
    monkeypatch.setenv("GENESIS_MODEL_GATEWAY", f"http://127.0.0.1:{prov.httpd.server_address[1]}")
    monkeypatch.setattr(llm_api, "MODEL_TIMEOUT_S", 1)
    httpd = _serve(monkeypatch, request_timeout=3.0)
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", CHAT)
    finally:
        _stop(httpd)
        prov.close()
    # 200 значи, че вторият модел е тръгнал преди start_by (3 − 1 = 2 s).
    assert status == 200, (status, body, prov.calls)
    assert _content(body) == "бързият" and body["model"] == "nvidia/m1"


# ── a5: обръщения, тръгнали без време да стигнат до клиента ─────────────────

def test_a5_a_slot_freed_too_late_does_not_start_a_model_call(monkeypatch) -> None:
    """parallel=1, модел 1.5 s, срок 2 s: втората заявка получаваше мястото с
    0.5 s оставащи, пускаше цяло обръщение (квота, място) и връщаше 504."""
    brain = FakeBrain(delay=1.5)
    httpd = _serve(monkeypatch, brain, request_timeout=2.0, parallel=1)
    statuses: list[int] = []

    def ask() -> None:
        statuses.append(_call(httpd, "POST", "/v1/chat/completions", CHAT)[0])

    try:
        workers = [threading.Thread(target=ask) for _ in range(2)]
        for w in workers:
            w.start()
            time.sleep(0.05)
        for w in workers:
            w.join(10)
        time.sleep(0.3)
    finally:
        _stop(httpd)
    assert sorted(statuses) == [200, 503]
    assert len(brain.calls) == 1


def test_a5_the_json_retry_is_not_started_without_time_for_it(monkeypatch) -> None:
    """Първият отговор не е JSON след 1.5 s от срок 2 s: повторният опит на
    друг модел тръгваше на +1.5 s и клиентът пак получаваше 504."""
    brain = FakeBrain(["Не знам какво е JSON.", '{"ok": true}'], delay=1.5)
    httpd = _serve(monkeypatch, brain, request_timeout=2.0)
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", JSON_CHAT)
    finally:
        _stop(httpd)
    assert (status, len(brain.calls)) == (502, 1), body
    assert "JSON" in body["error"]["message"]


def test_a5_the_chain_stops_at_the_deadline_instead_of_walking_on(monkeypatch) -> None:
    """Истинският Brain, всеки модел мълчи 1 s: след 504 работникът вървеше по
    цялата верига (обръщения на +16, +24, +32, +40 s при истинските числа) и
    държеше мястото. Сега след срока не тръгва ново обръщение."""
    import requests

    from genesis_agent import brain as brain_mod
    _real_chain(monkeypatch, [("groq", f"m{i}") for i in range(4)], {"GROQ_API_KEY": "k"})
    started: list[str] = []

    def post(url, *, headers, json, timeout, deadline=None):
        started.append(json["model"])
        time.sleep(1.0)
        raise requests.exceptions.Timeout("мълчи")

    monkeypatch.setattr(brain_mod, "_post_with_deadline", post)
    httpd = _serve(monkeypatch, request_timeout=2.5, parallel=1)
    try:
        status, _, _ = _call(httpd, "POST", "/v1/chat/completions", CHAT)
        assert httpd.slots.acquire(timeout=10)  # работникът е свършил
        httpd.slots.release()
    finally:
        _stop(httpd)
    assert status == 504
    assert started == ["m0", "m1"]


# ── a6: боклук в usage на доставчика → 500 и загубен отговор ────────────────

@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": "n/a", "completion_tokens": 3}, (0, 3)),
    ({"prompt_tokens": {"x": 1}, "completion_tokens": 2}, (0, 2)),
    ({"prompt_tokens": float("nan"), "completion_tokens": 2}, (0, 2)),
    ({"prompt_tokens": float("inf"), "completion_tokens": 2}, (0, 2)),
    ({"prompt_tokens": [1], "completion_tokens": "lots"}, None),
    ({"prompt_tokens": 12.5, "completion_tokens": "3.0"}, (12, 3)),
])
def test_a6_garbage_usage_does_not_lose_the_reply(monkeypatch, usage, expected) -> None:
    httpd = _serve(monkeypatch, FakeBrain(usage=usage))
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", CHAT)
    finally:
        _stop(httpd)
    assert status == 200, body
    assert _content(body) == "Добър ден."
    if expected is None:
        assert "usage" not in body
    else:
        p, c = expected
        assert body["usage"] == {"prompt_tokens": p, "completion_tokens": c,
                                 "total_tokens": p + c}


# ── a9: тяло с UTF-8 BOM (C# Encoding.UTF8, PowerShell 5.1) → 400 ───────────

def test_a9_a_body_with_a_utf8_bom_is_accepted(monkeypatch) -> None:
    brain = FakeBrain(["Добре дошъл."])
    httpd = _serve(monkeypatch, brain)
    raw = b"\xef\xbb\xbf" + json.dumps(CHAT, ensure_ascii=False).encode("utf-8")
    try:
        status, body, _ = _call(httpd, "POST", "/v1/chat/completions", raw=raw)
    finally:
        _stop(httpd)
    assert status == 200, body
    assert brain.calls[0]["messages"] == CHAT["messages"]


# ── a13: preflight без X-Stainless-* заявките на openai JS SDK ──────────────

def test_a13_the_preflight_allows_the_headers_a_local_page_asks_for(monkeypatch) -> None:
    asked = ("authorization,content-type,x-stainless-os,x-stainless-lang,"
             "x-stainless-retry-count,x-stainless-timeout")
    hdrs = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": asked}
    httpd = _serve(monkeypatch, FakeBrain())
    try:
        status, _, headers = _call(httpd, "OPTIONS", "/v1/chat/completions", headers=hdrs)
        bad = _call(httpd, "OPTIONS", "/v1/chat/completions",
                    headers={**hdrs, "Access-Control-Request-Headers": "x-a, (bad)"})
    finally:
        _stop(httpd)
    assert status == 204
    allowed = {h.strip().lower() for h in headers["Access-Control-Allow-Headers"].split(",")}
    assert set(asked.split(",")) <= allowed
    # Нещо, което не е списък от имена на заглавки, не се отразява обратно.
    assert bad[0] == 204 and bad[2]["Access-Control-Allow-Headers"] == "Content-Type, Authorization"


# ── b8: тяло над 64 KB → RST/BrokenPipe вместо 413 ──────────────────────────

@pytest.mark.skipif(sys.platform == "win32",
                    reason="RST може да изпревари отговора (като в test_remote_voice)")
def test_b8_an_oversized_body_gets_the_413_not_a_reset(monkeypatch) -> None:
    """Измерено преди поправката: 4 MB → BrokenPipeError 20 от 20 — сървърът
    затваряше с непрочетено тяло и ядрото пращаше RST вместо 413."""
    httpd = _serve(monkeypatch, FakeBrain())
    port = httpd.server_address[1]
    size = 4 * 1024 * 1024
    chunk = b"A" * 65536
    try:
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Content-Type: application/json\r\nContent-Length: {size}\r\n\r\n"
                          .encode())
                try:
                    for _ in range(size // len(chunk)):
                        s.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError) as e:
                    pytest.fail(f"клиентът не стигна до отговора: {e!r}")
                got = b""
                while True:
                    part = s.recv(65536)
                    if not part:
                        break
                    got += part
            assert b" 413 " in got.split(b"\r\n", 1)[0], got[:200]
            assert "Тялото е над 64 KB." in got.decode("utf-8")
    finally:
        _stop(httpd)
