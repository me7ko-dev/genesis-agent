"""genesis_agent.budget — records/timestamps are UTC (2026-07-25 fix: comparing
a local date against a UTC timestamp gave a wrong "0 today" right around
midnight). LOG_PATH is monkeypatched to a tmp file so tests never touch the
real budget_log.jsonl."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from genesis_agent import budget


def _write_entry(path, *, ts: str, provider: str, prompt: int, completion: int) -> None:
    entry = {
        "ts": ts, "provider": provider, "model": "m",
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion, "context": "",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_record_usage_appends_jsonl(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    budget.record_usage(provider="huggingface", model="m", prompt_tokens=10, completion_tokens=20)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["total_tokens"] == 30
    assert entry["provider"] == "huggingface"


def test_record_usage_never_raises_on_bad_path(tmp_path, monkeypatch) -> None:
    class _UnwritablePath:
        def open(self, *a, **kw):
            raise OSError("no permission")

    monkeypatch.setattr(budget, "LOG_PATH", _UnwritablePath())
    budget.record_usage(provider="p", model="m", prompt_tokens=1, completion_tokens=1)  # must not raise


def test_daily_totals_uses_utc_date_not_local(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    today_utc = datetime.now(timezone.utc).date()
    _write_entry(log, ts=f"{today_utc.isoformat()}T12:00:00+00:00",
                 provider="p1", prompt=100, completion=50)
    totals = budget.today_totals()
    assert totals["calls"] == 1
    assert totals["total_tokens"] == 150
    assert totals["by_provider"]["p1"]["calls"] == 1


def test_daily_totals_excludes_entries_from_other_days(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    today_utc = datetime.now(timezone.utc).date()
    yesterday = today_utc - timedelta(days=1)
    _write_entry(log, ts=f"{yesterday.isoformat()}T23:59:59+00:00",
                 provider="p1", prompt=100, completion=50)
    totals = budget.today_totals()
    assert totals["calls"] == 0
    assert totals["total_tokens"] == 0


def test_daily_totals_specific_day_argument(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    target = date(2026, 1, 15)
    _write_entry(log, ts="2026-01-15T08:00:00+00:00", provider="p1", prompt=5, completion=5)
    _write_entry(log, ts="2026-01-16T08:00:00+00:00", provider="p1", prompt=5, completion=5)
    totals = budget.daily_totals(target)
    assert totals["calls"] == 1


def test_range_totals_respects_cutoff(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    today_utc = datetime.now(timezone.utc).date()
    in_range = today_utc - timedelta(days=6)
    out_of_range = today_utc - timedelta(days=8)
    _write_entry(log, ts=f"{in_range.isoformat()}T00:00:00+00:00", provider="p1", prompt=1, completion=1)
    _write_entry(log, ts=f"{out_of_range.isoformat()}T00:00:00+00:00", provider="p1", prompt=1, completion=1)
    totals = budget.range_totals(days=7)
    assert totals["calls"] == 1


def test_daily_totals_on_missing_log_is_all_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(budget, "LOG_PATH", tmp_path / "does_not_exist.jsonl")
    totals = budget.today_totals()
    assert totals["calls"] == 0
    assert totals["by_provider"] == {}


def test_daily_totals_skips_corrupt_lines(tmp_path, monkeypatch) -> None:
    log = tmp_path / "budget_log.jsonl"
    monkeypatch.setattr(budget, "LOG_PATH", log)
    today_utc = datetime.now(timezone.utc).date()
    with log.open("w", encoding="utf-8") as f:
        f.write("not valid json\n")
    _write_entry(log, ts=f"{today_utc.isoformat()}T00:00:00+00:00", provider="p1", prompt=1, completion=1)
    totals = budget.today_totals()
    assert totals["calls"] == 1
