#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end integration тест за свързаната Genesis система.

Проверява целия път без да пуска реалния TUI или облачни LLM повиквания:
  1. genesis_skills мостът парсва и изпълнява тул-тагове.
  2. Опасните команди се спират от sandbox-а (SAFE/CONFIRM/BLOCKED).
  3. Безопасните команди се изпълняват реално.
  4. Тул-повикванията се логват в споделената episodic памет.
  5. Разговорът се пише в споделената conversation_memory.
  6. Умение, генерирано през skills_manager, е видимо от trigger_engine.

Изход: 0 при пълен успех, 1 при провал.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import genesis_skills
from genesis_agent import sandbox, conversation_memory, skills_manager
from genesis_agent import episodic_memory as em
from genesis_agent.skill_loader import reload_skills_index, search_skills

PASS, FAIL = "✅", "❌"
_ok = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ok
    _ok = _ok and cond
    print(f"{PASS if cond else FAIL} {label}" + (f"  — {detail}" if detail else ""))


# Неинтерактивен режим → CONFIRM образците се отказват (не увисват).
sandbox.set_policy(sandbox.SandboxPolicy(mode="deny"))
genesis_skills.set_workspace(ROOT)

print("─── 1-3. Мост + sandbox (safe/blocked) ───────────────────")
eps_before = len(em._fetch_all_episodes())

results = genesis_skills.parse_and_execute_tools(
    "[RUN_CMD: echo e2e-ok && python3 -c 'print(7*6)']\n"
    "[LIST_DIR: genesis_agent]\n"
    "[RUN_CMD: rm -rf /]\n"
)
joined = "\n".join(results)
check("RUN_CMD безопасна се изпълни", "e2e-ok" in joined and "42" in joined)
check("LIST_DIR работи", "brain.py" in joined)
check("RUN_CMD 'rm -rf /' е BLOCKED", "SANDBOX BLOCKED" in joined)

print("\n─── 4. Логване в episodic памет ──────────────────────────")
eps_after = len(em._fetch_all_episodes())
check("нови епизоди записани", eps_after > eps_before, f"{eps_before} → {eps_after}")

print("\n─── 5. Споделена conversation_memory ─────────────────────")
# НЕ проверяваме n1 == n0 + 2 — базата е СПОДЕЛЕНА с реалната употреба
# (терминал+Discord), и summarize_old_context() легитимно компресира на порции
# щом премине threshold (виж коментара там). Ако n0 се окаже точно под прага,
# добавянето на тези 2 съобщения може само по себе си да го прекоси и нетната
# бройка да НАМАЛЕЕ въпреки коректен запис — хванато на живо 2026-07-26 (n0=49
# → n1=31), фалшив провал на теста, не бъг в conversation_memory. Вместо общ
# брой проверяваме, че последните 2 записа в хронологичния ред са точно тези,
# които току-що добавихме.
n0 = len(conversation_memory.get_history(last_n=1000))
conversation_memory.add_message("user", "E2E: тест на паметта")
conversation_memory.add_message("assistant", "E2E: ок")
history = conversation_memory.get_history(last_n=1000)
n1 = len(history)
last_two = history[-2:]
wrote_ok = (
    len(last_two) == 2
    and last_two[0].get("role") == "user" and "E2E: тест на паметта" in last_two[0].get("content", "")
    and last_two[1].get("role") == "assistant" and "E2E: ок" in last_two[1].get("content", "")
)
check("разговорът се пише в споделената база", wrote_ok,
      f"{n0} → {n1} (общата бройка може да спадне при компресия — проверени са последните 2 записа)")

print("\n─── 6. skills_manager → trigger_engine видимост ──────────")
test_slug = "e2e_integration_probe_skill"
skills_manager.save_skill(
    slug=test_slug,
    code="print('e2e probe skill ran')",
    goal="E2E probe: verify save_skill is visible to trigger_engine search",
)
reload_skills_index()
found = search_skills("e2e integration probe")
names = [s["name"] for s in found]
check("ново умение е видимо от trigger_engine", test_slug in names, str(names[:3]))

# ── Почистване на тестовите артефакти ─────────────────────────────
import json
conversation_memory.clear_session() if n0 == 0 else None
skills_json = ROOT / "skills" / "skills.json"
idx = json.loads(skills_json.read_text(encoding="utf-8"))
idx["skills"] = [s for s in idx["skills"] if s["name"] != test_slug]
skills_json.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
(ROOT / "skills" / f"{test_slug}.md").unlink(missing_ok=True)

print("\n" + ("═" * 50))
print("ВСИЧКО МИНАВА ✅" if _ok else "ИМА ПРОВАЛ ❌")
sys.exit(0 if _ok else 1)
