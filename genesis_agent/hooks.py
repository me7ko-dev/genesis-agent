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
every tool). The command gets a JSON description on stdin and GENESIS_TOOL,
GENESIS_FILE, GENESIS_WORKSPACE in its environment, runs in the workspace,
for at most `timeout` seconds (default 60).

Exit code 2 is the one that matters, as in Claude Code:
  PreToolUse        → the tool does not run; stderr goes to the model.
  PostToolUse       → stderr is added to the tool's result for the model.
  UserPromptSubmit  → the message is not sent; stderr is shown. Exit 0 with
                      stdout → that text is added to the message as context.
  Stop              → the turn goes on: stderr goes to the model as the next
                      message (at most twice in a row).
Any other code: the hook failed, the work goes on, the operator sees it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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


def is_trusted(path: Path) -> bool:
    try:
        return _trusted().get(_key(path)) == _digest(path)
    except OSError:
        return False


def trust(path: Path) -> None:
    data = _trusted()
    data[_key(path)] = _digest(path)
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
        pass


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
                    try:
                        timeout = int(c.get("timeout") or entry.get("timeout") or _TIMEOUT)
                    except (TypeError, ValueError):
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


def _matches(hook: Hook, tool: str) -> bool:
    if not hook.matcher or hook.matcher in ("*", ".*"):
        return True
    try:
        return re.fullmatch(hook.matcher, tool) is not None
    except re.error:
        return hook.matcher == tool


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


def fire(event: str, payload: dict, tool: str = "", file: str = "") -> Outcome:
    hooks, _ = configured()
    result = Outcome()
    env = {"GENESIS_TOOL": tool, "GENESIS_FILE": file}
    for hook in hooks:
        if hook.event != event or (tool and not _matches(hook, tool)):
            continue
        done = _run(hook, dict(payload, hook_event=event), env)
        if done is None:
            continue
        if done.returncode == 2:
            result.blocked = True
            result.message += (done.stderr or done.stdout or "").strip()[:_MAX_OUT] + "\n"
        elif done.returncode == 0:
            if done.stdout.strip():
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


def pre_tool(tool: str, args: dict) -> str | None:
    """A refusal for the model when a PreToolUse hook blocks the tool."""
    out = fire("PreToolUse", {"tool_name": tool, "tool_input": args}, tool, _file_of(args))
    if out.blocked:
        return f"[{tool}] ⛔ Спряно от hook на оператора:\n{out.message or '(без обяснение)'}"
    return None


def post_tool(tool: str, args: dict, result: str) -> str:
    out = fire("PostToolUse", {"tool_name": tool, "tool_input": args,
                               "tool_response": result[:_MAX_OUT]}, tool, _file_of(args))
    if out.blocked and out.message:
        return f"{result}\n[hook на оператора след {tool}]\n{out.message}"
    return result
