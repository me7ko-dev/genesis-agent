"""
genesis_agent.chat_commands — the chat commands that steer the agent the way
Claude Code's do: /init, /memory, /plan, /undo, /compact, /hooks, /commands,
/bg, /context, /review, and the operator's own commands from .genesis/commands/*.md.

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

BUILTIN = ("/init", "/memory", "/plan", "/undo", "/compact", "/hooks", "/commands", "/bg", "/mcp",
           "/agents", "/todos", "/context")
_NAME = re.compile(r"^/([A-Za-z0-9][\w-]{0,40})(?:\s+(.*))?$", re.DOTALL)
_YES = ("", "да", "д", "d", "da", "y", "yes")


@dataclass
class Result:
    prompt: str | None = None      # run this as a turn for the model
    messages: Any = None           # the history, replaced (/compact)


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


# Имената, които чатът обработва преди тези команди — файл с такова име
# никога не би тръгнал, затова не се и показва (одит 2026-10-08).
RESERVED = {"help", "clear", "model", "models", "agent", "backup", "status", "history",
            "export", "update", "skills", "tasks", "done", "drop", "state", "maxcoding",
            "local_model_max", "local_model_normal", "exit", "quit"}


def command_dirs(workspace: Path) -> list[Path]:
    """The operator's commands first: a cloned repository's command does not
    replace one the operator wrote under the same name."""
    from genesis_agent.project_instructions import _project_root
    root = _project_root(Path(workspace).resolve())
    return [_home() / "commands", root / ".genesis" / "commands"]


def custom_commands(workspace: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    builtin = {b[1:] for b in BUILTIN} | RESERVED
    for folder in command_dirs(workspace):
        if folder.is_dir():
            for f in sorted(folder.glob("*.md")):
                if f.stem.lower() not in builtin:
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
    used = re.search(r"\$(ARGUMENTS|\d)", text)

    def fill(m: re.Match) -> str:
        # Едно минаване: `$5` вътре в самите аргументи не се замества пак
        # („price must be $5“ ставаше „price must be and“ — одит 2026-10-08).
        if m.group(1) == "ARGUMENTS":
            return args
        n = int(m.group(1))
        return words[n - 1] if 0 < n <= len(words) else ""
    text = re.sub(r"\$(ARGUMENTS|\d)", fill, text)
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


def _split(text: str) -> tuple[str, str]:
    """`/команда` и останалото — делени от първия празен знак (и нов ред)."""
    parts = text.strip().split(None, 1)
    return (parts[0] if parts else "", parts[1].strip() if len(parts) > 1 else "")


def handle(text: str, *, messages: Any, workspace: Path,
           out: Callable[[str], None], ask: Callable[[str], str],
           compact: Callable[[Any], Any] | None = None,
           context: Callable[[], dict] | None = None) -> Result | None:
    """None = not one of these commands (the caller goes on as before).

    `context` gives /context what only the caller knows: {"tools": the schemas
    of this turn, "window": the context window, "last": tokens of the last
    request as the provider counted them}."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    # Всеки празен знак дели: `/plan` + Enter + задачата (многоредовото поле на
    # телефона, `"""` в терминала) иначе отиваше при модела без режим план —
    # с разрешен запис (одит 2026-10-09).
    head, rest = _split(stripped)
    cmd = head.lower()

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
        note = edit_history.untracked_note()
        # Командите вече се следят в папката на проекта (checkpoints.py) — освен
        # ако снимката не е станала; тогава казваме защо (2026-10-09).
        why = (f" Командите на последния ход не са проследени: {note}." if note else
               " Командите се следят само в папката на проекта — без node_modules, .venv, "
               "кешовете, build/dist/target/vendor/env в корена, игнорираното от "
               ".gitignore и файловете над 20 MB.")
        if not plan:
            out("Няма промени по файлове за връщане (в този разговор)." + why)
            return Result()
        out("Ще върна последния ход, който промени файлове:\n  " + "\n  ".join(plan)
            + (f"\n⚠ Командите на хода не са проследени: {note}." if note else ""))
        if ask("Да върна ли? (Enter = да / не) > ").strip().lower() not in _YES:
            out("Нищо не е върнато.")
            return Result()
        label, done = edit_history.undo()
        out(f"↶ Върнат ходът „{label}“:\n  " + "\n  ".join(done or ["(без разлика)"]))
        if messages is not None:
            # bounded_history, не append: пълен deque(maxlen) иначе изхвърляше
            # системния промпт завинаги (одит 2026-10-09).
            from genesis_agent.agent_core import bounded_history
            messages = bounded_history(
                [*messages, {"role": "user", "content":
                             "[оператор] /undo върна файловете от предния ход към състоянието "
                             "отпреди него. Не разчитай на онези промени."}],
                getattr(messages, "maxlen", None))
        return Result(messages=messages)

    if cmd == "/compact":
        if compact is None or messages is None:
            return Result()
        before = len(messages)
        try:
            new = compact(messages)
        except Exception as e:
            out(f"❌ Компресията не стана: {e}")
            return Result()
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
            out(f"/{name} — {_description(path)}  ({path})")
        return Result()

    if cmd == "/mcp":
        return _mcp(rest, workspace, out, ask)

    if cmd == "/bg":
        from genesis_agent import background
        out(background.summary())
        return Result()

    if cmd == "/agents":
        from genesis_agent import agents
        out(agents.summary(workspace))
        return Result()

    if cmd == "/todos":
        from genesis_agent import todos
        out(todos.render())
        return Result()

    # Собствената review.md на оператора (често я има) остава с предимство —
    # вградената е само за когато няма такава.
    if cmd == "/review" and "review" not in custom_commands(workspace):
        from genesis_agent import review
        prompt, note = review.build(workspace, rest)
        out(note)
        return Result(prompt=prompt)

    if cmd == "/context":
        from genesis_agent import context_usage
        info = context() if context is not None else {}
        out(context_usage.report(messages, info.get("tools"), int(info.get("window") or 0),
                                 int(info.get("last") or 0)))
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
        _trust(target, workspace, [h.command for h in hooks._parse(target)],
               "Тези команди ще се пускат на тази машина при работата на агента.", out, ask)
        return Result()
    if not active:
        out(f"Няма активни hooks. Файл: {_home() / 'hooks.json'} (твои, за всички проекти) или "
            ".genesis/hooks.json в проекта (иска /hooks trust).")
    for h in active:
        out(f"{h.event:<17} {h.matcher or '*':<22} {h.command}")
    if untrusted:
        out(f"⚠ {untrusted} съществува, но не е доверен — не се пуска. Прегледай го и /hooks trust.")
    return Result()


def _mcp(rest: str, workspace: Path, out: Callable[[str], None],
         ask: Callable[[str], str]) -> Result:
    from genesis_agent import mcp_client
    if rest.lower() == "trust":
        target = mcp_client.project_file(workspace)
        if not target.is_file():
            out(f"Няма {target}.")
            return Result()
        if _trust(target, workspace,
                  [f"{x.name}: {x.command} {' '.join(x.args)}".strip() for x in mcp_client._parse(target)],
                  "Тези програми ще се пускат на тази машина като MCP сървъри.", out, ask):
            out("/mcp restart ги пуска.")
        return Result()
    if rest.lower() == "restart":
        for line in mcp_client.start_all(workspace) or ["Няма описани MCP сървъри."]:
            out(line)
        out("Новите инструменти важат от следващата реплика; за текстовите модели — след /clear.")
        return Result()
    verb, name = _split(rest)
    if verb.lower() in ("login", "logout"):
        return _mcp_login(verb.lower(), name, workspace, out, ask)
    out(mcp_client.summary())
    return Result()


def _mcp_login(verb: str, name: str, workspace: Path, out: Callable[[str], None],
               ask: Callable[[str], str]) -> Result:
    """`/mcp login <име>`: OAuth вход в браузъра за MCP сървър по HTTP
    (mcp_oauth); `/mcp logout <име>` забравя токена (2026-10-09).
    `--paste` (или по SSH/без графична среда): адресът се поставя на ръка."""
    from genesis_agent import mcp_client, mcp_oauth
    manual = "--paste" in name.split()
    name = " ".join(w for w in name.split() if w != "--paste")
    servers = [s for s in mcp_client.configured(workspace)[0] if isinstance(s, mcp_client.HttpServer)]
    srv = next((s for s in servers if s.name == name), None)
    if srv is None:
        names = ", ".join(s.name for s in servers) or "няма MCP сървъри по HTTP"
        out(f"/mcp {verb} <име> — {names}.")
        return Result()
    url = mcp_client._expand(srv.url)
    if verb == "logout":
        out(f"✓ Токенът за {name} е забравен." if mcp_oauth.forget(url) else f"Няма вход за {name}.")
        return Result()
    running = mcp_client._servers.get(name)
    hint = running.www_authenticate if isinstance(running, mcp_client.HttpServer) else ""
    try:
        mcp_oauth.login(url, www_authenticate=hint, say=out,
                        paste=ask if manual or mcp_oauth.no_browser() else None)
    except mcp_oauth.OAuthError as e:
        out(f"❌ Входът в {name} не стана: {e}")
        return Result()
    out(f"✓ Вход в {name}. Пускам MCP сървърите наново…")
    for line in mcp_client.start_all(workspace):
        out(line)
    return Result()


_MAX_TRUST_PREVIEW = 20000


def _trust(target: Path, workspace: Path, commands: list[str], warning: str,
           out: Callable[[str], None], ask: Callable[[str], str]) -> bool:
    """Shows the WHOLE file and what will run, then asks. Refuses a file that
    leads out of the project or is too long to read — the preview was cut at
    3000 characters and the last hook ran unseen (audit 2026-10-08)."""
    from genesis_agent import hooks
    from genesis_agent.project_instructions import _project_root
    if not hooks.stays_in_project(target, _project_root(Path(workspace).resolve())):
        out(f"❌ {target} води извън проекта (символна връзка) — не може да бъде доверен.")
        return False
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_TRUST_PREVIEW:
        out(f"❌ {target} е {len(text)} знака — твърде дълъг за преглед тук. Прегледай го в "
            "редактор и го съкрати, ако искаш да му се довериш.")
        return False
    out(f"{target}:\n{text}")
    out("Ще се пускат:\n  " + ("\n  ".join(commands) if commands else "(нищо)"))
    if ask(f"{warning} Доверяваш ли им се? (да / Enter = не) > ").strip().lower() in ("да", "д", "y", "yes"):
        hooks.trust(target)
        out("✓ Доверени (до следващата промяна на файла).")
        return True
    out("Не са доверени — няма да се пускат.")
    return False
