#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.setup_wizard — first-run configuration.

    genesis setup

Asks for one key at a time, tests each one against the real endpoint before
saving, and writes `~/.genesis/.env` with mode 600. Every provider is optional:
you can finish with a single key and add more later by running setup again.

Deliberately not silent about failures. A key that does not work is worth
knowing about now, not on your first mission.
"""
from __future__ import annotations

import os
import sys

import requests

from genesis_agent.paths import ENV_FILE, ensure_genesis_home, read_env_files

# (env var, display name, where to get it, base_url, a model to smoke-test with)
PROVIDERS: list[tuple[str, str, str, str, str]] = [
    ("HF_TOKEN", "HuggingFace",
     "https://huggingface.co/settings/tokens",
     "https://router.huggingface.co/v1", "Qwen/Qwen2.5-Coder-32B-Instruct"),
    ("OPENROUTER_API_KEY", "OpenRouter",
     "https://openrouter.ai/keys",
     "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    ("OLLAMA_API_KEY", "Ollama Cloud",
     "https://ollama.com/settings/keys",
     "https://ollama.com/v1", "gpt-oss:120b-cloud"),
    ("GROQ_API_KEY", "Groq",
     "https://console.groq.com/keys",
     "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("NVIDIA_API_KEY", "NVIDIA NIM",
     "https://build.nvidia.com/",
     "https://integrate.api.nvidia.com/v1", "mistralai/mixtral-8x7b-instruct-v0.1"),
    ("COHERE_API_KEY", "Cohere",
     "https://dashboard.cohere.com/api-keys",
     "https://api.cohere.ai/compatibility/v1", "command-a-03-2025"),
]


def _test_key(base_url: str, key: str, model: str) -> tuple[bool, str]:
    """One real, tiny request. Returns (ok, human-readable reason)."""
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "say ok"}],
                  "max_tokens": 5},
            timeout=30,
        )
    except requests.RequestException as e:
        return False, f"network error: {type(e).__name__}"

    if r.status_code == 200:
        return True, "works"
    if r.status_code in (401, 403):
        return False, "key rejected (401/403) — wrong or expired"
    if r.status_code == 402:
        return False, "payment required — the account needs billing enabled"
    if r.status_code == 429:
        # The key is valid; the quota is just busy right now.
        return True, "valid, but rate-limited at the moment"
    if r.status_code == 404:
        return True, f"key accepted, but the test model is unavailable ({model})"
    return False, f"HTTP {r.status_code}: {r.text[:100]}"


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nПрекратено.")
        sys.exit(1)


def _existing(var: str) -> str:
    return os.environ.get(var) or read_env_files(var) or ""


def run() -> int:
    print("\n  ⚡ Genesis Agent — настройка\n")
    print("  Всеки ключ е по избор. Един е достатъчен, за да тръгнеш.")
    print("  Enter без нищо = пропусни този доставчик.")
    print("  Ключовете се записват в:", ENV_FILE)
    print()

    collected: dict[str, str] = {}
    working = 0

    for var, name, url, base_url, model in PROVIDERS:
        current = _existing(var)
        if current:
            print(f"  ✓ {name}: вече е зададен — пропускам "
                  f"(изтрий го от {ENV_FILE}, за да го смениш)")
            collected[var] = current
            working += 1
            continue

        print(f"  {name}  —  вземи ключ от: {url}")
        key = _prompt(f"    {var} = ")
        if not key:
            print("    (пропуснат)\n")
            continue

        print("    проверявам…", end=" ", flush=True)
        ok, why = _test_key(base_url, key, model)
        print("✅" if ok else "❌", why)
        if ok:
            collected[var] = key
            working += 1
        else:
            keep = _prompt("    Да го запиша ли въпреки това? [y/N] ").lower()
            if keep.startswith("y"):
                collected[var] = key
        print()

    if not collected:
        print("  Нито един ключ не е зададен. Genesis може да работи и само с")
        print("  локален Ollama модел, но без ключ облачната верига е празна.")
        print("  Пусни `genesis setup` пак, когато имаш ключ.\n")
        return 1

    # ── Discord (optional) ────────────────────────────────────────────────
    print("  Discord бот (по избор — чат с агента от телефона).")
    print("  Enter, за да пропуснеш.")
    token = _prompt("    GENESIS_DISCORD_BOT_TOKEN = ")
    if token:
        collected["GENESIS_DISCORD_BOT_TOKEN"] = token
        print("    Твоят Discord user ID е ЗАДЪЛЖИТЕЛЕН — без него ботът не")
        print("    отговаря на никого (Settings → Advanced → Developer Mode →")
        print("    десен клик на профила ти → Copy User ID).")
        owner = _prompt("    GENESIS_DISCORD_OWNER_ID = ")
        if owner:
            collected["GENESIS_DISCORD_OWNER_ID"] = owner
        else:
            print("    ⚠️  Без ID ботът ще мълчи. Добави го по-късно в", ENV_FILE)
    print()

    # ── Write ─────────────────────────────────────────────────────────────
    ensure_genesis_home()
    lines = ["# Genesis Agent — written by `genesis setup`.",
             "# Keep this file private. It is never committed.", ""]
    lines += [f"{k}={v}" for k, v in collected.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)

    print(f"  ✅ Записано в {ENV_FILE} (права 600 — само ти можеш да го четеш)")
    print(f"  {working} работещи доставчика.\n")
    print("  Пусни агента:   genesis\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
