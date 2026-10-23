"""Одит на шлюза 2026-10-22: `models`/`plugins` на OpenRouter заобикаляха
GATEWAY_MODELS, картинки по адрес минаваха бюджета ~19×, две паралелни заявки
харчеха ~1.9× бюджета, а прекъснат отговор не се таксуваше изобщо.
Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import http.client
import http.server
import json
import threading
from typing import Any

import pytest

from cloud.gateway import gateway as gw

SECRET = "s" * 40


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a: object) -> None:
        pass


def _grant(budget: int, job: str = "job1") -> gw.Grant:
    return gw.Grant(job, 10 ** 12, budget)


# ── 1. Резервните модели на OpenRouter не заобикалят списъка ─────────────────

@pytest.mark.parametrize("extra", [
    {"models": ["meta-llama/llama-3.1-8b-instruct", "openai/o1-pro"]},
    {"route": "fallback", "models": ["openai/o1-pro"]},
    {"plugins": [{"id": "web", "max_results": 10}]},
    {"web_search_options": {"search_context_size": "high"}},
])
def test_openrouter_routing_fields_cannot_reach_a_model_off_the_list(tmp_path, extra) -> None:
    seen: list[dict[str, Any]] = []

    def opener(req: Any, timeout: float = 0) -> _Resp:
        d = json.loads(req.data)
        seen.append(d)
        used = (d.get("models") or [d["model"]])[-1]   # OpenRouter минава към следващия
        return _Resp(200, json.dumps({"model": used, "usage": {
            "prompt_tokens": 100, "completion_tokens": 8000, "total_tokens": 8100}}).encode())

    keys = {"OPENROUTER_API_KEY": "k", "GATEWAY_MODELS": "openrouter/meta-llama/llama-3.1-8b-instruct"}
    g = gw.Gateway(SECRET, keys, gw.Ledger(tmp_path / "u.jsonl"), opener=opener,
                   models=gw.read_models(keys))
    body = {"model": "meta-llama/llama-3.1-8b-instruct", **extra}
    status, _ = g.forward("openrouter", json.dumps(body).encode(), _grant(300_000))
    assert status == 400 and seen == []
    assert g.ledger.reserved == {}
    # Обикновената заявка към разрешения модел още минава.
    plain = {"model": "meta-llama/llama-3.1-8b-instruct"}
    assert g.forward("openrouter", json.dumps(plain).encode(), _grant(300_000))[0] == 200


# ── 2. Картинка по адрес се брои по горна граница, не по байтовете си ────────

def test_image_parts_are_counted_in_the_budget(tmp_path) -> None:
    seen: list[int] = []

    def opener(req: Any, timeout: float = 0) -> _Resp:
        n = len(json.loads(req.data)["messages"][0]["content"])
        seen.append(n)
        # gpt-4o, detail=high, 1024x1024: 765 токена на картинка (тарифата на OpenAI)
        return _Resp(200, json.dumps({"usage": {"prompt_tokens": 765 * n, "completion_tokens": 10,
                                                "total_tokens": 765 * n + 10}}).encode())

    g = gw.Gateway(SECRET, {"OPENAI_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"), opener=opener)
    parts = [{"type": "image_url", "image_url": {"url": f"https://example.com/{i}.png", "detail": "high"}}
             for i in range(500)]
    body = json.dumps({"model": "gpt-4o", "max_tokens": 10,
                       "messages": [{"role": "user", "content": parts}]}).encode()
    assert len(body) // 3 < 20_000           # по байтове заявката „се събира“
    status, _ = g.forward("openai", body, _grant(20_000))
    assert status == 429 and seen == []
    # Една картинка с текст (vision.py) още минава.
    one = json.dumps({"model": "gpt-4o", "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "какво не е наред тук?"}, parts[0]]}]}).encode()
    assert g.forward("openai", one, _grant(20_000))[0] == 200 and seen == [2]


# ── 3. Две паралелни заявки не харчат повече от бюджета ─────────────────────

def test_two_parallel_requests_cannot_overspend(tmp_path, monkeypatch) -> None:
    # Детерминирано: двете нишки се срещат в _cap_completion — преди поправката
    # и двете вече са прочели един и същ остатък (left), след нея — вече са
    # запазили своя дял.
    barrier = threading.Barrier(2)
    orig = gw._cap_completion

    def cap(*a: Any) -> bytes:
        try:
            barrier.wait(5)
        except threading.BrokenBarrierError:
            pass
        return orig(*a)

    monkeypatch.setattr(gw, "_cap_completion", cap)
    gate = threading.Event()

    def opener(req: Any, timeout: float = 0) -> _Resp:
        d = json.loads(req.data)
        gate.wait(5)
        return _Resp(200, json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": d["max_tokens"],
                                                "total_tokens": 10 + d["max_tokens"]}}).encode())

    g = gw.Gateway(SECRET, {"GROQ_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"), opener=opener)
    budget = 20_000
    body = json.dumps({"model": "m", "max_tokens": int(budget * 0.6)}).encode()
    res: list[int] = []
    ts = [threading.Thread(target=lambda: res.append(g.forward("groq", body, _grant(budget))[0]))
          for _ in range(2)]
    for t in ts:
        t.start()
    threading.Timer(0.5, gate.set).start()
    for t in ts:
        t.join(15)
    assert len(res) == 2
    assert g.ledger.used("job1") <= budget
    assert g.ledger.reserved == {}


# ── 4. Отговор, прекъснат след изпращането, се таксува с оценката ────────────

def test_a_timeout_after_the_request_was_sent_is_charged_the_estimate(tmp_path) -> None:
    def opener(req: Any, timeout: float = 0) -> _Resp:
        # Доставчикът е приел заявката (генерира и таксува ключа), но отговорът
        # не стига преди срока на шлюза.
        raise TimeoutError("timed out")

    g = gw.Gateway(SECRET, {"OPENAI_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"), opener=opener)
    body = json.dumps({"model": "o1-pro", "max_completion_tokens": 64_000,
                       "messages": [{"role": "user", "content": "think long"}]}).encode()
    status, data = g.forward("openai", body, _grant(300_000))
    assert status == 502 and "error" in json.loads(data)
    assert g.ledger.used("job1") >= 64_000
    lines = gw.usage_lines(tmp_path / "u.jsonl", "job1")
    assert lines and all(line.get("estimated") for line in lines)
    assert g.ledger.reserved == {}


def test_a_connection_that_never_opened_is_not_charged(tmp_path) -> None:
    import urllib.error

    def opener(req: Any, timeout: float = 0) -> _Resp:
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    g = gw.Gateway(SECRET, {"OPENAI_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"), opener=opener)
    status, _ = g.forward("openai", json.dumps({"model": "gpt-4o"}).encode(), _grant(300_000))
    assert status == 502 and g.ledger.used("job1") == 0 and g.ledger.reserved == {}


def test_a_reply_cut_mid_body_is_a_502_and_is_charged(tmp_path, monkeypatch) -> None:
    """Истинският urlopen срещу локален доставчик, който праща 100 от ~5 000
    байта и затваря: IncompleteRead излизаше от forward() и клиентът получаваше
    RemoteDisconnected вместо 502."""
    class Up(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"choices": [{"message": {"content": "x" * 5000}}],
                               "usage": {"total_tokens": 9000}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:100])
            self.wfile.flush()
            self.close_connection = True

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    monkeypatch.setitem(gw.UPSTREAMS, "groq", (f"http://127.0.0.1:{up.server_address[1]}", "GROQ_API_KEY"))
    g = gw.Gateway(SECRET, {"GROQ_API_KEY": "gsk_real"}, gw.Ledger(tmp_path / "u.jsonl"))
    srv = gw.make_server(g, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tok = gw.make_token(SECRET, "job1", budget=100_000, ttl=60)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", "/groq/chat/completions", body=b'{"model":"m","max_tokens":9000}',
                  headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        r = c.getresponse()
        status, data = r.status, r.read()
        c.close()
        assert status == 502 and "error" in json.loads(data)
        assert g.ledger.used("job1") >= 9000 and g.ledger.reserved == {}
    finally:
        srv.shutdown()
        srv.server_close()
        up.shutdown()
        up.server_close()
