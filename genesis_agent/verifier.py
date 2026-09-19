#!/usr/bin/env python3
"""
genesis_agent.verifier — реална верификация на умение преди да се приеме за годно.

Вместо само LLM-критик да казва YES/NO, тук умението се ИЗПЪЛНЯВА в sandbox-а и
получава обективна присъда:

    verified=True,  method="self_test_passed"  — има assert/__main__ и излезе с код 0
    verified=True,  method="runs_clean"        — изпълни се без грешка (но без тест)
    verified=False, method="syntax_err"        — не се парсва
    verified=False, method="placeholder"       — съдържа заместващ ключ (няма да работи)
    verified=False, method="blocked"           — sandbox-ът го блокира (опасен)
    verified=False, method="runtime_err"       — гръмна при изпълнение (вкл. липсващ пакет)

Забележка: sandbox-ът дава минимална среда, затова умения, които искат външни
пакети (pandas и т.н.), ще дадат "runtime_err: No module named ...". Това е
честно — те не са проверени в текущата среда, а не че кодът е грешен по принцип.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from genesis_agent import sandbox

_PLACEHOLDER_RE = re.compile(
    r"your_api_key|YOUR_API_KEY|your-api-key|api_key\s*=\s*['\"]your|"
    r"<your_|replace_with_your|xxxxxxxx",
    re.IGNORECASE,
)


@dataclass
class VerifyResult:
    verified: bool
    method: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"verified": self.verified, "method": self.method, "detail": self.detail[:500]}


def _is_tautological_assert(node: ast.Assert) -> bool:
    """assert True / assert 1==1 стил — двата операнда на сравнение (или
    единственият за bare-константа) са литерали, значи тестът НЕ проверява
    нищо реално. Слаб модел, инструктиран да "винаги сложи self-test", но без
    уменията да напише истински такъв, свършва точно тук — verify_skill досега
    приемаше това като пълноценен self_test_passed (design note, 2026-08-11)."""
    test = node.test
    if isinstance(test, ast.Constant):
        return True
    if isinstance(test, ast.Compare):
        operands = [test.left, *test.comparators]
        return all(isinstance(o, ast.Constant) for o in operands)
    return False


def verify_skill(code: str, *, timeout: int = 30) -> VerifyResult:
    """Изпълнява умението в sandbox и връща обективна присъда."""
    # 1. Статични проверки (бързи, без изпълнение).
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return VerifyResult(False, "syntax_err", str(e))
    if _PLACEHOLDER_RE.search(code):
        return VerifyResult(False, "placeholder", "съдържа заместващ ключ/стойност")

    has_self_test = "__main__" in code or any(
        isinstance(n, ast.Assert) and not _is_tautological_assert(n)
        for n in ast.walk(tree)
    )

    # 2. Реално изпълнение в sandbox (deny режим → опасното не се пуска).
    res = sandbox.run_python(code, policy=sandbox.SandboxPolicy(mode="deny"), timeout=timeout)
    if res.blocked:
        return VerifyResult(False, "blocked", res.stderr[:300])
    if not res.ok:
        return VerifyResult(False, "runtime_err", (res.stderr or res.stdout)[:300])

    # "OK" в stdout се изисква изрично навсякъде в промптите (autonomous_loop,
    # orchestrator, ensemble, brain.system_prompt_base — "print 'OK' on
    # success"), но досега verify_skill не проверяваше дали моделът реално го
    # е спазил — само дали ИМА assert. Двете заедно (нетавтологичен assert +
    # действително отпечатано OK) са единствената комбинация, приемана за
    # self_test_passed.
    if has_self_test and "OK" in res.stdout:
        return VerifyResult(True, "self_test_passed", res.stdout.strip()[:300])
    return VerifyResult(True, "runs_clean", res.stdout.strip()[:300])


if __name__ == "__main__":
    samples = {
        "добро (self-test)": "def add(a,b):\n    return a+b\nassert add(2,3)==5\nprint('OK')",
        "runs_clean": "print(sum(range(10)))",
        "syntax": "def broken(:\n    pass",
        "placeholder": "api_key='your_api_key'\nprint(api_key)",
        "runtime": "import nonexistent_pkg_xyz\nprint('hi')",
    }
    for label, code in samples.items():
        r = verify_skill(code)
        print(f"{label:22} → verified={r.verified}  method={r.method}")
