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


class TestClipForContext:
    """clip_for_context() is what keeps one noisy tool result from being
    re-sent to the model on every subsequent round. Two properties matter:
    it must not touch anything that already fits, and when it does cut, the
    END must survive — that is where the traceback and the verdict line live,
    and a plain text[:limit] would throw exactly that away.
    """

    def test_short_results_pass_through_byte_identical(self) -> None:
        text = "line\n" * 50
        assert budget.clip_for_context(text) == text

    def test_a_result_exactly_at_the_limit_is_untouched(self) -> None:
        text = "x" * 100
        assert budget.clip_for_context(text, limit=100) == text

    def test_both_ends_survive_the_cut(self) -> None:
        body = "".join(f"line {i}\n" for i in range(4000))
        out = budget.clip_for_context(body, limit=2000)
        assert len(out) < len(body)
        assert out.startswith("line 0\n")
        # The tail is the half a naive truncation drops, and the half that
        # usually carries the error the model has to react to.
        assert out.rstrip().endswith("line 3999")

    def test_the_cut_is_announced_so_the_model_knows_it_is_partial(self) -> None:
        out = budget.clip_for_context("y" * 9000, limit=1000)
        assert "отрязани" in out

    def test_zero_disables_the_cap_entirely(self) -> None:
        body = "z" * 50_000
        assert budget.clip_for_context(body, limit=0) == body

    def test_output_stays_within_the_budget_it_was_given(self) -> None:
        # The notice itself adds a little, but the payload must obey the cap —
        # otherwise the "cap" silently is not one.
        for limit in (500, 1000, 4000):
            out = budget.clip_for_context("q" * 200_000, limit=limit)
            payload = out.replace("q", "")
            assert len(out) - len(payload) <= limit
