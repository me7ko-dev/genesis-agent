#!/usr/bin/env python3
"""
genesis_agent.project_builder — генериране на МНОГОФАЙЛОВИ проекти (L2-3).

Вместо единично умение (.py), тук Genesis произвежда цял малък пакет в
projects_out/<slug>/:
    <module>.py     — имплементацията
    test_<module>.py — тестове (unittest, stdlib)
    README.md       — кратко описание + употреба

Пакетът се верифицира като се пуснат тестовете в sandbox-а (реален изход 0).
Индексира се в projects_out/projects.json (отделно от skills).

Употреба:
    from genesis_agent.project_builder import build_project
    out = build_project("A small stack data structure with push/pop/peek")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from genesis_agent import dna, sandbox
from genesis_agent.brain import Brain
from genesis_agent.config import DATA_DIR, MAX_LLM_RETRIES
from genesis_agent.skills_manager import slugify

# DATA_DIR (not PROJECT_ROOT): same reasoning as SKILLS_DIR/DATA_DIR in
# config.py — for an installed copy PROJECT_ROOT is site-packages, and
# generated multi-file projects need a location the user actually owns.
PROJECTS_DIR = DATA_DIR / "projects_out"
PROJECTS_INDEX = PROJECTS_DIR / "projects.json"


@dataclass
class ProjectOutcome:
    success: bool
    rounds: int
    path: str = ""
    files: list[str] = field(default_factory=list)
    last_error: str = ""


_ARCH_SYS = (
    "Ти си софтуерен архитект на Genesis. Създаваш МАЛЪК Python пакет от точно 2 файла: "
    "модул с имплементацията и unittest тестове (само стандартна библиотека). "
    "Отговори С ТОЧНО ДВА блока в този формат, без друг текст:\n"
    "```python name=<module>.py\n<код на модула>\n```\n"
    "```python name=test_<module>.py\n<unittest тестове, които импортват модула>\n```"
)

_FILE_RE = re.compile(r"```python\s+name=([\w./-]+)\n(.*?)\n```", re.DOTALL)


def _ensure_layout() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECTS_INDEX.exists():
        PROJECTS_INDEX.write_text(json.dumps({"version": 1, "projects": []}, indent=2),
                                  encoding="utf-8")


def _parse_files(raw: str) -> dict[str, str]:
    return {m.group(1).strip(): m.group(2).strip() for m in _FILE_RE.finditer(raw)}


def build_project(goal: str, *, max_rounds: int | None = None,
                  operator_id: str | None = None) -> ProjectOutcome:
    max_rounds = max_rounds or MAX_LLM_RETRIES
    _ensure_layout()
    # min_size_b=120 (design note, 2026-07-25): многофайлови проекти = трайни умения.
    brain = Brain(min_size_b=120)
    try:
        dna.validate_goal_ethics(goal)
        dna.assert_operator_if_strict(operator_id)
    except dna.GenesisDNAError as e:
        return ProjectOutcome(False, 0, last_error=str(e))

    slug = slugify(goal)
    proj_dir = PROJECTS_DIR / slug
    messages = [
        {"role": "system", "content": _ARCH_SYS},
        {"role": "user", "content": f"Цел:\n{goal}\n\nМодулът да е с ясно име, тестовете да го импортват."},
    ]

    last_error = ""
    for round_i in range(max_rounds):
        messages = Brain.trim_round_history(messages)
        reply = brain.complete(messages)
        raw = reply.raw_text or ""
        files = _parse_files(raw)
        # трябва поне модул + тест файл
        mod_files = [f for f in files if f.endswith(".py") and not f.startswith("test_")]
        test_files = [f for f in files if f.startswith("test_")]
        if not (mod_files and test_files):
            if raw.startswith("Error:"):
                return ProjectOutcome(False, round_i + 1, last_error=raw)
            messages.append({"role": "assistant", "content": raw[:1500]})
            messages.append({"role": "user", "content":
                "Върни ТОЧНО два блока с формат ```python name=<module>.py``` и "
                "```python name=test_<module>.py```."})
            continue

        # Записваме файловете и пускаме тестовете в sandbox.
        proj_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            (proj_dir / fname).write_text(content.rstrip() + "\n", encoding="utf-8")

        test_file = test_files[0]
        runner = f"import unittest,sys\nl=unittest.TestLoader().discover('.', pattern='{test_file}')\nr=unittest.TextTestRunner(verbosity=1).run(l)\nprint('OK' if r.wasSuccessful() else 'FAIL')\nsys.exit(0 if r.wasSuccessful() else 1)"
        # cwd=proj_dir, НЕ `cd {proj_dir} &&` в самия команден низ (design note,
        # 2026-08-11): последното чупи се на всеки път с интервал в него (напр.
        # Windows "C:\Users\John Doe\...") — run_shell вече приема cwd нативно
        # през subprocess, същия mechanism като _tool_run_cmd/run_tests другаде.
        res = sandbox.run_shell(f'python3 -c "{runner}"', cwd=proj_dir,
                                policy=sandbox.SandboxPolicy(mode="deny"), timeout=60)

        if res.ok and "OK" in res.stdout:
            # README
            readme = f"# {goal}\n\nАвтоматично генериран пакет от Genesis.\n\n## Файлове\n"
            readme += "\n".join(f"- `{f}`" for f in sorted(files))
            readme += f"\n\n## Тест\n```bash\ncd projects_out/{slug} && python3 -m unittest\n```\n"
            (proj_dir / "README.md").write_text(readme, encoding="utf-8")

            idx = json.loads(PROJECTS_INDEX.read_text(encoding="utf-8"))
            entry = {"slug": slug, "goal": goal, "files": sorted(files) + ["README.md"],
                     "created": datetime.now(timezone.utc).isoformat(), "verified": True}
            idx["projects"] = [p for p in idx["projects"] if p["slug"] != slug] + [entry]
            PROJECTS_INDEX.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
            return ProjectOutcome(True, round_i + 1, path=str(proj_dir.relative_to(DATA_DIR)),
                                  files=sorted(files) + ["README.md"])

        last_error = (res.stderr or res.stdout)[:300]
        messages.append({"role": "assistant", "content": raw[:1500]})
        messages.append({"role": "user", "content":
            f"Тестовете паднаха:\n{last_error}\n\nПоправи и върни пак двата блока."})

    return ProjectOutcome(False, max_rounds, last_error=last_error)


if __name__ == "__main__":
    out = build_project("A Stack class with push, pop, peek, is_empty methods")
    print("успех:", out.success, "| рундове:", out.rounds)
    if out.success:
        print("проект:", out.path, "| файлове:", out.files)
    else:
        print("грешка:", out.last_error[:150])
