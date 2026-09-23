"""genesis_agent.model_check — `genesis models --check` и прескачането на мъртви
модели. Поводът (2026-09-23): 4 от ~17 модела в config.yaml бяха умрели
(410/404) и седяха във веригата, един от тях №2."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from genesis_agent import brain as brain_mod
from genesis_agent import model_check as mc


class _Resp:
    def __init__(self, code: int, text: str = "") -> None:
        self.status_code, self.text = code, text


def _write(results, days_ago: float = 0) -> None:
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    mc.CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    mc.CHECK_PATH.write_text(json.dumps({"checked_at": at.isoformat(), "results": results}),
                             encoding="utf-8")


@pytest.mark.parametrize("code,status", [
    (200, "ok"), (404, "dead"), (410, "dead"), (429, "busy"), (503, "busy"),
    (401, "key"), (402, "key"), (403, "key"), (500, "error"),
])
def test_status_codes_are_classified(code, status) -> None:
    assert mc._classify(code) == status


def test_configured_models_cover_start_chain_coding_and_light() -> None:
    models = mc.configured_models()
    assert models[0] == ("groq", "openai/gpt-oss-120b")  # стартовият е пръв
    assert ("groq", "openai/gpt-oss-20b") in models       # light_models
    assert len(models) == len(set(models))


def test_a_run_writes_results_and_needs_no_new_check(monkeypatch) -> None:
    monkeypatch.setattr(brain_mod.Brain, "_provider_key", lambda self, env: "k")
    codes = {"alive": 200, "gone": 410, "busy": 429}
    monkeypatch.setattr(mc.requests, "post",
                        lambda url, json=None, **kw: _Resp(codes[json["model"]], "x"))
    assert mc.needs_check()
    res = mc.run_check([("groq", "alive"), ("groq", "gone"), ("groq", "busy")])
    assert [r["status"] for r in res] == ["ok", "dead", "busy"]
    assert not mc.needs_check()
    assert mc.dead_models() == {("groq", "gone")}


def test_a_missing_key_is_not_a_dead_model(monkeypatch) -> None:
    monkeypatch.setattr(brain_mod.Brain, "_provider_key", lambda self, env: None)
    res = mc.run_check([("huggingface", "some/model")])
    assert res[0]["status"] == "nokey"
    assert mc.dead_models() == set()


def test_busy_is_not_dead() -> None:
    _write([{"provider": "nvidia", "model": "m", "status": "busy"}])
    assert mc.dead_models() == set()


def test_an_old_verdict_is_not_trusted() -> None:
    """Моделът може да е върнат — стара присъда не бива да го държи вън."""
    _write([{"provider": "nvidia", "model": "m", "status": "dead"}], days_ago=mc.DEAD_TRUST_DAYS + 1)
    assert mc.dead_models() == set()
    assert mc.needs_check()


def test_the_chain_skips_models_the_last_check_saw_dead() -> None:
    first = brain_mod._load_chain()[1]
    _write([{"provider": first["provider"], "model": first["model"], "status": "dead"}])
    chain = [(c["provider"], c["model"]) for c in brain_mod._load_chain()]
    assert (first["provider"], first["model"]) not in chain
    assert chain, "веригата не бива да остава празна"


def test_report_names_the_dead_ones() -> None:
    out = mc.format_report([
        {"provider": "groq", "model": "a", "status": "ok", "latency": 0.4},
        {"provider": "nvidia", "model": "b", "status": "dead", "latency": 0.2, "detail": "Gone"},
    ])
    assert "1/2 отговарят" in out
    assert "nvidia/b" in out
