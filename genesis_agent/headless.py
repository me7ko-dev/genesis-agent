"""
genesis_agent.headless — `genesis -p "task"`: one turn, no chat, for scripts
and CI (Claude Code's print mode).

    genesis -p "обясни какво прави main.py"
    git diff | genesis -p "прегледай промяната" --json
    genesis -p "оправи падащите тестове" --cwd ~/code/app --dangerously-allow

The work happens in the current folder (or --cwd), with the same tools,
GENESIS.md, hooks and MCP servers as the chat. Progress goes to stderr; only
the answer (or one JSON object with --json) goes to stdout, so it can be piped.
Nobody is there to say yes: anything the sandbox would ask about is REFUSED
unless --dangerously-allow is given. --plan runs in plan mode (nothing changes).
Exit codes: 0 answered; 1 no answer, no model, cut off by the round cap or
the spin guard, or an error (still one JSON object with --json); 2 bad usage;
3 the agent asks a question (it is in "question"); 130 Ctrl-C.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, cast

USAGE = """Употреба: genesis -p "задача" [--json] [--plan] [--cwd ПАПКА] [--dangerously-allow]
  stdin, ако е подаден (`cat лог | genesis -p "обясни"`), се добавя към задачата.
  `@път` в задачата прикача файла (или списъка на папката).
  --json               един JSON обект на stdout: result, tools, warnings, seconds
  --plan               режим план: само чете и планира, нищо не променя
  --cwd ПАПКА          работната папка (по подразбиране текущата)
  --dangerously-allow  рисковите действия се изпълняват без питане (иначе се отказват)"""

_MAX_STDIN = 200_000


def _parse(argv: list[str]) -> dict[str, Any] | str:
    opts: dict[str, Any] = {"json": False, "plan": False, "cwd": "", "allow": False, "prompt": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["json"] = True
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
    """TurnUI за един ход без човек: напредъкът — на stderr, отговорът — събран."""

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.answer = ""
        self.tools: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.question = ""
        self.spun = False

    def _err(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr, flush=True)

    def thinking(self, label: str, spinner: str = "dots"):
        from contextlib import nullcontext
        return nullcontext()

    def assistant(self, text: str) -> None:
        self.answer = text

    def tool(self, name: str, result: str) -> None:
        self.tools.append({"name": name, "result": result[:2000]})
        first = (result.strip().splitlines() or [""])[0][:160]
        self._err(f"· {name}: {first}")

    def asked(self, question: str) -> None:
        self.question = question

    def spinning(self, note: str) -> None:
        self.spun = True
        self.warnings.append(note)
        self._err(f"⚠ {note}")

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self._err(f"⚠ {text}")

    def info(self, text: str) -> None:
        self._err(text)

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


def run(argv: list[str]) -> int:
    opts = _parse(argv)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 0 if opts == USAGE else 2
    prompt = " ".join(opts["prompt"]).strip()
    cwd = Path(opts["cwd"] or ".").expanduser().resolve()
    if not cwd.is_dir():
        print(f"Няма такава папка: {cwd}", file=sys.stderr)
        return 2
    # `@файл` — само в задачата; подаденото на stdin е данни, не молба за прикачване.
    if prompt:
        from genesis_agent import mentions
        prompt, notes = mentions.expand(prompt, cwd)
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
    from genesis_agent import plan_mode, sandbox

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
    ui = HeadlessUI(quiet=bool(opts["json"]))
    out = sys.stdout
    error = ""
    messages: Any = []
    try:
        # Веригата от модели и инструментите печатат на stdout ([Brain] …) — по
        # време на хода stdout е stderr, за да остане истинският чист за отговора.
        with contextlib.redirect_stdout(sys.stderr):
            system_prompt, _ = gta.build_system_prompt()
            messages = gta.run_turn(deque([{"role": "system", "content": system_prompt}]),
                                    prompt, cast("gta.TurnUI", ui))
    except KeyboardInterrupt:
        error = "прекъснато (Ctrl-C)"
    except Exception as e:  # отговорът е JSON и при грешка — скриптът трябва да я види
        error = f"{type(e).__name__}: {e}"
    finally:
        gta.console, gta.WORKSPACE, ws, policy, plan_on, gta.PERSIST_HISTORY = saved
        genesis_skills.set_workspace(ws)
        sandbox.set_policy(policy)
        plan_mode.set_active(plan_on)

    answer = ui.answer or next((str(m.get("content") or "") for m in reversed(messages)
                                if m.get("role") == "assistant" and m.get("content")), "")
    # Спрян по таван или въртене — отговорът е недовършен, не успех (одит 2026-10-09).
    if ui.question and ui.question not in answer:
        answer = f"{answer}\n\n{ui.question}".strip()
    cut = next((w for w in ui.warnings if "таван" in w), "") or (ui.warnings[-1] if ui.spun else "")
    failed = bool(error or cut) or not answer.strip() or \
        answer.lstrip().startswith(("[Грешка", "Error:"))
    code = 130 if error.startswith("прекъснато") else (3 if ui.question and not failed else
                                                        1 if failed else 0)
    if opts["json"]:
        print(json.dumps({"ok": code == 0, "result": answer, "question": ui.question or None,
                          "error": error or cut or None,
                          "tools": [t["name"] for t in ui.tools], "warnings": ui.warnings,
                          "workspace": str(cwd), "plan": bool(opts["plan"]),
                          "seconds": round(time.monotonic() - started, 1)},
                         ensure_ascii=False), file=out)
    else:
        if error:
            print(f"❌ {error}", file=sys.stderr)
        print(answer, file=out)
    return code
