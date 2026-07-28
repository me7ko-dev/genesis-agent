"""
genesis_agent.agent_core — споделеното ядро зад ВСЕКИ пълноценен фронтенд (design note, 2026-07-27): терминал, Discord, GTK чат, и новия Jarvis гласов агент.

Извадено от genesis_agent/gui/genesis_gui.py (по-рано дефинирано САМО там) при строежа на
Jarvis фронтенда — иначе гласовото приложение трябваше да копира ~150 реда
tool-loop логика буквално, точно дублирането, което проектът изрично избягва
("не дублирай логика, фронтендите делят едно ядро" — виж genesis_gui.py
header comment). Сега GTK чатът И Jarvis викат СЪЩИЯ `run_tool_loop`.

Нула GTK/аудио зависимости тук — чист Python + genesis_agent ядрото, за да може
Jarvis да го internal-но ползва без да тегли GTK, и обратно.
"""
from __future__ import annotations

import json
import os
import re
import traceback
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOOL_ROUND_CAP = 8
MIN_SIZE_B = 32          # само модели ≥32B за интерактивен чат (като терминала)
COMPACT_THRESHOLD = 16
COMPACT_KEEP_RECENT = 10


def env_facts(workspace: str = "") -> str:
    """Реалните пътища на машината, инжектирани в системния промпт (design note, 2026-07-27).

    Открито при жив тест: помолен да сложи файл "на десктопа", моделът писа в
    измислена sandbox директория, която НЕ СЪЩЕСТВУВА. Нямаше откъде да
    знае home директорията на потребителя, затова я измисли (думата "sandbox" е
    навсякъде в промпта, вероятно оттам). Записът беше отказан, файлът се озова
    в repo-то вместо на десктопа.

    Това не е нещо, което моделът бива да отгатва или да пита за него — то е
    фиксиран факт за машината, който струва ~60 токена. Пътищата за папките се
    четат от XDG (user-dirs.dirs), защото на локализирана система "Desktop"
    може да е "Работен плот" — жестоко закованото `~/Desktop` би било грешно.
    """
    home = Path.home()
    dirs = {"DESKTOP": home / "Desktop", "DOWNLOAD": home / "Downloads",
            "DOCUMENTS": home / "Documents", "PICTURES": home / "Pictures",
            "MUSIC": home / "Music", "VIDEOS": home / "Videos"}
    try:
        cfg = home / ".config" / "user-dirs.dirs"
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'\s*XDG_(\w+)_DIR\s*=\s*"(.+)"\s*$', line)
            if m and m.group(1) in dirs:
                dirs[m.group(1)] = Path(m.group(2).replace("$HOME", str(home)))
    except OSError:
        pass  # без XDG конфигурация остават разумните подразбирания

    lines = [f"- Домашна директория: {home}   (потребител: {os.environ.get('USER', 'unknown')})"]
    for label, key in (("Десктоп", "DESKTOP"), ("Изтегляния", "DOWNLOAD"),
                       ("Документи", "DOCUMENTS"), ("Снимки", "PICTURES")):
        p = dirs[key]
        lines.append(f"- {label}: {p}" + ("" if p.is_dir() else "   (НЕ съществува)"))
    if workspace:
        lines.append(f"- Работна директория (относителните пътища са спрямо нея): {workspace}")
    return ("## СРЕДАТА (реални пътища на тази машина — НЕ ги отгатвай)\n"
            + "\n".join(lines)
            + "\nАко ти трябва път извън изброените, провери с LIST_DIR преди да пишеш в него.")


class Core:
    """Зарежда конфигурация + мозък + памет. Мек внос: липсваща зависимост
    връща error вместо traceback в невидим терминал/systemd лог."""

    def __init__(self) -> None:
        self.ok = False
        self.error = ""
        self.system_prompt = ""
        self.briefing = ""
        self.tools = None
        self.provider = ""
        self.model = ""
        self.pin_model: tuple[str, str] | None = None
        try:
            import yaml

            from genesis_agent.paths import CONFIG_PATH as cfg_path
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            self.cfg = cfg

            for envf in cfg.get("env_files", []):
                self._load_env(Path(envf).expanduser())

            import sys
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            import genesis_skills

            self.skills = genesis_skills
            workspace = cfg.get("workspace", {}).get("path") or str(PROJECT_ROOT)
            genesis_skills.set_workspace(workspace)
            self.workspace = workspace

            from genesis_agent.tool_schemas import FULL_TOOLS

            self.tools = FULL_TOOLS
            self.system_prompt = cfg.get(
                "system_prompt", "You are Genesis, an autonomous AI coding agent."
            )
            self.system_prompt += "\n\n" + env_facts(workspace)
            self.provider = cfg.get("models", {}).get("default_provider", "")
            self.model = ""

            try:
                from genesis_agent import workspace_memory as wm

                self.wm = wm
                self.briefing = wm.briefing() or ""
                if self.briefing:
                    self.system_prompt += (
                        "\n\n## СЪСТОЯНИЕ НА РАБОТАТА (от предишни сесии)\n" + self.briefing
                    )
            except Exception:
                self.wm = None  # type: ignore[assignment]

            try:
                from genesis_agent import conversation_memory as cm

                self.conv_mem = cm
            except Exception:
                self.conv_mem = None  # type: ignore[assignment]

            self.ok = True
        except Exception:
            self.error = traceback.format_exc()

    @staticmethod
    def _load_env(path: Path) -> None:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
        except OSError:
            pass

    def remember(self, role: str, content: str) -> None:
        if not self.conv_mem or not content:
            return
        try:
            self.conv_mem.add_message(role, content)
        except Exception:
            pass

    def complete(self, messages: list) -> tuple:
        """Едно обръщение към мозъка. Връща (text, tool_calls, provider, model)."""
        from genesis_agent.brain import Brain

        brain = Brain(min_size_b=MIN_SIZE_B, pin_model=self.pin_model)
        reply = brain.complete(list(messages), tools=self.tools)
        cur = brain.current or {}
        return (
            reply.raw_text or "",
            getattr(reply, "tool_calls", None),
            cur.get("provider", ""),
            cur.get("model", ""),
        )

    def available_models(self) -> list[dict]:
        """Моделите в текущата верига (за model-picker UI). Не мери latency/
        не вика мрежата — само чете config.yaml през Brain-овия chain builder,
        филтрирано през същия MIN_SIZE_B праг като реалните обаждания, за да
        не предлага в picker-а модел, който complete() никога не би избрал."""
        from genesis_agent.brain import Brain

        brain = Brain(min_size_b=MIN_SIZE_B)
        return [{"provider": c["provider"], "model": c["model"],
                 "size_b": c.get("size_b", 0)} for c in brain.chain]


def _diff_for_write(skills, args: dict) -> Optional[str]:
    """Unified diff за предстоящ WRITE_FILE (design note, 2026-07-28) — четем
    СТАРОТО съдържание ПРЕДИ dispatch_tool_call презапише файла, за да могат
    фронтендите да покажат какво реално се променя, вместо голия
    "✓ записани N символа" статус, който `_tool_write_file` връща на модела.
    Използва `skills._resolve()` (същата path-логика като реалния запис,
    вкл. workspace-relative разрешаване) — да няма разминаване между това
    какво показваме и какво реално се пише. Best-effort: никога не хвърля,
    UI преглед не бива да чупи истинския tool loop."""
    path = args.get("path")
    new_content = args.get("content")
    if not path or new_content is None:
        return None
    try:
        import difflib

        target = skills._resolve(path)
        old_content = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if old_content == new_content:
            return None
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )
        return "".join(diff) or None
    except Exception:
        return None


def _is_question(result: str) -> bool:
    """Разпознава резултат от ASK_USER. Маркерът се внася лениво, за да остане
    agent_core вносим и когато мостът genesis_skills липсва (тестове)."""
    try:
        from genesis_skills import ASK_USER_MARKER
    except Exception:
        ASK_USER_MARKER = "__GENESIS_ASK_USER__"
    return bool(result) and ASK_USER_MARKER in result


def _clean_question(result: str) -> str:
    """Маха вътрешния маркер, преди въпросът да се покаже/изговори."""
    try:
        from genesis_skills import ASK_USER_MARKER
    except Exception:
        ASK_USER_MARKER = "__GENESIS_ASK_USER__"
    return result.replace(ASK_USER_MARKER, "").strip()


def run_tool_loop(
    core: Core,
    messages: list,
    *,
    on_assistant: Callable[[str, str, str], None],
    on_tool_result: Callable[[str, str, Optional[str]], None],
    on_status: Optional[Callable[[str], None]] = None,
    round_cap: int = TOOL_ROUND_CAP,
) -> list:
    """Пълният агентен цикъл: complete → (native tool_calls | текстови тагове)
    → изпълни → повтори, докато моделът спре да вика инструменти или се удари
    в тавана. После компресия (Brain.compact_chat_history) + auto_capture на
    предкомпресираната история — същата логика, която терминалът и GTK чатът
    вече ползват поотделно (сега само тук, веднъж).

    on_assistant(text, provider, model)  — извиква се на всеки текстов отговор.
    on_tool_result(name, result, extra)  — извиква се на всеки изпълнен tool.
        `extra` е `None` освен за WRITE_FILE през native tool-calling, където
        носи unified diff текст (design note, 2026-07-28) — фронтенди, които
        не го ползват (Jarvis — говори резултата, не показва diff), просто
        игнорират третия аргумент.
    on_status(text)                     — по избор, кратки статус съобщения.

    Хвърля само ако Core.complete() хвърли; извикващият решава как да покаже
    грешка (всеки фронтенд има собствен error-widget/глас).
    """
    _status = on_status or (lambda _s: None)
    rounds = 0

    text, tool_calls, prov, model = core.complete(messages)
    while True:
        if prov:
            _status(f"{prov} · {model}")
        if text.strip():
            on_assistant(text, prov, model)
        msg: dict = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        messages.append(msg)
        core.remember("assistant", text or f"[повикани {len(tool_calls or [])} tool(-а)]")

        if tool_calls:
            _status("изпълнява инструменти…")
            asked = ""
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                diff = _diff_for_write(core.skills, args) if name == "WRITE_FILE" else None
                result = core.skills.dispatch_tool_call(name, args)
                on_tool_result(name, result, diff)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": name, "content": result})
                if _is_question(result):
                    asked = result
            if asked:
                # Агентът е попитал → цикълът СПИРА и контролът се връща на
                # човека. Без това ASK_USER би бил безсмислен: моделът пита и
                # веднага сам продължава да гадае, точно поведението, което
                # инструментът съществува да спре.
                on_assistant(_clean_question(asked), prov, model)
                _status("чака отговор")
                break
            rounds += 1
            if rounds >= round_cap:
                on_assistant(f"Достигнат таван от {round_cap} инструмент-рунда. "
                             "Продължи с ново съобщение.", "", "")
                break
            _status("анализира…")
            text, tool_calls, prov, model = core.complete(messages)
            continue

        # Текстови тагове — за модели без native tool-calling в тази ротация.
        results = core.skills.parse_and_execute_tools(text)
        if not results:
            break
        for r in results:
            on_tool_result("инструмент", r, None)
        asked = next((r for r in results if _is_question(r)), "")
        if asked:
            on_assistant(_clean_question(asked), prov, model)
            _status("чака отговор")
            break
        rounds += 1
        if rounds >= round_cap:
            break
        messages.append({
            "role": "system",
            "content": "[Резултат]:\n" + "\n\n".join(results) +
                       "\n\nАко това вече изпълнява заявката напълно — дай КРАТКО "
                       "финално обобщение БЕЗ нови tool тагове. Викай нов tool САМО "
                       "ако наистина има следваща реална стъпка. Ако команда е отказана "
                       "(SANDBOX DECLINED), но друг резултат вече доказва целта — не "
                       "настоявай за отказаната.",
        })
        text, tool_calls, prov, model = core.complete(messages)

    before_len = len(messages)
    pre_compact = [m for m in messages if m.get("role") in ("user", "assistant")]
    from genesis_agent.brain import Brain

    messages = Brain.compact_chat_history(
        messages, threshold=COMPACT_THRESHOLD, keep_recent=COMPACT_KEEP_RECENT
    )
    if len(messages) < before_len and core.wm:
        try:
            core.wm.auto_capture(pre_compact)
        except Exception:
            pass
        _status(f"история компресирана ({before_len} → {len(messages)})")

    return messages
