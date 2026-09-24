"""cloud/gateway — ключовете не влизат в контейнера, сметката е от шлюза.
Без мрежа: истинският HTTP сървър на шлюза на случаен порт, а доставчиците
са фалшив `opener`."""
from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from cloud.gateway import gateway as gw
from cloud.runner import launch
from genesis_agent import brain

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


class FakeUpstream:
    """Отговаря с `script` по ред: (статус, usage). Пази каквото е получило."""

    def __init__(self, script: list[tuple[int, dict[str, int] | None]]) -> None:
        self.script = list(script)
        self.seen: list[urllib.request.Request] = []

    def __call__(self, req: Any, timeout: float = 0) -> _Resp:
        self.seen.append(req)
        status, usage = self.script.pop(0) if self.script else (200, {"total_tokens": 1})
        body = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": usage}).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b'{"error":1}'))  # type: ignore[arg-type]
        return _Resp(200, body)


# ── таблицата е същата като в brain.py ────────────────────────────────────────

def test_upstreams_match_the_agents_providers() -> None:
    expected = {p: v for p, v in brain._PROVIDERS.items()
                if v[0].startswith("https://") and p not in brain._NATIVE_PROVIDERS}
    assert gw.UPSTREAMS == expected


# ── жетони ────────────────────────────────────────────────────────────────────

def test_token_round_trip() -> None:
    t = gw.make_token(SECRET, "genesis-task-abc", budget=5000, ttl=60, now=1000)
    assert gw.check_token(SECRET, t, now=1030) == gw.Grant("genesis-task-abc", 1060, 5000)


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace(".5000.", ".9999999."),          # по-голям бюджет
    lambda t: t.replace("job1", "job2"),                   # чужда задача
    lambda t: t[:-2] + "xx",                               # подпис
    lambda t: "garbage",
    lambda t: "a.b.c.d",
    lambda t: "",
])
def test_a_forged_token_is_refused(mutate) -> None:
    t = gw.make_token(SECRET, "job1", budget=5000, ttl=60, now=1000)
    assert gw.check_token(SECRET, mutate(t), now=1001) is None


def test_expired_or_foreign_secret_is_refused() -> None:
    t = gw.make_token(SECRET, "job1", budget=10, ttl=60, now=1000)
    assert gw.check_token(SECRET, t, now=1061) is None
    assert gw.check_token("x" * 40, t, now=1001) is None


def test_job_names_cannot_smuggle_dots() -> None:
    with pytest.raises(ValueError):
        gw.make_token(SECRET, "a.9999.1", budget=1, ttl=1)


# ── пренасочване и сметка ─────────────────────────────────────────────────────

def _gateway(tmp_path: Path, keys: dict[str, str], upstream: FakeUpstream) -> gw.Gateway:
    return gw.Gateway(SECRET, keys, gw.Ledger(tmp_path / "usage.jsonl"), opener=upstream)


def test_the_real_key_is_added_and_usage_is_billed(tmp_path) -> None:
    up = FakeUpstream([(200, {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})])
    g = _gateway(tmp_path, {"GROQ_API_KEY": "gsk_real"}, up)
    grant = gw.Grant("job1", 10 ** 12, 1000)
    status, _ = g.forward("groq", b'{"model": "openai/gpt-oss-120b"}', grant)
    assert status == 200
    assert up.seen[0].full_url == "https://api.groq.com/openai/v1/chat/completions"
    assert up.seen[0].get_header("Authorization") == "Bearer gsk_real"
    assert gw.usage_for(tmp_path / "usage.jsonl", "job1") == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "calls": 1}
    assert gw.usage_for(tmp_path / "usage.jsonl", "other")["calls"] == 0


def test_the_next_key_is_tried_on_429(tmp_path) -> None:
    up = FakeUpstream([(429, None), (200, {"total_tokens": 5})])
    g = _gateway(tmp_path, {"GROQ_API_KEY": "k1", "GROQ_API_KEY_2": "k2"}, up)
    status, _ = g.forward("groq", b'{"model": "m"}', gw.Grant("j", 10 ** 12, 100))
    assert status == 200
    assert [r.get_header("Authorization") for r in up.seen] == ["Bearer k1", "Bearer k2"]


def test_a_failed_call_is_not_billed(tmp_path) -> None:
    up = FakeUpstream([(500, None)])
    g = _gateway(tmp_path, {"GROQ_API_KEY": "k"}, up)
    assert g.forward("groq", b'{"model": "m"}', gw.Grant("j", 10 ** 12, 100))[0] == 500
    assert gw.usage_for(tmp_path / "usage.jsonl", "j")["calls"] == 0


def test_usage_survives_a_restart(tmp_path) -> None:
    g = _gateway(tmp_path, {"GROQ_API_KEY": "k"}, FakeUpstream([(200, {"total_tokens": 70})]))
    g.forward("groq", b'{"model": "m"}', gw.Grant("j", 10 ** 12, 100))
    fresh = gw.Ledger(tmp_path / "usage.jsonl")
    assert fresh.load() == 1 and fresh.used("j") == 70


def test_a_weak_secret_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        gw.Gateway("short", {}, gw.Ledger(None))


# ── HTTP ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def served(tmp_path):
    up = FakeUpstream([])
    g = _gateway(tmp_path, {"GROQ_API_KEY": "gsk_real"}, up)
    srv = gw.make_server(g, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(path: str, token: str, body: bytes = b'{"model": "m"}') -> tuple[int, dict[str, Any]]:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", path, body=body, headers={"Authorization": f"Bearer {token}",
                                                    "Content-Type": "application/json"})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    yield g, up, post
    srv.shutdown()
    srv.server_close()


def test_http_forwards_with_a_valid_token(served) -> None:
    _, up, post = served
    tok = gw.make_token(SECRET, "job1", budget=1000, ttl=60)
    code, data = post("/groq/chat/completions", tok)
    assert code == 200 and data["choices"][0]["message"]["content"] == "ok"
    # жетонът не стига до доставчика
    assert tok not in json.dumps(dict(up.seen[0].header_items()))


def test_http_refuses_what_it_should(served) -> None:
    _, up, post = served
    tok = gw.make_token(SECRET, "job1", budget=1000, ttl=60)
    assert post("/groq/chat/completions", "gsk_stolen_real_key")[0] == 401
    assert post("/groq/embeddings", tok)[0] == 404
    assert post("/evil/chat/completions", tok)[0] == 404
    assert post("/nvidia/chat/completions", tok)[0] == 404      # няма ключ за него
    assert post("/groq/chat/completions", tok, body=b"")[0] == 413
    assert up.seen == []


def test_http_stops_at_the_budget(served) -> None:
    g, _, post = served
    tok = gw.make_token(SECRET, "job1", budget=100, ttl=60)
    g.ledger.add("job1", "groq", "m", {"total_tokens": 100})
    code, data = post("/groq/chat/completions", tok)
    assert code == 429 and "бюджетът" in data["error"]["message"]


# ── агентът вика шлюза ────────────────────────────────────────────────────────

def test_the_agent_goes_through_the_gateway_when_asked(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_MODEL_GATEWAY", raising=False)
    assert brain.provider_base_url("groq") == "https://api.groq.com/openai/v1"
    monkeypatch.setenv("GENESIS_MODEL_GATEWAY", "http://genesis-gateway:8090/")
    assert brain.provider_base_url("groq") == "http://genesis-gateway:8090/groq"
    assert brain.provider_base_url("ollama_local") == "http://localhost:11434/v1"
    assert brain.provider_base_url("anthropic") == "native://anthropic"
    assert brain.provider_base_url("vertex") == "dynamic://vertex"


def test_a_real_call_hits_the_gateway_url(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_MODEL_GATEWAY", "http://gw:8090")
    seen: list[str] = []

    class R:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(brain.requests, "post", lambda url, **kw: seen.append(url) or R())
    b = brain.Brain.__new__(brain.Brain)
    b.keys = {"GROQ_API_KEY": "job-token"}
    b.timeout = 5
    b._premium_meta = {}
    b._call("groq", "openai/gpt-oss-120b", [{"role": "user", "content": "x"}])
    assert seen == ["http://gw:8090/groq/chat/completions"]


# ── контейнерът: без ключове, без прокси, сметката от шлюза ───────────────────

def _gw_config(tmp_path: Path) -> launch.Gateway:
    keys = tmp_path / "keys.env"
    keys.write_text(f"GATEWAY_SECRET={SECRET}\nGROQ_API_KEY=gsk_real\nGROQ_API_KEY_2=gsk_2\n"
                    "NVIDIA_API_KEY=nvapi_real\nHF_TOKEN=\n", encoding="utf-8")
    return launch.Gateway.from_keys_file(keys, tmp_path / "usage.jsonl")


def test_keys_file_needs_a_secret(tmp_path) -> None:
    keys = tmp_path / "keys.env"
    keys.write_text("GROQ_API_KEY=x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        launch.Gateway.from_keys_file(keys, tmp_path / "u.jsonl")
    assert _gw_config(tmp_path).key_envs == ("GROQ_API_KEY", "NVIDIA_API_KEY")


def test_the_container_gets_a_token_not_the_keys(tmp_path) -> None:
    g = _gw_config(tmp_path)
    args = launch.docker_args("genesis-task-x", tmp_path, "задача", gateway=g, token="TOK",
                              env_file=str(tmp_path / "keys.env"))
    joined = " ".join(args)
    assert "gsk_real" not in joined and "nvapi_real" not in joined and SECRET not in joined
    assert "--env-file" not in args
    assert "HTTPS_PROXY" not in joined and "https_proxy" not in joined
    assert "GENESIS_MODEL_GATEWAY=http://genesis-gateway:8090" in args
    assert "GROQ_API_KEY=TOK" in args and "NVIDIA_API_KEY=TOK" in args


class _Proc:
    def __init__(self, argv: list[str], usage: Path) -> None:
        name = argv[argv.index("--name") + 1]
        # докато „върви" контейнерът, шлюзът записва истинския разход
        gw.Ledger(usage).add(name, "groq", "m", {"prompt_tokens": 90, "completion_tokens": 10})
        claim = {"kind": "done", "ok": True, "error": "", "seconds": 1.0,
                 "tokens": {"total_tokens": 1}}   # контейнерът лъже, че е 1 токен
        self.stdout = io.StringIO(json.dumps(claim) + "\n")
        self.stderr = io.StringIO("")

    def wait(self) -> int:
        return 0


def test_the_bill_comes_from_the_gateway_not_the_container(monkeypatch, tmp_path) -> None:
    g = _gw_config(tmp_path)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: _Proc(argv, g.usage))
    res = launch.run_task("задача", tmp_path / "ws", gateway=g)
    assert res.ok
    assert res.tokens == {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100, "calls": 1}


def test_the_token_lives_as_long_as_the_task(monkeypatch, tmp_path) -> None:
    g = _gw_config(tmp_path)
    seen: dict[str, str] = {}

    def fake(argv: list[str], **kw: Any) -> _Proc:
        seen["token"] = next(a.split("=", 1)[1] for a in argv if a.startswith("GROQ_API_KEY="))
        seen["name"] = argv[argv.index("--name") + 1]
        return _Proc(argv, g.usage)

    monkeypatch.setattr(launch.subprocess, "Popen", fake)
    launch.run_task("x", tmp_path / "ws", gateway=g, limits=launch.Limits(seconds=30))
    grant = gw.check_token(SECRET, seen["token"])
    assert grant is not None and grant.job == seen["name"] and grant.budget == g.budget
    assert gw.check_token(SECRET, seen["token"], now=grant.expires + 1) is None
