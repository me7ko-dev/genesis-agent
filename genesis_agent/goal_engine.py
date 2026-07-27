#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.goal_engine — двигател на цели за автономното самонадграждане.

Вместо да повтаря една обща подкана, тук целите се извеждат от РЕАЛНИ ЛИПСИ:
  - кои категории умения са слабо покрити спрямо целеви домейни;
  - кои минали цели са се провалили (за повторен опит);
  - кои умения не са verified (кандидати за подобряване).

Целите се дедупликират спрямо съществуващите 2100+ умения и минали цели, и се
ранкират по стойност (празнота × приоритет на домейна).

Публичен интерфейс:
    next_goals(n=10) -> list[str]        # готови цели за маратона
    coverage_report() -> dict            # покритие по домейн (за диагностика)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from genesis_agent.config import PROJECT_ROOT, SKILLS_DIR
from genesis_agent.skills_manager import slugify

SKILLS_JSON = SKILLS_DIR / "skills.json"

# Целеви домейни и желани градивни блокове. Тежестта е приоритет (по-високо = по-важно).
_DOMAINS: dict[str, dict] = {
    "reliability": {
        "weight": 5,
        "keywords": ["retry", "circuit_breaker", "timeout", "backoff", "rate_limit",
                     "health_check", "self_heal", "watchdog", "graceful"],
        "templates": [
            "Implement a production-grade {topic} utility in pure Python with type hints, "
            "docstring, and an inline assert-based self-test that prints OK.",
        ],
        "topics": ["exponential backoff retry", "circuit breaker", "token-bucket rate limiter",
                   "deadline/timeout wrapper", "bulkhead isolation", "health-check aggregator",
                   "jittered retry with max-delay cap", "sliding-window rate limiter",
                   "leaky-bucket rate limiter", "idempotency-key deduplication guard",
                   "graceful shutdown coordinator (SIGTERM drain)", "dead-letter queue handler",
                   "fallback-chain resolver (try N strategies in order)",
                   "heartbeat/liveness monitor with timeout",
                   "exponential backoff with decorrelated jitter",
                   "resource pool with health-based eviction"],
    },
    "data": {
        "weight": 4,
        "keywords": ["csv", "json", "parse", "clean", "dedupe", "transform", "validate",
                     "schema", "flatten", "diff"],
        "templates": [
            "Build a self-contained (standard-library-only) {topic} module with a docstring, "
            "type hints and an assert-based self-test that prints OK.",
        ],
        "topics": ["CSV schema validator", "JSON diff tool", "streaming line deduplicator",
                   "nested-dict flattener/unflattener", "fixed-width record parser",
                   "data-quality report over a list of dicts",
                   "JSON Lines (JSONL) streaming reader/writer",
                   "CSV-to-dict-of-lists columnar converter",
                   "schema migration diff generator for dict records",
                   "outlier detector for a list of numeric records",
                   "record batching/chunking iterator",
                   "duplicate-key merge resolver for nested dicts",
                   "type-coercing CSV loader (auto-detect int/float/bool)",
                   "sparse-to-dense matrix converter from dict records",
                   "time-series gap filler (forward-fill/interpolate)",
                   "pivot table generator from a list of dicts"],
    },
    "text": {
        "weight": 3,
        "keywords": ["regex", "token", "nlp", "extract", "entity", "slug", "template",
                     "markdown", "levenshtein", "fuzzy"],
        "templates": [
            "Create a stdlib-only {topic} with type hints, docstring, and an assert self-test "
            "printing OK.",
        ],
        "topics": ["regex entity extractor (dates/amounts/emails)", "Levenshtein distance function",
                   "simple template engine", "markdown table generator", "text tokenizer",
                   "slug generator with collision suffixing",
                   "camelCase/snake_case/kebab-case converter",
                   "word-wrap/text-justify formatter",
                   "word-level diff highlighter between two strings",
                   "n-gram frequency analyzer", "simple Markov-chain text generator",
                   "ANSI color code stripper/formatter for terminal text",
                   "URL query-string parser/builder",
                   "text similarity ranker (Jaccard/cosine on tokens)",
                   "citation/reference string formatter"],
    },
    "algorithms": {
        "weight": 3,
        "keywords": ["sort", "search", "graph", "tree", "cache", "lru", "heap", "trie",
                     "dijkstra", "dynamic_programming"],
        "templates": [
            "Implement {topic} from scratch (stdlib only) with type hints, docstring and an "
            "assert-based self-test that prints OK.",
        ],
        "topics": ["an LRU cache", "a trie with prefix search", "Dijkstra shortest path",
                   "a min-heap priority queue", "binary search variants", "topological sort",
                   "union-find (disjoint set) with path compression",
                   "A* pathfinding on a grid", "sliding-window maximum",
                   "a consistent-hashing ring", "a bloom filter", "a skip list",
                   "interval merging and scheduling", "reservoir sampling",
                   "a quad-tree spatial index", "a simplified balanced BST (AVL rotations)"],
    },
    "concurrency": {
        "weight": 2,
        "keywords": ["async", "queue", "worker", "pool", "thread", "lock", "semaphore",
                     "pipeline", "producer", "consumer"],
        "templates": [
            "Build a stdlib-only {topic} with type hints, docstring and an assert self-test "
            "printing OK.",
        ],
        "topics": ["asyncio producer-consumer queue", "thread pool with bounded workers",
                   "a simple actor mailbox", "a debounce/throttle decorator",
                   "async rate-limited request batcher", "async retry-with-backoff wrapper",
                   "thread-safe singleton/lazy-init decorator",
                   "async semaphore-bounded fan-out/fan-in",
                   "an in-process event bus / pub-sub dispatcher",
                   "async timeout-cancellation wrapper",
                   "a work-stealing task queue",
                   "an async context-manager resource lock"],
    },
    "security": {
        "weight": 3,
        "keywords": ["hash", "encrypt", "sanitize", "csrf", "sign", "token", "escape",
                     "password", "sql_injection"],
        "templates": [
            "Implement a stdlib-only {topic} with type hints, docstring, and an assert-based "
            "self-test that prints OK.",
        ],
        "topics": ["constant-time string comparison utility",
                   "HMAC-based token signer/verifier",
                   "input sanitizer against path traversal",
                   "simple password strength checker",
                   "secure random token generator",
                   "SQL-injection-safe query parameter builder",
                   "JWT-like token encoder/decoder using hmac (HS256)",
                   "rate-limited login-attempt guard"],
    },
    "cli": {
        "weight": 2,
        "keywords": ["argparse", "cli", "subcommand", "dotenv", "progress_bar", "logging_setup"],
        "templates": [
            "Build a stdlib-only {topic} with type hints, docstring, and an assert-based "
            "self-test that prints OK.",
        ],
        "topics": ["argparse-based CLI with subcommands",
                   "layered config loader (env over file over defaults)",
                   "structured JSON logger setup",
                   "terminal progress-bar utility",
                   "colored terminal output helper",
                   "CLI table formatter",
                   "dotenv file parser"],
    },
}


def _load_skill_names() -> list[str]:
    if not SKILLS_JSON.exists():
        return []
    try:
        data = json.loads(SKILLS_JSON.read_text(encoding="utf-8"))
        return [s.get("name", "") for s in data.get("skills", [])]
    except Exception:
        return []


def _load_past_goals() -> set[str]:
    """Слугове на минали цели (от next_goals.json), за да не ги повтаряме."""
    seen: set[str] = set()
    ng = PROJECT_ROOT / "next_goals.json"
    if ng.exists():
        try:
            for g in json.loads(ng.read_text(encoding="utf-8")):
                if isinstance(g, str):
                    seen.add(slugify(g))
        except Exception:
            pass
    return seen


def coverage_report() -> dict[str, int]:
    """Брой умения на домейн (по ключови думи в имената)."""
    names = _load_skill_names()
    report: dict[str, int] = {}
    for domain, cfg in _DOMAINS.items():
        pat = re.compile("|".join(cfg["keywords"]))
        report[domain] = sum(1 for n in names if pat.search(n))
    return report


def next_goals(n: int = 10, *, use_semantic: bool = True) -> list[str]:
    """
    Връща до n нови, дедупликирани цели, ранкирани по стойност:
    празнотата на домейна × неговия приоритет. Никога не връща цел, чийто slug
    вече съществува като умение или минала цел.

    use_semantic=True добавя СЕМАНТИЧНА проверка (genesis_agent.embeddings) — цел,
    която е ПЕРИФРАЗА на съществуващо умение (различен slug, същия смисъл),
    също се прескача. Без това, "reverse a string" и "invert character order in
    text" биха минали като различни задачи и biха създали дублиращо умение.
    """
    existing = set(_load_skill_names()) | _load_past_goals()
    coverage = coverage_report()

    semantic_ok = False
    if use_semantic:
        try:
            from genesis_agent.embeddings import available
            semantic_ok = available()
        except Exception:
            semantic_ok = False

    # Стойност на домейн = weight / (1 + покритие) → слабо покритите се вдигат.
    scored_domains = sorted(
        _DOMAINS.items(),
        key=lambda kv: kv[1]["weight"] / (1 + coverage.get(kv[0], 0)),
        reverse=True,
    )

    goals: list[str] = []
    seen_slugs: set[str] = set()
    # Обхождаме домейните по стойност, взимаме по една тема наведнъж (round-robin),
    # докато напълним n.
    topic_pools = {d: list(cfg["topics"]) for d, cfg in _DOMAINS.items()}
    progress = True
    while len(goals) < n and progress:
        progress = False
        for domain, cfg in scored_domains:
            pool = topic_pools.get(domain, [])
            if not pool:
                continue
            topic = pool.pop(0)
            progress = True
            goal = cfg["templates"][0].format(topic=topic)
            slug = slugify(goal)
            if slug in existing or slug in seen_slugs:
                continue
            if semantic_ok:
                try:
                    from genesis_agent.embeddings import semantic_duplicate
                    dup = semantic_duplicate(goal, threshold=0.90)
                    if dup:
                        continue  # перифраза на съществуващо умение — прескачаме
                except Exception:
                    pass
            seen_slugs.add(slug)
            goals.append(goal)
            if len(goals) >= n:
                break
    return goals


if __name__ == "__main__":
    print("=== Покритие по домейн ===")
    for d, c in sorted(coverage_report().items(), key=lambda x: x[1]):
        print(f"  {d:14}: {c} умения")
    print("\n=== Следващи 10 цели (gap-driven, дедупликирани) ===")
    for i, g in enumerate(next_goals(10), 1):
        print(f"  {i:2}. {g}")
