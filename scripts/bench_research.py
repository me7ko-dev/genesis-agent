#!/usr/bin/env python3
"""bench_research.py — колко добре и колко бързо Genesis проучва в интернет.

Мярката за [RESEARCH: …] (genesis_agent.research.grounded_research): въпроси с
проверим отговор (регулярен израз), част от тях на български и част за неща
след 2025 г., които моделите не помнят. За всеки въпрос:
  • ok      — верният отговор е в текста на RESEARCH;
  • cited   — отговорът носи поне един адрес на източник (http…);
  • seconds — колко чака агентът.

Иска мрежа и ключове (~/.genesis/.env), затова НЕ е в CI — пуска се на лаптопа:

    python scripts/bench_research.py                      # всички въпроси
    python scripts/bench_research.py --only 1,5,12 --runs 2
    python scripts/bench_research.py --compare ~/.genesis/bench/<папка>/results.json

Резултатът: ~/.genesis/bench/<дата>-research[-етикет]/results.json + таблица.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (въпрос, регулярен израз за верния отговор). Изразите приемат и двете
# писмености и запетая/точка в числата; проверката е без значение на регистъра.
QUESTIONS: list[tuple[str, str]] = [
    ("Каква е надморската височина на връх Мусала в метри?", r"2\s?925"),
    # Източниците (bg.wikipedia, planinka.bg, trekfactor.bg) дават 2626, други — 2627.
    ("Колко висок е връх Белмекен в Рила?", r"2\s?62[67]"),
    ("На кой порт по подразбиране слуша Ollama API?", r"11434"),
    ("Кой RFC въвежда HTTP статус 429 Too Many Requests?", r"6585"),
    ("Колко общини има в България?", r"\b265\b"),
    ("Каква е стандартната ставка на ДДС в България?", r"\b20\s?(%|процента|на сто)"),
    ("В коя година е пусната първата версия на SQLite?", r"\b2000\b"),
    ("Каква е дължината на река Искър в километри?", r"\b368\b"),
    ("Who created the Zig programming language?", r"andrew kelley|андрю кели"),
    ("Под какъв лиценз се разпространява Godot Engine?", r"\bMIT\b"),
    ("В кое село е роден Капитан Петко войвода?", r"доганхисар|doğanhisar|doganhisar|аисими|aisymi"),
    ("От коя дата България използва еврото като официална валута?", r"(1|първи)\s+януари\s+2026|2026-01-01|january\s+1,?\s+2026"),
    ("Коя компания разработва езиковите модели Qwen?", r"alibaba|алибаба"),
    ("Какъв е фиксираният курс на лева към еврото?", r"1[.,]95583"),
    ("Who wrote the Python library requests?", r"kenneth reitz|кенет райц"),
    ("Коя държава спечели Евровизия 2025?", r"австри|austria"),
    ("В кой град се проведе Евровизия 2026?", r"виена|vienna|wien"),
    ("Коя версия на Python излезе през октомври 2025 г.?", r"3\.14"),
    ("Колко е максималната дължина на пост в X (Twitter) за безплатен акаунт в знаци?", r"\b280\b"),
    ("На кой порт по подразбиране работи Redis?", r"6379"),
]

_URL = re.compile(r"https?://\S+")


def _run_one(question: str, pattern: str) -> dict:
    from genesis_agent.research import grounded_research
    t0 = time.monotonic()
    try:
        out = grounded_research(question)
    except Exception as e:  # noqa: BLE001 — бенчът записва и срива, не спира
        out = f"СРИВ: {type(e).__name__}: {e}"
    seconds = round(time.monotonic() - t0, 2)
    return {"question": question, "ok": bool(re.search(pattern, out, re.IGNORECASE)),
            "cited": bool(_URL.search(out)), "seconds": seconds, "chars": len(out),
            "output": out[:4000]}


def _summary(rows: list[dict]) -> dict:
    secs = [r["seconds"] for r in rows]
    return {"ok": sum(r["ok"] for r in rows), "cited": sum(r["cited"] for r in rows),
            "total": len(rows), "median_s": round(statistics.median(secs), 2) if secs else 0,
            "mean_s": round(statistics.mean(secs), 2) if secs else 0,
            "max_s": max(secs) if secs else 0}


def main() -> int:
    from genesis_agent.paths import ensure_utf8_streams
    ensure_utf8_streams()
    ap =argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="", help="номера на въпросите (1-базирани), напр. 1,5,12")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--compare", default="", help="results.json от предишно пускане")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    picked = list(enumerate(QUESTIONS, 1))
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}
        picked = [(i, q) for i, q in picked if i in wanted]

    rows: list[dict] = []
    for run in range(args.runs):
        for i, (q, pat) in picked:
            row = _run_one(q, pat)
            row.update(n=i, run=run + 1)
            rows.append(row)
            mark = "✅" if row["ok"] else "❌"
            src = "🔗" if row["cited"] else "  "
            print(f"{mark}{src} {i:2}. {row['seconds']:6.2f}s  {q[:70]}", flush=True)

    summ = _summary(rows)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out_dir = Path(args.out) if args.out else (
        Path.home() / ".genesis" / "bench" / f"{stamp}-research{('-' + args.label) if args.label else ''}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps({"summary": summ, "rows": rows},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nВерни: {summ['ok']}/{summ['total']}   с източник: {summ['cited']}/{summ['total']}   "
          f"медиана {summ['median_s']} s, средно {summ['mean_s']} s, най-бавно {summ['max_s']} s")
    print(f"Записано: {out_dir / 'results.json'}")

    if args.compare:
        old = json.loads(Path(args.compare).expanduser().read_text(encoding="utf-8"))
        o = old["summary"]
        print(f"Преди:  {o['ok']}/{o['total']} верни, {o['cited']} с източник, медиана {o['median_s']} s")
        old_by = {(r["n"], r["run"]): r for r in old["rows"]}
        for r in rows:
            prev = old_by.get((r["n"], r["run"]))
            if prev and prev["ok"] != r["ok"]:
                print(f"  {'▲' if r['ok'] else '▼'} {r['n']:2}. {r['question'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
