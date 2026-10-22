"""Anthropic с API ключ в менюто `/model` (2026-10-12).

Операторът имаше ключ в ~/.genesis/.env и пакета `anthropic`, а в `/model`
нямаше ред за него — Claude с ключа влизаше само с `--max`. Тук `anthropic`
е фалшив модул: CI няма ключ и не бива да плаща.
"""
from __future__ import annotations

import importlib.machinery
import sys
import types
from types import SimpleNamespace

import pytest

from genesis_agent import brain


def _fake_sdk(models=None, error: Exception | None = None) -> types.ModuleType:
    """Модул `anthropic`, чийто Models API връща `models` или вдига `error`."""
    seen: dict = {}

    class _Models:
        def list(self):
            if error:
                raise error
            return [SimpleNamespace(id=m) for m in models or []]

    class Anthropic:
        def __init__(self, **kw):
            seen.update(kw)
            self.models = _Models()

    mod = types.ModuleType("anthropic")
    mod.__spec__ = importlib.machinery.ModuleSpec("anthropic", None)
    mod.Anthropic = Anthropic  # type: ignore[attr-defined]
    mod.seen = seen  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def gta(monkeypatch):
    import genesis_terminal_agent as gta
    monkeypatch.setattr(gta, "MODELS_CACHE", {})
    monkeypatch.setitem(gta.KEYS, "ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    return gta


# ── в менюто ─────────────────────────────────────────────────────────────────

def test_the_model_menu_has_a_paid_anthropic_entry(gta) -> None:
    p = gta.PROVIDERS["anthropic"]
    assert p["key_env"] == "ANTHROPIC_API_KEY"
    # .env ключът стига до терминала само ако е в KEYS (load_env чете само тях).
    assert "ANTHROPIC_API_KEY" in gta.KEYS
    assert "PAID" in gta.model_badge("anthropic", "claude-opus-5")
    assert gta._brain_handles("anthropic")


def test_without_a_key_the_menu_names_the_missing_key(gta, monkeypatch) -> None:
    monkeypatch.setitem(gta.KEYS, "ANTHROPIC_API_KEY", "")
    ready, why = gta.provider_ready("anthropic")
    assert not ready and "ANTHROPIC_API_KEY" in why


def test_without_the_package_the_menu_says_how_to_add_it(gta, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)   # find_spec → None
    ready, why = gta.provider_ready("anthropic")
    assert not ready and "pipx inject" in why


def test_with_a_key_and_the_package_the_menu_is_ready(gta, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", _fake_sdk())
    assert gta.provider_ready("anthropic") == (True, "")


def test_the_models_come_from_the_keys_live_list(gta, monkeypatch) -> None:
    sdk = _fake_sdk(["claude-opus-5-5", "claude-sonnet-5"])
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    assert gta.fetch_models("anthropic") == ["claude-opus-5-5", "claude-sonnet-5"]
    assert sdk.seen["api_key"] == "sk-test-not-a-real-key"


def test_without_network_the_menu_still_offers_models(gta, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", _fake_sdk(error=OSError("няма мрежа")))
    assert gta.fetch_models("anthropic") == gta.FALLBACKS["anthropic"]
    assert "anthropic" not in gta.MODELS_CACHE      # следващото отваряне пробва пак


# ── в Brain ──────────────────────────────────────────────────────────────────

@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(brain, "_load_keys", lambda: {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"})
    monkeypatch.setattr(brain, "_local_available", lambda _m: False)
    monkeypatch.setattr(brain, "_EXHAUSTED", {})
    monkeypatch.delenv("GENESIS_MODEL_GATEWAY", raising=False)
    monkeypatch.delenv("GENESIS_QUALITY", raising=False)


def _capture(b: brain.Brain, monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake(key, model, messages, tools, effort):
        calls.append({"key": key, "model": model, "tools": tools, "effort": effort})
        return "готово", None

    monkeypatch.setattr(b, "_call_anthropic", fake)
    return calls


def test_a_pinned_anthropic_model_answers_with_tools_and_the_config_effort(with_key, monkeypatch) -> None:
    """Същото като --max за този модел: native tools и effort от premium_models."""
    # 2026-10-22: Sonnet 5.5 е записът в premium_models (наследникът на Sonnet 5).
    b = brain.Brain(pin_model=("anthropic", "claude-sonnet-5-5"), use_local=False)
    calls = _capture(b, monkeypatch)
    tools = [{"type": "function", "function": {"name": "READ_FILE", "parameters": {}}}]
    reply = b.complete([{"role": "user", "content": "здравей"}], tools=tools)
    assert reply.raw_text == "готово"
    assert b.current["provider"] == "anthropic"
    assert calls[0]["model"] == "claude-sonnet-5-5"
    assert calls[0]["tools"]                      # не текстови тагове
    effort = next(c["effort"] for c in brain._load_premium_chain()
                  if c["model"] == "claude-sonnet-5-5")
    assert calls[0]["effort"] == effort


def test_a_pinned_model_outside_the_config_still_gets_tools(with_key, monkeypatch) -> None:
    b = brain.Brain(pin_model=("anthropic", "claude-haiku-4-5"), use_local=False)
    assert b.chain[0]["supports_tools"] is True
    calls = _capture(b, monkeypatch)
    b.complete([{"role": "user", "content": "x"}])
    assert calls[0]["effort"] == ""               # _call_anthropic решава по модела


def test_pinning_one_paid_model_adds_no_other_paid_model(with_key) -> None:
    """Изборът в `/model` е за един модел; останалите платени чакат --max."""
    chain = brain.Brain(pin_model=("anthropic", "claude-sonnet-5"), use_local=False).chain
    assert chain[0]["model"] == "claude-sonnet-5"
    assert all(c["provider"] != "anthropic" for c in chain[1:])
