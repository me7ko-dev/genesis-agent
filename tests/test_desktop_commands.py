"""`/` командите за Genesis Desktop: числата в /usage и границите на /history."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from genesis_agent import desktop_commands as dc

TODAY = date(2026, 9, 25)


def _entry(day: date, provider: str, model: str, prompt: int, completion: int, **extra) -> dict:
    # Обед по местно време: денят не зависи от часовата зона на машината с тестовете.
    ts = datetime(day.year, day.month, day.day, 12).astimezone().isoformat()
    return {"ts": ts, "provider": provider, "model": model, "prompt_tokens": prompt,
            "completion_tokens": completion, "total_tokens": prompt + completion, **extra}


def test_usage_report_periods_daily_and_paid() -> None:
    entries = [
        _entry(TODAY, "groq", "a", 100, 50),
        _entry(TODAY - timedelta(days=3), "openai", "gpt", 1000, 0, cached_read_tokens=400),
        _entry(TODAY - timedelta(days=20), "groq", "a", 10, 10),
        _entry(TODAY - timedelta(days=200), "groq", "a", 5, 5),
        {"ts": "not a date", "provider": "groq", "model": "a", "total_tokens": 999},
    ]
    r = dc.usage_report(entries, days=30, today=TODAY, is_free=lambda p, _m: p != "openai")
    p = r["periods"]
    assert (p["today"]["tokens"], p["week"]["tokens"], p["month"]["tokens"], p["all"]["tokens"]) == \
        (150, 1150, 1170, 1180)
    assert p["week"]["cached"] == 400 and p["all"]["calls"] == 4
    assert len(r["daily"]) == 30 and r["daily"][-1] == {"day": TODAY.isoformat(), "calls": 1, "tokens": 150}
    assert r["paid_tokens"] == 1000
    assert [x["provider"] for x in r["providers"]] == ["openai", "groq"]
    assert r["providers"][0]["free"] is False and r["providers"][1]["free"] is True
    assert r["since"] == (TODAY - timedelta(days=200)).isoformat()


def test_usage_report_quota_counts_only_its_window() -> None:
    entries = [_entry(TODAY, "ollama_cloud", "m", 1_000_000, 0),
               _entry(TODAY - timedelta(days=6), "ollama_cloud", "m", 500_000, 0),
               _entry(TODAY - timedelta(days=7), "ollama_cloud", "m", 9_000_000, 0)]
    q = dc.usage_report(entries, today=TODAY)["quotas"]
    assert q == [{"provider": "ollama_cloud", "limit": 5_000_000, "spent": 1_500_000,
                  "left": 3_500_000, "label": "~5M токена седмично", "period": "седмица"}]


def test_usage_report_empty() -> None:
    r = dc.usage_report([], today=TODAY)
    assert r["periods"]["all"]["calls"] == 0 and r["quotas"] == [] and r["since"] is None


def _ctx(messages=None, busy=False):
    state = {"m": messages or [{"role": "system", "content": "S"}]}
    events: list = []
    return dc.CommandContext(get_messages=lambda: state["m"],
                             set_messages=lambda m: state.update(m=m),
                             busy=lambda: busy, emit=lambda k, **d: events.append((k, d))), state, events


def test_run_never_raises() -> None:
    ctx, _, _ = _ctx()
    assert dc.run("nope", {}, ctx) == {"ok": False, "error": "непозната команда: nope"}
    assert dc.run("done", {}, ctx)["ok"] is False  # без номер


gta = pytest.importorskip("genesis_terminal_agent")


def test_load_history_restores_and_replays(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    saved = [{"role": "system", "content": "old"}, {"role": "user", "content": "здравей"},
             {"role": "assistant", "content": "здрасти"}]
    (tmp_path / "session_20260925_010203.json").write_text(json.dumps(saved), encoding="utf-8")
    ctx, state, events = _ctx()
    listed = dc.run("history", {}, ctx)
    assert listed["sessions"][0]["preview"] == "здравей" and listed["sessions"][0]["messages"] == 2
    assert dc.run("load_history", {"file": "session_20260925_010203.json"}, ctx)["ok"]
    assert state["m"][0] == {"role": "system", "content": "S"}  # свежият промпт, не старият
    assert [k for k, _ in events] == ["cleared", "user", "assistant"]


def test_load_history_refuses_paths_and_busy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path / "h")
    (tmp_path / "h").mkdir()
    (tmp_path / "session_x.json").write_text("[]", encoding="utf-8")
    ctx, _, _ = _ctx()
    assert dc.run("load_history", {"file": "../session_x.json"}, ctx)["ok"] is False
    busy, _, _ = _ctx(busy=True)
    assert dc.run("load_history", {"file": "session_x.json"}, busy) == {"ok": False, "error": "busy"}


def test_set_model_and_toggles(monkeypatch) -> None:
    monkeypatch.setattr(gta, "current_provider", gta.current_provider)
    monkeypatch.setattr(gta, "current_model_id", gta.current_model_id)
    monkeypatch.setattr(gta, "_CODING_MODE", False)
    monkeypatch.setattr(gta, "provider_ready", lambda k: (k == "ollama", "липсва ключ"))
    ctx, _, _ = _ctx()
    assert dc.run("set_model", {"provider": "groq", "model": "x"}, ctx) == {"ok": False, "error": "липсва ключ"}
    assert dc.run("set_model", {"provider": "ollama", "model": "qwen"}, ctx)["ok"]
    assert (gta.current_provider, gta.current_model_id) == ("ollama", "qwen")
    status = dc.run("status", {}, ctx)
    assert status["model"] == "qwen" and status["context_window"] > 0
