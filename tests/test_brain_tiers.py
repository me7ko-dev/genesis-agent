"""genesis_agent.brain — the quality tiers (`coding` / `max`).

The property that matters most is the negative one: **without a paid key and
without asking, nothing changes.** Anyone who clones this repo must not
discover a paid model in their chain, so `test_default_chain_has_no_paid_model`
is the test to keep green even if the tiers are redesigned.

The chain is loaded from the real config.yaml on purpose. A mocked config would
happily pass while the shipped file names a provider nobody can call, or drops
`supports_tools` on a model the repair loop needs it from.
"""
from __future__ import annotations

import pytest

from genesis_agent.brain import (
    _PROVIDERS,
    Brain,
    _load_chain,
    _load_coding_chain,
    _load_premium_chain,
)

PAID = {"anthropic", "openai", "deepseek"}


@pytest.fixture(autouse=True)
def _no_local_probe(monkeypatch):
    """Skip the Ollama probe — it makes these tests depend on what's installed."""
    monkeypatch.setattr("genesis_agent.brain._local_available", lambda _m: False)


@pytest.fixture
def no_paid_keys(monkeypatch):
    monkeypatch.setattr("genesis_agent.brain._load_keys", dict)


@pytest.fixture
def with_anthropic_key(monkeypatch):
    monkeypatch.setattr("genesis_agent.brain._load_keys",
                        lambda: {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"})


# ── the negative guarantee ───────────────────────────────────────────────────

def test_default_chain_has_no_paid_model(no_paid_keys) -> None:
    assert all(c["provider"] not in PAID for c in Brain().chain)


def test_default_chain_is_untouched_by_the_tiers(no_paid_keys) -> None:
    assert Brain().chain == _load_chain()


def test_max_without_a_paid_key_falls_back_to_free_coding(no_paid_keys) -> None:
    """"Максимално качество" must stay a meaningful request with no card."""
    chain = Brain(quality="max").chain
    assert all(c["provider"] not in PAID for c in chain)
    assert chain[0]["model"] == _load_coding_chain()[0]["model"]


def test_max_with_a_key_puts_the_paid_model_first(with_anthropic_key) -> None:
    brain = Brain(quality="max")
    assert brain.chain[0]["provider"] == "anthropic"
    assert brain.premium and brain.premium[0]["premium"] is True
    # The free chain survives underneath — an expired card is a weaker answer,
    # not a stopped agent.
    assert any(c["provider"] not in PAID for c in brain.chain)


# ── the coding tier ──────────────────────────────────────────────────────────

def test_coding_tier_leads_the_chain(no_paid_keys) -> None:
    coding = _load_coding_chain()
    assert coding, "config.yaml трябва да дефинира coding_models"
    head = Brain(quality="coding").chain[:len(coding)]
    assert [c["model"] for c in head] == [c["model"] for c in coding]


def test_coding_tier_keeps_the_normal_chain_as_fallback(no_paid_keys) -> None:
    brain = Brain(quality="coding")
    assert len(brain.chain) > len(_load_coding_chain())


def test_coding_models_are_not_duplicated_when_already_in_the_chain(no_paid_keys) -> None:
    brain = Brain(quality="coding")
    seen = [(c["provider"], c["model"]) for c in brain.chain]
    assert len(seen) == len(set(seen))


def test_coding_models_survive_a_min_size_filter(no_paid_keys) -> None:
    """The terminal chat runs min_size_b=32; north-mini-code is 30B and curated.

    Filtering it out there would silently gut the mode in the one frontend most
    likely to use it.
    """
    coding = _load_coding_chain()
    head = Brain(quality="coding", min_size_b=32).chain[:len(coding)]
    assert [c["model"] for c in head] == [c["model"] for c in coding]


def test_every_configured_tier_model_names_a_known_provider() -> None:
    for entry in _load_coding_chain() + _load_premium_chain():
        assert entry["provider"] in _PROVIDERS, entry


def test_coding_models_all_support_native_tool_calling() -> None:
    """Without tool_calls the repair loop drops to the text-tag protocol.

    That path works, but it is measurably more fragile — this mode exists for
    the hard cases, so a model that cannot call tools does not belong in it.
    """
    assert all(c["supports_tools"] for c in _load_coding_chain())


def test_unknown_quality_value_is_ignored_not_an_error(no_paid_keys) -> None:
    assert Brain(quality="ultra-turbo").chain == _load_chain()


def test_env_var_selects_the_tier(no_paid_keys, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_QUALITY", "coding")
    assert Brain().chain[0]["model"] == _load_coding_chain()[0]["model"]
