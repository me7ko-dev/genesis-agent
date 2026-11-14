"""Одит 2026-11-15: разлика между разбора на шлюза и на доставчика.

Шлюзът проверява модел/ограничения върху разобрания JSON, а препраща
оригиналните байтове. Повтарящ се ключ, NaN/Infinity, BOM и UTF-16 са
случаи, в които двата разбора могат да се разминат — всички са 400 преди
такса и препращане.
"""
from __future__ import annotations

import http.client
import json
import threading
from typing import Any

import pytest

from cloud.gateway import gateway as gw

SECRET = "s" * 40


class _Resp:
    status = 200

    def read(self) -> bytes:
        return json.dumps({"usage": {"total_tokens": 10}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a: object) -> None:
        pass


def _gateway(tmp_path, seen: list[bytes]) -> gw.Gateway:
    def opener(req: Any, timeout: float = 0) -> _Resp:
        seen.append(req.data)
        return _Resp()

    keys = {"GROQ_API_KEY": "k", "GATEWAY_MODELS": "groq/cheap"}
    return gw.Gateway(SECRET, keys, gw.Ledger(tmp_path / "u.jsonl"), opener=opener,
                      models=gw.read_models(keys))


_GRANT = gw.Grant("job1", 10 ** 12, 300_000)

BAD = [
    b'{"model": "expensive", "messages": [], "model": "cheap"}',
    b'{"model": "cheap", "model": "cheap"}',
    b'{"model": "cheap", "messages": [{"role": "user", "content": "a", "content": "b"}]}',
    b'{"model": "cheap", "messages": [{"x": {"y": 1, "y": 2}}]}',
    b'{"model": "cheap", "max_tokens": NaN}',
    b'{"model": "cheap", "temperature": Infinity}',
    b'{"model": "cheap", "temperature": -Infinity}',
    b'\xef\xbb\xbf{"model": "cheap"}',
    '{"model": "cheap"}'.encode("utf-16"),
    '{"model": "cheap"}'.encode("utf-32"),
    b'{"model": "cheap", "messages": "\xff"}',
    b'{"model": "cheap", "a": ' + b'[' * 100_000 + b']' * 100_000 + b'}',
]


@pytest.mark.parametrize("body", BAD, ids=[f"bad{i}" for i in range(len(BAD))])
def test_ambiguous_body_is_a_400_before_anything_is_charged_or_forwarded(tmp_path, body) -> None:
    seen: list[bytes] = []
    g = _gateway(tmp_path, seen)
    status, data = g.forward("groq", body, _GRANT)
    assert status == 400, data
    assert seen == []
    assert g.ledger.reserved == {}
    assert g.ledger.used("job1") == 0
    assert "JSON" in json.loads(data)["error"]["message"]


def test_a_clean_body_still_passes(tmp_path) -> None:
    seen: list[bytes] = []
    g = _gateway(tmp_path, seen)
    body = json.dumps({"model": "cheap", "messages": [{"role": "user", "content": "здрасти"}]},
                      ensure_ascii=False).encode()
    assert g.forward("groq", body, _GRANT)[0] == 200
    assert len(seen) == 1


def test_deep_but_parseable_nesting_does_not_kill_the_handler(tmp_path) -> None:
    # 400 нива са под лимита на разбора — не трябва да събарят следващите
    # стъпки (_media_tokens и т.н.): или 200, или чист 400, но не изключение.
    seen: list[bytes] = []
    g = _gateway(tmp_path, seen)
    deep = b'{"model":"cheap","a":' + b'[' * 400 + b']' * 400 + b'}'
    status, _ = g.forward("groq", deep, _GRANT)
    assert status in (200, 400)
    assert g.ledger.reserved == {}


def test_over_http_a_duplicate_key_is_a_400_and_the_server_stays_alive(tmp_path) -> None:
    seen: list[bytes] = []
    g = _gateway(tmp_path, seen)
    server = gw.make_server(g, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = gw.make_token(SECRET, "job1", budget=300_000, ttl=600)
        port = server.server_address[1]
        good = b'{"model": "cheap"}'
        for body in (BAD[0], BAD[-1], good):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/groq/chat/completions", body=body,
                      headers={"Authorization": f"Bearer {token}"})
            r = c.getresponse()
            r.read()
            assert r.status == (200 if body == good else 400)
            c.close()
        assert len(seen) == 1
    finally:
        server.shutdown()
        server.server_close()
