"""
genesis_agent.headless — `genesis -p "task"`: one turn, no chat, for scripts
and CI (Claude Code's print mode).

    genesis -p "обясни какво прави main.py"
    git diff | genesis -p "прегледай промяната" --output-format json | jq .result
    genesis -p "оправи падащите тестове" --cwd ~/code/app --dangerously-allow

The work happens in the current folder (or --cwd), with the same tools,
GENESIS.md, hooks and MCP servers as the chat. Progress goes to stderr; only
the answer goes to stdout, so it can be piped. `--output-format json` (alias
`--json`) prints one JSON object instead: the answer, ok/is_error/exit_code,
the models that answered with their tokens (as the provider counted them),
cost when the provider reports it, rounds, the files the turn changed and the
errors. `stream-json` prints one JSON line per event while the turn runs and
the same result object last.
Nobody is there to say yes: anything the sandbox would ask about is REFUSED
unless --dangerously-allow is given. --plan runs in plan mode (nothing changes).
Exit codes: 0 answered; 1 no answer, no model, cut off by the round cap or
the spin guard, a green claim after a red test run, or an error (still one
JSON object with json); 2 bad usage; 3 the agent asks a question (it is in
"question"); 130 Ctrl-C.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

USAGE = """Употреба: genesis -p "задача" [--output-format text|json|stream-json] [--plan]
                  [--cwd ПАПКА] [--dangerously-allow]
  stdin, ако е подаден (`cat лог | genesis -p "обясни"`), се добавя към задачата.
  `@път` в задачата прикача файла (или списъка на папката).
  --output-format json  един JSON обект на stdout (и при грешка): result, is_error,
                       exit_code, models, usage, cost_usd, rounds, files, errors;
                       напредъкът не се печата. --json е същото.
  --output-format stream-json  по един JSON ред на събитие (tool, warning, …),
                       накрая същият обект с "type": "result"
  --plan               режим план: само чете и планира, нищо не променя
  --cwd ПАПКА          работната папка (по подразбиране текущата)
  --dangerously-allow  рисковите действия се изпълняват без питане (иначе се отказват)"""

FORMATS = ("text", "json", "stream-json")
_MAX_STDIN = 200_000


def _parse(argv: list[str]) -> dict[str, Any] | str:
    opts: dict[str, Any] = {"format": "text", "plan": False, "cwd": "", "allow": False,
                            "prompt": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["format"] = "json"
        elif a == "--output-format" or a.startswith("--output-format="):
            if "=" in a:
                value = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                i += 1
                value = argv[i]
            else:
                return f"--output-format иска {' | '.join(FORMATS)}"
            if value not in FORMATS:
                return f"--output-format иска {' | '.join(FORMATS)}, не {value!r}"
            opts["format"] = value
        elif a == "--plan":
            opts["plan"] = True
        elif a == "--dangerously-allow":
            opts["allow"] = True
        elif a == "--cwd":
            if i + 1 >= len(argv):
                return "--cwd иска папка"
            i += 1
            opts["cwd"] = argv[i]
        elif a in ("-h", "--help"):
            return USAGE
        else:
            opts["prompt"].append(a)
        i += 1
    return opts


class HeadlessUI:
    """TurnUI за един ход без човек: напредъкът — на stderr, отговорът — събран.

    `stream` (stream-json): истинският stdout — всяко събитие става един JSON
    ред там, докато ходът върви. Без него stdout не се пипа до края."""

    def __init__(self, quiet: bool, stream: Any = None) -> None:
        self.quiet = quiet
        self.stream = stream
        self.answer = ""
        self.tools: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.question = ""
        self.spun = False
        self._lock = threading.Lock()

    def _err(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr, flush=True)

    def event(self, kind: str, **data: Any) -> None:
        if self.stream is None:
            return
        line = json.dumps({"type": kind, **data}, ensure_ascii=False)
        with self._lock:
            print(line, file=self.stream, flush=True)

    def thinking(self, label: str, spinner: str = "dots"):
        from contextlib import nullcontext
        return nullcontext()

    def assistant(self, text: str) -> None:
        self.answer = text
        if text.strip():   # рунд само с инструменти идва с празен текст
            self.event("assistant", text=text)

    def tool(self, name: str, result: str) -> None:
        self.tools.append({"name": name, "result": result[:2000]})
        first = (result.strip().splitlines() or [""])[0][:160]
        self._err(f"· {name}: {first}")
        self.event("tool", name=name, result=result[:2000])

    def asked(self, question: str) -> None:
        self.question = question
        self.event("question", text=question)

    def spinning(self, note: str) -> None:
        self.spun = True
        self.warnings.append(note)
        self._err(f"⚠ {note}")
        self.event("warning", text=note)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self._err(f"⚠ {text}")
        self.event("warning", text=text)

    def info(self, text: str) -> None:
        self._err(text)
        self.event("info", text=text)

    def cancelled(self) -> bool:
        return False


def _read_stdin() -> str:
    """Подаденото на stdin, ако има такова — без да виси и без да пада.

    stdin може да е затворен (`<&-` → None), да е тръба, която никой не
    затваря (ssh, IDE), да носи невалиден UTF-8 или да е в кодировката на
    Windows конзолата — всеки от тези случаи сриваше или замразяваше `-p`
    (одит 2026-10-09). Четат се байтове, декодират се като UTF-8 със замяна;
    на POSIX се чака до 2 s да се появи нещо, иначе stdin се пропуска."""
    stream = sys.stdin
    if stream is None:
        return ""
    try:
        if stream.isatty():
            return ""
    except (ValueError, OSError):
        return ""
    if sys.platform != "win32":
        import select
        try:
            ready, _, _ = select.select([stream], [], [], 2.0)
        except (ValueError, OSError, TypeError):
            ready = [stream]
        if not ready:
            print("(stdin е отворен, но празен — пропуснат)", file=sys.stderr)
            return ""
    buffer = getattr(stream, "buffer", None)
    try:
        raw = buffer.read(_MAX_STDIN + 1) if buffer is not None else stream.read(_MAX_STDIN + 1)
    except (OSError, ValueError):
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if len(text) > _MAX_STDIN:
        text = text[:_MAX_STDIN] + f"\n… [stdin отрязан до {_MAX_STDIN} знака]"
    return text


def _models_and_usage(entries: list[dict], estimated: tuple[int, int]) -> tuple[list[dict], dict, float | None]:
    """Моделите, токените и цената на хода от записите на budget.record_usage.

    Без нито един запис (доставчик без `usage` в отговора) токените са
    оценката на чата (count_usage, ~4 знака на токен) и `estimated` е true —
    иначе скрипт, който сумира разхода, би видял нула."""
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_read_tokens": 0,
             "cached_write_tokens": 0, "estimated": False}
    cost: float | None = None
    for e in entries:
        key = (str(e.get("provider") or "?"), str(e.get("model") or "?"))
        row = by_model.setdefault(key, {"provider": key[0], "model": key[1], "calls": 0,
                                        "input_tokens": 0, "output_tokens": 0})
        prompt, completion = int(e.get("prompt_tokens") or 0), int(e.get("completion_tokens") or 0)
        row["calls"] += 1
        row["input_tokens"] += prompt
        row["output_tokens"] += completion
        usage["input_tokens"] += prompt
        usage["output_tokens"] += completion
        usage["cached_read_tokens"] += int(e.get("cached_read_tokens") or 0)
        usage["cached_write_tokens"] += int(e.get("cached_write_tokens") or 0)
        if isinstance(e.get("cost_usd"), (int, float)):
            cost = (cost or 0.0) + float(e["cost_usd"])
    if not entries and any(estimated):
        usage.update(input_tokens=estimated[0], output_tokens=estimated[1], estimated=True)
    return list(by_model.values()), usage, (round(cost, 6) if cost is not None else None)


def _shown(path: str, cwd: Path) -> str:
    """Пътят спрямо работната папка (с `/`), ако е в нея; иначе целият."""
    try:
        return Path(path).resolve().relative_to(cwd).as_posix()
    except ValueError:
        return path


def run(argv: list[str]) -> int:
    opts = _parse(argv)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 0 if opts == USAGE else 2
    fmt = str(opts["format"])
    prompt = " ".join(opts["prompt"]).strip()
    cwd = Path(opts["cwd"] or ".").expanduser().resolve()
    if not cwd.is_dir():
        print(f"Няма такава папка: {cwd}", file=sys.stderr)
        return 2
    # `@файл` — само в задачата; подаденото на stdin е данни, не молба за прикачване.
    # `@shot.png` и абсолютен път до картинка — като картинка за модела (2026-10-15).
    images: list[dict] = []
    if prompt:
        from genesis_agent import mentions
        prompt, notes = mentions.expand(prompt, cwd, images)
        for note in notes:
            print(f"📎 {note}", file=sys.stderr)
    piped = _read_stdin()
    if piped.strip():
        prompt = f"{prompt}\n\n```\n{piped.rstrip()}\n```" if prompt else piped
    if not prompt:
        print(USAGE, file=sys.stderr)
        return 2

    from rich.console import Console

    import genesis_skills
    import genesis_terminal_agent as gta
    from genesis_agent import budget, edit_history, plan_mode, sandbox

    # Всичко сменено тук се връща накрая: извикване от същия процес (тестове,
    # вграждане) не бива да остане с чужда папка, политика или режим план.
    saved = (gta.console, gta.WORKSPACE, genesis_skills._WORKSPACE, sandbox.get_policy(),
             plan_mode.active(), gta.PERSIST_HISTORY)
    # stdout е само за отговора: всичко, което чатът рисува, отива на stderr.
    gta.console = Console(stderr=True)
    gta.WORKSPACE = cwd
    gta.PERSIST_HISTORY = False
    genesis_skills.set_workspace(cwd)
    # Никой не може да каже „да“: рисковото се отказва (или се пуска изрично).
    sandbox.set_policy(sandbox.SandboxPolicy(mode="allow" if opts["allow"] else "deny"))
    plan_mode.set_active(bool(opts["plan"]))

    started = time.monotonic()
    out = sys.stdout
    ui = HeadlessUI(quiet=fmt != "text", stream=out if fmt == "stream-json" else None)
    ui.event("system", subtype="init", workspace=str(cwd), plan=bool(opts["plan"]))
    error = ""
    messages: Any = []
    # Рундовете — заявките към модела в хода (всеки tool рунд е една).
    real_ask = gta.ask_genesis
    rounds = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal rounds
        rounds += 1
        return real_ask(*args, **kwargs)
    tokens_before = (gta.total_input_tokens, gta.total_output_tokens)
    turns_before = edit_history.recorded()
    entries: list[dict] = []
    try:
        # Веригата от модели и инструментите печатат на stdout ([Brain] …) — по
        # време на хода stdout е stderr, за да остане истинският чист за отговора.
        with contextlib.redirect_stdout(sys.stderr), budget.collect() as entries:
            gta.ask_genesis = counted
            system_prompt, _ = gta.build_system_prompt()
            messages = gta.run_turn(deque([{"role": "system", "content": system_prompt}]),
                                    prompt, cast("gta.TurnUI", ui),
                                    **({"images": images} if images else {}))
    except KeyboardInterrupt:
        error = "прекъснато (Ctrl-C)"
    except Exception as e:  # отговорът е JSON и при грешка — скриптът трябва да я види
        error = f"{type(e).__name__}: {e}"
    finally:
        gta.ask_genesis = real_ask
        gta.console, gta.WORKSPACE, ws, policy, plan_on, gta.PERSIST_HISTORY = saved
        genesis_skills.set_workspace(ws)
        sandbox.set_policy(policy)
        plan_mode.set_active(plan_on)

    answer = ui.answer or next((str(m.get("content") or "") for m in reversed(messages)
                                if m.get("role") == "assistant" and m.get("content")), "")
    # Спрян по таван или въртене — отговорът е недовършен, не успех (одит 2026-10-09).
    if ui.question and ui.question not in answer:
        answer = f"{answer}\n\n{ui.question}".strip()
    # Ctrl-C вече спира хода отвътре (историята се пази) — тук идва като предупреждение.
    if not error and any(w.startswith("⏹ Прекъснато") for w in ui.warnings):
        error = "прекъснато (Ctrl-C)"
    cut = next((w for w in ui.warnings if "таван" in w), "") or (ui.warnings[-1] if ui.spun else "")
    # Грешката на веригата идва като предупреждение, не като отговор (2026-10-11).
    chain_errors = [w for w in ui.warnings if w.lstrip().startswith("[Грешка")]
    if not error:
        error = next(iter(chain_errors), "")
    # Зелено, казано срещу червено пускане на тестовете — неуспех, не ok:true
    # (2026-10-11: CI скрипт приемаше „Всички тестове минават“ след pytest rc=1).
    from genesis_agent.claim_check import RED_TESTS_WARNING
    red = next((w for w in ui.warnings if w.startswith(RED_TESTS_WARNING)), "")
    failed = bool(error or cut or red) or not answer.strip() or \
        answer.lstrip().startswith(("[Грешка", "Error:"))
    code = 130 if error.startswith("прекъснато") else (3 if ui.question and not failed else
                                                        1 if failed else 0)
    if fmt == "text":
        if error:
            print(f"❌ {error}", file=sys.stderr)
        print(answer, file=out)
        return code

    errors = list(dict.fromkeys(x for x in (error, cut, red, *chain_errors) if x))
    if failed and not errors:
        errors = ["няма отговор" if not answer.strip() else answer.strip().splitlines()[0][:300]]
    models, usage, cost = _models_and_usage(
        entries, (gta.total_input_tokens - tokens_before[0], gta.total_output_tokens - tokens_before[1]))
    # Само ако ТОЗИ ход е записал промени: иначе last_changes е на предишен ход.
    changes = edit_history.last_changes() if edit_history.recorded() > turns_before else \
        {"created": [], "edited": [], "deleted": []}
    seconds = time.monotonic() - started
    result = {"type": "result", "ok": code == 0, "is_error": code in (1, 130), "exit_code": code,
              "result": answer, "question": ui.question or None,
              "error": error or cut or red or None, "errors": errors, "tests_red": bool(red),
              "tools": [t["name"] for t in ui.tools], "warnings": ui.warnings,
              "models": models, "usage": usage, "cost_usd": cost, "rounds": rounds,
              "files": {k: [_shown(p, cwd) for p in v] for k, v in changes.items()},
              "workspace": str(cwd), "plan": bool(opts["plan"]),
              "seconds": round(seconds, 1), "duration_ms": int(seconds * 1000)}
    print(json.dumps(result, ensure_ascii=False), file=out, flush=True)
    return code
