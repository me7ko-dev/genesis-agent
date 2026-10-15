"""
genesis_agent.doctor — `genesis doctor` и `/doctor`: работи ли всичко около
агента (Claude Code има същото).

    genesis doctor              бързо и без мрежа: всяка проверка с ✓/✗ и ред за поправка
    genesis doctor --models     и по една малка заявка до всеки модел (като `genesis models --check`)
    genesis doctor --json       същото като JSON

Защо (2026-10-15): „не работи“ на нова машина беше всеки път едно и също
разследване — няма git, ruff не е до Python-а на pipx, pytest липсва в
Python-а на проекта, Chromium не е свален, `.mcp.json` не е доверен, `claude`
не е влязъл. Всяко от тези се виждаше чак посред задача, като странно
поведение на модела.

Правила: нищо не виси — всяка проба има срок, а всички вървят едновременно с
общ срок (_DEADLINE), след който неотговорилите са ✗; без --models мрежа не
се пипа (моделите са „не е проверено“); стойност на ключ не се печата никога —
само имената, а всеки ред минава през notifier.redact и изрично махане на
стойностите на конфигурираните ключове.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

MARK = {"ok": "✓", "fail": "✗", "skip": "·"}
_RUN_TIMEOUT = 8.0       # `git --version` и подобни
_PY_TIMEOUT = 20.0       # Python с pytest/playwright — първото внасяне е бавно
_MCP_TIMEOUT = 10.0      # старт на един MCP сървър (в чата: 20 s)
_CLAUDE_TIMEOUT = 10.0
_DEADLINE = 30.0         # общо за всички проби без --models
_MODELS_DEADLINE = 90.0  # с --models: по 30 s на модел, 8 едновременно
_MIN_FREE = 1 << 30      # 1 GB свободни под ~/.genesis


@dataclass
class Check:
    name: str
    status: str          # ok | fail | skip (не е проверено / не се ползва)
    detail: str
    fix: str = ""


def _cmd(exe: str, *args: str) -> str:
    """Команда за поправка, която се поставя и в cmd.exe, и в POSIX shell:
    път с интервал — в двойни кавички (единичните cmd.exe не ги разбира)."""
    head = f'"{exe}"' if any(c.isspace() for c in exe) else exe
    return " ".join([head, *args])


def _env() -> dict[str, str]:
    """Средата за пробите — без ключовете (като page_check: детето няма нужда)."""
    try:
        from genesis_agent.page_check import _clean_env
        return _clean_env()
    except Exception:
        return {k: v for k, v in os.environ.items() if "KEY" not in k and "TOKEN" not in k}


def _run(argv: list[str], timeout: float = _RUN_TIMEOUT) -> tuple[int | None, str]:
    """(код, изходът) или (None, защо не стана). Никога не хвърля; stdin е
    затворен, за да не чака програма, която пита нещо."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           env=_env(), check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, f"не отговори за {timeout:.0f} s"
    except OSError as e:
        return None, f"не тръгва: {e}"
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _first(text: str) -> str:
    return (text.splitlines() or [""])[0].strip()[:120]


# ── отделните проверки ──────────────────────────────────────────────────────

def check_python(_ws: Path) -> list[Check]:
    ok = sys.version_info >= (3, 10)
    return [Check("Python", "ok" if ok else "fail", f"{platform.python_version()} ({sys.executable})",
                  "" if ok else "Genesis иска Python 3.10 или по-нов — https://www.python.org/downloads/")]


def _version_of(name: str, exe: str | None, fix: str) -> Check:
    if not exe:
        return Check(name, "fail", "няма го в PATH", fix)
    code, text = _run([exe, "--version"])
    if code != 0:
        return Check(name, "fail", f"{exe}: {_first(text) or f'код {code}'}", fix)
    return Check(name, "ok", _first(text))


def check_git(_ws: Path) -> list[Check]:
    return [_version_of("git", shutil.which("git"),
                        "инсталирай git (https://git-scm.com) — без него няма /review, а /undo не "
                        "следи командите")]


def check_node(_ws: Path) -> list[Check]:
    fix = "инсталирай Node.js LTS (https://nodejs.org) — за MCP сървърите през npx и JS проектите"
    node = _version_of("Node.js", shutil.which("node"), fix)
    # npm под Windows е npm.cmd — shutil.which го намира по PATHEXT.
    npm = _version_of("npm", shutil.which("npm"), fix)
    return [node, npm]


def check_ruff(_ws: Path) -> list[Check]:
    from genesis_agent.code_validate import _ruff_exe
    exe = _ruff_exe()
    fix = (f"{_cmd(sys.executable, '-m', 'pip', 'install', 'ruff')} — без него WRITE_FILE не "
           "хваща недефинирани имена")
    return [_version_of("ruff", exe, fix)]


def check_pytest(ws: Path) -> list[Check]:
    from genesis_agent.paths import project_python
    py = project_python(ws)
    fix = f"{_cmd(py, '-m', 'pip', 'install', 'pytest')} — с този Python агентът пуска тестовете"
    code, text = _run([py, "-m", "pytest", "--version"], timeout=_PY_TIMEOUT)
    if code != 0:
        why = "няма pytest" if "No module named" in text else (_first(text) or f"код {code}")
        return [Check("pytest", "fail", f"{why} ({py})", fix)]
    return [Check("pytest", "ok", f"{_first(text)} ({py})")]


def _key_names() -> list[str]:
    from genesis_agent.brain import _PROVIDERS, _load_keys
    keys = _load_keys()
    envs = {env for _, env in _PROVIDERS.values() if env}
    return sorted(e for e in envs if str(keys.get(e) or "").strip())


def check_keys(_ws: Path) -> list[Check]:
    names = _key_names()
    if names:
        return [Check("API ключове", "ok", ", ".join(names))]
    from genesis_agent import claude_cli
    if claude_cli.available():
        return [Check("API ключове", "skip", "няма — само Claude абонаментът през `claude`",
                      "genesis setup добавя безплатни ключове (Groq, NVIDIA, Ollama Cloud…) за резерва")]
    return [Check("API ключове", "fail", "няма нито един — нито един облачен модел няма да отговори",
                  "genesis setup (безплатни ключове: Groq, NVIDIA, Ollama Cloud…)")]


def check_models(_ws: Path, online: bool = False) -> list[Check]:
    from genesis_agent import model_check
    if online:
        results = model_check.run_check()
        if not results:
            return [Check("Модели", "fail", "config.yaml не описва модели", "genesis models")]
        ok = sum(1 for r in results if r.get("status") == "ok")
        dead = [f"{r['provider']}/{r['model']}" for r in results if r.get("status") == "dead"]
        detail = f"{ok}/{len(results)} отговарят" + (f"; мъртви: {', '.join(dead[:5])}" if dead else "")
        return [Check("Модели", "ok" if ok else "fail", detail,
                      "" if ok else "genesis models --check показва причината за всеки; genesis setup — ключ")]
    age = model_check.age_days()
    fix = "genesis doctor --models (в чата: /doctor --models) — по една малка заявка до всеки"
    if age is None:
        return [Check("Модели", "skip", "не е проверено (без мрежа)", fix)]
    last = list(model_check.last_results().values())
    ok = sum(1 for r in last if r.get("status") == "ok")
    return [Check("Модели", "skip",
                  f"не е проверено сега; преди {age:.0f} дни отговаряха {ok}/{len(last)}", fix)]


def _mcp_live(servers: list, live: dict) -> list[Check]:
    out = []
    for srv in servers:
        running = live.get(srv.name)
        if running is not None and running.alive():
            out.append(Check(f"MCP {srv.name}", "ok", f"върви, {len(running.tools)} инструмента"))
        else:
            why = (running.error if running is not None else "") or "не върви"
            out.append(Check(f"MCP {srv.name}", "fail", why[:200],
                             f"провери командата в {srv.source}; /mcp restart"))
    return out


def check_mcp(ws: Path) -> list[Check]:
    """Описаните сървъри. В чата, който вече ги е пуснал — състоянието им;
    иначе всеки се пуска за кратко (срок _MCP_TIMEOUT) и се спира."""
    from genesis_agent import mcp_client
    from genesis_agent.project_instructions import _project_root
    servers, untrusted = mcp_client.configured(ws)
    out: list[Check] = []
    if untrusted:
        out.append(Check("MCP на проекта", "fail", f"{untrusted} не е доверен — сървърите му не тръгват",
                         "прегледай го и /mcp trust (в терминала)"))
    if not servers:
        if not untrusted:
            out.append(Check("MCP", "skip", "няма описани сървъри (~/.genesis/mcp.json, .mcp.json)"))
        return out
    if mcp_client._started:
        return out + _mcp_live(servers, dict(mcp_client._servers))
    root = _project_root(ws.resolve())
    forbidden = [root, ws.resolve()]
    project = mcp_client.project_file(ws)
    results: dict[str, Check] = {}

    def probe(srv: mcp_client.Server) -> None:
        try:
            srv.start([] if srv.source == project else forbidden, timeout=_MCP_TIMEOUT)
            results[srv.name] = Check(f"MCP {srv.name}", "ok", f"тръгва, {len(srv.tools)} инструмента")
        except Exception as e:
            results[srv.name] = Check(f"MCP {srv.name}", "fail", (str(e) or type(e).__name__)[:200],
                                      f"провери командата в {srv.source}; /mcp показва подробности")
        finally:
            srv.stop()

    saved, mcp_client.workspace = mcp_client.workspace, ws
    try:
        threads = [threading.Thread(target=probe, args=(s,), daemon=True) for s in servers]
        for t in threads:
            t.start()
        end = time.monotonic() + _MCP_TIMEOUT + 3
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
    finally:
        mcp_client.workspace = saved
    for srv in servers:
        out.append(results.get(srv.name) or Check(f"MCP {srv.name}", "fail",
                                                  f"не тръгна за {_MCP_TIMEOUT:.0f} s",
                                                  f"провери командата в {srv.source}"))
    return out


def check_hooks(ws: Path) -> list[Check]:
    from genesis_agent import hooks
    active, untrusted = hooks.configured(ws)
    out: list[Check] = []
    if untrusted:
        out.append(Check("Hooks на проекта", "fail", f"{untrusted} не е доверен — куките му не се пускат",
                         "прегледай го и /hooks trust (в терминала)"))
    if active:
        out.append(Check("Hooks", "ok", f"{len(active)} активни"))
    elif not untrusted:
        out.append(Check("Hooks", "skip", "няма (~/.genesis/hooks.json, .genesis/hooks.json)"))
    return out


def check_disk(_ws: Path) -> list[Check]:
    from genesis_agent.paths import GENESIS_HOME
    home = Path(GENESIS_HOME)
    target = home
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError as e:
        return [Check("Диск", "fail", f"{home}: {e}")]
    detail = f"{free / (1 << 30):.1f} GB свободни ({home})"
    if free < _MIN_FREE:
        return [Check("Диск", "fail", detail,
                      "освободи поне 1 GB — историята, снимките за /undo и bench-ът пишат там")]
    return [Check("Диск", "ok", detail)]


# Пуска драйвера на playwright само за да каже пътя до Chromium — без браузър.
_PLAYWRIGHT_PROBE = (
    "import os\n"
    "from playwright.sync_api import sync_playwright\n"
    "p = sync_playwright().start()\n"
    "try:\n"
    "    path = p.chromium.executable_path\n"
    "finally:\n"
    "    p.stop()\n"
    "print('CHROMIUM_OK' if os.path.exists(path) else 'CHROMIUM_MISSING')\n")


def check_playwright(ws: Path) -> list[Check]:
    """Python с playwright и свален Chromium — за проверката на уеб страниците."""
    from genesis_agent.paths import project_python
    pythons = list(dict.fromkeys([sys.executable, project_python(ws)]))
    for py in pythons:
        code, text = _run([py, "-c", _PLAYWRIGHT_PROBE], timeout=_PY_TIMEOUT)
        if code is None or "No module named" in text:
            continue
        if "CHROMIUM_OK" in text:
            return [Check("Playwright/Chromium", "ok", f"готов ({py})")]
        return [Check("Playwright/Chromium", "fail", f"playwright има, Chromium — не ({py})",
                      _cmd(py, "-m", "playwright", "install", "chromium"))]
    py = pythons[0]
    return [Check("Playwright/Chromium", "fail", "няма Python с playwright — уеб страниците не се проверяват",
                  f"{_cmd(py, '-m', 'pip', 'install', 'playwright')}, после "
                  f"{_cmd(py, '-m', 'playwright', 'install', 'chromium')}")]


def check_claude(_ws: Path) -> list[Check]:
    from genesis_agent import claude_cli
    if not claude_cli.available():
        return [Check("claude CLI", "skip", "не е инсталиран — нужен е само за Claude абонамента")]
    method = claude_cli.auth_method(timeout=int(_CLAUDE_TIMEOUT))
    if method is None:
        return [Check("claude CLI", "fail", "`claude auth status` не отговори",
                      "пусни `claude` веднъж в терминала и влез с /login")]
    if method == "none":
        return [Check("claude CLI", "fail", "не е влязъл", "пусни `claude` и влез с /login")]
    label = "абонамент (claude.ai)" if method == "claude.ai" else method or "влязъл"
    return [Check("claude CLI", "ok", f"вход: {label}")]


# ── пускане ─────────────────────────────────────────────────────────────────

def probes(online: bool = False) -> list[tuple[str, Callable[[Path], list[Check]]]]:
    return [("Python", check_python), ("git", check_git), ("Node.js / npm", check_node),
            ("ruff", check_ruff), ("pytest", check_pytest), ("API ключове", check_keys),
            ("Модели", lambda ws: check_models(ws, online)), ("MCP", check_mcp),
            ("Hooks", check_hooks), ("Диск", check_disk), ("Playwright/Chromium", check_playwright),
            ("claude CLI", check_claude)]


def run_checks(workspace: Path | str | None = None, *, online: bool = False,
               deadline: float | None = None) -> list[Check]:
    """Всички проби едновременно, всяка в своя нишка (daemon: увиснала проба
    не държи процеса). Която не свърши до общия срок, е ✗ „не отговори“."""
    ws = Path(workspace or Path.cwd()).expanduser()
    limit = deadline if deadline is not None else (_MODELS_DEADLINE if online else _DEADLINE)
    plan = probes(online)
    results: dict[int, list[Check]] = {}

    def work(i: int, name: str, fn: Callable[[Path], list[Check]]) -> None:
        try:
            results[i] = fn(ws)
        except Exception as e:  # една счупена проба не скрива другите
            results[i] = [Check(name, "fail", f"проверката гръмна: {type(e).__name__}: {e}"[:200])]

    threads = [threading.Thread(target=work, args=(i, name, fn), daemon=True)
               for i, (name, fn) in enumerate(plan)]
    for t in threads:
        t.start()
    end = time.monotonic() + limit
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    out: list[Check] = []
    for i, (name, _) in enumerate(plan):
        out += results.get(i) or [Check(name, "fail", f"не отговори за {limit:.0f} s")]
    return [_scrubbed(c) for c in out]


def _secret_values() -> list[str]:
    """Стойностите на всичко, което по име е тайна (ключове от средата и .env).
    Само по име: _load_keys носи цялата среда, а пътят до Python не е тайна —
    без филтъра всеки ред ставаше „***“ (2026-10-15)."""
    try:
        from genesis_agent.brain import _PROVIDERS, _load_keys
        from genesis_agent.page_check import _SECRET_ENV
        provider_envs = {env for _, env in _PROVIDERS.values() if env}
        values = [str(v).strip() for k, v in _load_keys().items()
                  if k in provider_envs or _SECRET_ENV.search(k)]
    except Exception:
        values = []
    # Къса стойност (`1`, `true`) не е ключ, а би изтрила половината текст.
    return sorted({v for v in values if len(v) >= 6}, key=len, reverse=True)


def _scrubbed(check: Check) -> Check:
    from genesis_agent.notifier import redact
    secrets = _secret_values()

    def clean(text: str) -> str:
        for value in secrets:
            text = text.replace(value, "***")
        return redact(text)
    return Check(check.name, check.status, clean(check.detail), clean(check.fix))


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{MARK.get(c.status, '?')} {c.name}: {c.detail}")
        if c.fix and c.status != "ok":
            lines.append(f"    → {c.fix}")
    bad = sum(1 for c in checks if c.status == "fail")
    lines.append("")
    lines.append("Всичко нужно е наред." if not bad else
                 f"{bad} {'проблем' if bad == 1 else 'проблема'} (✗) — поправката е на реда под всеки. · = не е проверено или не се ползва.")
    return "\n".join(lines)


USAGE = """Употреба: genesis doctor [--models] [--json] [--cwd ПАПКА]
  без мрежа и бързо: Python, git, Node/npm, ruff, pytest, ключовете (само имената),
  MCP сървърите, hooks, диска, Playwright/Chromium, входа на `claude`
  --models   и по една малка заявка до всеки модел от config.yaml (мрежа)
  --json     резултатът като JSON (status: ok | fail | skip)
  --cwd      проектът, чиито .mcp.json, hooks и Python се проверяват"""


def main(argv: list[str]) -> int:
    online = as_json = False
    cwd = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return 0
        if a == "--models":
            online = True
        elif a == "--json":
            as_json = True
        elif a == "--cwd":
            if i + 1 >= len(argv):
                print("--cwd иска папка", file=sys.stderr)
                return 2
            i += 1
            cwd = argv[i]
        else:
            print(f"Непозната опция: {a}\n\n{USAGE}", file=sys.stderr)
            return 2
        i += 1
    ws = Path(cwd or ".").expanduser().resolve()
    if not ws.is_dir():
        print(f"Няма такава папка: {ws}", file=sys.stderr)
        return 2
    if not as_json:
        print("Проверявам…" + (" (и моделите — по една заявка)" if online else ""), flush=True)
    checks = run_checks(ws, online=online)
    if as_json:
        print(json.dumps({"ok": not any(c.status == "fail" for c in checks),
                          "checks": [asdict(c) for c in checks]}, ensure_ascii=False))
    else:
        print(format_report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0
