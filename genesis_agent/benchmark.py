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
from typing import Any
from pathlib import Path

from genesis_agent.brain import Brain
from genesis_agent.executor import run_python_subprocess
from genesis_agent.config import DATA_DIR

HISTORY = DATA_DIR / "benchmark_history.json"

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
    # ── HARD (external audit, 2026-07-28) — edge cases, security, concurrency ──
    ("Implement safe_divide(a, b) that returns a/b, but raises a ValueError with message 'division by zero' if b is 0. Must work for both int and float inputs.",
     "try:\n    safe_divide(1, 0)\n    raise AssertionError('should have raised')\nexcept ValueError as e:\n    assert 'division by zero' in str(e)\nassert safe_divide(10, 2) == 5\nassert abs(safe_divide(1, 3) - 0.3333333333333333) < 1e-9\nprint('CHECK_OK')"),
    ('Implement merge_intervals(intervals) taking a list of [start, end] pairs (may be unsorted, may overlap) and returning a new list of merged, non-overlapping intervals sorted by start.',
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\nassert merge_intervals([]) == []\nassert merge_intervals([[1,4],[4,5]]) == [[1,5]]\nassert merge_intervals([[5,6],[1,2]]) == [[1,2],[5,6]]\nprint('CHECK_OK')"),
    ("Implement deep_merge(a, b) that recursively merges dict b into dict a and returns a NEW dict (does not mutate a or b). If both values at a key are dicts, merge recursively; otherwise b's value wins. Non-dict nested structures (lists etc.) are simply overwritten by b's value.",
     "a = {'x': 1, 'y': {'p': 1, 'q': 2}, 'z': [1,2]}\nb = {'y': {'q': 99, 'r': 3}, 'z': [9], 'w': 5}\nout = deep_merge(a, b)\nassert out == {'x': 1, 'y': {'p': 1, 'q': 99, 'r': 3}, 'z': [9], 'w': 5}\nassert a == {'x': 1, 'y': {'p': 1, 'q': 2}, 'z': [1,2]}\nassert b == {'y': {'q': 99, 'r': 3}, 'z': [9], 'w': 5}\nprint('CHECK_OK')"),
    ("Implement is_valid_ipv4(s) returning True only if s is a syntactically valid IPv4 address: exactly 4 dot-separated decimal octets, each 0-255, no leading zeros (except '0' itself), no extra whitespace.",
     "assert is_valid_ipv4('192.168.1.1')\nassert is_valid_ipv4('0.0.0.0')\nassert is_valid_ipv4('255.255.255.255')\nassert not is_valid_ipv4('256.1.1.1')\nassert not is_valid_ipv4('192.168.1')\nassert not is_valid_ipv4('192.168.01.1')\nassert not is_valid_ipv4('192.168.1.1.1')\nassert not is_valid_ipv4('192.168.1.-1')\nassert not is_valid_ipv4('a.b.c.d')\nassert not is_valid_ipv4(' 192.168.1.1')\nprint('CHECK_OK')"),
    ('Implement parse_csv_line(line) that parses a single CSV line into a list of fields, handling double-quoted fields that may contain commas and escaped double-quotes written as "" inside a quoted field.',
     'assert parse_csv_line(\'a,b,c\') == ["a","b","c"]\nassert parse_csv_line(\'"hello, world",b,c\') == ["hello, world","b","c"]\nassert parse_csv_line(\'a,"she said ""hi""",c\') == ["a",\'she said "hi"\',"c"]\nassert parse_csv_line(\'\') == [\'\']\nprint(\'CHECK_OK\')'),
    ('Implement eval_expr(expr) that evaluates a simple arithmetic expression string containing non-negative integers, +, -, *, /, and parentheses, respecting standard operator precedence, WITHOUT using eval() or exec(). Division is integer division. Return an int.',
     "assert eval_expr('2+3*4') == 14\nassert eval_expr('(2+3)*4') == 20\nassert eval_expr('10-2-3') == 5\nassert eval_expr('2*(3+(4-1))') == 12\nassert eval_expr('100/5/2') == 10\nprint('CHECK_OK')"),
    ("Implement run_length_encode(s) and run_length_decode(s) that are exact inverses of each other. Encoding format: each run is written as <count><char>, e.g. 'aaab' -> '3a1b'.",
     "assert run_length_encode('aaabbbccd') == '3a3b2c1d'\nassert run_length_decode('3a3b2c1d') == 'aaabbbccd'\nassert run_length_decode(run_length_encode('')) == ''\nassert run_length_decode(run_length_encode('xyz')) == 'xyz'\nprint('CHECK_OK')"),
    ('Implement a class LRUCache(capacity) with get(key) -> value or -1 if absent, and put(key, value). Both operations must be O(1) average time. When capacity is exceeded, evict the least recently used entry. Getting or updating a key counts as using it.',
     "c = LRUCache(2)\nc.put(1, 1); c.put(2, 2)\nassert c.get(1) == 1\nc.put(3, 3)  # evicts key 2 (least recently used)\nassert c.get(2) == -1\nc.put(4, 4)  # evicts key 1\nassert c.get(1) == -1\nassert c.get(3) == 3\nassert c.get(4) == 4\nprint('CHECK_OK')"),
    ('Implement a class Trie with insert(word) and autocomplete(prefix) -> sorted list of all inserted words starting with prefix (empty list if none match).',
     "t = Trie()\nfor w in ['cat', 'car', 'card', 'dog', 'do']:\n    t.insert(w)\nassert t.autocomplete('ca') == ['car', 'card', 'cat']\nassert t.autocomplete('do') == ['do', 'dog']\nassert t.autocomplete('z') == []\nassert t.autocomplete('') == ['car', 'card', 'cat', 'do', 'dog']\nprint('CHECK_OK')"),
    ("Implement topo_sort(graph) where graph is a dict[node] -> list[neighbor] (a directed graph). Return a valid topological order as a list. If the graph contains a cycle, raise ValueError('cycle detected').",
     "g = {'a': ['b', 'c'], 'b': ['d'], 'c': ['d'], 'd': []}\norder = topo_sort(g)\npos = {n: i for i, n in enumerate(order)}\nassert pos['a'] < pos['b'] < pos['d']\nassert pos['a'] < pos['c'] < pos['d']\nassert set(order) == set(g)\ntry:\n    topo_sort({'x': ['y'], 'y': ['x']})\n    raise AssertionError('should have raised')\nexcept ValueError:\n    pass\nprint('CHECK_OK')"),
    ('Implement sliding_window_max(nums, k) returning a list of the maximum value in every contiguous window of size k, in O(n) time overall (not O(n*k)).',
     "assert sliding_window_max([1,3,-1,-3,5,3,6,7], 3) == [3,3,5,5,6,7]\nassert sliding_window_max([1], 1) == [1]\nassert sliding_window_max([9,8,7,6], 2) == [9,8,7]\nprint('CHECK_OK')"),
    ('Implement constant_time_compare(a, b) comparing two strings (or bytes) for equality WITHOUT early-exit on the first mismatch (constant-time with respect to where the first difference occurs), to avoid timing side-channel attacks when comparing secrets like tokens.',
     "assert constant_time_compare('secret123', 'secret123') is True\nassert constant_time_compare('secret123', 'secret124') is False\nassert constant_time_compare('', '') is True\nassert constant_time_compare('a', 'ab') is False\nprint('CHECK_OK')"),
    ("Implement safe_join(base_dir, user_path) that joins user_path onto base_dir and returns the resulting absolute path as a string, but raises ValueError('path traversal') if the resolved path would escape base_dir (e.g. via '../' segments or an absolute user_path).",
     "import os\nbase = '/tmp/sandbox_root'\nos.makedirs(base, exist_ok=True)\np = safe_join(base, 'a/b.txt')\nassert p.startswith(os.path.realpath(base))\nfor bad in ['../etc/passwd', '../../x', '/etc/passwd', 'a/../../etc']:\n    try:\n        safe_join(base, bad)\n        raise AssertionError(f'should have raised for {bad!r}')\n    except ValueError:\n        pass\nprint('CHECK_OK')"),
    ("Implement sign_token(payload: str, secret: str) -> str returning a token of the form '<payload>.<hex_hmac_sha256>', and verify_token(token: str, secret: str) -> bool that verifies it (using a constant-time comparison for the HMAC), using Python's stdlib hmac module.",
     "tok = sign_token('user=42', 'mysecret')\nassert verify_token(tok, 'mysecret') is True\nassert verify_token(tok, 'wrongsecret') is False\nassert verify_token(tok[:-1] + ('0' if tok[-1] != '0' else '1'), 'mysecret') is False\nassert verify_token('garbage', 'mysecret') is False\nprint('CHECK_OK')"),
    ('Implement a class ThreadSafeCounter with increment() and value property, safe to call increment() concurrently from multiple threads without losing updates (use a lock).',
     "import threading\nc = ThreadSafeCounter()\ndef worker():\n    for _ in range(2000):\n        c.increment()\nthreads = [threading.Thread(target=worker) for _ in range(8)]\n[t.start() for t in threads]\n[t.join() for t in threads]\nassert c.value == 16000, c.value\nprint('CHECK_OK')"),
    ("Implement a decorator retry(max_attempts=3, base_delay=0) that retries a function call if it raises an exception, up to max_attempts total attempts, sleeping base_delay * (2 ** attempt_index) between attempts (use time.sleep). If all attempts fail, re-raise the last exception. The decorated function's __name__ must be preserved (use functools.wraps).",
     "calls = {'n': 0}\n@retry(max_attempts=3, base_delay=0)\ndef flaky():\n    calls['n'] += 1\n    if calls['n'] < 3:\n        raise RuntimeError('fail')\n    return 'ok'\nassert flaky() == 'ok'\nassert calls['n'] == 3\nassert flaky.__name__ == 'flaky'\n\n@retry(max_attempts=2, base_delay=0)\ndef always_fails():\n    raise ValueError('nope')\ntry:\n    always_fails()\n    raise AssertionError('should have raised')\nexcept ValueError:\n    pass\nprint('CHECK_OK')"),
    ('Implement levenshtein(a, b) computing the edit distance (insertions, deletions, substitutions) between two strings.',
     "assert levenshtein('kitten', 'sitting') == 3\nassert levenshtein('', 'abc') == 3\nassert levenshtein('abc', 'abc') == 0\nassert levenshtein('flaw', 'lawn') == 2\nprint('CHECK_OK')"),
    ("Implement coin_change(coins, amount) returning the minimum number of coins from the given denominations (unlimited supply of each) that sum to amount, or -1 if it's not possible. amount can be 0 (answer 0).",
     "assert coin_change([1,2,5], 11) == 3\nassert coin_change([2], 3) == -1\nassert coin_change([1], 0) == 0\nassert coin_change([1,3,4], 6) == 2\nprint('CHECK_OK')"),
    ('Implement median_of_two_sorted(a, b) returning the median (float) of the combined values of two already-sorted lists a and b, without fully concatenating-and-sorting from scratch conceptually being required (a straightforward correct approach is fine, efficiency not graded).',
     "assert median_of_two_sorted([1,3], [2]) == 2.0\nassert median_of_two_sorted([1,2], [3,4]) == 2.5\nassert median_of_two_sorted([], [1]) == 1.0\nassert median_of_two_sorted([0,0], [0,0]) == 0.0\nprint('CHECK_OK')"),
    ('Implement word_ladder_len(begin, end, word_list) returning the length of the shortest transformation sequence from begin to end, changing one letter at a time, where every intermediate word must be in word_list (a list/set of same-length words). Return 0 if no such sequence exists. The length counts the number of words in the sequence, including begin and end.',
     "wl = ['hot','dot','dog','lot','log','cog']\nassert word_ladder_len('hit', 'cog', wl) == 5\nassert word_ladder_len('hot', 'dog', wl) == 3\nassert word_ladder_len('hot', 'cog', wl) == 4\nassert word_ladder_len('hit', 'zzz', wl) == 0\nprint('CHECK_OK')"),
    ('Define a custom exception hierarchy: base class GenesisError(Exception), and two subclasses ValidationError(GenesisError) and NotFoundError(GenesisError). Implement get_user(users: dict, user_id) that raises ValidationError if user_id is not a positive int, raises NotFoundError if user_id is not a key in users, otherwise returns users[user_id].',
     "users = {1: 'alice', 2: 'bob'}\nassert get_user(users, 1) == 'alice'\ntry:\n    get_user(users, -1)\n    raise AssertionError('expected ValidationError')\nexcept ValidationError:\n    pass\ntry:\n    get_user(users, 99)\n    raise AssertionError('expected NotFoundError')\nexcept NotFoundError:\n    pass\nassert issubclass(ValidationError, GenesisError)\nassert issubclass(NotFoundError, GenesisError)\nprint('CHECK_OK')"),
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
    hist: dict[str, list[Any]] = {"runs": []}
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
