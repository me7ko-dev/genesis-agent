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


class TestBudgetHistory:
    """budget_history() is where the actual saving happens: a tool result is
    re-sent on every later round until it falls out of the window, so by
    round 10 the first one has been paid for ten times while being useful
    for two. It must shrink the old ones WITHOUT touching the caller's own
    history, the system prompt, or the tool_call_id wiring.
    """

    @staticmethod
    def _history(n_results: int, size: int = 20_000) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": "SYSTEM"}]
        for i in range(n_results):
            msgs.append({"role": "assistant", "content": f"call {i}"})
            msgs.append({"role": "tool", "tool_call_id": str(i), "name": "RUN_CMD",
                         "content": f"START{i}" + "x" * size + f"END{i}"})
        return msgs

    def test_the_freshest_results_are_left_untouched(self) -> None:
        msgs = self._history(4)
        out = budget.budget_history(msgs, fresh=2, stale_limit=1000)
        assert out[-1]["content"] == msgs[-1]["content"]
        assert out[-3]["content"] == msgs[-3]["content"]

    def test_older_results_are_shrunk_but_keep_both_ends(self) -> None:
        msgs = self._history(4)
        out = budget.budget_history(msgs, fresh=2, stale_limit=1000)
        first = out[2]["content"]
        assert len(first) < 1300
        assert first.startswith("START0")
        assert first.endswith("END0")

    def test_the_callers_history_is_never_mutated(self) -> None:
        msgs = self._history(4)
        before = [dict(m) for m in msgs]
        budget.budget_history(msgs, fresh=1, stale_limit=500)
        assert msgs == before

    def test_tool_call_wiring_and_roles_survive(self) -> None:
        msgs = self._history(3)
        out = budget.budget_history(msgs, fresh=1, stale_limit=500)
        for original, produced in zip(msgs, out):
            assert produced["role"] == original["role"]
            assert produced.get("tool_call_id") == original.get("tool_call_id")
            assert produced.get("name") == original.get("name")

    def test_the_system_prompt_is_never_shrunk(self) -> None:
        msgs = self._history(3)
        msgs[0] = {"role": "system", "content": "S" * 50_000}
        out = budget.budget_history(msgs, fresh=0, stale_limit=100)
        assert out[0]["content"] == msgs[0]["content"]

    def test_text_tag_results_are_budgeted_too(self) -> None:
        """The text-tag path injects results as a system message, not role=tool.
        Missing that would silently exempt every non-native model."""
        msgs = [
            {"role": "system", "content": "SYSTEM"},
            {"role": "system", "content": "[Резултат]:\n" + "y" * 30_000},
            {"role": "assistant", "content": "ok"},
            {"role": "system", "content": "[Резултат]:\n" + "z" * 30_000},
        ]
        out = budget.budget_history(msgs, fresh=1, stale_limit=800)
        assert len(out[1]["content"]) < 1100, "старият текстов резултат трябва да е свит"
        assert out[3]["content"] == msgs[3]["content"], "последният остава пълен"

    def test_a_short_history_is_returned_as_is(self) -> None:
        msgs = self._history(1, size=50)
        assert budget.budget_history(msgs) == msgs

    def test_zero_stale_limit_disables_the_whole_pass(self) -> None:
        msgs = self._history(5)
        assert budget.budget_history(msgs, fresh=1, stale_limit=0) == msgs

    def test_it_saves_more_the_longer_the_session_runs(self) -> None:
        short = budget.budget_history(self._history(3), fresh=2, stale_limit=1000)
        long = budget.budget_history(self._history(12), fresh=2, stale_limit=1000)
        def ratio(original, out):
            return sum(len(str(m["content"])) for m in out) / \
                   sum(len(str(m["content"])) for m in original)
        assert ratio(self._history(12), long) < ratio(self._history(3), short)


class TestDuplicateResultsArePaidForOnce:
    """The model re-reads the same file before editing it, re-runs the same
    pytest after each fix, repeats `git status`. Every copy used to be sent
    again on every later round. Keep the newest copy (that's the one being
    worked on) and replace the earlier ones with a pointer.
    """

    @staticmethod
    def _msgs(contents: list[str]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": "S"}]
        for i, c in enumerate(contents):
            out.append({"role": "tool", "tool_call_id": str(i),
                        "name": "READ_FILE", "content": c})
        return out

    def test_repeated_identical_results_collapse_to_one_copy(self) -> None:
        same = "FILE BODY\n" + "x" * 3000
        out = budget.budget_history(self._msgs([same, "other" * 100, same, same]),
                                    fresh=1, stale_limit=2000)
        bodies = [str(m["content"]) for m in out if m.get("role") == "tool"]
        assert bodies.count(same) == 1, "само едно пълно копие трябва да остане"
        assert sum("идентичен" in b for b in bodies) == 2

    def test_the_newest_copy_is_the_one_kept(self) -> None:
        same = "BODY " + "y" * 3000
        out = budget.budget_history(self._msgs([same, same]), fresh=1, stale_limit=2000)
        assert "идентичен" in str(out[1]["content"])
        assert str(out[2]["content"]) == same

    def test_short_results_are_not_deduplicated(self) -> None:
        """A pointer longer than the content itself is not a saving."""
        out = budget.budget_history(self._msgs(["OK", "OK", "OK"]),
                                    fresh=1, stale_limit=2000)
        assert [str(m["content"]) for m in out if m.get("role") == "tool"] == ["OK"] * 3

    def test_different_results_are_left_alone(self) -> None:
        a, b = "A" * 3000, "B" * 3000
        out = budget.budget_history(self._msgs([a, b]), fresh=2, stale_limit=2000)
        assert str(out[1]["content"]) == a
        assert str(out[2]["content"]) == b

    def test_dedup_applies_even_to_the_fresh_window(self) -> None:
        """Two identical results back to back are still two copies of one
        thing — recency does not make the older one worth its tokens."""
        same = "Z" * 4000
        out = budget.budget_history(self._msgs([same, same]), fresh=2, stale_limit=2000)
        assert "идентичен" in str(out[1]["content"])
        assert str(out[2]["content"]) == same

    def test_the_callers_history_is_still_not_mutated(self) -> None:
        same = "W" * 3000
        msgs = self._msgs([same, same])
        before = [dict(m) for m in msgs]
        budget.budget_history(msgs, fresh=1, stale_limit=500)
        assert msgs == before


class TestCachedTokensAreCountedSeparately:
    """Prompt caching прави схемите на инструментите и системния промпт евтини
    при повтарящи се заявки, но проваля се БЕЗШУМНО: при развален префикс
    всичко продължава да работи и просто струва пълна цена. Затова
    прочетеното от кеша се записва отделно — нула при повтарящи се заявки е
    сигналът, който иначе никой не вижда."""

    def test_record_usage_stores_the_cache_fields(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "budget_log.jsonl"
        monkeypatch.setattr(budget, "LOG_PATH", log)
        budget.record_usage(provider="anthropic", model="m", prompt_tokens=100,
                            completion_tokens=20, cached_read_tokens=4300,
                            cached_write_tokens=15)
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert entry["cached_read_tokens"] == 4300
        assert entry["cached_write_tokens"] == 15

    def test_cached_reads_are_not_folded_into_prompt_tokens(self, tmp_path, monkeypatch) -> None:
        """Таксуват се различно — събирането им би скрило точно икономията,
        заради която съществуват."""
        log = tmp_path / "budget_log.jsonl"
        monkeypatch.setattr(budget, "LOG_PATH", log)
        budget.record_usage(provider="anthropic", model="m", prompt_tokens=100,
                            completion_tokens=20, cached_read_tokens=4300)
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert entry["prompt_tokens"] == 100
        assert entry["total_tokens"] == 120

    def test_a_provider_without_caching_records_zeroes(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "budget_log.jsonl"
        monkeypatch.setattr(budget, "LOG_PATH", log)
        budget.record_usage(provider="ollama", model="m", prompt_tokens=5, completion_tokens=5)
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert entry["cached_read_tokens"] == 0

    def test_totals_sum_the_cache_reads(self, tmp_path, monkeypatch) -> None:
        log = tmp_path / "budget_log.jsonl"
        monkeypatch.setattr(budget, "LOG_PATH", log)
        for _ in range(3):
            budget.record_usage(provider="anthropic", model="m", prompt_tokens=10,
                                completion_tokens=5, cached_read_tokens=1000)
        assert budget.today_totals()["cached_read_tokens"] == 3000
        assert budget.range_totals(days=7)["cached_read_tokens"] == 3000

    def test_entries_written_before_this_field_existed_still_total(
        self, tmp_path, monkeypatch
    ) -> None:
        """Логът е append-only и вече съдържа редове без тези ключове —
        отчетът за деня не бива да гръмне заради стар запис."""
        log = tmp_path / "budget_log.jsonl"
        monkeypatch.setattr(budget, "LOG_PATH", log)
        today_utc = datetime.now(timezone.utc).date()
        _write_entry(log, ts=f"{today_utc.isoformat()}T10:00:00+00:00",
                     provider="p1", prompt=10, completion=5)
        totals = budget.today_totals()
        assert totals["calls"] == 1
        assert totals["cached_read_tokens"] == 0


class TestFormatReport:
    def test_no_calls_says_so(self) -> None:
        totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0, "cached_read_tokens": 0, "cached_write_tokens": 0,
                  "by_provider": {}}
        out = budget.format_report(totals, title="Днес")
        assert "Днес" in out
        assert "Няма записани" in out

    def test_totals_and_provider_breakdown_show(self) -> None:
        totals = {"calls": 3, "prompt_tokens": 1000, "completion_tokens": 200,
                  "total_tokens": 1200, "cached_read_tokens": 0, "cached_write_tokens": 0,
                  "by_provider": {"anthropic": {"calls": 2, "total_tokens": 900},
                                  "deepseek": {"calls": 1, "total_tokens": 300}}}
        out = budget.format_report(totals, title="Днес")
        assert "3" in out and "1200" in out
        assert "anthropic" in out and "deepseek" in out
        # По-скъпият доставчик излиза пръв.
        assert out.index("anthropic") < out.index("deepseek")

    def test_cache_line_only_appears_when_there_is_cache_activity(self) -> None:
        no_cache = {"calls": 1, "prompt_tokens": 100, "completion_tokens": 10,
                    "total_tokens": 110, "cached_read_tokens": 0, "cached_write_tokens": 0,
                    "by_provider": {}}
        assert "Кеш" not in budget.format_report(no_cache, title="Днес")

    def test_cache_line_shows_the_read_percentage_of_prompt_tokens(self) -> None:
        totals = {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 10,
                  "total_tokens": 1010, "cached_read_tokens": 900, "cached_write_tokens": 50,
                  "by_provider": {}}
        out = budget.format_report(totals, title="Днес")
        assert "900" in out and "90%" in out and "50" in out
