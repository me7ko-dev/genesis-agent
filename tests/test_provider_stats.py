"""genesis_agent.provider_stats — the data-driven fallback-chain reordering.
_STATS_PATH is monkeypatched to a tmp file, never the real one."""
from __future__ import annotations

from genesis_agent import provider_stats


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(provider_stats, "_STATS_PATH", tmp_path / "provider_stats.json")


def test_success_rate_none_below_min_samples(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for _ in range(4):
        provider_stats.record_call("flaky", 1.0, False)
    assert provider_stats.success_rate("flaky") is None


def test_success_rate_computed_once_min_samples_reached(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for ok in [True, True, False, False, False]:
        provider_stats.record_call("p", 1.0, ok)
    assert provider_stats.success_rate("p") == 0.4


def test_rolling_window_caps_at_max_samples(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for _ in range(40):
        provider_stats.record_call("p", 1.0, True)
    data = provider_stats._load()
    assert len(data["p"]["samples"]) == provider_stats._MAX_SAMPLES


def test_deprioritize_flaky_moves_confirmed_bad_provider_to_end(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for _ in range(5):
        provider_stats.record_call("bad", 1.0, False)
    for _ in range(5):
        provider_stats.record_call("good", 1.0, True)
    chain = [
        {"provider": "bad", "model": "m1"},
        {"provider": "good", "model": "m2"},
    ]
    reordered = provider_stats.deprioritize_flaky(chain)
    assert [c["provider"] for c in reordered] == ["good", "bad"]


def test_deprioritize_flaky_leaves_undersampled_provider_alone(tmp_path, monkeypatch) -> None:
    """Fewer than 5 samples → not enough data to judge, order must not change."""
    _isolate(monkeypatch, tmp_path)
    for _ in range(3):
        provider_stats.record_call("new", 1.0, False)
    chain = [{"provider": "new", "model": "m1"}, {"provider": "other", "model": "m2"}]
    reordered = provider_stats.deprioritize_flaky(chain)
    assert [c["provider"] for c in reordered] == ["new", "other"]


def test_deprioritize_flaky_preserves_relative_order_within_groups(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for _ in range(5):
        provider_stats.record_call("bad1", 1.0, False)
        provider_stats.record_call("bad2", 1.0, False)
    chain = [
        {"provider": "bad1", "model": "a"},
        {"provider": "good1", "model": "b"},
        {"provider": "bad2", "model": "c"},
        {"provider": "good2", "model": "d"},
    ]
    reordered = provider_stats.deprioritize_flaky(chain)
    assert [c["provider"] for c in reordered] == ["good1", "good2", "bad1", "bad2"]


def test_avg_latency_only_counts_successful_calls(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    provider_stats.record_call("p", 10.0, False)
    provider_stats.record_call("p", 2.0, True)
    provider_stats.record_call("p", 4.0, True)
    assert provider_stats.avg_latency("p") == 3.0
