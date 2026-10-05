"""
genesis_agent.agent_core — ядрото на агента: един цикъл за всеки ход.

`run_tool_loop` е ходът: модел → инструменти → … → отговор, с проверките
преди „готово“. Терминалът (`genesis`), телефонът (`genesis serve`) и
scripts/capability_report.py го викат с това, което ги различава: `ask` (как
се пита моделът) и `ui` (как се показва ходът).

Цикълът беше два (2026-10-04): един тук — за GTK чата и Jarvis — и отделен в
терминала. Всяка поправка се правеше на две места и те се разминаха: в
единия резултатите от текстовите тагове не се показваха, в другия
проверката след „готово“ влезе по-късно. GUI-то е махнато на 2026-09-23;
остана цикълът на терминала, преместен тук.

Още тук: възстановяване и таван на историята, фактите за машината в
системния промпт. Без rich/GTK — чист Python.
"""
from __future__ import annotations

import json
import os
import re
from collections import deque
from collections.abc import Callable, MutableSequence
from contextlib import AbstractContextManager, nullcontext
from datetime import date
from pathlib import Path

from genesis_agent import claim_check
from genesis_agent.budget import clip_for_context
from genesis_agent.code_check import RunCheck
from genesis_agent.config import TOOL_ROUND_CAP
from genesis_agent.page_check import FinalCheck
from genesis_agent.repeat_guard import RepeatGuard
from genesis_agent.tool_schemas import load_tool_arguments

MIN_SIZE_B = 32          # само модели ≥32B за интерактивен чат
COMPACT_THRESHOLD = 16
COMPACT_KEEP_RECENT = 10
# Твърдият таван на живата история. Компресията (compact_chat_history) е
# ПРЕВАНТИВНА и обикновено се задейства далеч преди него — това е последната
# преграда, не основният механизъм.
HISTORY_MAXLEN = 30

Messages = MutableSequence[dict]
Ask = Callable[[Messages], "tuple[str, list | None]"]


def restored_history(saved: list[dict], system_prompt: str,
                     maxlen: int = HISTORY_MAXLEN) -> deque:
    """Заредена от диска сесия → живата `messages` структура на фронтенда.

    Съществува, защото `deque(maxlen=N)` изхвърля от ПРЕДНИЯ край (bug fix,
    2026-08-12). Наивното `deque([system_msg] + saved, maxlen=30)` изглежда
    правилно и работи за къси сесии, но при 30+ запазени реплики изхвърля
    точно системното съобщение, което току-що е сложено отпред — а с него
    env_facts и брифинга за работата, тоест моделът тихо остава без
    инструкции за целия остатък от сесията. Освен това
    Brain.compact_chat_history проверява `messages[0]["role"] == "system"` и
    иначе спира да компресира изобщо, така че историята после расте
    несъкратена до твърдия таван.

    Системният промпт се подава отделно (а не се чете от файла) нарочно:
    той носи СВЕЖИ env_facts/брифинг за текущата сесия, не онези отпреди
    седмица.
    """
    body = [m for m in saved if m.get("role") != "system"]
    return deque([{"role": "system", "content": system_prompt}] + body[-(maxlen - 1):],
                 maxlen=maxlen)


def bounded_history(messages, maxlen: int | None) -> deque:
    """Живата история обратно под тавана си — след хода, не по време на него.

    По време на хода историята е без таван (run_turn, run_tool_loop): deque с
    maxlen изхвърляше отпред посред задачата — първо системния промпт, после
    самата заявка на оператора. Измерено 2026-09-30: в 13 от 210 bench
    разговора ollama оттам нататък отговаряше празно за 0.3 s (без нито едно
    user съобщение той само „зарежда“ модела — done_reason=load, prompt=0),
    всичко отиваше на 8× по-бавния NVIDIA, а той работеше без задачата.
    Тук: системният промпт остава, после най-новото; без tool резултат,
    чието извикване е отрязано."""
    msgs = list(messages)
    if not maxlen or len(msgs) <= maxlen:
        return deque(msgs, maxlen=maxlen)
    head = msgs[:1] if msgs[0].get("role") == "system" else []
    tail = msgs[len(head):][-(maxlen - len(head)):]
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    return deque(head + tail, maxlen=maxlen)


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
    # Path.home() ignores $HOME on Windows (it reads USERPROFILE instead), so
    # an explicit HOME override — the standard way to redirect a program's
    # home directory, including in this project's own tests — silently had
    # no effect there. Respect HOME when set, on every platform.
    home = Path(os.environ["HOME"]) if os.environ.get("HOME") else Path.home()
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

    # USERNAME преди USER: на Windows `USER` не е зададен, така че този ред
    # казваше буквално "(потребител: unknown)" на всяка сесия там — точно вида
    # факт, който функцията съществува да НЕ оставя на модела да отгатва.
    user = os.environ.get("USER") or os.environ.get("USERNAME") or home.name or "unknown"
    # Датата също е факт, не догадка: на живо моделът кръсти архив
    # `...-20260812.zip` на 2026-09-23. Системните промптове се сглобяват в
    # началото на сесия, така че денят не се сменя под краката на кеша.
    lines = [f"- Днес: {date.today().isoformat()}",
             f"- Домашна директория: {home}   (потребител: {user})"]
    for label, key in (("Десктоп", "DESKTOP"), ("Изтегляния", "DOWNLOAD"),
                       ("Документи", "DOCUMENTS"), ("Снимки", "PICTURES")):
        p = dirs[key]
        lines.append(f"- {label}: {p}" + ("" if p.is_dir() else "   (НЕ съществува)"))
    if workspace:
        lines.append(f"- Работна директория (относителните пътища са спрямо нея): {workspace}")
    return ("## СРЕДАТА (реални пътища на тази машина — НЕ ги отгатвай)\n"
            + "\n".join(lines)
            + "\nАко ти трябва път извън изброените, провери с LIST_DIR преди да пишеш в него.")


# ── Ходът ────────────────────────────────────────────────────────────────────

class TurnUI:
    """Как ходът показва напредъка. Основата не показва нищо; терминалът
    рисува с rich (genesis_terminal_agent.RichTurnUI), `genesis serve` праща
    всичко към телефона (genesis_agent.remote_server.RemoteTurnUI)."""

    def thinking(self, label: str, spinner: str = "dots") -> AbstractContextManager:
        return nullcontext()

    def assistant(self, text: str) -> None: ...

    def tool(self, name: str, result: str) -> None: ...

    def asked(self, question: str) -> None: ...

    def spinning(self, note: str) -> None: ...

    def warn(self, text: str) -> None: ...

    def info(self, text: str) -> None: ...

    def cancelled(self) -> bool:
        """Поискано ли е спиране (бутонът „Стоп" на телефона). В терминала
        спирането е Ctrl+C, затова тук винаги „не"."""
        return False


_MALFORMED_TAG_NOTE = (
    "[Система]: В последния отговор не намерих валиден tool таг, но той изглежда като "
    "опит за такъв. Ако си искал да викнеш инструмент — използвай точния синтаксис "
    "`[TAG: аргумент]` (или `[WRITE_FILE: път]...[END_WRITE]` за файлове). Ако вече си "
    "приключил — дай кратък финален отговор БЕЗ скоби във формат на таг.")

_RESULTS_TAIL = (
    "\n\nАко тези резултати вече изпълняват заявката на потребителя напълно — дай "
    "КРАТКО финално обобщение БЕЗ никакви нови tool тагове. Викай нов tool САМО ако "
    "наистина има следваща реална стъпка. ВАЖНО: ако някоя команда е отказана от "
    "оператора (SANDBOX DECLINED), но ДРУГ резултат по-горе вече доказва, че целта е "
    "постигната (напр. командата вече работи правилно) — не настоявай за отказаната "
    "команда, просто отчети успех с наличните доказателства.")

_TAG_NAME = re.compile(r"\[([A-Z_]+)[:\]]")


def _text_tool_name(result: str) -> str:
    """`[RUN_CMD: ls]…` → RUN_CMD; резултат без таг → „инструмент“."""
    m = _TAG_NAME.match(result or "")
    return m.group(1) if m else "инструмент"


def run_tool_loop(messages: Messages, request: str, ui: TurnUI, ask: Ask, *,
                  remember: Callable[[str, str], None] | None = None,
                  round_cap: int = TOOL_ROUND_CAP) -> None:
    """Един ход: модел → инструменти → … → отговор. Добавя хода към `messages`.

    `messages` вече завършва със заявката (user). `request` е същата заявка,
    както операторът я е написал, без добавеното знание: в нея проверката на
    кода търси буквалните думи („ред ОБЩО“), от нея се пишат приемните тестове.
    `ask(messages)` → (текст, tool_calls или None): едно обръщение към модела.
    `remember(role, text)` по избор записва репликите в паметта на разговора.

    Ходът спира, когато моделът отговори без инструменти и проверките нямат
    какво да върнат, когато попита оператора (ASK_USER), когато се върти на
    място, на тавана от `round_cap` рунда или при „Стоп“ от `ui`. Хвърля само
    ако хвърли `ask` — фронтендът решава как да покаже грешката.
    """
    _Turn(messages, request, ui, ask, remember, round_cap).run()


class _Turn:
    """Състоянието на един ход. Методите, които връщат bool, казват дали ходът
    продължава — и щом е True, моделът вече е попитан за следващия отговор."""

    def __init__(self, messages: Messages, request: str, ui: TurnUI, ask: Ask,
                 remember: Callable[[str, str], None] | None, round_cap: int) -> None:
        self.messages = messages
        self.ui = ui
        self._ask = ask
        self._remember = remember or (lambda role, text: None)
        self.round_cap = round_cap
        self.rounds = 0
        # Какво РЕАЛНО е изпълнено в хода — срещу него се сверява какво моделът
        # твърди накрая (claim_check). Броят рундове не стига: един LIST_DIR
        # „оправдаваше“ твърдение за инсталация, която никога не е текла.
        self.executed: list[tuple[str, str]] = []
        # Уеб страниците от хода — в браузър, когато моделът каже „готово“.
        self.page_check = FinalCheck()
        # .py, записан и непуснат след последната промяна; буквалните думи от заявката.
        self.run_check = RunCheck(request)
        # Въртене на място: същият извик, същият резултат, пореден път. Таванът
        # го ограничава по цена, но не го разпознава.
        self.guard = RepeatGuard()
        self.retries = {"tag": 0, "promise": 0, "claim": 0}
        self.response = ""
        self.tool_calls: list | None = None

    def run(self) -> None:
        self._next("Genesis мисли...", "dots2")
        while self._step():
            pass

    def _next(self, label: str, spinner: str = "aesthetic") -> bool:
        with self.ui.thinking(label, spinner):
            self.response, self.tool_calls = self._ask(self.messages)
        return True

    def _step(self) -> bool:
        self._record_reply()
        if self.ui.cancelled():
            # Преди следващия инструмент, не по средата му. Незапочнатите
            # извиквания се махат — иначе следващото обръщение е невалидна история.
            self.messages[-1].pop("tool_calls", None)
            self.ui.warn("Спряно от оператора.")
            return False
        if self.tool_calls:
            return self._native_round()
        return self._text_round()

    def _record_reply(self) -> None:
        self.ui.assistant(self.response)
        msg: dict = {"role": "assistant", "content": self.response}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        self.messages.append(msg)
        # Празен отговор без инструменти е възможен (празно съдържание от
        # доставчика) и не се помни.
        if self.response.strip():
            self._remember("assistant", self.response)
        elif self.tool_calls:
            self._remember("assistant", f"[повикани {len(self.tool_calls)} tool(-а)]")

    def _observe(self, result: str) -> None:
        self.page_check.observe(result)
        self.run_check.observe(result)

    def _native_round(self) -> bool:
        """Native tool calling: същите backend-и като текстовите тагове
        (genesis_skills.dispatch_tool_call), без риск от сгрешен синтаксис."""
        import genesis_skills as skills

        asked = spinning = note = ""
        for tc in self.tool_calls or ():
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            try:
                args = load_tool_arguments(fn.get("arguments"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            result = skills.dispatch_tool_call(name, args)
            self._observe(result)
            entry = claim_check.counts_as_executed(
                name, " ".join(str(v) for v in args.values()), result)
            if entry:
                self.executed.append(entry)
            if skills.ASK_USER_MARKER in result:
                asked = result  # показва се веднъж, от ui.asked
            else:
                self.ui.tool(name, result)
            self.messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                  "name": name, "content": clip_for_context(result)})
            verdict = self.guard.observe(name, args, result)
            if verdict.stop:
                spinning = verdict.note
            elif verdict.note:
                note = verdict.note
        if not self._end_of_round(asked, spinning, note):
            return False
        return self._next("Анализирам...")

    def _text_round(self) -> bool:
        """Текстови тагове — модел без native tool calling (или такъв, който
        този път не вика нищо)."""
        import genesis_skills as skills

        results = skills.parse_and_execute_tools(self.response)
        self.executed.extend(claim_check.executed_from_text_results(results))
        for r in results:
            self._observe(r)
            if skills.ASK_USER_MARKER not in r:
                self.ui.tool(_text_tool_name(r), r)
        if not results:
            return self._before_done()
        spinning = note = ""
        for r in results:
            verdict = self.guard.observe_text_result(r)
            if verdict.stop:
                spinning = verdict.note
            elif verdict.note:
                note = verdict.note
        asked = next((r for r in results if skills.ASK_USER_MARKER in r), "")
        if not self._end_of_round(asked, spinning, note):
            return False
        self.messages.append({"role": "system", "content": "[Резултат]:\n" + "\n\n".join(
            clip_for_context(r) for r in results) + _RESULTS_TAIL})
        return self._next("Анализирам...")

    def _end_of_round(self, asked: str, spinning: str, note: str) -> bool:
        """Общото след всеки рунд инструменти. False — ходът спира тук."""
        if asked:
            # Агентът е попитал → контролът се връща на човека. Иначе моделът
            # би попитал и веднага сам би продължил да гадае.
            import genesis_skills as skills
            self.ui.asked(asked.replace(skills.ASK_USER_MARKER, "").strip())
            return False
        if spinning:
            # Нищо ново не може да дойде от още рундове — спира СЕГА и казва
            # защо, вместо да догори до тавана.
            self.ui.spinning(spinning)
            return False
        if note:
            self.messages.append({"role": "system", "content": note})
        self.rounds += 1
        if self.rounds >= self.round_cap:
            self.ui.warn(f"Достигнат таван от {self.round_cap} инструмент-рунда "
                         "за това съобщение — спирам тук, продължи с ново съобщение.")
            return False
        return True

    # ── Преди „готово“ ───────────────────────────────────────────────────────

    def _before_done(self) -> bool:
        """Моделът спря да вика инструменти. Преди ходът да свърши: объркан таг
        ли е това, после проверките — браузър, код, приемни тестове, обещание
        без работа, твърдение без изпълнение. True — върнат е за още работа."""
        import genesis_skills as skills

        # Празно ≠ непременно „приключи“: може да е объркан таг ([INSTALL: …]
        # вместо познат, липсващ [END_WRITE]) — иначе би минал за финален отговор.
        if self.retries["tag"] < 2 and skills.looks_like_attempted_tool_tag(self.response):
            self.retries["tag"] += 1
            return self._nudge(_MALFORMED_TAG_NOTE, "Анализирам...")
        return (self._browser_check() or self._code_check()
                or self._promise_check() or self._claim_check())

    def _nudge(self, note: str, label: str) -> bool:
        self.messages.append({"role": "system", "content": note})
        return self._next(label)

    def _browser_check(self) -> bool:
        if not self.page_check.due():
            return False
        with self.ui.thinking("Проверявам страницата в браузър…", "dots2"):
            note, line = self.page_check.check()
        if line:
            self.ui.tool("проверка в браузър", line)
        if self.page_check.passed:
            self.executed.append(("BROWSE", "page_check"))
        return bool(note) and self._nudge(note, "Оправям според браузъра…")

    def _code_check(self) -> bool:
        if not self.run_check.due():
            return False
        self.ui.warn("Написа код — казвам му да го пробва и извън примерите.")
        return self._nudge(self.run_check.note(), "Пробвам кода…")

    def _promise_check(self) -> bool:
        promise = claim_check.unfinished_promise(self.response)
        if not promise or self.retries["promise"] >= 1:
            return False
        self.retries["promise"] += 1
        self.ui.warn("Обещава работа и спира — казвам му да я направи.")
        return self._nudge(claim_check.promise_nudge(promise), "Продължавам…")

    def _claim_check(self) -> bool:
        # По ВИД на твърдението, не по броя рундове (виж claim_check.py).
        unsupported = claim_check.unsupported_claims(self.response, self.executed)
        if not unsupported or self.retries["claim"] >= 1:
            return False
        self.retries["claim"] += 1
        self.ui.warn("Твърди свършена работа, която никой изпълнен инструмент не "
                     "доказва — питам пак.")
        return self._nudge(claim_check.nudge_text(unsupported), "Проверявам…")
