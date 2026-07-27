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

import datetime
import os
import sys

import requests

from genesis_agent.paths import ENV_FILE, ensure_genesis_home, read_env_files

# (env var, display name, where to get it, base_url, a model to smoke-test with)
#
# The probe model is only ever used to make ONE request during setup — it is
# not the model the agent runs on (that chain lives in config.yaml). Providers
# retire models regularly, so `_test_key` treats a 404/410 here as "the probe
# is stale", never as "your key is bad".
PROVIDERS: list[tuple[str, str, str, str, str]] = [
    ("HF_TOKEN", "HuggingFace",
     "https://huggingface.co/settings/tokens",
     "https://router.huggingface.co/v1", "Qwen/Qwen2.5-Coder-32B-Instruct"),
    ("OPENROUTER_API_KEY", "OpenRouter",
     "https://openrouter.ai/keys",
     "https://openrouter.ai/api/v1", "inclusionai/ling-3.0-flash:free"),
    ("OLLAMA_API_KEY", "Ollama Cloud",
     "https://ollama.com/settings/keys",
     "https://ollama.com/v1", "gpt-oss:120b-cloud"),
    ("GROQ_API_KEY", "Groq",
     "https://console.groq.com/keys",
     "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("NVIDIA_API_KEY", "NVIDIA NIM",
     "https://build.nvidia.com/",
     "https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct"),
]


def _test_key(base_url: str, key: str, model: str) -> tuple[bool, str]:
    """
    Is this key usable? Returns (ok, human-readable reason).

    A real completion is the authority, because that is the request the agent
    actually makes. `GET /models` is tempting as an auth check and is a trap:
    measured across these six providers, four of them answer it with 200 even
    when the key is deliberate garbage. Trusting it hands out green checkmarks
    for expired keys.

    /models is still useful for one narrow job — telling "your key is bad"
    apart from "the model I probed with no longer exists" — so it is consulted
    only on a 404/410, and only for providers that actually authenticate it.
    """
    def _probe() -> requests.Response | None:
        try:
            return requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "say ok"}],
                      "max_tokens": 5},
                timeout=30,
            )
        except requests.RequestException:
            return None

    r = _probe()
    if r is None:
        return False, "мрежова грешка — не можах да достигна доставчика"

    if r.status_code == 200:
        return True, "работи"
    if r.status_code == 401:
        return False, "ключът е отхвърлен (401) — грешен или изтекъл"
    if r.status_code == 403:
        return False, ("403 — блокиран достъп. Ключът може да е наред, но "
                       "доставчикът не обслужва твоя регион (пробвай с VPN)")
    if r.status_code == 402:
        return False, "402 — валиден ключ, но акаунтът няма кредит/плащане"
    if r.status_code == 429:
        return True, "ключът е валиден · квотата е изчерпана в момента"
    if r.status_code in (404, 410):
        # Ambiguous on its own: a dead key and a retired model look the same.
        try:
            m = requests.get(f"{base_url}/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=15)
            if m.status_code in (401, 403):
                return False, "ключът е отхвърлен"
        except requests.RequestException:
            pass
        return True, f"ключът минава · тестовият модел {model} вече не съществува"
    return False, f"HTTP {r.status_code}: {r.text[:90]}"


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
            # An existing key is TESTED, not assumed good. A wizard that prints
            # a checkmark next to a dead key is worse than one that says
            # nothing — it is the reason you stop checking.
            print(f"  {name}  —  ключ вече има, проверявам…", end=" ", flush=True)
            ok, why = _test_key(base_url, current, model)
            print("✅" if ok else "❌", why)
            if ok:
                # Working key: keep it unless the user deliberately replaces it.
                key = _prompt("    [Enter] запази · или въведи нов ключ: ")
                if not key:
                    collected[var] = current
                    working += 1
                    print("    запазен\n")
                    continue
            else:
                # Broken key: replacing is the obvious move, so make that the
                # default and require a deliberate choice to keep it.
                print(f"    Вземи нов от: {url}")
                key = _prompt("    Нов ключ (Enter = остави счупения): ")
                if not key:
                    collected[var] = current
                    print("    оставен непроменен — този доставчик няма да работи\n")
                    continue
        else:
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
    # Carried over silently when already configured — there is nothing to
    # verify here without connecting to the gateway, so re-asking is noise.
    for var in ("GENESIS_DISCORD_BOT_TOKEN", "GENESIS_DISCORD_OWNER_ID",
                "GENESIS_DISCORD_WEBHOOK"):
        val = _existing(var)
        if val:
            collected[var] = val

    if collected.get("GENESIS_DISCORD_BOT_TOKEN"):
        has_owner = bool(collected.get("GENESIS_DISCORD_OWNER_ID"))
        print(f"  Discord бот: настроен {'✅' if has_owner else '⚠️'}")
        if not has_owner:
            print("    Липсва GENESIS_DISCORD_OWNER_ID — без него ботът НЕ отговаря")
            print("    на никого (Settings → Advanced → Developer Mode → десен")
            print("    клик на профила ти → Copy User ID).")
            owner = _prompt("    GENESIS_DISCORD_OWNER_ID = ")
            if owner:
                collected["GENESIS_DISCORD_OWNER_ID"] = owner
    else:
        print("  Discord бот (по избор — чат с агента от телефона).")
        token = _prompt("    GENESIS_DISCORD_BOT_TOKEN (Enter = пропусни) = ")
        if token:
            collected["GENESIS_DISCORD_BOT_TOKEN"] = token
            print("    Твоят Discord user ID е ЗАДЪЛЖИТЕЛЕН — без него ботът не")
            print("    отговаря на никого.")
            owner = _prompt("    GENESIS_DISCORD_OWNER_ID = ")
            if owner:
                collected["GENESIS_DISCORD_OWNER_ID"] = owner
            else:
                print("    ⚠️  Без ID ботът ще мълчи. Добави го по-късно в", ENV_FILE)
    print()

    # ── Write ─────────────────────────────────────────────────────────────
    ensure_genesis_home()

    # Anything already in the file that this wizard does not manage is carried
    # over verbatim. Overwriting with only what we collected silently deleted
    # unrelated settings — running setup to add one key must never cost you
    # another.
    preserved: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip().removeprefix("export ").strip()
            if k and k not in collected and v.strip():
                preserved[k] = v.strip()

        # A dated copy before every write, so a mistake here is recoverable.
        backup = ENV_FILE.with_name(f".env.backup-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        backup.chmod(0o600)
        print(f"  предишният файл е запазен като {backup.name}")

    lines = ["# Genesis Agent — written by `genesis setup`.",
             "# Keep this file private. It is never committed.",
             "#",
             "# Format is strict:  NAME=value   (no spaces around =).",
             "# Comments go on their own line, starting with #.",
             ""]
    lines += [f"{k}={v}" for k, v in collected.items()]
    if preserved:
        lines += ["", "# Kept from the previous file (not managed by setup):"]
        lines += [f"{k}={v}" for k, v in preserved.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)

    print(f"  ✅ Записано в {ENV_FILE} (права 600 — само ти можеш да го четеш)")
    print(f"  {working} работещи доставчика.\n")
    print("  Пусни агента:   genesis\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
