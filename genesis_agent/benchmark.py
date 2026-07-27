#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.benchmark — самооценка на агента (L2-4).

Пуска агента срещу фиксиран набор задачи с known-good проверки, отчита % успех
и записва резултата във времето (benchmark_history.json), за да се вижда
прогрес/регресия между версиите на мозъка.

Употреба:
    python3 -m genesis_agent.benchmark                 # пълен бенчмарк
    python3 -m genesis_agent.benchmark --quick          # само първите 3 задачи
    python3 -m genesis_agent.benchmark --orchestrated   # през мулти-агента
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from genesis_agent.brain import Brain
from genesis_agent.executor import run_python_subprocess
from genesis_agent.config import PROJECT_ROOT

HISTORY = PROJECT_ROOT / "benchmark_history.json"

# Всяка задача: (goal, check_code). check_code се долепя след генерирания код и
# трябва да мине (assert-и) → доказва, че решението наистина работи.
TASKS: list[tuple[str, str]] = [
    # ── ЛЕСНИ ──
    ("Implement a function is_prime(n) that returns True if n is prime.",
     "assert is_prime(7) and is_prime(2) and not is_prime(1) and not is_prime(9)\nprint('CHECK_OK')"),
    ("Implement factorial(n) recursively.",
     "assert factorial(0)==1 and factorial(5)==120\nprint('CHECK_OK')"),
    ("Implement reverse_string(s) returning the reversed string.",
     "assert reverse_string('abc')=='cba' and reverse_string('')==''\nprint('CHECK_OK')"),
    ("Implement fibonacci(n) returning the nth Fibonacci number (0-indexed, fib(0)=0, fib(1)=1).",
     "assert fibonacci(0)==0 and fibonacci(1)==1 and fibonacci(10)==55\nprint('CHECK_OK')"),
    ("Implement count_vowels(s) returning the number of vowels (aeiou) in s.",
     "assert count_vowels('hello')==2 and count_vowels('xyz')==0\nprint('CHECK_OK')"),
    ("Implement is_palindrome(s) returning True if s reads the same backwards.",
     "assert is_palindrome('racecar') and not is_palindrome('abc')\nprint('CHECK_OK')"),
    # ── СРЕДНИ ──
    ("Implement fizzbuzz(n) returning a list of strings for 1..n: 'Fizz' if divisible by 3, "
     "'Buzz' if by 5, 'FizzBuzz' if by both, else the number as a string.",
     "assert fizzbuzz(5)==['1','2','Fizz','4','Buzz'] and fizzbuzz(15)[-1]=='FizzBuzz'\nprint('CHECK_OK')"),
    ("Implement gcd(a,b) computing the greatest common divisor.",
     "assert gcd(12,8)==4 and gcd(17,5)==1 and gcd(100,10)==10\nprint('CHECK_OK')"),
    ("Implement bubble_sort(lst) returning a new sorted list (ascending).",
     "assert bubble_sort([3,1,2])==[1,2,3] and bubble_sort([])==[]\nprint('CHECK_OK')"),
    ("Implement binary_search(lst, target) on a sorted list, returning the index or -1 if absent.",
     "assert binary_search([1,2,3,4,5],4)==3 and binary_search([1,2,3],9)==-1\nprint('CHECK_OK')"),
    ("Implement is_anagram(a,b) returning True if a and b are anagrams (ignore case).",
     "assert is_anagram('Listen','Silent') and not is_anagram('abc','abd')\nprint('CHECK_OK')"),
    ("Implement flatten(lst) that flattens an arbitrarily nested list into a flat list.",
     "assert flatten([1,[2,[3,4]],5])==[1,2,3,4,5] and flatten([])==[]\nprint('CHECK_OK')"),
    # ── ПО-ТРУДНИ ──
    ("Implement roman_to_int(s) converting a Roman numeral string to an integer.",
     "assert roman_to_int('IX')==9 and roman_to_int('LVIII')==58 and roman_to_int('MCMXCIV')==1994\nprint('CHECK_OK')"),
    ("Implement merge_sorted(a,b) merging two sorted lists into one sorted list.",
     "assert merge_sorted([1,3,5],[2,4,6])==[1,2,3,4,5,6] and merge_sorted([],[1])==[1]\nprint('CHECK_OK')"),
    ("Implement balanced_parens(s) returning True if all (), [], {} brackets are balanced.",
     "assert balanced_parens('([]{})') and not balanced_parens('(]') and balanced_parens('')\nprint('CHECK_OK')"),
]

_SYS = (
    "Ти си Genesis coder. Върни САМО един ```python``` блок с исканата функция. "
    "Без обяснения, без тест — само дефиницията на функцията."
)


def _run_task(brain: Brain, goal: str, check: str, orchestrated: bool) -> tuple[bool, str]:
    if orchestrated:
        from genesis_agent.orchestrator import run_orchestrated
        # оркестраторът пази умение; тук само меряме успех
        out = run_orchestrated(goal + " Include a self-test that prints OK.")
        return out.success, f"rounds={out.rounds}"
    reply = brain.complete([
        {"role": "system", "content": _SYS},
        {"role": "user", "content": goal},
    ])
    code = reply.code
    if not code:
        return False, "no code"
    result = run_python_subprocess(code + "\n\n" + check)
    ok = result.ok and "CHECK_OK" in result.stdout
    return ok, (result.stderr[:80] if not ok else "ok")


def run_benchmark(quick: bool = False, orchestrated: bool = False) -> dict:
    tasks = TASKS[:3] if quick else TASKS
    brain = Brain()
    model = (brain.local or (brain.chain[0] if brain.chain else {})).get("model", "?")
    results = []
    passed = 0
    t0 = time.time()
    print(f"=== GENESIS BENCHMARK ({len(tasks)} задачи, мозък: {model}) ===")
    for i, (goal, check) in enumerate(tasks, 1):
        ok, note = _run_task(brain, goal, check, orchestrated)
        passed += ok
        print(f"  {'✅' if ok else '❌'} [{i}/{len(tasks)}] {goal[:50]:50} {note}")
        results.append({"goal": goal, "passed": ok, "note": note})

    pct = round(100 * passed / len(tasks))
    elapsed = round(time.time() - t0)
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "orchestrated": orchestrated,
        "tasks": len(tasks),
        "passed": passed,
        "score_pct": pct,
        "elapsed_sec": elapsed,
    }
    print(f"\n  РЕЗУЛТАТ: {passed}/{len(tasks)} = {pct}%  ({elapsed}s)")

    # Записваме в историята за проследяване във времето.
    hist = {"runs": []}
    if HISTORY.exists():
        try:
            hist = json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            pass
    hist["runs"].append(summary)
    HISTORY.write_text(json.dumps(hist, indent=2, ensure_ascii=False), encoding="utf-8")

    prev = [r for r in hist["runs"][:-1]]
    if prev:
        last = prev[-1]["score_pct"]
        trend = "📈 нагоре" if pct > last else ("📉 надолу" if pct < last else "➡️ същото")
        print(f"  Спрямо предишен ({last}%): {trend}")
    return summary


if __name__ == "__main__":
    run_benchmark(quick="--quick" in sys.argv, orchestrated="--orchestrated" in sys.argv)
