"""
genesis_agent.permissions — commands the operator has already approved, so a
risky (CONFIRM) command is not asked about again (Claude Code's permissions).

    session  — "да, за тази сесия" in the confirmation prompt: THIS exact
               command, in memory only, until /clear or the end of the process.
    project  — `/permissions add npm test` (or `pytest *`): kept in
               ~/.genesis/permissions.json per project root, each entry sealed
               with the same key as `/hooks trust` (hooks.trust_key). Nothing
               in the repository is read: a cloned project cannot grant itself
               a rule, and a file the model wrote cannot either (no seal).

A rule matches a WHOLE command made only of plain words — letters, digits and
`_ . , / : = + - @`. Anything a shell would interpret (`; & | < > ( ) $` and backticks,
quotes, globs, `~`, `%`, `^`, `!`, `#`, newlines, non-ASCII) means "not
plain": the command goes through the normal gate and is asked about. So
`git status` never covers `git status; rm -rf x`, `git status && curl …`,
`git status > f` or `FOO=1 git status`. `pytest *` covers `pytest` followed
by any number of plain words.

These rules only skip the QUESTION. sandbox._decide refuses BLOCKED before it
looks at them, and secrets / Genesis control files are always asked about.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
from pathlib import Path

workspace: Path | None = None

# Само ASCII и само знаци, които нито bash, нито cmd.exe, нито PowerShell
# тълкуват в обикновена дума (2026-10-15). Без re.IGNORECASE: с него знакът
# за келвин (U+212A) съвпада с `[a-z]`.
_PLAIN_TEXT = re.compile(r"[A-Za-z0-9_.,/:=+@\- \t]+")
_WORD = re.compile(r"[A-Za-z0-9_.,/:=+\-][A-Za-z0-9_.,/:=+@\-]*")
_SPLIT = re.compile(r"[ \t]+")
_MAX_RULE = 500

# Обвивки, шелове и интерпретатори: `bash *` или `sudo *` пускат всичко —
# такова правило с `*` не се приема (2026-10-15). Точното `sudo apt update` —
# може: то е една команда, видяна цялата.
_LAUNCHERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox",
    "cmd", "powershell", "pwsh", "wsl", "start", "call", "runas",
    "sudo", "doas", "su", "env", "xargs", "eval", "exec", "source", ".",
    "nohup", "time", "timeout", "nice", "ionice", "command", "builtin",
    "setsid", "stdbuf", "watch", "ssh", "chroot", "unshare", "script",
})
_INTERPRETERS = re.compile(r"^(python[\d.]*|py|pypy[\d.]*|perl|ruby|node|deno|bun|php|lua|osascript)$")
# Флагът веднага след интерпретатора, който значи „кодът е тук“ (или „пусни
# какъвто и да е модул/скрипт“): `python -c *`, `node -e *`, `deno run *`.
_INLINE_FLAGS = frozenset({"-c", "-e", "-E", "--eval", "-p", "--print", "-r", "run", "eval", "-m"})

# Причини, които никое правило не покрива: тайни и управляващите файлове на
# Genesis (hooks/MCP/доверие/тези правила). `cat *` иначе четеше ключа, с
# който се подписва доверието, а `cp *` пишеше ~/.genesis/hooks.json —
# програма извън sandbox-а с целия os.environ.
_NEVER_COVERED = ("достъп до ", "промяна на управляващ файл на Genesis")

_LOCK = threading.Lock()
_SESSION: dict[str, set[str]] = {}


# ── думите на командата ─────────────────────────────────────────────────────

def plain_words(command: str) -> list[str] | None:
    """The words of a command a shell runs exactly as written, or None.

    Only spaces and tabs separate words; a newline or CR inside the command
    (a second command in every shell) makes it not plain. A CRLF at the very
    end (Windows) is ignored."""
    text = command.strip(" \t\r\n")
    if not text or len(text) > _MAX_RULE or not _PLAIN_TEXT.fullmatch(text):
        return None
    words = _SPLIT.split(text)
    if not all(_WORD.fullmatch(w) for w in words):
        return None
    if "=" in words[0]:            # `FOO=1 git status` — задаване на променлива
        return None
    return words


def _base(word: str) -> str:
    name = word.rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def normalize_rule(text: str) -> tuple[str | None, str]:
    """(the rule as stored, "") or (None, why it is refused)."""
    raw = text.strip(" \t\r\n")
    if not raw:
        return None, "празно правило"
    if "\n" in raw or "\r" in raw:
        return None, "правилото е на един ред"
    parts = _SPLIT.split(raw)
    prefix = parts[-1] == "*"
    fixed = parts[:-1] if prefix else parts
    if not fixed:
        return None, "само „*“ би разрешило всичко"
    words = plain_words(" ".join(fixed))
    if words is None:
        return None, ("само прости думи (букви, цифри, _ . , / : = + - @); „*“ — "
                      "само сам и последен; без ; & | < > $ ` кавички, ~ и %")
    if prefix:
        first = _base(words[0])
        if first in _LAUNCHERS:
            return None, f"„{words[0]} *“ би пуснало всяка команда"
        if _INTERPRETERS.match(first) and (len(words) == 1 or (len(words) == 2 and words[1] in _INLINE_FLAGS)
                                           or words[1] in _INLINE_FLAGS - {"-m", "run"}):
            return None, f"„{' '.join(words)} *“ би пуснало всеки код"
    return " ".join(words) + (" *" if prefix else ""), ""


def matches(command: str, rule: str) -> bool:
    words = plain_words(command)
    if words is None:
        return False
    parts = rule.split(" ")
    if parts[-1] == "*":
        fixed = parts[:-1]
        return len(words) >= len(fixed) and words[:len(fixed)] == fixed
    return words == parts


def coverable(reasons: list[str]) -> bool:
    return not any(r.startswith(_NEVER_COVERED) for r in reasons)


# ── проектът ────────────────────────────────────────────────────────────────

def _ws() -> Path:
    if workspace is not None:
        return Path(workspace)
    skills = sys.modules.get("genesis_skills")
    ws = getattr(skills, "_WORKSPACE", None)
    return Path(ws) if ws else Path.cwd()


def project_key(ws: Path | None = None) -> str:
    from genesis_agent.project_instructions import _project_root
    return str(_project_root(Path(ws or _ws()).resolve()))


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


def store_file() -> Path:
    return _home() / "permissions.json"


def _seal(key: str, rules: list[str]) -> str:
    from genesis_agent import hooks
    message = ("permissions\0" + key + "\0" + "\n".join(rules)).encode()
    return hmac.new(hooks.trust_key(), message, hashlib.sha256).hexdigest()


def _read_store() -> dict:
    try:
        data = json.loads(store_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return projects if isinstance(projects, dict) else {}


def _verified(entry: object, key: str) -> tuple[list[str], bool]:
    """(the rules, whether the entry was there but its seal did not match)."""
    if not isinstance(entry, dict):
        return [], entry is not None
    rules = entry.get("allow")
    seal = entry.get("seal")
    if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules) \
            or not isinstance(seal, str):
        return [], True
    if not hmac.compare_digest(seal, _seal(key, rules)):
        return [], True
    return [r for r in rules if normalize_rule(r)[0] == r], False


def project_rules(ws: Path | None = None) -> list[str]:
    key = project_key(ws)
    return _verified(_read_store().get(key), key)[0]


def tampered(ws: Path | None = None) -> bool:
    key = project_key(ws)
    return _verified(_read_store().get(key), key)[1]


def _save(key: str, rules: list[str]) -> None:
    projects = _read_store()
    if rules:
        projects[key] = {"allow": rules, "seal": _seal(key, rules)}
    else:
        projects.pop(key, None)
    target = store_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"projects": projects}, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def add(rule: str, ws: Path | None = None) -> tuple[str | None, str]:
    norm, why = normalize_rule(rule)
    if norm is None:
        return None, why
    # Правило за BLOCKED команда не би пуснало нищо (_decide я отказва преди
    # правилата) — казва се веднага, а не се пази като лъжливо обещание.
    from genesis_agent import sandbox
    exact = norm.removesuffix(" *")
    if sandbox.assess_command(exact).level == sandbox.RiskLevel.BLOCKED:
        return None, "тази команда е BLOCKED — отказва се винаги, правило не я разрешава"
    key = project_key(ws)
    with _LOCK:
        rules = project_rules(ws)
        if norm not in rules:
            _save(key, [*rules, norm])
    return norm, ""


def remove(rule: str, ws: Path | None = None) -> bool:
    """Forgets the rule — from the project and from this session."""
    text = " ".join(_SPLIT.split(rule.strip(" \t\r\n")))
    key = project_key(ws)
    gone = False
    with _LOCK:
        rules = project_rules(ws)
        if text in rules:
            _save(key, [r for r in rules if r != text])
            gone = True
        session = _SESSION.get(key)
        if session and text in session:
            session.discard(text)
            gone = True
    return gone


# ── сесията ─────────────────────────────────────────────────────────────────

def session_candidate(command: str, reasons: list[str]) -> str | None:
    """What "да, за тази сесия" would remember for this command, or None when
    it cannot be remembered (not plain, or touches secrets / control files)."""
    if not coverable(reasons):
        return None
    words = plain_words(command)
    return " ".join(words) if words else None


def remember(rule: str, ws: Path | None = None) -> None:
    norm, _ = normalize_rule(rule)
    if norm is None:
        return
    with _LOCK:
        _SESSION.setdefault(project_key(ws), set()).add(norm)


def session_rules(ws: Path | None = None) -> list[str]:
    with _LOCK:
        return sorted(_SESSION.get(project_key(ws), set()))


def clear_session() -> None:
    with _LOCK:
        _SESSION.clear()


# ── решението ───────────────────────────────────────────────────────────────

def allowed_by(command: str, reasons: list[str], ws: Path | None = None) -> str | None:
    """The rule that lets this CONFIRM command run without asking, or None."""
    if not coverable(reasons) or plain_words(command) is None:
        return None
    for rule in [*session_rules(ws), *project_rules(ws)]:
        if matches(command, rule):
            return rule
    return None
