#!/usr/bin/env python3
"""
Benchmark for the skill-search embedding model on the operator's own phrasing.

    python scripts/bench_embed.py                          # nomic-embed-text vs bge-m3
    python scripts/bench_embed.py --models bge-m3

Why (NEXT_STEPS.md item 14): `nomic-embed-text` is mostly English, while the
operator writes Bulgarian, often transliterated. Each installed skill gets two
queries phrased the way the operator would ask for it, and some queries match
no skill at all. Every model indexes the same text `reindex_all()` uses
(name + description) into its own throwaway index; the real embeddings.db is
not touched. Reported per model:
  • top-1 / top-3: the right skill is first / among the first three
  • над прага: the right skill clears the semantic threshold (--threshold)
  • фалшиви: a no-skill query still gets something over that threshold
Needs a local ollama with the models pulled.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import requests

URL = "http://localhost:11434/api/embeddings"
THRESHOLD = 0.55  # --threshold; the one search_skills uses is embeddings.SEARCH_THRESHOLD

# skill -> queries (Cyrillic and transliterated, the way the operator writes)
QUERIES: dict[str, list[str]] = {
    "build_a_self_contained_standard_library_only_sch": [
        "генератор на разлики между схеми за миграция на записи",
        "sravni dve shemi i napravi migraciq",
    ],
    "build_a_stdlib_only_an_in_process_event_bus_pub": [
        "шина за събития с абонати и публикуване",
        "pub-sub dispecher za subitiq v programata",
    ],
    "build_a_stdlib_only_async_semaphore_bounded_fan": [
        "пусни много асинхронни задачи наведнъж, но не повече от 5 едновременно",
        "async s ogranichenie na paralelnite zadachi",
    ],
    "build_a_stdlib_only_async_timeout_cancellation_w": [
        "прекъсни асинхронна функция, ако се бави повече от 3 секунди",
        "timeout za async funkciq i otkaz",
    ],
    "create_a_stdlib_only_n_gram_frequency_analyzer_w": [
        "преброй колко често се срещат двойки думи в текст",
        "chestota na n-grami v teksta",
    ],
    "create_a_stdlib_only_word_level_diff_highlighter": [
        "покажи кои думи са различни между два текста",
        "razlikite mejdu dva teksta po dumi",
    ],
    "create_a_stdlib_only_word_wrap_text_justify_form": [
        "пренасяне на редове и подравняване на текста по ширина",
        "podravni teksta na 80 simvola",
    ],
    "implement_a_consistent_hashing_ring_from_scratch": [
        "разпредели ключове между сървъри с консистентно хеширане",
        "heshirasht prasten za razpredelqne na klyuchove",
    ],
    "implement_a_stdlib_only_jwt_like_token_encoder_d": [
        "подписан токен за вход като JWT",
        "napravi token s podpis hmac i go proveri",
    ],
    "implement_sliding_window_maximum_from_scratch_st": [
        "максимумът във всеки прозорец от k поредни числа",
        "maksimum v plazgashto prozorche",
    ],
    "implement_a_stdlib_only_function_chunked_seq_n_t": [
        "раздели списъка на парчета по 10 елемента",
        "razdeli spisaka na chasti po n",
    ],
    "implement_a_stdlib_only_function_flatten_nested": [
        "направи вложен списък на плосък",
        "izpravi vlojeni spisaci v edin",
    ],
    "build_a_stdlib_only_in_process_job_queue_with_re": [
        "опашка за задачи, която опитва пак при грешка с нарастващо чакане",
        "opashka ot zadachi s povtoren opit",
    ],
    "build_a_stdlib_only_in_process_rate_limiter_usin": [
        "ограничи заявките до 10 в секунда",
        "limit na zaqvkite token bucket",
    ],
    "project_checks_detect_and_run": [
        "пусни тестовете и линтера на проекта",
        "proveri dali proekta minava testovete",
    ],
    "first_real_failure_not_the_cascade": [
        "кое е първото истинско падане в изхода на pytest",
        "ot kade zapochva greshkata v testovete",
    ],
    "safe_edit_probe_anchor_uniqueness": [
        "провери дали мястото за редакция в файла е еднозначно",
        "redaktiraj faila bez da schupish drugo mqsto",
    ],
    "changed_surface_which_tests_to_run": [
        "кои тестове покриват файловете, които промених",
        "koi testove da pusna sled promenite",
    ],
    "regression_proof_against_old_code": [
        "докажи, че новият тест пада на стария код",
        "hvashta li testa bug-a na starata versiq",
    ],
    "sibling_paths_missing_the_guard": [
        "има ли други места, които стигат до ресурса без тази проверка",
        "drugite patishta bez sashtata zashtita",
    ],
    "probe_endpoint_before_coding_against_it": [
        "провери дали адресът на API-то съществува, преди да пишеш код",
        "udari endpoint-a i vij kakvo vrashta",
    ],
}
NEGATIVE = [
    "какво е времето утре в София",
    "napishi mi pesen za more",
    "колко е часът",
    "prevedi tova na angliiski",
    "направи ми CSV с числата от 1 до 10",
    "kakvo e stolicata na Franciq",
    "изчисти разговора",
    "напиши имейл до шефа, че закъснявам",
]


def embed(model: str, text: str) -> list[float]:
    r = requests.post(URL, json={"model": model, "prompt": text[:4000]}, timeout=120)
    r.raise_for_status()
    return r.json()["embedding"]


def cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def run(model: str, skills: dict[str, str], threshold: float) -> dict[str, float]:
    embed(model, "warm up")
    idx = {name: embed(model, text) for name, text in skills.items()}
    top1 = top3 = hit = 0
    t0 = time.perf_counter()
    n = 0
    misses = []
    right: list[float] = []
    for want, qs in QUERIES.items():
        for q in qs:
            qv = embed(model, q)
            n += 1
            ranked = sorted(((cos(qv, v), k) for k, v in idx.items()), reverse=True)
            names = [k for _, k in ranked]
            top1 += names[0] == want
            top3 += want in names[:3]
            score = next(s for s, k in ranked if k == want)
            hit += score >= threshold
            right.append(score)
            if names[0] != want:
                misses.append(f"{q!r} → {names[0]}")
    ms = (time.perf_counter() - t0) / n * 1000
    neg = [max(cos(embed(model, q), v) for v in idx.values()) for q in NEGATIVE]
    false = sum(x >= threshold for x in neg)
    # the lowest threshold that no no-skill query clears, and what it lets through
    safe = round(max(neg) + 0.01, 2)
    print(f"    верните: мин {min(right):.2f}, медиана {sorted(right)[len(right) // 2]:.2f}; "
          f"без умение: макс {max(neg):.2f} → праг {safe} пуска {sum(x >= safe for x in right) / n:.0%}")
    for m in misses:
        print(f"    промах: {m}")
    return {"top1": top1 / n, "top3": top3 / n, "hit": hit / n, "false": false / len(NEGATIVE), "ms": ms}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="nomic-embed-text,bge-m3")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--skills", default=str(Path.home() / ".genesis" / "skills" / "skills.json"))
    args = ap.parse_args()
    data = json.loads(Path(args.skills).read_text(encoding="utf-8"))
    skills = {s["name"]: f"{s['name'].replace('_', ' ')}. {s.get('description', '')}"
              for s in data["skills"] if s["name"] in QUERIES}
    n = sum(len(q) for q in QUERIES.values())
    print(f"{len(skills)} умения, {n} заявки, {len(NEGATIVE)} без умение, праг {args.threshold}\n")
    for model in args.models.split(","):
        print(f"  {model}")
        r = run(model, skills, args.threshold)
        print(f"  {model}: top-1 {r['top1']:.0%}  top-3 {r['top3']:.0%}  "
              f"над прага {r['hit']:.0%}  фалшиви {r['false']:.0%}  {r['ms']:.0f} ms/заявка\n")


if __name__ == "__main__":
    main()
