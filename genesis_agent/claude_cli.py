"""Claude през официалния `claude` CLI — с абонамента на оператора (2026-10-12).

Операторът има Claude абонамент и го ползва в Claude Code. Тук Genesis вика
немодифицирания `claude` в режим `-p` като чист текстов модел: без неговите
инструменти (`--tools ""`), без MCP, без CLAUDE.md/кукички (`--safe-mode`), с
нашия системен промпт. Инструментите остават на Genesis — текстовите тагове
(`[WRITE_FILE: …]`) минават през същия sandbox като всеки друг модел.

Правилата на Anthropic (code.claude.com/docs/en/legal-and-compliance, проверено
2026-10-12): входът с абонамент е за обикновена лична употреба на собствения
акаунт. Затова:
- само на машината на оператора, с неговия `/login`; облачната версия
  (cloud/) НЕ го ползва — за други хора е само API ключ;
- Genesis никога не чете, не копира и не пази токените на абонамента
  (`~/.claude/.credentials.json`, OAuth) — входът е изцяло на `claude`;
- `ANTHROPIC_API_KEY` се маха от средата на процеса: в `-p` режим ключът
  печели пред абонамента и щеше тихо да таксува ключа.

Измерено тук 2026-10-12 (claude 2.1.296): „Кажи само: да“ → „да“ за 2–4 s,
~1 240 токена върху нашия промпт (Claude Code добавя малко при `--system-prompt`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PROVIDER = "claude_cli"
MODELS = ["sonnet", "opus", "haiku"]
DEFAULT_MODEL = "sonnet"
# Дълъг ход с голям файл; отделно от общия таймаут на Brain (HTTP заявка).
TIMEOUT = int(os.environ.get("GENESIS_CLAUDE_CLI_TIMEOUT", "600") or 600)

# В `-p` режим ключът/токенът печели пред /login, а тези три пращат заявката
# към Bedrock/Vertex/Foundry (docs „authentication“, 2026-10-12). Махат се —
# иначе изборът „абонамент“ щеше да харчи по ключа.
_AUTH_OVERRIDES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                   "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                   "CLAUDE_CODE_USE_FOUNDRY")
# Ключовете на другите доставчици на Genesis не са работа на `claude`.
_OTHER_SECRET = re.compile(r"(?:_API_KEY|_TOKEN)(?:_\d+)?$")
_KEEP = {"CLAUDE_CODE_OAUTH_TOKEN"}   # `claude setup-token` на оператора — входът е негов

# Текстът в `result`, когато ходът не е минал (docs „errors“, 2026-10-12).
_LIMIT_RE = re.compile(r"hit your .*limit|usage limit|rate.?limit|limit · resets|"
                       r"credits_required|too many requests", re.IGNORECASE)
_AUTH_RE = re.compile(r"not logged in|/login|login expired|authentication|"
                      r"oauth token|invalid api key|\b401\b|\b403\b", re.IGNORECASE)
_BUSY_RE = re.compile(r"overloaded|\b529\b|\b503\b", re.IGNORECASE)

# Позиционният аргумент е на английски: под Windows `claude` често е .cmd и
# минава през cmd.exe, а кирилица в командния ред там се чупи (CLAUDE.md).
_INSTRUCTION = "Answer the conversation on stdin. Reply only with your next message."


def cli_path() -> str | None:
    """Пътят до `claude` (GENESIS_CLAUDE_CLI го надделява) или None."""
    explicit = (os.environ.get("GENESIS_CLAUDE_CLI") or "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    return shutil.which("claude")


def available() -> bool:
    return cli_path() is not None


def chosen() -> bool:
    """Избрал ли е операторът абонамента: при старта (GENESIS_START_PROVIDER)
    или от `/model` по време на сесията (терминалът задава GENESIS_CLAUDE_CLI_ACTIVE)."""
    return (os.environ.get("GENESIS_START_PROVIDER", "").strip() == PROVIDER
            or os.environ.get("GENESIS_CLAUDE_CLI_ACTIVE") == "1")


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Средата за `claude`: без ключа на Anthropic и без чуждите ключове."""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in _KEEP:
            continue
        if name in _AUTH_OVERRIDES or _OTHER_SECRET.search(name):
            env.pop(name, None)
    return env


_ROLE_LABEL = {"user": "Оператор", "assistant": "Genesis (ти)", "system": "Система",
               "tool": "Резултат от инструмент"}


def render(messages: list[dict]) -> tuple[str, str]:
    """(системен промпт, текст за stdin). Водещите system съобщения са промптът;
    останалото е разговорът като текст — `-p` приема един вход, а историята
    трябва да стигне цяла (без --resume: Genesis сам реже и свива историята)."""
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    leading = True
    for m in messages:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, list):   # блокове (OpenAI/Anthropic) → само текстът
            content = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
        text = str(content or "")
        if role == "system" and leading:
            if text:
                system.append(text)
            continue
        leading = False
        if role == "assistant" and m.get("tool_calls"):
            calls = "; ".join(f"{(tc.get('function') or {}).get('name')}"
                              f"({(tc.get('function') or {}).get('arguments', '')})"
                              for tc in m["tool_calls"])
            text = (text + f"\n[извикани инструменти: {calls}]").strip()
        if text:
            turns.append((role, text))
    if len(turns) == 1 and turns[0][0] == "user":
        return "\n\n".join(system), turns[0][1]
    body = "\n\n".join(f"=== {_ROLE_LABEL.get(r, r)} ===\n{t}" for r, t in turns)
    head = ("Това е разговорът досега. Ти си Genesis. Напиши САМО следващия си "
            "отговор — на последното съобщение — в същия формат (инструментите "
            "с техните тагове), без да повтаряш разговора.\n\n")
    return "\n\n".join(system), head + body


def _classify(text: str) -> str:
    if _LIMIT_RE.search(text):
        return f"HTTP_429: claude абонамент — лимитът е изчерпан: {text[:200]}"
    if _AUTH_RE.search(text):
        return (f"HTTP_401: claude не е влязъл — пусни `claude` и `/login` "
                f"(абонаментът): {text[:200]}")
    if _BUSY_RE.search(text):
        return f"HTTP_503: claude е претоварен: {text[:200]}"
    return f"HTTP_500: claude CLI: {text[:300]}"


def _parse(stdout: str) -> dict[str, Any] | None:
    """Последният ред, който е JSON обект (предупреждения отгоре не пречат)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def usage_of(data: dict[str, Any]) -> dict[str, int] | None:
    """В смисъла на OpenAI: prompt_tokens е С кеша (като _call_anthropic)."""
    u = data.get("usage")
    if not isinstance(u, dict):
        return None
    read = int(u.get("cache_read_input_tokens") or 0)
    write = int(u.get("cache_creation_input_tokens") or 0)
    return {"prompt_tokens": int(u.get("input_tokens") or 0) + read + write,
            "completion_tokens": int(u.get("output_tokens") or 0),
            "cached_read_tokens": read, "cached_write_tokens": write}


def complete(messages: list[dict], model: str = DEFAULT_MODEL,
             timeout: int | None = None) -> tuple[str, dict[str, int] | None]:
    """Един ход. (текст, usage) или RuntimeError с HTTP_* код, по който веригата
    на Brain решава: 429 → cooldown, 401 → ключът (тук входът) е мъртъв."""
    path = cli_path()
    if not path:
        raise RuntimeError("skip: няма `claude` CLI в PATH (инсталирай Claude Code и влез с /login)")
    system, prompt = render(messages)
    workdir = tempfile.mkdtemp(prefix="genesis-claude-")
    try:
        sp = Path(workdir) / "system.txt"
        sp.write_text(system or "Ти си Genesis, асистент за програмиране.", encoding="utf-8")
        argv = [path, "-p", _INSTRUCTION, "--safe-mode", "--tools", "",
                "--disallowedTools", "mcp__*", "--system-prompt-file", str(sp),
                "--no-session-persistence", "--max-turns", "1",
                "--output-format", "json", "--model", model or DEFAULT_MODEL]
        try:
            proc = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True,
                                  timeout=timeout or TIMEOUT, env=child_env(), cwd=workdir,
                                  check=False)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"HTTP_TIMEOUT: claude CLI не отговори за {e.timeout} s") from e
        except OSError as e:
            raise RuntimeError(f"skip: claude CLI не тръгва: {e}") from e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    data = _parse(out)
    if data is None:
        raise RuntimeError(_classify(err.strip() or out.strip() or f"rc={proc.returncode}"))
    if data.get("is_error") or data.get("subtype", "success") != "success":
        detail = data.get("result") or "; ".join(map(str, data.get("errors") or [])) \
            or str(data.get("subtype") or "грешка")
        raise RuntimeError(_classify(str(detail)))
    result = data.get("result")
    text = result if isinstance(result, str) else None
    if text is None:   # RuntimeError, не TypeError: веригата на Brain хваща само него
        raise RuntimeError("HTTP_500: claude CLI върна отговор без текст")
    _log_reply(text)
    return text, usage_of(data)


def _log_reply(text: str) -> None:
    """GENESIS_CLAUDE_CLI_LOG=<файл> — суровите отговори, по един JSON ред.
    Чатът показва отговора през Markdown, който може да скрие част от него
    (2026-10-12: провал на vat-check, при който от панела не личеше какво точно
    е написал моделът). Само при изрично зададен файл; нищо по подразбиране."""
    path = (os.environ.get("GENESIS_CLAUDE_CLI_LOG") or "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def auth_method(timeout: int = 20) -> str | None:
    """`claude auth status` → authMethod („claude.ai“ — абонамент, „api_key“ —
    ключ). None, когато CLI-то липсва или не отговори."""
    path = cli_path()
    if not path:
        return None
    try:
        proc = subprocess.run([path, "auth", "status"], capture_output=True, timeout=timeout,
                              env=child_env(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = _parse(proc.stdout.decode("utf-8", errors="replace"))
    if not data:
        return None
    if not data.get("loggedIn", True):
        return "none"
    return str(data.get("authMethod") or "")
