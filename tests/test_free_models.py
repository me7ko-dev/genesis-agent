"""genesis_agent.free_models — discovers the provider's free models instead of
having the list maintained by hand in config.yaml, which goes stale in weeks
(OpenRouter's free count went 29 -> 25 -> 23 over three months of 2026, and
config.yaml's own history records entries that started returning 404/410).

The risky parts, and what these tests pin down:
  1. "free" is decided by PRICE, not by the `:free` suffix — a zero prompt
     price with a non-zero completion price is not free.
  2. a MoE name like `...-550b-a55b` means 550B total, not 55B.
  3. every failure path stays fail-open: no network, no cache, corrupt cache
     must each mean "no extra models", never an exception at startup.
  4. a refresh that fails must NOT wipe a good cache — that cache is the
     fallback used precisely when something else already broke.
"""
from __future__ import annotations

import json

from genesis_agent import free_models as fm


class TestFreeIsDecidedByPrice:
    def test_all_zero_prices_is_free(self) -> None:
        assert fm._is_free({"pricing": {"prompt": "0", "completion": "0"}}) is True

    def test_zero_prompt_but_paid_completion_is_not_free(self) -> None:
        """The trap: the id may still end in `:free` while the completion side
        bills. What matters is whether the operator gets charged."""
        assert fm._is_free({"pricing": {"prompt": "0", "completion": "0.0000004"}}) is False

    def test_paid_prompt_is_not_free(self) -> None:
        assert fm._is_free({"pricing": {"prompt": "0.0000012", "completion": "0"}}) is False

    def test_missing_or_unparsable_pricing_is_not_assumed_free(self) -> None:
        assert fm._is_free({}) is False
        assert fm._is_free({"pricing": {}}) is False
        assert fm._is_free({"pricing": {"prompt": "n/a"}}) is False

    def test_a_request_fee_also_disqualifies(self) -> None:
        assert fm._is_free({"pricing": {"prompt": "0", "completion": "0",
                                        "request": "0.001"}}) is False


class TestSizeParsing:
    def test_moe_name_reports_total_not_active_params(self) -> None:
        assert fm._parse_size_b("nvidia/nemotron-3-ultra-550b-a55b") == 550.0

    def test_plain_size(self) -> None:
        assert fm._parse_size_b("meta-llama/Llama-3.3-70B-Instruct") == 70.0

    def test_unknown_size_is_zero_not_a_guess(self) -> None:
        assert fm._parse_size_b("some/mystery-model:free") == 0.0


class TestDiscover:
    def _catalog(self, monkeypatch, entries) -> None:
        monkeypatch.setattr(fm, "_fetch_catalog", lambda *a, **kw: entries)

    def test_only_free_entries_survive(self, monkeypatch) -> None:
        self._catalog(monkeypatch, [
            {"id": "big/paid-70b", "pricing": {"prompt": "0.001", "completion": "0"}},
            {"id": "big/free-70b:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        assert [m["model"] for m in fm.discover()] == ["big/free-70b:free"]

    def test_tool_support_comes_from_the_catalog_not_a_guess(self, monkeypatch) -> None:
        self._catalog(monkeypatch, [
            {"id": "a/model-70b:free", "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["temperature", "tools"]},
            {"id": "b/model-70b:free", "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["temperature"]},
        ])
        by_id = {m["model"]: m for m in fm.discover()}
        assert by_id["a/model-70b:free"]["supports_tools"] is True
        assert by_id["b/model-70b:free"]["supports_tools"] is False

    def test_models_below_the_size_floor_are_dropped(self, monkeypatch) -> None:
        self._catalog(monkeypatch, [
            {"id": "tiny/model-3b:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "big/model-70b:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        assert [m["model"] for m in fm.discover()] == ["big/model-70b:free"]

    def test_unknown_size_is_kept_rather_than_silently_dropped(self, monkeypatch) -> None:
        """A name that does not encode its size is not evidence of a small
        model — dropping it would hide usable fallbacks."""
        self._catalog(monkeypatch, [
            {"id": "some/mystery:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        assert [m["model"] for m in fm.discover()] == ["some/mystery:free"]

    def test_biggest_first(self, monkeypatch) -> None:
        self._catalog(monkeypatch, [
            {"id": "a/model-70b:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "b/model-550b:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "c/model-120b:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        assert [m["size_b"] for m in fm.discover()] == [550.0, 120.0, 70.0]

    def test_a_dead_network_yields_nothing_instead_of_raising(self, monkeypatch) -> None:
        def _boom(*a, **kw):
            raise OSError("network unreachable")
        monkeypatch.setattr(fm.urllib.request, "urlopen", _boom)
        assert fm.discover() == []


class TestCache:
    def test_refresh_writes_and_cached_reads_back(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(fm, "CACHE_PATH", tmp_path / "free.json")
        monkeypatch.setattr(fm, "discover", lambda **kw: [
            {"provider": "openrouter", "model": "x/y-70b:free",
             "size_b": 70.0, "supports_tools": True, "context_length": 1},
        ])
        count, _ = fm.refresh()
        assert count == 1
        assert [m["model"] for m in fm.cached()] == ["x/y-70b:free"]

    def test_a_failed_refresh_leaves_a_good_cache_alone(self, monkeypatch, tmp_path) -> None:
        cache = tmp_path / "free.json"
        cache.write_text(json.dumps({"fetched_at": "2026-09-01T00:00:00+00:00", "models": [
            {"provider": "openrouter", "model": "kept/model-70b:free",
             "size_b": 70.0, "supports_tools": True},
        ]}), encoding="utf-8")
        monkeypatch.setattr(fm, "CACHE_PATH", cache)
        monkeypatch.setattr(fm, "discover", lambda **kw: [])
        count, message = fm.refresh()
        assert count == 0
        assert "остава" in message
        assert [m["model"] for m in fm.cached()] == ["kept/model-70b:free"]

    def test_missing_cache_is_empty_not_an_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(fm, "CACHE_PATH", tmp_path / "nope.json")
        assert fm.cached() == []
        assert fm.cache_age_days() is None

    def test_corrupt_cache_is_empty_not_an_error(self, monkeypatch, tmp_path) -> None:
        cache = tmp_path / "free.json"
        cache.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(fm, "CACHE_PATH", cache)
        assert fm.cached() == []
        assert fm.cache_age_days() is None

    def test_entries_without_provider_or_model_are_skipped(self, monkeypatch, tmp_path) -> None:
        cache = tmp_path / "free.json"
        cache.write_text(json.dumps({"models": [
            {"provider": "openrouter"}, {"model": "x"}, "not a dict",
            {"provider": "openrouter", "model": "ok/model:free"},
        ]}), encoding="utf-8")
        monkeypatch.setattr(fm, "CACHE_PATH", cache)
        assert [m["model"] for m in fm.cached()] == ["ok/model:free"]

    def test_the_opt_out_env_var_disables_the_whole_layer(self, monkeypatch, tmp_path) -> None:
        cache = tmp_path / "free.json"
        cache.write_text(json.dumps({"models": [
            {"provider": "openrouter", "model": "x/y:free"}]}), encoding="utf-8")
        monkeypatch.setattr(fm, "CACHE_PATH", cache)
        monkeypatch.setenv("GENESIS_NO_FREE_MODELS", "1")
        assert fm.cached() == []


class TestChainIntegration:
    def test_discovered_models_are_appended_to_the_chain(self, monkeypatch) -> None:
        from genesis_agent import brain
        monkeypatch.setattr(fm, "cached", lambda: [
            {"provider": "openrouter", "model": "discovered/model-70b:free",
             "size_b": 70.0, "supports_tools": True},
        ])
        chain = brain._load_chain()
        assert any(c["model"] == "discovered/model-70b:free" for c in chain)

    def test_a_manually_curated_entry_keeps_its_place_and_metadata(self, monkeypatch) -> None:
        """config.yaml entries are verified live against a real key; a catalog
        claim must not be able to displace one or overwrite its metadata."""
        from genesis_agent import brain
        manual = brain._load_chain()[0]
        monkeypatch.setattr(fm, "cached", lambda: [
            {"provider": manual["provider"], "model": manual["model"],
             "size_b": 1.0, "supports_tools": not manual["supports_tools"]},
        ])
        chain = brain._load_chain()
        assert chain[0] == manual
        assert sum(1 for c in chain if c["model"] == manual["model"]) == 1

    def test_a_broken_free_models_layer_never_breaks_the_chain(self, monkeypatch) -> None:
        from genesis_agent import brain
        def _boom():
            raise RuntimeError("cache exploded")
        monkeypatch.setattr(fm, "cached", _boom)
        assert len(brain._load_chain()) > 0
