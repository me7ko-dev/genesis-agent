"""`/` командите за Genesis Desktop: числата в /usage и границите на /history."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

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


def test_export_zips_the_project_from_the_window(tmp_path, monkeypatch) -> None:
    """Проба 2026-10-12: /export (zip + отчет за предаване) беше само в терминала."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setattr(gta, "WORKSPACE", proj)
    ctx, _, _ = _ctx()
    r = dc.run("export", {"tests": False}, ctx)
    assert r["ok"] and Path(r["zip"]).is_file() and Path(r["zip"]).parent == tmp_path
    assert r["text"].startswith("📦") and r["tests_passed"] is None
    busy, _, _ = _ctx(busy=True)
    assert dc.run("export", {}, busy) == {"ok": False, "error": "busy"}
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(gta, "WORKSPACE", tmp_path / "empty")
    r = dc.run("export", {"tests": False}, ctx)
    assert r["ok"] is False and "няма файлове" in r["error"]


def test_done_shows_no_tag_meant_for_the_model() -> None:
    """Проба 2026-10-12: прозорецът показваше „[TASK_UPDATE] Няма нишка #999.“"""
    ctx, _, _ = _ctx()
    text = dc.run("done", {"ids": "999"}, ctx)["text"]
    assert text and not text.startswith("[") and "999" in text


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


def test_usage_subscription_tokens_are_not_paid(monkeypatch) -> None:
    """2026-10-12: Claude през абонамента (claude_cli) не се плаща на токен."""
    from genesis_agent import budget
    entries = [_entry(TODAY, "claude_cli", "opus", 1000, 100), _entry(TODAY, "openai", "gpt", 10, 1)]
    monkeypatch.setattr(budget, "_read_entries", lambda: entries)
    monkeypatch.setattr(dc, "_gta", lambda: _FakeGta())
    r = dc.cmd_usage({"days": 7}, None)  # type: ignore[arg-type]
    assert r["ok"] and r["paid_tokens"] == 11
    rows = {x["provider"]: x for x in r["providers"]}
    assert rows["claude_cli"]["free"] is True and rows["claude_cli"]["subscription"] is True
    assert rows["openai"]["free"] is False and rows["openai"]["subscription"] is False


class _FakeGta:
    PROVIDERS: ClassVar[dict] = {"claude_cli": {"name": "🟠 Claude (абонамент, claude CLI)"},
                                 "openai": {"name": "OpenAI"}}

    @staticmethod
    def is_free_model(_p: str, _m: str) -> bool:
        return False
