#!/usr/bin/env python3
"""
Benchmark for skill search in Bulgarian: which embedding model finds the right skill.

    python scripts/bench_embed.py                          # nomic-embed-text vs bge-m3
    python scripts/bench_embed.py --models bge-m3,nomic-embed-text

Why this exists (NEXT_STEPS.md, item 14): `nomic-embed-text` is mostly English,
and keyword search treats "изчисти" and "изчистя" as different words. The
question is whether `bge-m3` finds the right skill for a Bulgarian request
that keyword search misses, and at what cost in time.

Each query is a paraphrase the way the operator writes: it does NOT repeat the
skill's triggers, so exact word overlap cannot carry it. For every model it
reports hit@1, hit@3, how many right answers clear the 0.55 threshold that
`skill_loader.search_skills` uses, and ms per query. The keyword-only
baseline is printed first.

Needs Ollama on localhost with the models pulled (`ollama pull bge-m3`). It
does NOT touch `embeddings.db`: vectors are computed in memory for each model.
Switching models afterwards: set GENESIS_EMBED_MODEL, then run
`python -c "from genesis_agent.embeddings import reindex_all; reindex_all()"`.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesis_agent import embeddings
from genesis_agent.skill_loader import load_skills_index, search_skills

THRESHOLD = 0.55  # the same cut as skill_loader.search_skills

# (request as the operator would write it, the skill that should come out)
QUERIES: list[tuple[str, str]] = [
    ("искам да пусна линтера и тестовете на това репо", "project_checks_detect_and_run"),
    ("стартирай всички проверки, които проектът си има", "project_checks_detect_and_run"),
    ("pytest изкара 40 грешки, откъде да започна", "first_real_failure_not_the_cascade"),
    ("намери коренната причина сред куп паднали тестове", "first_real_failure_not_the_cascade"),
    ("преди да сменя този ред, виж дали се среща само веднъж във файла", "safe_edit_probe_anchor_uniqueness"),
    ("как да не объркам мястото при замяна на текст във файл", "safe_edit_probe_anchor_uniqueness"),
    ("какво съм променил и кои тестове го покриват", "changed_surface_which_tests_to_run"),
    ("изчисти ми тестовете само до засегнатите от промяната", "changed_surface_which_tests_to_run"),
    ("докажи, че новият тест щеше да падне преди поправката", "regression_proof_against_old_code"),
    ("хваща ли тестът наистина бъга в предишната версия", "regression_proof_against_old_code"),
    ("добавих проверка на едно място, къде другаде липсва", "sibling_paths_missing_the_guard"),
    ("други функции, които четат същия файл без защита", "sibling_paths_missing_the_guard"),
    ("съществува ли изобщо този API адрес, преди да пиша клиент", "probe_endpoint_before_coding_against_it"),
    ("провери какво отговаря сървърът на този url", "probe_endpoint_before_coding_against_it"),
    ("ограничител на заявките с кофа от жетони", "build_a_stdlib_only_in_process_rate_limiter_usin"),
    ("опашка от задачи с повторни опити и нарастващо изчакване", "build_a_stdlib_only_in_process_job_queue_with_re"),
    ("раздели списъка на парчета по n елемента", "implement_a_stdlib_only_function_chunked_seq_n_t"),
    ("изравни вложен списък в плосък", "implement_a_stdlib_only_function_flatten_nested"),
    ("максимумът във всеки плъзгащ се прозорец", "implement_sliding_window_maximum_from_scratch_st"),
    ("подпиши токен като JWT с hmac", "implement_a_stdlib_only_jwt_like_token_encoder_d"),
]


def skill_text(skill: dict) -> str:
    """The same text `embeddings.reindex_all` stores for a skill."""
    return f"{skill['name'].replace('_', ' ')}. {skill.get('description', '')}"


def rank(qvec: list[float], vectors: dict[str, list[float]]) -> list[tuple[str, float]]:
    scored = [(name, embeddings._cosine(qvec, vec)) for name, vec in vectors.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def bench_model(model: str, index: dict[str, dict], queries: list[tuple[str, str]]) -> dict | None:
    vectors: dict[str, list[float]] = {}
    for name, skill in index.items():
        # First call warms the model up (~20 s cold) — hence the long timeout.
        vec = embeddings.embed(skill_text(skill), timeout=180, model=model)
        if not vec:
            return None
        vectors[name] = vec
    hit1 = hit3 = over = 0
    elapsed = 0.0
    misses: list[str] = []
    for query, want in queries:
        t0 = time.perf_counter()
        qvec = embeddings.embed(query, timeout=180, model=model)
        elapsed += time.perf_counter() - t0
        if not qvec:
            return None
        ranked = rank(qvec, vectors)
        top = [name for name, _ in ranked[:3]]
        score = dict(ranked).get(want, 0.0)
        hit1 += top[0] == want
        hit3 += want in top
        over += score >= THRESHOLD
        if top[0] != want:
            misses.append(f"    {query!r}: {top[0]} ({ranked[0][1]:.2f}), right one {score:.2f}")
    return {"hit1": hit1, "hit3": hit3, "over": over,
            "ms": 1000 * elapsed / len(queries), "misses": misses}


def keyword_hits(queries: list[tuple[str, str]]) -> tuple[int, int]:
    hit1 = hit3 = 0
    for query, want in queries:
        top = [s["name"] for s in search_skills(query, top_n=3, use_semantic=False)]
        hit1 += bool(top) and top[0] == want
        hit3 += want in top
    return hit1, hit3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default="nomic-embed-text,bge-m3")
    args = ap.parse_args()

    index = load_skills_index()
    queries = [(q, w) for q, w in QUERIES if w in index]
    if len(queries) < len(QUERIES):
        print(f"{len(QUERIES) - len(queries)} queries skipped: their skill is not installed")
    if not queries:
        print("No skills to search — is the skills index installed?")
        return 1
    n = len(queries)
    print(f"{n} Bulgarian queries, {len(index)} skills\n")

    k1, k3 = keyword_hits(queries)
    print(f"{'keywords only':<20} hit@1 {k1:>2}/{n}  hit@3 {k3:>2}/{n}")

    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        if not embeddings.available(model):
            print(f"{model:<20} not available — ollama pull {model}")
            continue
        r = bench_model(model, index, queries)
        if r is None:
            print(f"{model:<20} embedding call failed")
            continue
        print(f"{model:<20} hit@1 {r['hit1']:>2}/{n}  hit@3 {r['hit3']:>2}/{n}  "
              f">={THRESHOLD} {r['over']:>2}/{n}  {r['ms']:.0f} ms/query")
        for line in r["misses"]:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
