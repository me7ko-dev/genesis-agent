#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent/discord_bot.py — ДВУПОСОЧЕН Discord чат с Genesis Agent.

Досега имаше само webhook (Genesis → Discord, еднопосочно известяване).
Този модул добавя истински РАЗГОВОР: пишеш в Discord, Genesis отговаря през
собствения си мозък (genesis_agent.brain.Brain — локален модел пръв, после облак).

Разлика webhook vs бот:
  • Webhook = само ИЗПРАЩА (notifier.py го ползва за известия). Не може да чете.
  • Бот     = свързва се към Discord gateway, ЧЕТЕ съобщения и ОТГОВАРЯ.

Нужен е BOT TOKEN (различен от webhook-а). Виж setup долу.

── ЕДНОКРАТНА НАСТРОЙКА ──────────────────────────────────────────────────────
1. https://discord.com/developers/applications → New Application → име "Genesis".
2. Ляво меню → Bot → Add Bot → Reset Token → копирай токена.
3. Пак в Bot: включи "MESSAGE CONTENT INTENT" (задължително, за да чете текста).
4. Ляво меню → OAuth2 → URL Generator → scopes: bot; permissions: Send Messages,
   Read Message History → отвори генерирания URL → покани бота в своя сървър.
5. Добави в ~/.genesis/.env:
       GENESIS_DISCORD_BOT_TOKEN=твоя_токен_тук
6. ЗАДЪЛЖИТЕЛНО добави и:
       GENESIS_DISCORD_OWNER_ID=твоя_Discord_user_ID
   (Discord → Settings → Advanced → Developer Mode → десен клик на своя
   профил → Copy User ID). БЕЗ това ботът не отговаря на НИКОГО — това е
   нарочно: бот, който изпълнява команди на твоята машина, не бива да
   слуша непознати. С него само ти получаваш отговори И реални инструменти
   (RUN_CMD/WRITE_FILE/USE_SKILL/DELEGATE/browser).
7. Пусни: python3 -m genesis_agent.discord_bot   (или Desktop → Genesis-Discord)

── УПОТРЕБА В DISCORD ────────────────────────────────────────────────────────
  • В ЛИЧНИ съобщения (DM) към бота — отговаря на всичко.
  • В КАНАЛ на сървър — отговаря само когато го споменеш (@Genesis ...) или
    когато съобщението започва с "genesis " / "!g ".
  • !mission <цел>  — пуска реална автономна мисия (Brain→sandbox→verified),
    връща резултата в канала (работи на заден план, не блокира чата).
  • !start24_7      — стартира непрекъснат автономен цикъл (goal_engine +
    autonomous_loop) на заден план, докато не спреш с !stop.
  • !stop           — реален kill-switch (genesis_agent.config.stop_event) — спира
    24/7 цикъла И всяка текуща !mission (глобален stop, не scoped).
  • !status247      — статус на 24/7 цикъла (мисии, успехи, uptime).
  • !budget         — token/API разход днес и последните 7 дни (genesis_agent.budget).
  • !help           — кратка помощ.

── 24/7 РЕЖИМ — ВАЖНО ────────────────────────────────────────────────────────
!start24_7 НЕ тръгва автоматично при boot дори ако ботът е systemd service —
изисква изрична команда всеки път. "Ботът е достъпен" ≠ "автономни мисии без
надзор" — второто е по-голямо решение. При стартиране цикълът задава sandbox
policy "deny" (безопасен default за unattended работа — CONFIRM операции се
отказват автоматично, само SAFE минават). goal_engine.next_goals() има КРАЕН
pool (~24 теми) — цикълът ще го изчерпи след ~24 мисии и ще идле грациозно,
не генерира безкрайно нови цели (познато ограничение, не е бъг тук).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import Any, Deque, Dict, List

try:
    import discord
except ImportError:  # pragma: no cover
    raise SystemExit(
        "discord.py липсва. Инсталирай: pip install -U discord.py"
    )

# ─── Конфигурация ─────────────────────────────────────────────────────────────
from genesis_agent.paths import ENV_FILES as _ENV_FILES, _strip_inline_comment
_CONFIG_YAML = Path(__file__).resolve().parent.parent / "config.yaml"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Колко реплики контекст да помни ботът за всеки канал (потребител↔Genesis).
# Намалено от 12 — всяко следващо съобщение плаща за ЦЯЛАТА история кумулативно,
# по-къса памет пести токени в дълги разговори без съществена загуба.
_HISTORY_TURNS = 6
# Разговорни префикси, на които ботът реагира в канал (извън DM/споменаване).
_PREFIXES = ("genesis ", "!g ")
# Максимум символи на едно Discord съобщение.
_DISCORD_LIMIT = 2000

# Разговорният чат в Discord е за ОБЩИ теми, не за код — там качеството на
# българския език тежи повече от скоростта/размера. Тествано наживо: малките
# модели (локалният 3b coder И общ 3b) пишат счупен български ("Мен е Genesis"
# вместо "Аз съм"). затова минимумът за Discord е 17B.
#
# По-рано чатът беше закован към ЕДИН модел (Llama-3.3-70B/HF) — единична
# точка на провал: при временен rate-limit тестът наживо даде лош
# български, защото каскадата падаше върху Qwen2.5-Coder-32B (следващият в
# HF списъка) — coder-специализиран модел, забележимо по-слаб в прозаичен
# текст от общите инструкт модели. Затова сега: филтър ≥17B (config.yaml
# size_b) ПЛЮС изричното изключване на "-Coder-" моделите от чат пула —
# нормалната ротация на Brain върши останалото (никой единичен провал не
# може да "залепи" разговора за слаб модел).
_CHAT_MIN_SIZE_B = 17

try:
    import genesis_skills
    from genesis_agent.tool_schemas import FULL_TOOLS as _DISCORD_TOOL_SCHEMAS
except Exception:  # pragma: no cover
    genesis_skills = None  # type: ignore
    _DISCORD_TOOL_SCHEMAS = None  # type: ignore[assignment]

_TOOL_ROUND_CAP = 6


def _chat_brain():
    """Brain, филтриран до ≥17B общи (не coder-специализирани) модели за чат."""
    from genesis_agent.brain import Brain

    b = Brain(use_local=False, min_size_b=_CHAT_MIN_SIZE_B)
    b.chain = [c for c in b.chain if "coder" not in c["model"].lower()]
    b.current = b.chain[0] if b.chain else None
    return b


def _read_env_file_value(key: str) -> str:
    for envf in _ENV_FILES:
        p = Path(envf)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip().removeprefix("export ").strip()
                if line.startswith(key) and "=" in line and not line.startswith("#"):
                    raw = line.split("=", 1)[1].strip()
                    return _strip_inline_comment(raw).strip('"').strip("'")
        except OSError:
            pass
    return ""


def _resolve(key: str) -> str:
    return os.environ.get(key, "") or _read_env_file_value(key)


# Owner-lock (design note, 2026-07-25): "само аз ползвам бота" — след като е зададен,
# ботът реагира ИЗКЛЮЧИТЕЛНО на този Discord user ID (DM или споменаване от
# всеки друг се игнорира мълчаливо), И само за него се включват реални
# action инструменти (RUN_CMD/WRITE_FILE/USE_SKILL/...). Ако не е зададен —
# ботът мълчи напълно (fail-closed) — виж on_message. Публичен продукт:
# по-добре бот, който не отговаря, отколкото бот, който изпълнява команди
# по молба на произволен непознат в Discord.
#
# Как да си вземеш ID-то: Discord → Settings → Advanced → включи Developer
# Mode → десен клик на своя профил → Copy User ID.
_OWNER_ID_RAW = _resolve("GENESIS_DISCORD_OWNER_ID")
try:
    _OWNER_ID: int | None = int(_OWNER_ID_RAW) if _OWNER_ID_RAW.strip() else None
except ValueError:
    _OWNER_ID = None


def _load_persona() -> str:
    """
    Кратка Discord chat persona — НЕ config.yaml's пълен system_prompt (836
    токена измерено, включва RUN_CMD/WRITE_FILE/DELEGATE tool документация,
    която _brain_reply() изобщо не поддържа — !mission ползва отделен,
    вече скромен промпт през run_autonomous_loop). Всяко съобщение в чата
    плащаше за инструменти, които буквално не могат да се извикат тук.
    Измерено намаление: ~836 → ~110 токена на съобщение (commit 2026-07-25).
    """
    return (
        "Ти си Genesis Agent — автономен AI агент, който работи на машината на потребителя. НЕ си ChatGPT/"
        "Gemini/друг AI — Genesis. Отговаряй разговорно и КРАТКО (Discord чат, "
        "не терминал), под ~1500 символа, на български освен ако потребителят "
        "пише на друг език. Директен и ефикасен тон, без излишни уговорки."
    )


def _chunk(text: str, size: int = _DISCORD_LIMIT) -> List[str]:
    """Реже дълъг отговор на части по границата на Discord лимита (по редове)."""
    text = text.strip() or "…"
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size:
            if cur:
                chunks.append(cur)
            # Ако единичен ред е по-дълъг от лимита — режем твърдо.
            while len(line) > size:
                chunks.append(line[:size])
                line = line[size:]
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    return chunks


class GenesisClient(discord.Client):
    """Discord бот, който води разговор през мозъка на Genesis."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # привилегирован — включи го и в Dev Portal
        super().__init__(intents=intents)
        self.persona = _load_persona()
        # Кратка памет за контекст, отделно за всеки канал.
        self._history: Dict[int, Deque[Dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=_HISTORY_TURNS)
        )
        # 24/7 цикъл — състояние на background thread-а.
        self._loop_thread: threading.Thread | None = None
        self._loop_stats: Dict[str, Any] = {
            "missions": 0, "successes": 0, "started_at": None,
        }

    # ── Помощни ───────────────────────────────────────────────────────────────
    def _brain_reply(self, channel_id: int, user_text: str, enable_tools: bool = False) -> tuple[str, int]:
        """СИНХРОННО извикване на Brain (върти се в thread, за да не блокира gateway).
        Връща (текст, общо_токени) — реален брой от reply.usage, за live footer.

        enable_tools (design note, 2026-07-25, "upgrade to max" — само owner-а вече
        ползва бота): когато е True, минава native function-calling (същите
        tool schemas като терминалния чат) — реално изпълнява RUN_CMD/
        WRITE_FILE/USE_SKILL/... вместо само да "знае" за тях. Ограничен до
        _TOOL_ROUND_CAP рунда в рамките на ЕДНО съобщение (не блокира gateway
        безкрайно). Sandbox-ът вече е безопасен по подразбиране тук — ботът
        върви като systemd service (без tty), policy.resolve_mode() пада на
        "deny" автоматично → CONFIRM операции се отказват, не увисват на
        въпрос, който никой не може да отговори."""
        hist = self._history[channel_id]
        persona = self.persona
        use_tools = enable_tools and _DISCORD_TOOL_SCHEMAS and genesis_skills
        if use_tools:
            persona += (" Имаш реални инструменти (файлове, команди, умения от библиотеката, "
                        "браузър) — викай ги директно вместо само да описваш какво трябва "
                        "да се направи. За всичко проверимо (изчисления, крипто/hash резултати, "
                        "съдържание на файлове, команди) — НИКОГА не измисляй правдоподобен "
                        "отговор от паметта си, винаги викай подходящ tool и цитирай РЕАЛНИЯ "
                        "резултат от него. Имаш и памет за работата (TASK_ADD/TASK_UPDATE/"
                        "REMEMBER) — записвай в нея веднага щом нещо се реши или остане "
                        "недовършено, същата е като в терминалния чат.")

        # Състоянието на работата — същата workspace памет като терминала, за да
        # може потребителят да продължи от телефона без да преразказва (design note, 2026-07-25).
        # Само за owner-а с tools: без tools ботът не може да я поддържа, а без
        # owner-lock би изтекло лично състояние към произволен потребител.
        if use_tools:
            try:
                from genesis_agent import workspace_memory as wm
                b = wm.briefing(max_threads=5, max_decisions=3)
                if b:
                    persona += "\n\n## Текущо състояние на работата\n" + b
            except Exception:
                pass
            # Реални пътища — само в tools режим, защото само там могат да се
            # ползват (без tools ботът само говори). Виж agent_core.env_facts.
            try:
                from genesis_agent.agent_core import env_facts
                persona += "\n\n" + env_facts()
            except Exception:
                pass
        messages: List[Dict] = [{"role": "system", "content": persona}]
        messages.extend(hist)
        messages.append({"role": "user", "content": user_text})

        brain = _chat_brain()
        tools = _DISCORD_TOOL_SCHEMAS if use_tools else None
        total_tokens = 0
        text = ""
        for _round in range(_TOOL_ROUND_CAP + 1):
            reply = brain.complete(messages, tools=tools)
            if getattr(reply, "usage", None):
                total_tokens += (reply.usage or {}).get("total_tokens", 0)
            text = (reply.raw_text or "").strip()
            tool_calls = getattr(reply, "tool_calls", None)
            assistant_msg: Dict = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            if not tool_calls:
                break
            if _round >= _TOOL_ROUND_CAP:
                text = text or "(достигнат таван инструмент-рундове за това съобщение)"
                break
            asked = ""
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                result = genesis_skills.dispatch_tool_call(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                  "name": name, "content": result})
                if genesis_skills.ASK_USER_MARKER in result:
                    asked = result
            if asked:
                # ASK_USER (design note, 2026-07-27) — в Discord "спирането" е
                # естествено: въпросът се праща като отговор, а следващото
                # съобщение на потребителя е отговорът му. Текстът на модела (ако
                # има) се запазва над въпроса, за да не се губи контекстът.
                q = asked.replace(genesis_skills.ASK_USER_MARKER, "").strip()
                text = f"{text}\n\n{q}".strip() if text else q
                break
        text = text or "(празен отговор)"

        # Обнови контекста и споделената памет (безопасно). Пазим само
        # user/assistant текст в историята (не native tool_calls формата —
        # виж genesis_agent.brain._sanitize_for_textmode за защо).
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": text})
        try:
            from genesis_agent import conversation_memory as cm
            cm.add_message("user", user_text)
            cm.add_message("assistant", text)
        except Exception:
            pass
        return text, total_tokens

    def _run_mission(self, goal: str) -> str:
        """СИНХРОННО пускане на реална автономна мисия (върти се в thread)."""
        try:
            from genesis_agent.autonomous_loop import run_autonomous_loop
            out = run_autonomous_loop(goal, max_rounds=6)
            if out.success:
                reused = " ♻️ (композиция от съществуващо умение)" if out.reused_existing else ""
                return (f"✅ Мисията успя за {out.rounds} рунда{reused}.\n"
                        f"📦 `{out.skill_path}`")
            return (f"❌ Мисията не успя след {out.rounds} рунда.\n"
                    f"Последна грешка: {(out.last_stderr or '')[:400]}")
        except Exception as e:
            return f"❌ Грешка при мисията: {e}"

    @staticmethod
    def _sleep_interruptible(seconds: int, stop_event) -> None:
        """time.sleep, но проверява stop_event на всеки 5с — за да реагира !stop
        веднага, не да чака до края на дълъг интервал."""
        step = 5
        elapsed = 0
        while elapsed < seconds and not stop_event.is_set():
            time.sleep(min(step, seconds - elapsed))
            elapsed += step

    def _run_247_loop(self) -> None:
        """
        Непрекъснат автономен цикъл (background thread): goal_engine.next_goals()
        + run_autonomous_loop(), с реални safety проверки (storage, daily cap) и
        реален kill-switch (stop_event — за пръв път в проекта нещо действително
        го задейства/чете в цикъл). Sandbox policy "deny" — unattended default.
        """
        from genesis_agent import sandbox, storage_monitor, goal_engine, budget
        from genesis_agent.config import stop_event
        from genesis_agent.autonomous_loop import run_autonomous_loop
        from genesis_agent.notifier import notify

        interval_sec = max(60, int(os.environ.get("GENESIS_247_INTERVAL_MIN", "30")) * 60)
        max_per_day = int(os.environ.get("GENESIS_247_MAX_PER_DAY", "20"))
        max_tokens_per_day = int(os.environ.get("GENESIS_247_MAX_TOKENS_PER_DAY", "500000"))

        sandbox.set_policy(sandbox.SandboxPolicy(mode="deny"))
        self._loop_stats["started_at"] = time.time()
        notify(f"🟢 **Genesis 24/7 цикъл стартиран.** Интервал: {interval_sec // 60}мин, "
               f"макс {max_per_day} мисии/ден, макс {max_tokens_per_day} токена/ден, sandbox=deny.")

        today = date.today()
        today_count = 0
        pool_exhausted_notified = False

        while not stop_event.is_set():
            if date.today() != today:
                today, today_count, pool_exhausted_notified = date.today(), 0, False

            if today_count >= max_per_day:
                self._sleep_interruptible(interval_sec, stop_event)
                continue

            try:
                report = storage_monitor.check_storage()
            except Exception:
                report = None
            if report is not None and report.compression_required:
                notify(f"⚠️ Genesis 24/7: диск над лимита "
                       f"({storage_monitor.human_gb(report.total_bytes)}), пропускам цикъла.")
                self._sleep_interruptible(interval_sec, stop_event)
                continue

            try:
                tokens_today = budget.today_totals().get("total_tokens", 0)
            except Exception:
                tokens_today = 0
            if tokens_today >= max_tokens_per_day:
                notify(f"⚠️ Genesis 24/7: дневен token лимит достигнат "
                       f"({tokens_today}/{max_tokens_per_day}), пропускам цикъла.")
                self._sleep_interruptible(interval_sec, stop_event)
                continue

            # ── РАБОТАТА НА ПОТРЕБИТЕЛЯ Е С ПРИОРИТЕТ (design note, 2026-07-26) ──────────
            # Преди цикълът въртеше само генерични goal_engine теми — полезно
            # за библиотеката, безполезно за реалните му задачи. Сега първо
            # минава подготвителен пас по отворените нишки: САМО четене/търсене
            # (виж genesis_agent/thread_worker.py за границата), находките отиват в
            # нишката и в Discord. Промени по машината/проектите НЕ се правят
            # без надзор — решението за изпълнение остава на потребителя.
            try:
                from genesis_agent import thread_worker, workspace_memory as wm
                open_threads = wm.list_threads("open", 2)
            except Exception:
                open_threads = []

            for th in open_threads:
                if stop_event.is_set() or today_count >= max_per_day:
                    break
                today_count += 1
                try:
                    res = thread_worker.prepare_thread(th)
                    if res.ok and res.findings:
                        notify(f"🔎 **Подготвих: {res.title[:70]}**\n{res.findings[:1200]}\n"
                               f"_(само проучване — нищо не е променено)_")
                    elif res.error:
                        notify(f"⚠️ Genesis 24/7: подготовката по «{res.title[:60]}» "
                               f"не успя: {res.error[:120]}")
                except Exception as e:
                    notify(f"❌ Genesis 24/7 грешка по нишка '{th.get('title','')[:60]}': {e}")

            if open_threads:
                # Имаше реална работа този цикъл — не пълним с генерични цели.
                self._sleep_interruptible(interval_sec, stop_event)
                continue

            goals = goal_engine.next_goals(3)
            if not goals:
                if not pool_exhausted_notified:
                    notify("💤 Genesis 24/7: няма отворени нишки И goal pool-ът е изчерпан — "
                           "идле. Дай ми задача (`!tasks` показва състоянието).")
                    pool_exhausted_notified = True
                self._sleep_interruptible(interval_sec, stop_event)
                continue

            for goal in goals:
                if stop_event.is_set() or today_count >= max_per_day:
                    break
                self._loop_stats["missions"] = int(self._loop_stats["missions"]) + 1
                today_count += 1
                try:
                    out = run_autonomous_loop(goal, operator_id=str(_OWNER_ID or "operator"))
                    if out.success:
                        self._loop_stats["successes"] = int(self._loop_stats["successes"]) + 1
                except Exception as e:
                    notify(f"❌ Genesis 24/7 грешка при '{goal[:80]}': {e}")

            self._sleep_interruptible(interval_sec, stop_event)

        notify("🔴 **Genesis 24/7 цикъл спрян.**")

    async def _send(self, channel: "discord.abc.Messageable", text: str) -> None:
        for part in _chunk(text):
            await channel.send(part)

    # ── Събития ─────────────────────────────────────────────────────────────────
    async def on_ready(self) -> None:
        print(f"[discord_bot] ✅ Свързан като {self.user} (id={self.user.id})")
        print("[discord_bot] Пиши в DM или спомени бота в канал. !help за помощ.")

    async def on_message(self, message: "discord.Message") -> None:
        if message.author == self.user or message.author.bot:
            return
        # Owner-lock, fail-CLOSED: без зададен GENESIS_DISCORD_OWNER_ID ботът
        # не отговаря на НИКОГО. Публичен продукт — човек, който забрави да си
        # сложи ID-то, не бива да получи бот, отговарящ на всеки непознат от
        # машината му. По-добре мълчалив бот, отколкото отворен.
        if _OWNER_ID is None or message.author.id != _OWNER_ID:
            return

        is_dm = message.guild is None
        content = (message.content or "").strip()
        mentioned = self.user in message.mentions

        # Реши дали това съобщение е адресирано към Genesis.
        addressed = is_dm or mentioned
        lowered = content.lower()
        if not addressed and any(lowered.startswith(p) for p in _PREFIXES):
            addressed = True
        if not addressed:
            return

        # Изчисти споменаването/префикса от текста.
        text = content
        if mentioned:
            text = text.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        for p in _PREFIXES:
            if lowered.startswith(p):
                text = content[len(p):].strip()
                break

        if not text:
            await self._send(message.channel, "Кажи? (пиши текст, или `!help`)")
            return

        # ── Команди ──
        if text.lower() in ("!help", "!помощ"):
            await self._send(message.channel,
                "**Genesis Discord**\n"
                "• Пиши нормално — водим разговор.\n"
                "• `!mission <цел>` — пускам реална автономна мисия и връщам резултата.\n"
                "• `!start24_7` — непрекъснат автономен цикъл, докато не спреш с `!stop`.\n"
                "• `!stop` — kill-switch (спира 24/7 И текуща !mission).\n"
                "• `!status247` — статус на 24/7 цикъла.\n"
                "• `!budget` — token/API разход днес и последните 7 дни.\n"
                "• `!tasks` — какво е отворено, следващи стъпки, решения.\n"
                "• `!done <id>` / `!drop <id>` — затвори нишка / изхвърли я.\n"
                "• `!clear` — забравям контекста на този канал.\n"
                "В канал ме спомени (@Genesis) или почни с `genesis ` / `!g `.")
            return

        if text.lower() in ("!clear", "!изчисти"):
            self._history.pop(message.channel.id, None)
            await self._send(message.channel, "🧹 Контекстът на този канал е изчистен.")
            return

        if text.lower() == "!start24_7":
            if self._loop_thread and self._loop_thread.is_alive():
                await self._send(message.channel, "⚠️ 24/7 цикълът вече тече. `!status247` за статус, `!stop` за спиране.")
                return
            from genesis_agent.config import stop_event
            stop_event.clear()
            self._loop_stats.update(missions=0, successes=0, started_at=None)
            self._loop_thread = threading.Thread(target=self._run_247_loop, daemon=True)
            self._loop_thread.start()
            interval = os.environ.get("GENESIS_247_INTERVAL_MIN", "30")
            try:
                from genesis_agent import workspace_memory as wm
                n_open = wm.stats()["open"]
            except Exception:
                n_open = 0
            await self._send(message.channel,
                f"🟢 24/7 цикълът стартира (интервал {interval}мин). Sandbox: deny (безопасен).\n"
                f"Приоритет: твоите отворени нишки ({n_open}) — подготвям ги със "
                f"**само четене/търсене**, нищо не променям без теб. Без нишки → "
                f"генерични умения от goal_engine.\n`!stop` за спиране по всяко време.")
            return

        if text.lower() == "!stop":
            from genesis_agent.config import stop_event
            stop_event.set()
            await self._send(message.channel,
                "🔴 Kill-switch задействан — спирам 24/7 цикъла И всяка текуща !mission "
                "(глобален stop). `!start24_7` за нов старт (изчиства stop_event).")
            return

        if text.lower() == "!status247":
            running = bool(self._loop_thread and self._loop_thread.is_alive())
            missions = self._loop_stats.get("missions", 0)
            successes = self._loop_stats.get("successes", 0)
            started = self._loop_stats.get("started_at")
            uptime = f"{round((time.time() - started) / 60)}мин" if started else "—"
            await self._send(message.channel,
                f"**24/7 статус**\n"
                f"Активен: {'✅ да' if running else '❌ не'}\n"
                f"Мисии тази сесия: {missions} ({successes} успешни)\n"
                f"Uptime: {uptime}")
            return

        if text.lower() == "!budget":
            from genesis_agent import budget
            today = budget.today_totals()
            week = budget.range_totals(7)
            by_prov = ", ".join(f"{p}:{v['total_tokens']}" for p, v in today["by_provider"].items()) or "—"
            await self._send(message.channel,
                f"**Budget**\n"
                f"Днес: {today['calls']} извиквания, {today['total_tokens']} токена ({by_prov})\n"
                f"Последните 7 дни: {week['calls']} извиквания, {week['total_tokens']} токена")
            return

        # Състояние на работата — същата workspace памет като терминалния чат,
        # достъпна от телефона (design note, 2026-07-25).
        if text.lower() in ("!tasks", "!задачи", "!state"):
            try:
                from genesis_agent import workspace_memory as wm
                st = wm.stats()
                b = wm.briefing(max_threads=15, max_decisions=5)
                await self._send(message.channel,
                    f"**📋 Работа** — {st['open']} отворени, {st['blocked']} блокирани, "
                    f"{st['done']} готови\n\n" + (b or "Още нищо не е записано."))
            except Exception as e:
                await self._send(message.channel, f"⚠ {e}")
            return

        # Хигиена на нишките от телефона — !done 3 / !drop 3 (може и няколко id).
        if text.lower().startswith(("!done", "!drop", "!готово")):
            parts = text.split()
            if len(parts) < 2:
                await self._send(message.channel,
                                 "Дай номер: `!done 3` (готово) или `!drop 3` (изхвърли).")
                return
            try:
                from genesis_agent import workspace_memory as wm
                drop = text.lower().startswith("!drop")
                out = [wm.close_thread(i, drop=drop) for i in parts[1:]]
                await self._send(message.channel, "\n".join(out))
            except Exception as e:
                await self._send(message.channel, f"⚠ {e}")
            return

        if text.lower().startswith("!mission"):
            goal = text[len("!mission"):].strip()
            if not goal:
                await self._send(message.channel, "Дай цел: `!mission <какво да построя>`")
                return
            await self._send(message.channel, f"🚀 Стартирам мисия: *{goal[:180]}*\n(работя на заден план…)")
            result = await asyncio.to_thread(self._run_mission, goal)
            await self._send(message.channel, result)
            return

        # ── Обикновен разговор ──
        # Action tools само за потвърдения owner (виж _OWNER_ID) — ако не е
        # конфигуриран, никой не получава tools (безопасен default).
        enable_tools = _OWNER_ID is not None and message.author.id == _OWNER_ID
        async with message.channel.typing():
            reply, tokens = await asyncio.to_thread(
                self._brain_reply, message.channel.id, text, enable_tools)
        if tokens:
            reply += f"\n-# 🪙 {tokens} токена"
        await self._send(message.channel, reply)


def main() -> None:
    token = _resolve("GENESIS_DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "❌ Няма GENESIS_DISCORD_BOT_TOKEN.\n"
            "   Създай бот и токен (виж инструкциите най-горе в този файл), после\n"
            "   добави в ~/.genesis/.env:  GENESIS_DISCORD_BOT_TOKEN=...\n"
        )
    if genesis_skills:
        genesis_skills.set_workspace(_PROJECT_ROOT)
    if _OWNER_ID is None:
        print("[discord_bot] ⛔ GENESIS_DISCORD_OWNER_ID не е зададен — ботът НЯМА да "
              "отговаря на никого (fail-safe). Сложи своя Discord user ID в "
              "~/.genesis/.env и рестартирай. Виж коментара до _OWNER_ID в този файл.")
    else:
        print(f"[discord_bot] 🔒 Owner-lock активен (ID {_OWNER_ID}) — само този потребител "
              "получава отговори и action tools.")
    print("[discord_bot] Свързвам се към Discord…")
    GenesisClient().run(token, log_handler=None)


if __name__ == "__main__":
    main()
