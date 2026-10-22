"""
genesis_agent.hooks — the operator's own commands around the agent's work
(Claude Code's hooks): format after every edit, refuse writes into a folder,
run the tests before the turn may end, log every command.

    ~/.genesis/hooks.json             — always on (the operator wrote it)
    <project>/.genesis/hooks.json     — only after `/hooks trust`: a cloned
                                        repository must not run code on this
                                        machine because the agent opened it.
                                        Trust is for the file's exact content;
                                        any change needs it again.

    {"hooks": {
      "PreToolUse":  [{"matcher": "WRITE_FILE|EDIT_FILE", "command": "python check.py"}],
      "PostToolUse": [{"matcher": "EDIT_FILE", "command": "ruff format \\"$GENESIS_FILE\\""}],
      "UserPromptSubmit": [{"command": "..."}],
      "Stop": [{"command": "python -m pytest -q"}]
    }}

Claude Code's nested form ({"matcher": ..., "hooks": [{"type": "command",
"command": ...}]}) is read too. `matcher` is a regex on the tool name (empty =
every tool): the Genesis name or Claude Code's (`Bash`, `Write|Edit`, `Read`).
The command gets a JSON description on stdin (with Claude Code's keys too:
`hook_event_name`, `cwd`, `tool_input.file_path`) and GENESIS_TOOL,
GENESIS_FILE, GENESIS_WORKSPACE in its environment, runs in the workspace,
for at most `timeout` seconds (default 60, at least 1).

Exit code 2 is the one that matters, as in Claude Code:
  PreToolUse        → the tool does not run; stderr goes to the model.
  PostToolUse       → stderr is added to the tool's result for the model.
  UserPromptSubmit  → the message is not sent; stderr is shown. Exit 0 with
                      stdout → that text is added to the message as context.
  Stop              → the turn goes on: stderr goes to the model as the next
                      message (at most twice in a row).
Any other code: the hook failed, the work goes on, the operator sees it.

Exit 0 with Claude Code's JSON on stdout decides like exit 2:
`{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": ...}}`
or `{"decision": "block", "reason": ...}` blocks (the reason is the message);
`hookSpecificOutput.additionalContext` is the context. Such JSON is never
added to the message as text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("genesis.hooks")

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
_TIMEOUT = 60
_MAX_OUT = 4000

workspace: Path | None = None


def _print(text: str) -> None:
    print(text)


notify: Callable[[str], None] = _print  # what the operator sees; the chat replaces it


@dataclass
class Hook:
    event: str
    matcher: str
    command: str
    timeout: int
    source: Path


@dataclass
class Outcome:
    blocked: bool = False
    message: str = ""     # stderr for code 2
    context: str = ""     # stdout for code 0 (UserPromptSubmit)


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


def _trust_file() -> Path:
    return _home() / "trusted_hooks.json"


def _ws() -> Path:
    if workspace is not None:
        return Path(workspace)
    try:
        import genesis_skills
        return Path(genesis_skills._WORKSPACE)
    except Exception:
        log.debug("работната папка на genesis_skills не е достъпна — cwd", exc_info=True)
        return Path.cwd()


def project_file(ws: Path | None = None) -> Path:
    from genesis_agent.project_instructions import _project_root
    root = _project_root(Path(ws or _ws()).resolve())
    return root / ".genesis" / "hooks.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted() -> dict[str, str]:
    try:
        data = json.loads(_trust_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(path: Path) -> str:
    # Пътят, както е в проекта — НЕ разрешен през символни връзки: иначе
    # доверието за проект A важеше и за B с `.genesis/hooks.json -> A/...`,
    # а командите (`sh ./check.sh`) се пускаха в B (одит 2026-10-08).
    return str(Path(os.path.abspath(path)))


def stays_in_project(path: Path, root: Path) -> bool:
    """The file is really inside the project (no symlink out of it)."""
    try:
        real, top = path.resolve(), root.resolve()
    except OSError:
        return False
    return top in real.parents and not path.is_symlink() and not path.parent.is_symlink()


def _trust_key_file() -> Path:
    return _home() / "hooks_trust.key"


def trust_key() -> bytes:
    """Ключът, с който се подписва всеки запис за доверие. Голият sha256 на
    файла можеше да се сглоби от команда на модела (одит 2026-10-11:
    `python3 -c` от RUN_CMD записа trusted_hooks.json сам и проектните hooks
    станаха доверени без `/hooks trust`). Името е в _SECRET_PATHS на
    sandbox-а, а py_guard не пуска до GENESIS_HOME изобщо."""
    path = _trust_key_file()
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    import secrets
    data = secrets.token_hex(32).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return data


def _seal(path: Path) -> str:
    import hmac
    message = f"{_key(path)}\0{_digest(path)}".encode()
    return hmac.new(trust_key(), message, hashlib.sha256).hexdigest()


def is_trusted(path: Path) -> bool:
    import hmac
    try:
        stored = _trusted().get(_key(path))
        return isinstance(stored, str) and hmac.compare_digest(stored, _seal(path))
    except OSError:
        return False


def trust(path: Path) -> None:
    data = _trusted()
    data[_key(path)] = _seal(path)
    target = _trust_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _say(text: str) -> None:
    """notify, който никога не прекъсва работата: съобщение с `[` в Rich
    хвърляше MarkupError, а повикващият го гълташе — и PreToolUse блокът
    изчезваше (одит 2026-10-08)."""
    try:
        notify(text)
    except Exception:
        log.debug("известието от куката не замина", exc_info=True)


def _parse(path: Path) -> list[Hook]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        if path.exists():
            _say(f"⚠ hooks: {path} не се чете ({e}) — пропуснат")
        return []
    table = data.get("hooks", data) if isinstance(data, dict) else {}
    out: list[Hook] = []
    for event in EVENTS:
        entries = table.get(event) if isinstance(table, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher") or "")
            inner = entry.get("hooks")
            commands = inner if isinstance(inner, list) else [entry]
            for c in commands:
                if isinstance(c, dict) and str(c.get("command") or "").strip():
                    # Таван ≥1 s и закръгляне нагоре, не int(): `timeout: 0.5`
                    # ставаше 0, а -1 оставаше -1 — communicate() хвърляше
                    # TimeoutExpired веднага, блокиращият hook се пропускаше и
                    # инструментът минаваше; `inf` срутваше четенето (одит
                    # 2026-10-22). Същият таван като stt.timeout_s във voice.py.
                    try:
                        raw = float(c.get("timeout") or entry.get("timeout") or _TIMEOUT)
                        timeout = max(1, math.ceil(raw))
                    except (TypeError, ValueError, OverflowError):
                        timeout = _TIMEOUT
                    out.append(Hook(event, matcher, str(c["command"]), timeout, path))
    return out


def configured(ws: Path | None = None) -> tuple[list[Hook], Path | None]:
    """The hooks in force, and the project file when it exists but is not trusted."""
    from genesis_agent.project_instructions import _project_root
    hooks = _parse(_home() / "hooks.json")
    project = project_file(ws)
    untrusted = None
    if project.is_file():
        root = _project_root(Path(ws or _ws()).resolve())
        if stays_in_project(project, root) and is_trusted(project):
            hooks += _parse(project)
        else:
            untrusted = project
    return hooks, untrusted


# Имената на инструментите в Claude Code (обратното на agents._CLAUDE_TOOLS).
# hooks.json, копиран оттам, е с matcher `Bash` или `Write|Edit` и не
# съвпадаше с нито един инструмент — hook-ът се пропускаше без дума и
# записът/командата минаваше (одит 2026-10-22).
_CLAUDE_NAMES: dict[str, tuple[str, ...]] = {
    "RUN_CMD": ("Bash",), "WRITE_FILE": ("Write",), "EDIT_FILE": ("Edit", "MultiEdit"),
    "READ_FILE": ("Read",), "SEARCH_CODE": ("Grep",), "GLOB": ("Glob",), "LIST_DIR": ("LS",),
    "WEB_FETCH": ("WebFetch",), "WEB_SEARCH": ("WebSearch",), "TODO_WRITE": ("TodoWrite",),
}


def _matches(hook: Hook, tool: str) -> bool:
    if not hook.matcher or hook.matcher in ("*", ".*"):
        return True
    for name in (tool, *_CLAUDE_NAMES.get(tool, ())):
        try:
            if re.fullmatch(hook.matcher, name) is not None:
                return True
        except re.error:
            if hook.matcher == name:
                return True
    return False


def _run(hook: Hook, payload: dict, env_extra: dict[str, str]) -> subprocess.CompletedProcess | None:
    """Runs the hook in its own process group: on a timeout the whole tree
    goes (subprocess.run killed only the shell — `sleep 77` lived on; on
    Windows it could hang in communicate() — audit 2026-10-08)."""
    from genesis_agent import sandbox
    env = dict(os.environ, GENESIS_WORKSPACE=str(_ws()), GENESIS_HOOK_EVENT=hook.event, **env_extra)
    try:
        proc = subprocess.Popen(
            hook.command, shell=True, cwd=str(_ws()), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", start_new_session=(os.name == "posix"),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as e:
        _say(f"⚠ hook ({hook.event}) не тръгна: {e}")
        return None
    try:
        out, err = proc.communicate(json.dumps(payload, ensure_ascii=False), timeout=hook.timeout)
    except subprocess.TimeoutExpired:
        sandbox.stop_process(proc)
        _say(f"⚠ hook ({hook.event}) спрян след {hook.timeout} s: {hook.command}")
        return None
    return subprocess.CompletedProcess(hook.command, proc.returncode, out or "", err or "")


# Ключовете, по които stdout на изход 0 е отговор във формата на Claude Code,
# а не текст за контекст (обикновен JSON без тях си остава контекст).
_JSON_KEYS = frozenset({"hookSpecificOutput", "decision", "continue", "reason", "stopReason",
                        "suppressOutput", "systemMessage"})


def _json_answer(event: str, stdout: str) -> Outcome | None:
    """Claude Code's JSON answer on exit 0, or None when stdout is plain text.

    Изход 0 с JSON е обичайният начин hook от Claude Code да откаже. Genesis
    гледаше само код 2 и лепеше JSON-а като контекст: PreToolUse
    `permissionDecision: deny` пускаше WRITE_FILE, UserPromptSubmit
    `decision: block` пращаше съобщението с тайната към модела, а Stop
    `decision: block` приключваше хода (одит 2026-10-22)."""
    text = stdout.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not _JSON_KEYS & data.keys():
        return None
    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    out = Outcome()
    permission = str(specific.get("permissionDecision") or "").lower()
    if permission in ("deny", "ask"):
        # „ask“: тук няма кого да пита преди инструмента — отказ, не тихо „да“.
        # „allow“ не заобикаля sandbox-а: той решава сам.
        out.blocked = True
        out.message = str(specific.get("permissionDecisionReason") or data.get("reason")
                          or ("hook поиска потвърждение" if permission == "ask" else ""))
    elif str(data.get("decision") or "").lower() == "block":
        out.blocked = True
        out.message = str(data.get("reason") or "")
    if data.get("continue") is False:
        # Stop: „спри“ значи, че ходът свършва (над decision: block); другаде — отказ.
        out.blocked = event != "Stop"
        if out.blocked and not out.message:
            out.message = str(data.get("stopReason") or "")
    extra = specific.get("additionalContext")
    if isinstance(extra, str):
        out.context = extra.strip()
    note = data.get("systemMessage")
    if isinstance(note, str) and note.strip():
        _say(f"hook ({event}): {note.strip()[:300]}")
    return out


def fire(event: str, payload: dict, tool: str = "", file: str = "") -> Outcome:
    hooks, _ = configured()
    result = Outcome()
    env = {"GENESIS_TOOL": tool, "GENESIS_FILE": file}
    # hook_event_name и cwd — ключовете от Claude Code, до тези на Genesis.
    full = dict(payload, hook_event=event, hook_event_name=event, cwd=str(_ws()))
    for hook in hooks:
        if hook.event != event or (tool and not _matches(hook, tool)):
            continue
        done = _run(hook, full, env)
        if done is None:
            continue
        if done.returncode == 2:
            result.blocked = True
            result.message += (done.stderr or done.stdout or "").strip()[:_MAX_OUT] + "\n"
        elif done.returncode == 0:
            answer = _json_answer(event, done.stdout)
            if answer is not None:
                if answer.blocked:
                    result.blocked = True
                    result.message += answer.message.strip()[:_MAX_OUT] + "\n"
                if answer.context:
                    result.context += answer.context[:_MAX_OUT] + "\n"
            elif done.stdout.strip():
                result.context += done.stdout.strip()[:_MAX_OUT] + "\n"
        else:
            _say(f"⚠ hook ({event}) върна {done.returncode}: "
                   f"{(done.stderr or done.stdout).strip()[:300]}")
    result.message, result.context = result.message.strip(), result.context.strip()
    return result


def _file_of(args: dict) -> str:
    for key in ("path", "file", "file_path"):
        if args.get(key):
            return str(args[key])
    return ""


def _tool_input(args: dict) -> dict:
    """The arguments as a hook sees them: the Genesis keys plus Claude Code's
    absolute `file_path`. Hook „пази .env“ от Claude Code чете
    `.tool_input.file_path`, виждаше само `path` и пускаше записа (одит
    2026-10-22). Копие — аргументите на самия инструмент не се пипат."""
    view = dict(args)
    path = _file_of(args)
    if path and "file_path" not in view:
        view["file_path"] = os.path.normpath(os.path.join(str(_ws()), path))
    return view


def pre_tool(tool: str, args: dict) -> str | None:
    """A refusal for the model when a PreToolUse hook blocks the tool."""
    out = fire("PreToolUse", {"tool_name": tool, "tool_input": _tool_input(args)}, tool, _file_of(args))
    if out.blocked:
        return f"[{tool}] ⛔ Спряно от hook на оператора:\n{out.message or '(без обяснение)'}"
    return None


def post_tool(tool: str, args: dict, result: str) -> str:
    out = fire("PostToolUse", {"tool_name": tool, "tool_input": _tool_input(args),
                               "tool_response": result[:_MAX_OUT]}, tool, _file_of(args))
    if out.blocked and out.message:
        return f"{result}\n[hook на оператора след {tool}]\n{out.message}"
    return result
