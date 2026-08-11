"""genesis_agent.product_api — the network-exposed service (bound to 0.0.0.0,
rate-limited but deliberately unauthenticated, see the module docstring).

Two bugs fixed here on 2026-08-12, both of which only bite once more than one
client talks to it — which is exactly what binding to 0.0.0.0 invites:

  * _check_rate never released a bucket. Only the caller's own list got
    pruned, so every IP ever seen kept a dict entry forever: unbounded growth
    keyed by a value the sender controls.
  * /v1/chat/completions mutated process-global state (set_local_only writes
    os.environ["GENESIS_LOCAL_ONLY"] plus brain.LOCAL_MODEL) while FastAPI
    served sync endpoints from a threadpool, so concurrent requests clobbered
    each other's model choice.

fastapi is an optional dependency; this file skips without it.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from genesis_agent import product_api as pa


@pytest.fixture(autouse=True)
def _clean_rate_state():
    pa._RATE.clear()
    yield
    pa._RATE.clear()


class TestRateLimit:
    def test_allows_up_to_the_limit(self) -> None:
        for _ in range(pa._RATE_LIMIT):
            pa._check_rate("1.2.3.4")  # must not raise

    def test_blocks_past_the_limit(self) -> None:
        from fastapi import HTTPException
        for _ in range(pa._RATE_LIMIT):
            pa._check_rate("1.2.3.4")
        with pytest.raises(HTTPException) as exc:
            pa._check_rate("1.2.3.4")
        assert exc.value.status_code == 429

    def test_separate_ips_have_separate_budgets(self) -> None:
        for _ in range(pa._RATE_LIMIT):
            pa._check_rate("1.1.1.1")
        pa._check_rate("2.2.2.2")  # a different client is unaffected

    def test_old_hits_expire_so_the_caller_gets_a_fresh_budget(self, monkeypatch) -> None:
        base = 1_000_000.0
        monkeypatch.setattr(pa.time, "time", lambda: base)
        for _ in range(pa._RATE_LIMIT):
            pa._check_rate("1.2.3.4")
        monkeypatch.setattr(pa.time, "time", lambda: base + pa._RATE_WINDOW + 1)
        pa._check_rate("1.2.3.4")  # window rolled over, must not raise

    def test_idle_ips_are_evicted_not_kept_forever(self, monkeypatch) -> None:
        """The leak: buckets for IPs that never come back used to stay in the
        dict for the lifetime of the process."""
        base = 1_000_000.0
        monkeypatch.setattr(pa.time, "time", lambda: base)
        for i in range(50):
            pa._check_rate(f"10.0.0.{i}")
        assert len(pa._RATE) == 50

        monkeypatch.setattr(pa.time, "time", lambda: base + pa._RATE_WINDOW + 1)
        pa._check_rate("192.168.1.1")

        # Only the still-active caller remains.
        assert list(pa._RATE) == ["192.168.1.1"]

    def test_an_active_ip_is_not_evicted_by_someone_elses_request(self, monkeypatch) -> None:
        base = 1_000_000.0
        monkeypatch.setattr(pa.time, "time", lambda: base)
        pa._check_rate("1.1.1.1")
        monkeypatch.setattr(pa.time, "time", lambda: base + 1)
        pa._check_rate("2.2.2.2")
        assert set(pa._RATE) == {"1.1.1.1", "2.2.2.2"}


class TestLocalModeIsolation:
    def test_the_lock_exists_and_is_a_real_lock(self) -> None:
        import threading
        assert isinstance(pa._LOCAL_MODE_LOCK, type(threading.Lock()))

    def test_local_mode_aliases_map_to_real_brain_tiers(self) -> None:
        """The sentinel values are the client's only way to pick a tier (they
        speak JSON, not Python), so they must stay in step with brain.py
        rather than drifting into their own hardcoded copies."""
        from genesis_agent.brain import LOCAL_TIER_MAX, LOCAL_TIER_NORMAL
        assert pa._LOCAL_MODE_ALIASES["local-max"] == LOCAL_TIER_MAX
        assert pa._LOCAL_MODE_ALIASES["local-normal"] == LOCAL_TIER_NORMAL

    def test_unknown_model_name_means_normal_cloud_first_order(self) -> None:
        assert pa._LOCAL_MODE_ALIASES.get("gpt-4") is None
        assert pa._LOCAL_MODE_ALIASES.get("") is None
