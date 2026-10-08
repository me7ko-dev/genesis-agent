"""
genesis_agent.chat_commands — the chat commands that steer the agent the way
Claude Code's do: /init, /memory, /plan, /undo, /compact, /hooks, /commands,
/bg, and the operator's own commands from .genesis/commands/*.md.

Kept out of genesis_terminal_agent.main() (already a long if-chain) and free
of rich/console: `out` prints, `ask` asks a yes/no question, so the same code
can serve the phone later.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILTIN = ("/init", "/memory", "/plan", "/undo", "/compact", "/hooks", "/commands", "/bg")
_NAME = re.compile(r"^/([A-Za-z0-9][\w-]{0,40})(?:\s+(.*))?$", re.DOTALL)
_YES = ("", "да", "д", "d", "da", "y", "yes")


@dataclass
class Result:
    prompt: str | None = None      # run this as a turn for the model
    messages: Any = None           # the history, replaced (/compact)


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


def command_dirs(workspace: Path) -> list[Path]:
    """Project commands first (they win over the operator's same-named ones)."""
    from genesis_agent.project_instructions import _project_root
    root = _project_root(Path(workspace).resolve())
    return [root / ".genesis" / "commands", _home() / "commands"]


def custom_commands(workspace: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder in command_dirs(workspace):
        if folder.is_dir():
            for f in sorted(folder.glob("*.md")):
                found.setdefault(f.stem.lower(), f)
    return found


def expand_command(path: Path, args: str) -> str:
    """The command file as a prompt: $ARGUMENTS (or $1 $2 …) replaced; without
    a placeholder the arguments go after it."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if text.startswith("---"):  # frontmatter (description: …) is for /commands
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    words = args.split()
    used = "$ARGUMENTS" in text or re.search(r"\$\d", text)
    text = text.replace("$ARGUMENTS", args)
    text = re.sub(r"\$(\d)", lambda m: words[int(m.group(1)) - 1]
                  if 0 < int(m.group(1)) <= len(words) else "", text)
    if args and not used:
        text = f"{text.rstrip()}\n\n{args}"
    return text.strip()


def _description(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", text[:600], re.MULTILINE)
    if m:
        return m.group(1).strip()
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()
                  and not ln.startswith("---")), "")
    return first[:80]


def handle(text: str, *, messages: Any, workspace: Path,
           out: Callable[[str], None], ask: Callable[[str], str],
           compact: Callable[[Any], Any] | None = None) -> Result | None:
    """None = not one of these commands (the caller goes on as before)."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped.partition(" ")
    cmd, rest = head.lower(), rest.strip()

    if cmd == "/init":
        from genesis_agent.project_instructions import INIT_PROMPT
        return Result(prompt=INIT_PROMPT + (f"\n\nОт оператора: {rest}" if rest else ""))

    if cmd == "/memory":
        from genesis_agent.project_instructions import load
        sources = load(workspace)
        if not sources:
            out("Няма инструкции за проекта. /init ще напише GENESIS.md от самия проект; "
                f"общите ти инструкции за всички проекти — {_home() / 'GENESIS.md'}.")
        for s in sources:
            out(f"📄 {s.path} — {len(s.text)} знака{' (отрязан)' if s.truncated else ''}")
        if sources:
            out("Важат от началото на следващия разговор (/clear) — редактирай ги като обикновени файлове.")
        return Result()

    if cmd == "/plan":
        from genesis_agent import plan_mode
        if rest:
            plan_mode.set_active(True)
            out("⏸ Режим план: проучвам и правя план, нищо не променям. /plan — за изпълнение.")
            return Result(prompt=rest)
        on = plan_mode.toggle()
        if on:
            out("⏸ Режим план: само чета и планирам, нищо не променям. Кажи задачата.")
            return Result()
        out("▶ Режимът план е изключен.")
        return Result(prompt="Планът е одобрен. Изпълни го сега, стъпка по стъпка, и провери резултата.")

    if cmd == "/undo":
        from genesis_agent import edit_history
        plan = edit_history.pending()
        if not plan:
            out("Няма промени по файлове за връщане (в този разговор). Каквото е правено с "
                "RUN_CMD (команди), не се следи.")
            return Result()
        out("Ще върна последния ход, който промени файлове:\n  " + "\n  ".join(plan))
        if ask("Да върна ли? (Enter = да / не) > ").strip().lower() not in _YES:
            out("Нищо не е върнато.")
            return Result()
        label, done = edit_history.undo()
        out(f"↶ Върнат ходът „{label}“:\n  " + "\n  ".join(done or ["(без разлика)"]))
        if messages is not None:
            messages.append({"role": "user", "content":
                             "[оператор] /undo върна файловете от предния ход към състоянието "
                             "отпреди него. Не разчитай на онези промени."})
        return Result(messages=messages)

    if cmd == "/compact":
        if compact is None or messages is None:
            return Result()
        before = len(messages)
        new = compact(messages)
        out(f"🗜 Историята е компресирана: {before} → {len(new)} съобщения."
            if len(new) < before else "Историята е още къса — няма какво да се компресира.")
        return Result(messages=new)

    if cmd == "/hooks":
        return _hooks(rest, workspace, out, ask)

    if cmd == "/commands":
        found = custom_commands(workspace)
        if not found:
            out("Няма твои команди. Файл .genesis/commands/<име>.md в проекта (или "
                f"{_home() / 'commands'}) става /<име>; $ARGUMENTS е текстът след командата.")
        for name, path in found.items():
            out(f"/{name} — {_description(path)}  [dim]({path})[/]")
        return Result()

    if cmd == "/bg":
        from genesis_agent import background
        out(background.summary())
        return Result()

    m = _NAME.match(stripped)
    if m and m.group(1).lower() not in {b[1:] for b in BUILTIN}:
        cmd_file = custom_commands(workspace).get(m.group(1).lower())
        if cmd_file is not None:
            out(f"↪ /{m.group(1)} ({cmd_file.name})")
            return Result(prompt=expand_command(cmd_file, (m.group(2) or "").strip()))
    return None


def _hooks(rest: str, workspace: Path, out: Callable[[str], None],
           ask: Callable[[str], str]) -> Result:
    from genesis_agent import hooks
    active, untrusted = hooks.configured(workspace)
    if rest.lower() == "trust":
        target = hooks.project_file(workspace)
        if not target.is_file():
            out(f"Няма {target}.")
            return Result()
        out(f"{target}:\n{target.read_text(encoding='utf-8', errors='replace')[:3000]}")
        if ask("Тези команди ще се пускат на тази машина при работата на агента. "
               "Доверяваш ли им се? (да / Enter = не) > ").strip().lower() in ("да", "д", "y", "yes"):
            hooks.trust(target)
            out("✓ Доверени (до следващата промяна на файла).")
        else:
            out("Не са доверени — няма да се пускат.")
        return Result()
    if not active:
        out(f"Няма активни hooks. Файл: {_home() / 'hooks.json'} (твои, за всички проекти) или "
            ".genesis/hooks.json в проекта (иска /hooks trust).")
    for h in active:
        out(f"{h.event:<17} {h.matcher or '*':<22} {h.command}")
    if untrusted:
        out(f"⚠ {untrusted} съществува, но не е доверен — не се пуска. Прегледай го и /hooks trust.")
    return Result()
