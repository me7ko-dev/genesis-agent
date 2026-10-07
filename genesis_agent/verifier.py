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
    verified=False, method="needs_confirmation"— иска операция, която проверката
                                                 отказва без надзор (подпроцес,
                                                 chmod, инсталация) — НЕдоказано,
                                                 не доказано опасно
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

# `[SANDBOX DENIED]` = CONFIRM операция при неинтерактивен режим;
# `[SANDBOX BLOCKED]` = катастрофално, отказва се винаги. Маркерите се пишат в
# sandbox._decide и са частта от stderr, която различава двете.
_DENIED_RE = re.compile(r"\[SANDBOX (DENIED|DECLINED)\]")

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
    return _self_evident(node.test)


def _self_evident(test: ast.AST) -> bool:
    """Израз без нищо от кода — без име, извикване, атрибут: `2 + 3 == 5`,
    `True`, `(1, 2) == (1, 2)`. Резултатът му е известен преди да се пусне."""
    return not any(isinstance(n, (ast.Name, ast.Call, ast.Attribute, ast.Subscript))
                   for n in ast.walk(test))


_SWALLOWS = {"AssertionError", "Exception", "BaseException"}


_EXITS = {"exit", "_exit", "abort"}


def _nonzero_exit_code(args: list[ast.expr]) -> bool:
    """`exit(1)`, `exit("FAIL")` — да; `exit()`, `exit(0)`, `exit(None)` — не."""
    if not args:
        return False
    try:
        return ast.literal_eval(args[0]) not in (None, 0, False)
    except (ValueError, TypeError, SyntaxError):
        return True  # изчислен код — приема се за провал, както би го приел shell


def _handler_fails_loudly(body: list[ast.stmt]) -> bool:
    """Обработчикът пак проваля: `raise`, `sys.exit(1)`, `os._exit(1)`.
    Не: `sys.exit(0)`, `raise SystemExit`, и нищо във вложена функция/lambda."""
    stack: list[ast.AST] = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(n, ast.Raise):
            exc = n.exc
            if exc is None:
                return True                      # голо `raise` — пак хвърля
            call = exc if isinstance(exc, ast.Call) else None
            target = call.func if call else exc
            if getattr(target, "id", getattr(target, "attr", "")) == "SystemExit":
                if call is not None and _nonzero_exit_code(call.args):
                    return True
                continue
            return True
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in _EXITS and _nonzero_exit_code(n.args):
                return True
        stack.extend(ast.iter_child_nodes(n))
    return False


def _swallows_failures(node: ast.Try) -> bool:
    for h in node.handlers:
        names = [] if h.type is None else (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])
        catches = h.type is None or any(isinstance(n, ast.Name) and n.id in _SWALLOWS for n in names)
        # `except AssertionError: print(...); sys.exit(1)` е най-честият честен
        # самотест — той пак проваля (преглед 2026-10-07).
        if catches and not _handler_fails_loudly(h.body):
            return True
    return False


def _dead_branch(test: ast.AST) -> bool:
    """`if False:`, `while 0:`, `if 1 == 2:` — известно преди пускане, че е лъжа.
    `while True:` и `if 1:` са живи."""
    import operator
    ops = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not}
    try:
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in ops:
            left = ast.literal_eval(test.left)
            right = ast.literal_eval(test.comparators[0])
            return not ops[type(test.ops[0])](left, right)
        return not bool(ast.literal_eval(test))   # без eval: само литерали
    except (ValueError, TypeError, SyntaxError):
        return False


def _suppresses(node: ast.With | ast.AsyncWith) -> bool:
    """`with contextlib.suppress(AssertionError):` — като try, който поглъща."""
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            f = ctx.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == "suppress":
                return True
    return False


_Func = ast.FunctionDef | ast.AsyncFunctionDef


def _checks_in(stmts: list[ast.stmt], funcs: dict[str, _Func], seen: set[str]) -> bool:
    """Има ли проверка, която РЕАЛНО се изпълнява, в тези оператори?

    Одит 2026-10-07: брояше се всеки assert в кода — и в `def _test()`,
    която никой не вика, и в `try: assert … / except AssertionError: pass`,
    и `if False: raise`. Счупена `add` минаваше като self_test_passed."""
    for st in stmts:
        if isinstance(st, ast.Assert) and not _is_tautological_assert(st):
            return True
        if isinstance(st, (ast.If, ast.While)) and _dead_branch(st.test):
            if _checks_in(st.orelse, funcs, seen):
                return True
            continue
        if isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While)) and any(
                isinstance(b, ast.Raise) for b in st.body):
            return True
        if isinstance(st, ast.Try):
            if not _swallows_failures(st) and _checks_in(st.body, funcs, seen):
                return True
            if _checks_in(st.orelse, funcs, seen) or _checks_in(st.finalbody, funcs, seen):
                return True
            continue
        if isinstance(st, (ast.With, ast.AsyncWith)):
            if not _suppresses(st) and _checks_in(st.body, funcs, seen):
                return True
            continue
        if isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            if _checks_in(st.body, funcs, seen) or _checks_in(st.orelse, funcs, seen):
                return True
            # `for t in (test_add, test_sub): t()` — функциите са в итерируемото,
            # но само ако тялото наистина вика променливата на цикъла.
            if (isinstance(st, (ast.For, ast.AsyncFor)) and isinstance(st.target, ast.Name)
                    and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == st.target.id for b in st.body for n in ast.walk(b))):
                for n in ast.walk(st.iter):
                    if not isinstance(n, ast.Name):
                        continue
                    f_def = funcs.get(n.id)
                    if f_def is not None and n.id not in seen:
                        seen.add(n.id)
                        if _checks_in(f_def.body, funcs, seen):
                            return True
            continue
        if isinstance(st, ast.Match):
            if any(_checks_in(c.body, funcs, seen) for c in st.cases):
                return True
            continue
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Извикана функция (`_test()`) или метод на клас от файла (`T().run()`):
        # проверките в нея се броят. Само споменато име (`_test` без скоби,
        # `TESTS = [test_add]`) не е извикване; `os.environ.get()` не е `def get`
        # (преглед 2026-10-07).
        for n in ast.walk(st):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name):
                key, f_def = f.id, funcs.get(f.id)
            elif isinstance(f, ast.Attribute):
                key = "." + f.attr      # методите стоят под „.име“ — виж _has_real_check
                f_def = funcs.get(key)
            else:
                continue
            if f_def is not None and key not in seen:
                seen.add(key)
                if _checks_in(f_def.body, funcs, seen):
                    return True
    return False


def _has_real_check(tree: ast.AST) -> bool:
    """Съдържа ли кодът проверка, която МОЖЕ да се провали?

    Два начина се броят: нетавтологичен `assert`, и условно `raise`
    (`if got != expected: raise ValueError(...)`) — вторият е напълно
    легитимен self-test и е причината `__main__` блокът някога да се брои.

    Присъствието на `__main__` НЕ се брои (bug fix, 2026-09-20). Дотук
    условието беше `"__main__" in code` — търсене на НИЗ, което хваща и
    коментар, и docstring, и име на променлива. И дори истински
    `if __name__ == "__main__": print("OK")` не проверява нищо: точно това
    пише слаб модел, инструктиран „винаги слагай self-test и печатай OK",
    и точно него `_is_tautological_assert` вече отказваше по другия клон.
    Присъдата отиваше в библиотеката като self_test_passed и оттам се
    преизползва от бъдещи мисии през RAG.
    """
    # Всички функции по име — и вложените (`async def _run_tests()` в
    # `if __name__ == "__main__":`, извикана с asyncio.run).
    # Методите на класовете във файла — под „.име“, отделно от функциите на
    # модула, за да не стане `os.environ.get()` „извикване“ на `def get`.
    funcs: dict[str, _Func] = {}
    method_ids: set[int] = set()
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for m in cls.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs["." + m.name] = m
                    method_ids.add(id(m))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(n) not in method_ids:
            funcs.setdefault(n.name, n)
    return _checks_in(getattr(tree, "body", []), funcs, set())


# Промптите навсякъде искат „print 'OK' on success", и всяко умение в
# библиотеката наистина печата OK на отделен ред. Проверката обаче беше
# `"OK" in stdout` — подниз, който се съдържа в BROKEN, TOKEN, LOOKUP и в
# самото „NOT OK" (bug fix, 2026-09-20). Умение, което ИЗРИЧНО съобщава, че
# е счупено, минаваше за преминал self-test. Иска се ред, който ЗАПОЧВА с
# OK като отделна дума — така „OK", „OK: 5 проверки" и „OK (3)" минават,
# а BROKEN и NOT OK не.
_OK_LINE_RE = re.compile(r"^OK\b")


def _printed_ok(stdout: str) -> bool:
    return any(_OK_LINE_RE.match(line.strip()) for line in (stdout or "").splitlines())


def verify_skill(code: str, *, timeout: int = 30) -> VerifyResult:
    """Изпълнява умението в sandbox и връща обективна присъда."""
    # 1. Статични проверки (бързи, без изпълнение).
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return VerifyResult(False, "syntax_err", str(e))
    if _PLACEHOLDER_RE.search(code):
        return VerifyResult(False, "placeholder", "съдържа заместващ ключ/стойност")

    has_self_test = _has_real_check(tree)

    # 2. Реално изпълнение в sandbox (deny режим → опасното не се пуска).
    res = sandbox.run_python(code, policy=sandbox.SandboxPolicy(mode="deny"), timeout=timeout)
    if res.blocked:
        # Две много различни неща излизаха с една и съща дума. Измерено:
        # умение, което вика `git rev-parse` през subprocess, получаваше
        # същата присъда ("blocked", в документацията — „опасен“), както
        # `os.system("rm -rf /")`. Първото е НЕдоказано, второто е доказано
        # опасно, а разликата решава какво да се каже на модела после: при
        # смесването единственият начин да изпълни инструкцията е да махне
        # подпроцеса, тоест да направи умението безполезно, или да си
        # измисли тест. Гейтът не се разхлабва — и двете остават verified=False.
        if _DENIED_RE.match(res.stderr.lstrip()):
            return VerifyResult(False, "needs_confirmation", res.stderr[:300])
        return VerifyResult(False, "blocked", res.stderr[:300])
    if not res.ok:
        return VerifyResult(False, "runtime_err", (res.stderr or res.stdout)[:300])

    # "OK" в stdout се изисква изрично навсякъде в промптите (autonomous_loop,
    # orchestrator, ensemble, brain.system_prompt_base — "print 'OK' on
    # success"), но досега verify_skill не проверяваше дали моделът реално го
    # е спазил — само дали ИМА assert. Двете заедно (нетавтологичен assert +
    # действително отпечатано OK) са единствената комбинация, приемана за
    # self_test_passed.
    if has_self_test and _printed_ok(res.stdout):
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
