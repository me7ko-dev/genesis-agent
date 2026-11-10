"""Одит, седми кръг (2026-11-10) — веригата на моделите, цените, budget, genesis serve.
Всеки тест пада без поправката си."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from genesis_agent import anthropic_spend, budget
from genesis_agent import remote_server as rs
from tests.test_brain_audit_2026_10_27 import (  # noqa: F401  (_clean е autouse там)
    _clean,
    _fake_anthropic,
    _pinned_anthropic,
)

# ── скъсан поток: сурово изключение на транспорта, не APIConnectionError ───────

class RemoteProtocolError(Exception):
    """Като httpx.RemoteProtocolError — НЕ е подклас на anthropic.APIConnectionError."""


@pytest.mark.parametrize("error", [
    RemoteProtocolError("peer closed connection without sending complete message body"),
    TimeoutError("timed out"),
    AssertionError(),            # 200 text/html (портал на хотелски Wi-Fi)
])
def test_a_raw_transport_error_mid_stream_moves_to_the_next_model(monkeypatch, error) -> None:
    """Истинският SDK срещу фалшив сървър: изключението излизаше от complete()
    и ходът падаше, без да се пита следващият модел."""
    behaviour: dict = {"raise": error}
    _fake_anthropic(monkeypatch, behaviour)
    b, seen = _pinned_anthropic(monkeypatch)
    r = b.complete([{"role": "user", "content": "hi"}])
    assert r.raw_text == "groq B" and seen == ["B"]
    assert anthropic_spend.total_usd() > 0          # входът на скъсания поток е платен


def test_ctrl_c_mid_stream_is_not_swallowed(monkeypatch) -> None:
    _fake_anthropic(monkeypatch, {"raise": KeyboardInterrupt()})
    b, seen = _pinned_anthropic(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        b.complete([{"role": "user", "content": "hi"}])
    assert seen == []


def test_the_sdk_does_not_retry_behind_the_chains_back(monkeypatch) -> None:
    """SDK-то по подразбиране повтаря 2 пъти и спи retry-after: 429 с 10 s държеше
    хода 20 s, преди веригата да види каквото и да е."""
    fake, _calls = _fake_anthropic(monkeypatch, {"raise": TimeoutError("x")})
    made: list[dict] = []
    real = fake.Anthropic

    def client(**kw):
        made.append(kw)
        return real(**kw)

    fake.Anthropic = client
    b, _seen = _pinned_anthropic(monkeypatch)
    b.complete([{"role": "user", "content": "hi"}])
    assert made and all(kw.get("max_retries") == 0 for kw in made)


# ── цените ─────────────────────────────────────────────────────────────────────

def test_sonnet_5_5_cache_reads_and_the_5_2_models_are_priced() -> None:
    """Sonnet 5.5 чете от кеша за $0.10/MTok (не $0.20 — оттам +5.6% спрямо
    конзолата на 10.11); Fable/Mythos 5.2 нямаха ред и с таван се отказваха."""
    assert anthropic_spend.PRICES["claude-sonnet-5-5"] == (2.00, 10.00, 0.10)
    assert anthropic_spend.PRICES["claude-sonnet-5"] == (2.00, 10.00, 0.20)
    for model in ("claude-fable-5-2", "claude-mythos-5-2"):
        assert anthropic_spend.PRICES[model] == (8.00, 40.00, 0.20)


# ── genesis budget: ред с валиден JSON и поле от грешен тип ────────────────────

@pytest.mark.parametrize("row", [
    {"provider": "x", "prompt_tokens": None, "completion_tokens": 1, "total_tokens": 1},
    {"ts": 1731200000, "provider": "x"},
    {"provider": ["a"], "total_tokens": 1},
    {"provider": "x", "total_tokens": "много"},
])
def test_a_wrongly_typed_row_does_not_crash_the_report(row) -> None:
    budget.record_usage(provider="groq", model="m", prompt_tokens=10, completion_tokens=2)
    row = {"ts": datetime.now(timezone.utc).isoformat(), **row}
    with Path(budget.LOG_PATH).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    for totals in (budget.today_totals(), budget.range_totals(7)):
        assert totals["by_provider"]["groq"]["total_tokens"] == 12
        assert isinstance(totals["total_tokens"], int)


# ── два genesis serve с един файл за видяното ──────────────────────────────────

def _payload(rid: str) -> dict:
    return {"ts": int(time.time() * 1000), "rid": rid, "op": "send", "text": "deploy"}


def test_a_replay_is_refused_by_the_other_running_server(tmp_path) -> None:
    """Desktop пуска свой `genesis serve` до този за телефона — същият ключ и
    същият remote_seen.json. Хванат плик минаваше през втория."""
    path = tmp_path / "remote_seen.json"
    lan, desktop = rs.ReplayGuard(path=path), rs.ReplayGuard(path=path)
    captured = _payload("captured-rid-0001")
    lan.check(captured)
    with pytest.raises(rs.ProtocolError, match="replayed"):
        desktop.check(captured)


def test_one_server_does_not_erase_what_the_other_has_seen(tmp_path) -> None:
    path = tmp_path / "remote_seen.json"
    lan, desktop = rs.ReplayGuard(path=path), rs.ReplayGuard(path=path)
    captured = _payload("captured-rid-0002")
    lan.check(captured)
    desktop.check(_payload("desktop-poll-0001"))       # презаписваше файла само със своето
    assert "captured-rid-0002" in json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(rs.ProtocolError, match="replayed"):
        rs.ReplayGuard(path=path).check(captured)       # първият е рестартиран
    assert not list(tmp_path.glob("*.tmp"))
