#!/usr/bin/env python3
"""
Does a provider cache the prompt prefix, and which history shape gets more of it?

    python scripts/cache_replay.py nvidia=nvidia/nemotron-3-super-120b-a12b
    python scripts/cache_replay.py --rounds 8 groq=openai/gpt-oss-120b ollama_cloud=gpt-oss:120b-cloud

Why this exists (2026-10-11): the free providers cache by prefix on their side
with no markers, and each one differently — Ollama Cloud reads 70-90% of the
input from cache, NVIDIA nemotron-3-super does once the history stops changing,
free Groq does not at all. `config.CACHE_PROVIDERS` decides who gets the
append-only history (`budget.cache_history`) instead of the shrinking one
(`budget.budget_history`), and a guess there costs either cache hits or plain
extra tokens. This replays the same growing agent history (a tool call and a
~3 200-character result per round) both ways against one model and prints what
came from cache — the number that decides the list.

Every round is a real request (max 24 output tokens): it spends free quota.
Each shape gets its own random session tag in the system prompt, so the second
run cannot read the first one's cache.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesis_agent.brain import (
    _PROVIDERS,
    _load_keys,
    _openai_usage,
    provider_base_url,
)
from genesis_agent.budget import budget_history, cache_history
from genesis_agent.config import CACHE_HISTORY_MAX_CHARS_FREE

TOOLS = [{"type": "function", "function": {
    "name": "RUN_CMD", "description": "Пуска команда в терминала.",
    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}}]


def _code(seed: int, size: int = 3200) -> str:
    """Различен за всяко семе код — резултат на инструмент, който не се повтаря."""
    rnd = random.Random(seed)
    out: list[str] = []
    while sum(map(len, out)) < size:
        name = "".join(rnd.choice("abcdefghijklmnop") for _ in range(8))
        out.append(f"def {name}(x, y={rnd.randint(1, 99)}):\n    if x > {rnd.randint(1, 500)}:\n"
                   f"        return x * y - {rnd.randint(1, 50)}\n    return '{name}' + str(x)\n")
    return "".join(out)


def _history(tag: str, rounds: int) -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": f"Ти си Genesis, агент за код. Сесия {tag}.\n" + _code(len(tag), 9000)},
        {"role": "user", "content": "Направи модул egn.py с функция за проверка на ЕГН и тестове."}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{i}", "type": "function",
            "function": {"name": "RUN_CMD", "arguments": json.dumps({"cmd": f"cat part{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": _code(i * 31 + 7)})
    return msgs


def _send(provider: str, model: str, key: str, messages: list[dict]) -> dict:
    body = {"model": model, "max_tokens": 24, "messages": messages, "tools": TOOLS}
    req = urllib.request.Request(
        provider_base_url(provider).rstrip("/") + "/chat/completions", method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "genesis-cache-replay"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return _openai_usage(json.loads(r.read()).get("usage") or {}) or {}


def replay(provider: str, model: str, shape: str, rounds: int) -> dict:
    key = _load_keys().get(_PROVIDERS[provider][1] or "", "")
    tag = f"{shape}-{random.randint(10**6, 10**7)}"
    sent = cached = ok = 0
    for n in range(1, rounds + 1):
        full = _history(tag, n)
        view = (cache_history(full, max_chars=CACHE_HISTORY_MAX_CHARS_FREE) if shape == "append"
                else budget_history(full))
        for _ in range(3):
            try:
                usage = _send(provider, model, key, view)
                break
            except urllib.error.HTTPError as e:
                print(f"   рунд {n}: HTTP {e.code}", flush=True)
                time.sleep(15)
        else:
            continue
        sent += int(usage.get("prompt_tokens") or 0)
        cached += int(usage.get("cached_read_tokens") or 0)
        ok += 1
        time.sleep(2)
    return {"requests": ok, "sent": sent, "cached": cached}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("models", nargs="+", metavar="доставчик=модел")
    ap.add_argument("--rounds", type=int, default=14)
    args = ap.parse_args(argv)
    for spec in args.models:
        provider, _, model = spec.partition("=")
        if provider not in _PROVIDERS or not model:
            print(f"{spec}: очаквам доставчик=модел, доставчик от {sorted(_PROVIDERS)}")
            return 2
        for shape in ("shrink", "append"):
            r = replay(provider, model, shape, args.rounds)
            fresh = r["sent"] - r["cached"]
            print(f"{provider} {model} {shape:6}: заявки {r['requests']}, вход {r['sent']}, "
                  f"от кеша {r['cached']} ({100 * r['cached'] / max(r['sent'], 1):.0f}%), "
                  f"некеширани {fresh} ({fresh // max(r['requests'], 1)} на заявка)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
