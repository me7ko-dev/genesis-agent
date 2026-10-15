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

# Командният ред е само ASCII: под Windows `claude` често е .cmd и минава през
# cmd.exe, а кирилица там се чупи (CLAUDE.md). Текстът идва през stdin.


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


# `claude` добавя към нашия промпт и своята среда: работна папка (временната
# genesis-claude-…), scratchpad, git, прокси. Мерено 2026-10-12, bench с
# абонамента: 2 от 3 пуска на orders-api/inventory-pkg губят ход заради нея —
# LIST_DIR на /tmp/genesis-claude-…, и WRITE_FILE в scratchpad-а на claude
# извън проекта (sandbox-ът го отказва, следващият RUN_CMD няма файла).
HARNESS_NOTE = ("\n\n[Среда] Работната папка, scratchpad-ът, git и другите сведения "
                "за средата, които `claude` добавя, НЕ са твоите: там нищо не се "
                "чете и не се пише. Твоят проект е работната папка на Genesis от "
                "този промпт; файловете се пишат само там, с таговете на Genesis.")

_ROLE_LABEL = {"user": "Оператор", "assistant": "Genesis (ти)", "system": "Система",
               "tool": "Резултат от инструмент"}


def render_blocks(messages: list[dict]) -> tuple[str, list[str]]:
    """(системен промпт, блоковете за stdin). Водещите system съобщения са
    промптът; останалото е разговорът — по един текстов блок на съобщение.
    Без --resume: Genesis сам реже и свива историята.

    По блок, не един общ текст (мерено 2026-10-12, claude 2.1.296): кешът на
    Anthropic търси съвпадение само по граници на блокове. С един текст всеки
    ход записваше целия разговор в кеша наново и не четеше нищо от него
    (orders-api: 20–30K записани на ход, прочетен само системният промпт); с
    блокове вторият ход чете 17 646 и записва 6 548 вместо 24 203."""
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
        return "\n\n".join(system), [turns[0][1]]
    head = ("Това е разговорът досега. Ти си Genesis. Напиши САМО следващия си "
            "отговор — на последното съобщение — в същия формат (инструментите "
            "с техните тагове), без да повтаряш разговора.")
    return "\n\n".join(system), [head] + [f"=== {_ROLE_LABEL.get(r, r)} ===\n{t}"
                                           for r, t in turns]


def render(messages: list[dict]) -> tuple[str, str]:
    """(системен промпт, разговорът като един текст) — за четене и тестове."""
    system, blocks = render_blocks(messages)
    return system, "\n\n".join(blocks)


def stdin_message(blocks: list[str]) -> bytes:
    """Едно потребителско съобщение във формата stream-json, по блок на ход.
    ensure_ascii: под Windows stdin минава и през .cmd обвивката."""
    content = [{"type": "text", "text": b} for b in blocks if b] or [{"type": "text", "text": "."}]
    return (json.dumps({"type": "user", "message": {"role": "user", "content": content}})
            + "\n").encode("ascii")


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
    """Редът с резултата (`"type": "result"`). В stream-json преди него идват
    init и самото съобщение — те не са отговорът; предупреждения не пречат."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("type") == "result":
                return data
    return None


def usage_of(data: dict[str, Any]) -> dict[str, Any] | None:
    """В смисъла на OpenAI: prompt_tokens е С кеша (като _call_anthropic).
    `cost_usd` е total_cost_usd на самия `claude` — с абонамента е ориентир
    (колко би струвало по API), не сметка; `genesis -p --output-format json`
    го показва, щом го има (2026-10-15)."""
    u = data.get("usage")
    if not isinstance(u, dict):
        return None
    read = int(u.get("cache_read_input_tokens") or 0)
    write = int(u.get("cache_creation_input_tokens") or 0)
    out: dict[str, Any] = {"prompt_tokens": int(u.get("input_tokens") or 0) + read + write,
                           "completion_tokens": int(u.get("output_tokens") or 0),
                           "cached_read_tokens": read, "cached_write_tokens": write}
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        out["cost_usd"] = float(cost)
    return out


def complete(messages: list[dict], model: str = DEFAULT_MODEL,
             timeout: int | None = None) -> tuple[str, dict[str, Any] | None]:
    """Един ход. (текст, usage) или RuntimeError с HTTP_* код, по който веригата
    на Brain решава: 429 → cooldown, 401 → ключът (тук входът) е мъртъв."""
    path = cli_path()
    if not path:
        raise RuntimeError("skip: няма `claude` CLI в PATH (инсталирай Claude Code и влез с /login)")
    system, blocks = render_blocks(messages)
    workdir = tempfile.mkdtemp(prefix="genesis-claude-")
    try:
        sp = Path(workdir) / "system.txt"
        sp.write_text((system or "Ти си Genesis, асистент за програмиране.") + HARNESS_NOTE,
                      encoding="utf-8")
        # stream-json на входа иска stream-json и --verbose на изхода (claude 2.1.296).
        argv = [path, "-p", "--safe-mode", "--tools", "",
                "--disallowedTools", "mcp__*", "--system-prompt-file", str(sp),
                "--no-session-persistence", "--max-turns", "1",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--model", model or DEFAULT_MODEL]
        try:
            proc = subprocess.run(argv, input=stdin_message(blocks), capture_output=True,
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
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("loggedIn", True):
        return "none"
    return str(data.get("authMethod") or "")
