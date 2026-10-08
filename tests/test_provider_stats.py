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


def test_old_failures_expire_and_the_provider_gets_its_place_back(tmp_path, monkeypatch) -> None:
    """2026-09-25: 16 провала на ollama за 2 минути го пратиха зад nvidia —
    и понеже nvidia отговаряше първа, ollama не получи нов шанс часове наред."""
    _isolate(monkeypatch, tmp_path)
    real_time = provider_stats.time.time
    monkeypatch.setattr(provider_stats.time, "time", lambda: real_time() - 3600)
    for _ in range(16):
        provider_stats.record_call("ollama_cloud", 0.3, False)
    monkeypatch.setattr(provider_stats.time, "time", real_time)
    chain = [{"provider": "ollama_cloud", "model": "a"}, {"provider": "nvidia", "model": "b"}]
    assert [c["provider"] for c in provider_stats.deprioritize_flaky(chain)] == ["ollama_cloud", "nvidia"]
    assert provider_stats.success_rate("ollama_cloud") is None


def test_fresh_failures_still_demote(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for _ in range(6):
        provider_stats.record_call("ollama_cloud", 0.3, False)
    chain = [{"provider": "ollama_cloud", "model": "a"}, {"provider": "nvidia", "model": "b"}]
    assert [c["provider"] for c in provider_stats.deprioritize_flaky(chain)] == ["nvidia", "ollama_cloud"]


def test_concurrent_processes_do_not_erase_each_other(tmp_path) -> None:
    """Одит 2026-10-07: 4 процеса × 30 записа → оставаха 1–4 извадки."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    stats = str(tmp_path / "s.json")
    code = (f"import sys; sys.path.insert(0, {str(root)!r})\n"
            "from pathlib import Path\n"
            "import genesis_agent.provider_stats as ps\n"
            f"ps._STATS_PATH = Path({stats!r})\n"
            "for _ in range(30): ps.record_call(sys.argv[1], 0.1, True)\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, f"p{i}"]) for i in range(4)]
    for p in procs:
        assert p.wait(60) == 0
    import json
    data = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert {k: len(v["samples"]) for k, v in data.items()} == {f"p{i}": 30 for i in range(4)}
