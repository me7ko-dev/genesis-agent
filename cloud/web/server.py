"""Етап 2: вход, уеб чат и сваляне на файловете от задачата; етап 3: плащане.

    python -m cloud.web.server add-user ivan@example.com        # отпечатва парола
    python -m cloud.web.server serve --data /srv/genesis/web --jobs /srv/jobs \\
        --env-file /srv/genesis/keys.env                          # 127.0.0.1:8080
    python -m cloud.web.server users | balance | grant | disable | enable |
        limits | invite | reconcile                               # оператор, само локално

Всяко съобщение в разговор е нов контейнер (cloud/runner/launch.py) върху
СЪЩАТА папка — файловете остават, историята е в /work/.genesis (task.py).
Само стандартната библиотека: услугата стои зад Cloudflare/Caddy с HTTPS, тук
е само приложението.

Какво пази:
  - вход с имейл и парола (scrypt); бисквитка HttpOnly + SameSite=Strict (+ Secure);
  - POST иска JSON и Origin от същия хост — чужда страница не може да праща
    от името на влязъл човек;
  - 5 грешни пароли за 15 мин → 429 (на имейл и на IP);
  - разговорите и файловете на друг човек изглеждат като липсващи (404);
  - по един ход на човек, общо най-много `--workers` контейнера наведнъж;
  - файловете се сваля само като прикачени (никога не се показват в нашия
    произход), без символни връзки (кодът в контейнера е чужд) и само докато
    в разговора не върви ход;
  - етап 3 (2026-10-11): спрян профил няма сесия; таксата е от шлюза, не от
    контейнера; webhook-ът на Stripe е единственият POST без вход — пази го
    подписът; админ командите са само в командния ред.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import re
import secrets
import signal
import sqlite3
import stat
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cloud.gateway import gateway as gw
from cloud.runner import launch
from cloud.web import billing
from cloud.web.store import MICRO, SESSION_SECONDS, Store

STATIC = Path(__file__).with_name("static")
COOKIE = "genesis_session"
MAX_BODY = 64 * 1024
MAX_WEBHOOK = 256 * 1024     # събитие от Stripe (сесия/фактура) е няколко KiB
MAX_TEXT = 4000
MAX_FILES = 1000
# Архивът „сваля всичко“ — не спира на MAX_FILES (одит 2026-10-11: при 1500
# файла в out/ result/report.pdf липсваше без знак). Над този брой — 413.
MAX_ZIP_FILES = 20000
MAX_ZIP_BYTES = 200 * 1024 * 1024
LOGIN_WINDOW = 15 * 60
LOGIN_FAILURES = 5
# Подразбиранията за всеки човек (2026-10-11) — профилът може да ги смени
# (`limits`). Ходовете на ден важат и без плащане: всеки ход е контейнер.
TURNS_PER_DAY = 50
MONTHLY_CAP = 2000 * MICRO
HIDDEN = ".genesis"          # историята на разговора (task.py) — не е за сваляне
# През колко секунди shutdown() почиства отново, докато чака живите ходове.
SHUTDOWN_POLL = 1.0

Runner = Callable[..., launch.Result]

_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class LoginBlocked(Exception):
    """Твърде много неуспешни опити — 429."""


class TurnRefused(Exception):
    """Ходът не започва: HTTP статус и обяснение за човека."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    """Всичко без HTTP: може да се тества и да се пусне с фалшив runner."""

    def __init__(self, store: Store, jobs_dir: Path, *, runner: Runner = launch.run_task,
                 workers: int = 2, limits: launch.Limits | None = None,
                 env_file: str | None = None, runtime: str | None = None,
                 secure_cookie: bool = True, trust_proxy: bool | str = False,
                 gateway: launch.Gateway | None = None, recover: bool = True,
                 cleanup: Callable[[], int] | None = None,
                 prices: billing.Prices | None = None, turns_per_day: int = TURNS_PER_DAY,
                 max_tokens_per_turn: int = launch.TASK_BUDGET,
                 monthly_cap: int = MONTHLY_CAP, stripe: billing.StripeConfig | None = None,
                 stripe_opener: billing.Opener | None = None) -> None:
        if prices is not None and gateway is None:
            # Без шлюза токените ги казва кодът в контейнера (2026-10-11).
            raise ValueError("плащането иска шлюза (--gateway-usage)")
        self.store = store
        self.jobs_dir = jobs_dir
        self.runner = runner
        self.limits = limits or launch.Limits()
        self.env_file = env_file
        self.runtime = runtime
        self.secure_cookie = secure_cookie
        self.trust_proxy = trust_proxy
        self.gateway = gateway
        self.prices = prices          # None = без плащане
        self.turns_per_day = turns_per_day
        self.max_tokens_per_turn = max_tokens_per_turn
        self.monthly_cap = monthly_cap  # µc
        if stripe is not None and (prices is None or not stripe.public_url):
            raise ValueError("Stripe иска плащане (--billing) и --public-url")
        self.stripe = stripe
        self.stripe_opener = stripe_opener
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="turn")
        self._failures: dict[str, list[float]] = {}
        self._noted: dict[str, float] = {}     # ключ → кога заключването е записано
        self._lock = threading.Lock()
        self._downloading: set[str] = set()
        self._cleanup = cleanup or launch.cleanup_leftovers
        self._stopping = threading.Event()
        self._futures: set[Future[None]] = set()
        if recover:
            self.recover()

    # ── старт и спиране ─────────────────────────────────────────────────────
    def recover(self) -> None:
        """При старт: контейнерите от предишния процес се убиват, после ходовете
        им стават „failed“ — в тази последователност, иначе човекът пуска нов
        ход в папка, в която старият контейнер още пише (деплой одит 2026-10-11:
        два genesis-task контейнера върху една jobs/1/<чат>). main() го вика
        ЧАК след като портът е зает — втори `serve`, който пада с „Address
        already in use“, не пипа ходовете и контейнерите на живия сървър."""
        self._cleanup()
        self.store.fail_unfinished("сървърът беше рестартиран по време на задачата")
        if self.prices is not None and self.gateway is not None:
            # Ходове без такса от предишния живот: срив между края и таксата,
            # или прекъснати от рестарта (горе) — шлюзът ги е таксувал.
            charged = billing.reconcile(self.store, self.prices, self.gateway.usage)
            if charged:
                print(f"сметка: таксувани {charged} хода от преди рестарта", file=sys.stderr)

    def shutdown(self) -> None:
        """SIGTERM/Ctrl+C: текущите задачи се спират (контейнерите им се убиват),
        чакащите не тръгват. Без това контейнер оставаше Up без срок."""
        self._stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        # Почиства се отново, докато има живи ходове (ревю 2026-10-11): ход,
        # минал проверката `_stopping` точно преди set(), пуска контейнера СЛЕД
        # първото почистване — тогава shutdown чакаше задачата до края ѝ (до
        # 600 s), контейнерът живееше, а ходът ставаше „ok“.
        while True:
            self._cleanup()
            with self._lock:
                running = set(self._futures)
            if not running or not wait(running, timeout=SHUTDOWN_POLL).not_done:
                break
        self.pool.shutdown(wait=True)

    # ── вход ────────────────────────────────────────────────────────────────
    def _recent_failures(self, key: str) -> list[float]:
        now = time.time()
        with self._lock:
            kept = [t for t in self._failures.get(key, []) if now - t < LOGIN_WINDOW]
            self._failures[key] = kept
            return kept

    @staticmethod
    def _email_key(email: str) -> str:
        # Същото свеждане като store.check_password: „ivan@x.bg “ с интервал е
        # същият човек, а броячът беше нов за всеки вариант (одит 2026-10-07:
        # 12 грешни опита с 0..11 интервала — нито един 429).
        return f"e:{email.strip().lower()}"

    def _fresh_lockouts(self, keys: list[str], now: float) -> list[str]:
        """Под self._lock: от заключените `keys` — тези, чието заключване още не
        е записано в този прозорец. По един ред на ключ на 15 мин, не на всеки
        отказан опит: при налучкване с 1000 заявки/s базата не расте с тях."""
        self._noted = {k: t for k, t in self._noted.items() if now - t < LOGIN_WINDOW}
        fresh = [k for k in keys if k not in self._noted]
        for k in fresh:
            self._noted[k] = now
        return fresh

    def _note_lockouts(self, keys: list[str]) -> None:
        """Записва заключванията за cloud/monitor.py (2026-10-12) — само вида."""
        for k in keys:
            kind = "email" if k.startswith("e:") else "signup" if k.startswith("signup:") else "ip"
            # 429 на човека е по-важен от реда за наблюдението.
            with contextlib.suppress(sqlite3.Error):
                self.store.add_lockout(kind)

    def login_blocked(self, email: str, ip: str) -> bool:
        return any(len(self._recent_failures(k)) >= LOGIN_FAILURES
                   for k in (self._email_key(email), f"ip:{ip}"))

    def login(self, email: str, password: str, ip: str) -> str | None:
        """None при грешка ИЛИ при заключване. Опитът се записва като провал
        ПРЕДИ проверката на паролата и под заключване (одит 2026-10-07: 60
        паралелни опита минаваха проверката преди първият провал да е записан
        — 60 проверки, 0 × 429) и се маха само при успех."""
        keys = (self._email_key(email), f"ip:{ip}")
        stamp = time.time()
        with self._lock:
            now = time.time()
            for k in keys:
                self._failures[k] = [t for t in self._failures.get(k, []) if now - t < LOGIN_WINDOW]
            blocked = [k for k in keys if len(self._failures[k]) >= LOGIN_FAILURES]
            fresh = self._fresh_lockouts(blocked, now)
            if not blocked:
                for k in keys:
                    self._failures[k].append(stamp)
        if blocked:
            self._note_lockouts(fresh)
            raise LoginBlocked
        user_id = self.store.check_password(email, password)
        if user_id is None:
            return None
        with self._lock:
            self._failures.pop(self._email_key(email), None)
            ip_list = self._failures.get(f"ip:{ip}", [])
            if stamp in ip_list:
                ip_list.remove(stamp)
        return self.store.new_session(user_id)

    def signup(self, code: str, email: str, password: str, ip: str) -> str:
        """Регистрация с покана → жетон на сесия. ValueError с причината.

        Същата спирачка като входа, на IP (2026-10-11): опитът се брои като
        провал преди проверката и се маха само при успех — иначе поканите се
        налучкваха без таван (6-ият грешен код от един IP е 429)."""
        key = f"ip:{ip}"
        stamp = time.time()
        with self._lock:
            now = time.time()
            self._failures[key] = [t for t in self._failures.get(key, []) if now - t < LOGIN_WINDOW]
            blocked = len(self._failures[key]) >= LOGIN_FAILURES
            fresh = self._fresh_lockouts([f"signup:{key}"] if blocked else [], now)
            if not blocked:
                self._failures[key].append(stamp)
        if blocked:
            self._note_lockouts(fresh)
            raise LoginBlocked
        user_id = self.store.signup(code, email, password)
        with self._lock:
            ip_list = self._failures.get(key, [])
            if stamp in ip_list:
                ip_list.remove(stamp)
        return self.store.new_session(user_id)

    # ── сваляне ─────────────────────────────────────────────────────────────
    def begin_download(self, chat_id: str) -> bool:
        """Сваляне само между ходовете — и ход не започва, докато трае.

        Проверка „не е зает“ веднъж в началото не стигаше (одит 2026-10-07):
        ход, пуснат милисекунди след нея, подменяше папка с връзка посред
        архивирането и в zip-а влизаха файлове на сървъра."""
        with self._lock:
            if self.store.busy(chat_id=chat_id) or chat_id in self._downloading:
                return False
            self._downloading.add(chat_id)
            return True

    def end_download(self, chat_id: str) -> None:
        with self._lock:
            self._downloading.discard(chat_id)

    # ── ходове ──────────────────────────────────────────────────────────────
    def workspace(self, user_id: int, chat_id: str) -> Path:
        return self.jobs_dir / str(user_id) / chat_id

    def limits_for(self, account: dict[str, Any]) -> tuple[int, int, int]:
        """(ходове на ден, токени на ход, месечен таван в µc) за този профил."""
        def pick(key: str, default: int) -> int:
            return default if account[key] is None else int(account[key])
        return (pick("turns_per_day", self.turns_per_day),
                pick("max_tokens_per_turn", self.max_tokens_per_turn),
                pick("monthly_cap", self.monthly_cap))

    def account_view(self, user: dict[str, Any]) -> dict[str, Any]:
        """Сметката на ТОЗИ човек за страницата „Сметка“. Без `ref` (Stripe
        номера, разбивка по модели) — те са за оператора."""
        uid = user["id"]
        account = self.store.account(uid)
        per_day, max_tokens, cap = self.limits_for(account)
        balance = self.store.balance(uid)
        spent = self.store.month_spent(uid)
        prices = self.prices

        def offers(items: Any) -> list[dict[str, Any]]:
            # Само купуваемите: без price id бутонът би дал 400.
            return [{"id": o.id, "credits": o.credits, "price": o.price}
                    for o in items.values() if o.stripe_price]

        fmt = billing.fmt_credits
        return {
            "email": user["email"], "balance": fmt(balance), "balance_micro": balance,
            "held": fmt(self.held(uid)),
            "month_spent": fmt(spent), "monthly_cap": fmt(cap),
            "turns_today": self.store.turns_today(uid), "turns_per_day": per_day,
            "max_tokens_per_turn": max_tokens, "plan": account["plan"],
            "billing": prices is not None, "stripe": self.stripe is not None,
            "packs": offers(prices.packs) if prices else [],
            "plans": offers(prices.plans) if prices else [],
            "history": [{"at": e["created"], "kind": e["kind"], "credits": fmt(e["amount"]),
                         "turn_id": e["turn_id"]} for e in self.store.ledger_entries(uid, 50)],
        }

    def held(self, user_id: int) -> int:
        """µc, задържани за ходове с още жив жетон (0 без плащане)."""
        if self.prices is None:
            return 0
        return billing.held(self.store, self.prices, user_id)

    def _affordable(self, user_id: int, cap: int, prices: billing.Prices, budget: int) -> int:
        """Колко токена може да плати човекът за следващия ход — или TurnRefused.

        Таванът на токените в шлюза (жетонът на задачата) става толкова: шлюзът
        вече спира задачата на него, значи надхарчването е най-много грешката
        в оценката на шлюза. Неизхарченото от жетоните, които още живеят, е
        задържано (ревю 2026-10-11): иначе всеки ход харчеше и бюджета на
        предишния — жетонът му, записан в /work, важи до 11 мин след края."""
        assert self.gateway is not None   # prices без шлюз не тръгва (__init__)
        # Ходовете с изтекъл жетон — окончателно, преди да се смята задържаното.
        billing.settle(self.store, prices, self.gateway.usage, user_id=user_id)
        spent = self.store.month_spent(user_id)
        if spent >= cap:
            raise TurnRefused(402, f"достигна месечния си таван от {billing.fmt_credits(cap)} "
                                   "кредита — пиши ни, ако ти трябва повече")
        balance = self.store.balance(user_id)
        held = billing.held(self.store, prices, user_id)
        room = min(balance, cap - spent) - held
        left = room - prices.per_turn_micro
        if prices.max_rate:
            affordable = left // prices.max_rate
        else:
            affordable = budget if left >= 0 else -1
        if affordable < prices.min_turn_tokens:
            free = min(balance, cap - spent) - prices.per_turn_micro
            if held and (free // prices.max_rate if prices.max_rate
                         else (budget if free >= 0 else -1)) >= prices.min_turn_tokens:
                minutes = -(-(launch.token_ttl(self.limits) + billing.LATE_MARGIN) // 60)
                raise TurnRefused(402, f"{billing.fmt_credits(held)} кредита са задържани за "
                                       "последните ти задачи, докато изтече достъпът им до "
                                       f"моделите (до {minutes} мин след началото им) — опитай "
                                       "след малко или зареди от „Сметка“.")
            if cap - spent < balance:
                raise TurnRefused(402, f"достигна месечния си таван от {billing.fmt_credits(cap)} "
                                       "кредита — пиши ни, ако ти трябва повече")
            need = prices.per_turn_micro + prices.min_turn_tokens * prices.max_rate
            raise TurnRefused(402, f"балансът ти е {billing.fmt_credits(balance)} кредита — "
                                   f"за нова задача трябват поне {billing.fmt_credits(need)}. "
                                   "Зареди от „Сметка“.")
        return affordable

    def start_turn(self, user: dict[str, Any], chat_id: str, text: str) -> int:
        """Нов ход — или TurnRefused. Всички проверки са под заключването: два
        раздела не могат да минат едновременно покрай тях."""
        uid = user["id"]
        with self._lock:
            account = self.store.account(uid)
            if account["disabled"]:
                # Сесията вече е невалидна (session_user) — това е само втора стена.
                raise TurnRefused(403, "профилът е спрян — пиши на поддръжката")
            if self.store.busy(user_id=uid) or chat_id in self._downloading:
                raise TurnRefused(409, "предишната ти задача още върви")
            per_day, budget, cap = self.limits_for(account)
            if self.store.turns_today(uid) >= per_day:
                raise TurnRefused(429, f"днешният лимит от {per_day} задачи е изчерпан — "
                                       "опитай пак утре")
            if self.prices is not None:
                budget = min(budget, self._affordable(uid, cap, self.prices, budget))
            gateway = (dataclasses.replace(self.gateway, budget=min(self.gateway.budget, budget))
                       if self.gateway is not None else None)
            turn_id = self.store.add_turn(chat_id, text)
            job = launch.new_task_name()
            usage_start = gw.usage_size(gateway.usage) if gateway is not None else 0
            if self.prices is not None and gateway is not None:
                # Името на задачата в шлюза — ПРЕДИ контейнера: срив посред хода
                # не губи сметката, reconcile при старт я намира по него. С
                # бюджета на жетона — от него е задържаното (billing.held), и
                # докъде е файлът на шлюза сега: по-рано редове на задачата
                # няма, четенето на сметката почва оттам (както usage_for).
                self.store.set_job(turn_id, job, gateway.budget, usage_start=usage_start)
        fut = self.pool.submit(self._run, turn_id, uid, text, self.workspace(uid, chat_id),
                               job, gateway, usage_start)
        with self._lock:
            self._futures.add(fut)
        fut.add_done_callback(self._forget)
        return turn_id

    def _forget(self, fut: Future[None]) -> None:
        with self._lock:
            self._futures.discard(fut)

    def _run(self, turn_id: int, user_id: int, text: str, workspace: Path, job: str,
             gateway: launch.Gateway | None, usage_start: int = 0) -> None:
        prices = self.prices if gateway is not None else None
        if self._stopping.is_set():
            # Ход, тръгнал след shutdown(), би пуснал контейнер след почистването.
            # Контейнер и жетон няма — няма и какво да се таксува (2026-10-11).
            self.store.drop_job(turn_id)
            self.store.finish_turn(turn_id, ok=False, tokens={}, seconds=0.0,
                                   error="сървърът спря по време на задачата — пусни я пак")
            return
        self.store.set_status(turn_id, "running")
        on_token: Callable[[int], None] | None = None
        if prices is not None:
            # Жетонът се прави в run_task, след мрежата на задачата: дотогава —
            # оценка оттук (не NULL: settle смята NULL за „не е тръгнал“), а
            # on_token я сменя с истинското изтичане, преди контейнерът да
            # тръгне (ревю 2026-10-11: бавен docker демон — до 2×60 s по-късно).
            self.store.job_started(turn_id, billing.settle_after(time.time(),
                                                                 launch.token_ttl(self.limits)))

            def on_token(expires: int) -> None:
                self.store.job_started(turn_id, expires + billing.LATE_MARGIN)
        ok, error, seconds = False, "", 0.0
        tokens: dict[str, int] = {}
        try:
            res = self.runner(text, workspace, limits=self.limits,
                              on_event=lambda e: self.store.add_event(turn_id, e),
                              env_file=self.env_file, runtime=self.runtime,
                              gateway=gateway, name=job, on_token=on_token)
            ok, error, tokens, seconds = res.ok, res.error, res.tokens, res.seconds
            if not ok and self._stopping.is_set():
                error = "сървърът спря по време на задачата — пусни я пак"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        if prices is not None and gateway is not None:
            # Таксата ПРЕДИ края на хода (ревю 2026-10-11): докато е 'running',
            # човекът е зает и нов ход не минава приема с баланс без нея. И
            # провален ход се таксува: шлюзът вече е платил токените му.
            # Сметката е от файла на шлюза по името на задачата — същият път
            # като reconcile, независимо какво е върнал контейнерът. Грешка тук
            # не проваля хода: reconcile при следващия старт го таксува.
            try:
                billing.charge_turn(self.store, prices, user_id, turn_id,
                                    gw.usage_lines(gateway.usage, job, usage_start))
            except Exception as e:
                print(f"сметка: ход {turn_id} не е таксуван: {type(e).__name__}: {e}",
                      file=sys.stderr)
        self.store.finish_turn(turn_id, ok=ok, error=error, tokens=tokens, seconds=seconds)
        if prices is not None and gateway is not None:
            # Покрай всеки ход — окончателната сметка на ходовете с изтекъл жетон
            # (и на хора, които не са пускали нищо оттогава).
            try:
                billing.settle(self.store, prices, gateway.usage)
            except Exception as e:
                print(f"сметка: окончателната сметка не мина: {type(e).__name__}: {e}",
                      file=sys.stderr)


# ── файлове: кодът в контейнера е чужд, всичко от /work е недоверено ────────

# Ревю 2026-10-11: име извън UTF-8 (cp1251 от стар архив или от кода в
# контейнера) идва от os.walk като lone surrogates. JSON-ът и zip-ът гърмяха с
# тях, а пропускането им криеше файла без знак. Показва се с U+FFFD на
# мястото на всеки невалиден байт; за четене остава истинското име.
_SURROGATES = re.compile("[\ud800-\udfff]")


def _shown(workspace: Path, rel: str, used: set[str]) -> str:
    """Показаното име на `rel` — уникално в папката.

    Ревю 2026-10-11: две имена, различни само по невалидните байтове (или
    истинско „�.txt“ до невалидно), се показваха еднакво — find_file даваше
    винаги първото, а files.zip пишеше два записа с едно име и при разархивиране
    единият файл затриваше другия. При съвпадение се добавя „ (2)“, „ (3)“…
    Проверява се и диска, не само обходеното: показано име никога не е името
    на истински файл, затова safe_file(показано) не може да даде чужд файл."""
    shown = _SURROGATES.sub("�", rel)
    if shown == rel:
        return rel
    base = PurePosixPath(shown)
    n = 1
    while shown in used or os.path.lexists(workspace / shown):
        n += 1
        shown = str(base.with_name(f"{base.stem} ({n}){base.suffix}"))
    used.add(shown)
    return shown


def _walk(workspace: Path, limit: int) -> list[tuple[str, str, int]]:
    """До `limit` файла като (показано име, истинско име, размер)."""
    out: list[tuple[str, str, int]] = []
    used: set[str] = set()
    if not workspace.is_dir():
        return out
    for root, dirs, files in os.walk(workspace, followlinks=False):
        rel_root = Path(root).relative_to(workspace)
        dirs[:] = sorted(d for d in dirs if not (rel_root == Path(".") and d == HIDDEN)
                         and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            p = Path(root) / name
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue  # символна връзка, fifo, устройство — не се дава
            rel = (rel_root / name).as_posix()
            out.append((_shown(workspace, rel, used), rel, st.st_size))
            if len(out) >= limit:
                return out
    return out


def list_files(workspace: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """До `limit` файла (MAX_FILES по подразбиране)."""
    return [{"path": shown, "size": size}
            for shown, _, size in _walk(workspace, MAX_FILES if limit is None else limit)]


def find_file(workspace: Path, rel: str) -> Path | None:
    """Като safe_file, но намира и файл, показан с U+FFFD вместо истинското му
    име. Показаните имена са уникални (_shown), затова съвпадението е едно."""
    p = safe_file(workspace, rel)
    if p is None and "\ufffd" in rel:
        real = next((r for shown, r, _ in _walk(workspace, MAX_ZIP_FILES)
                     if shown == rel and r != rel), None)
        p = safe_file(workspace, real) if real is not None else None
    return p


def safe_file(workspace: Path, rel: str) -> Path | None:
    """Пътят до обикновен файл вътре в папката, или None. Без `..`, без
    абсолютни пътища, без символна връзка по пътя, без скритата история."""
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")  # не PurePosixPath: той мълчаливо маха "." и "//"
    if parts[0] == HIDDEN or any(p in ("", ".", "..") for p in parts):
        return None
    p = workspace
    for part in parts:
        p = p / part
        try:
            if stat.S_ISLNK(p.lstat().st_mode):
                return None
        except OSError:
            return None
    if not stat.S_ISREG(p.lstat().st_mode):
        return None
    if not p.resolve().is_relative_to(workspace.resolve()):
        return None
    return p


def read_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def zip_files(workspace: Path) -> bytes | None:
    """Всички файлове в един zip, или None ако са над тавана."""
    files = _walk(workspace, MAX_ZIP_FILES + 1)
    if len(files) > MAX_ZIP_FILES or sum(size for _, _, size in files) > MAX_ZIP_BYTES:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for shown, real, _ in files:
            p = safe_file(workspace, real)
            if p is not None:
                z.writestr(shown, read_file(p))
    return buf.getvalue()


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _with_cost(turn: dict[str, Any]) -> dict[str, Any]:
    """`cost` (кредити като низ) само ако ходът е таксуван; µc не излизат."""
    micro = turn.pop("cost_micro", None)
    if micro is not None:
        turn["cost"] = billing.fmt_credits(micro)
    return turn


_CHAT = re.compile(r"^/api/chats/([0-9a-f]{32})$")
_TURNS = re.compile(r"^/api/chats/([0-9a-f]{32})/turns$")
_FILES = re.compile(r"^/api/chats/([0-9a-f]{32})/files$")
_ZIP = re.compile(r"^/api/chats/([0-9a-f]{32})/files\.zip$")
_FILE = re.compile(r"^/api/chats/([0-9a-f]{32})/files/(.+)$")
_EVENTS = re.compile(r"^/api/turns/(\d+)/events$")


class Handler(BaseHTTPRequestHandler):
    # Сокетът чака най-много толкова и при четене, и при запис: свалящ,
    # който спре да чете, иначе държеше разговора заключен безкрайно (преглед
    # 2026-10-07 — нов ход връщаше 409 и след 30 s).
    timeout = 60
    server_version = "genesis-web"
    sys_version = ""
    app: App  # закача се от make_server

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    # ── помощни ──
    def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**_SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _ip(self) -> str:
        # Заглавие се вярва само от прокси, който го СЛАГА: зад Caddy клиентът
        # може да напише свой CF-Connecting-IP на всяка заявка и да получи нов
        # брояч за опити (одит 2026-10-07). Затова двата режима са отделни.
        if self.app.trust_proxy == "cloudflare":
            ip = self.headers.get("CF-Connecting-IP", "").strip()
            if ip:
                return ip
        elif self.app.trust_proxy:
            # От X-Forwarded-For — ПОСЛЕДНИЯТ адрес (добавен от нашия прокси);
            # първият е каквото клиентът е написал.
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[-1].strip()
        return self.client_address[0]

    def _token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else ""

    def _user(self) -> dict[str, Any] | None:
        return self.app.store.session_user(self._token())

    def _cookie(self, value: str, max_age: int) -> str:
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.app.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def _body(self) -> dict[str, Any] | None:
        """JSON тялото на POST, или None (и отговорът вече е пратен)."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(415, "очаква се JSON")
            return None
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self._error(403, "чужд произход")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._error(413, "твърде голяма заявка")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, RecursionError):   # "[" × 60 000 е RecursionError (2026-10-11)
            self._error(400, "невалиден JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "очаква се обект")
            return None
        return data

    # ── GET ──
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html", "/app.js", "/app.css"):
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            self._send(200, f.read_bytes(), _STATIC_TYPES[f.suffix])
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        user = self._user()
        if user is None:
            self._error(401, "нужен е вход")
            return
        store = self.app.store
        if path == "/api/me":
            me = {"email": user["email"]}
            if self.app.prices is not None:
                me["balance"] = billing.fmt_credits(store.balance(user["id"]))
            self._json(200, me)
        elif path == "/api/account":
            self._json(200, self.app.account_view(user))
        elif path == "/api/chats":
            self._json(200, {"chats": store.chats(user["id"])})
        elif m := _CHAT.match(path):
            chat = store.chat(user["id"], m[1])
            if chat is None:
                self._error(404, "няма такъв разговор")
                return
            turns = store.turns(chat["id"])
            for t in turns:
                t["events"] = store.events(t["id"])
                _with_cost(t)
            self._json(200, {**chat, "turns": turns})
        elif m := _EVENTS.match(path):
            turn = store.turn(user["id"], int(m[1]))
            if turn is None:
                self._error(404, "няма такъв ход")
                return
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json(200, _with_cost({
                "status": turn["status"], "error": turn["error"], "tokens": turn["tokens"],
                "seconds": turn["seconds"], "events": store.events(turn["id"], after),
                "cost_micro": turn.get("cost_micro")}))
        elif (m := _FILES.match(path)) or (m := _ZIP.match(path)) or (m := _FILE.match(path)):
            self._files(user, m)
        else:
            self._error(404, "няма такъв адрес")

    def _files(self, user: dict[str, Any], m: re.Match[str]) -> None:
        store = self.app.store
        chat = store.chat(user["id"], m[1])
        if chat is None:
            self._error(404, "няма такъв разговор")
            return
        ws = self.app.workspace(user["id"], chat["id"])
        if m.re is _FILES:
            # Едно отгоре — за да се знае, че списъкът е отрязан (2026-10-11).
            files = list_files(ws, MAX_FILES + 1)
            self._json(200, {"files": files[:MAX_FILES], "truncated": len(files) > MAX_FILES,
                             "busy": bool(store.busy(chat_id=chat["id"]))})
            return
        # Докато върви ход, кодът в контейнера може да подмени файл с връзка
        # между проверката и четенето. Затова се сваля само между ходовете —
        # и докато трае свалянето, нов ход в този разговор не започва.
        if not self.app.begin_download(chat["id"]):
            self._error(409, "задачата още върви — файловете ще са готови след нея")
            return
        try:
            if m.re is _ZIP:
                data = zip_files(ws)
                if data is None:
                    self._error(413, "файловете са твърде много или твърде големи за един архив")
                    return
                self._send(200, data, "application/octet-stream", {
                    "Content-Disposition": "attachment; filename*=UTF-8''genesis-files.zip"})
                return
            rel = unquote(m[2])
            p = find_file(ws, rel)
            if p is None:
                self._error(404, "няма такъв файл")
                return
            self._send_file(p, PurePosixPath(rel).name)
        finally:
            self.app.end_download(chat["id"])

    def _send_file(self, path: Path, name: str) -> None:
        """Един файл, на парчета — не целият в паметта (одит 2026-10-07: 3 GiB
        празен файл, направен от кода в контейнера, сваляше сървъра с MemoryError)."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            self._error(404, "няма такъв файл")
            return
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > MAX_ZIP_BYTES:
                self._error(413, "файлът е твърде голям за сваляне")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            headers = {**_SECURITY_HEADERS,
                       "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return
            left = size
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ── POST ──
    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/stripe/webhook":
            # Преди _body(): подписът е върху суровите байтове, а Stripe не
            # праща Origin. Единственият POST без вход — пази го HMAC-ът.
            self._stripe_webhook()
            return
        body = self._body()
        if body is None:
            return
        if path == "/api/login":
            email, password = str(body.get("email", "")), str(body.get("password", ""))
            ip = self._ip()
            try:
                token = self.app.login(email, password, ip)
            except LoginBlocked:
                self._error(429, "твърде много опити — опитай пак след 15 минути")
                return
            if token is None:
                self._error(401, "грешен имейл или парола")
                return
            self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(token, SESSION_SECONDS)})
            return
        if path == "/api/signup":
            try:
                token = self.app.signup(str(body.get("code", "")), str(body.get("email", "")),
                                        str(body.get("password", "")), self._ip())
            except LoginBlocked:
                self._error(429, "твърде много опити — опитай пак след 15 минути")
                return
            except ValueError as e:
                self._error(400, str(e))
                return
            self._json(201, {"ok": True}, {"Set-Cookie": self._cookie(token, SESSION_SECONDS)})
            return
        user = self._user()
        if user is None:
            self._error(401, "нужен е вход")
            return
        store = self.app.store
        if path == "/api/logout":
            store.end_session(self._token())
            self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
        elif path == "/api/checkout":
            self._checkout(user, body)
        elif path == "/api/chats":
            title = str(body.get("title") or "Нов разговор").strip()
            self._json(201, {"id": store.new_chat(user["id"], title)})
        elif m := _TURNS.match(path):
            chat = store.chat(user["id"], m[1])
            if chat is None:
                self._error(404, "няма такъв разговор")
                return
            text = str(body.get("text", "")).strip()
            if not text or len(text) > MAX_TEXT:
                self._error(400, f"съобщението трябва да е между 1 и {MAX_TEXT} знака")
                return
            try:
                turn_id = self.app.start_turn(user, chat["id"], text)
            except TurnRefused as e:
                self._error(e.status, e.message)
                return
            self._json(202, {"turn_id": turn_id})
        else:
            self._error(404, "няма такъв адрес")


    # ── плащане ──
    def _stripe_webhook(self) -> None:
        app = self.app
        if app.stripe is None or app.prices is None:
            self._error(404, "няма такъв адрес")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_WEBHOOK:
            self._error(413, "тялото липсва или е твърде голямо")
            return
        payload = self.rfile.read(length)
        if not billing.verify_stripe_signature(payload, self.headers.get("Stripe-Signature", ""),
                                               app.stripe.webhook_secret):
            self._error(400, "невалиден подпис")
            return
        try:
            event = json.loads(payload)
        except (ValueError, RecursionError):
            self._error(400, "невалиден JSON")
            return
        if not isinstance(event, dict):
            self._error(400, "очаква се обект")
            return
        try:
            status, note = billing.handle_event(app.store, app.prices, event, live=app.stripe.live)
        except Exception as e:
            # Всеки запис е един ред (или нищо) — Stripe ще опита пак.
            print(f"stripe: {event.get('id')}: {type(e).__name__}: {e}", file=sys.stderr)
            self._error(500, "грешка — ще се опита пак")
            return
        self._json(status, {"ok": status == 200, "note": note})

    def _checkout(self, user: dict[str, Any], body: dict[str, Any]) -> None:
        app = self.app
        if app.prices is None:
            self._error(404, "няма такъв адрес")
            return
        if app.stripe is None:
            self._error(503, "плащането още не е включено")
            return
        if body.get("pack"):
            offer, mode = app.prices.packs.get(str(body["pack"])), "payment"
        else:
            offer, mode = app.prices.plans.get(str(body.get("plan", ""))), "subscription"
        if offer is None or not offer.stripe_price:
            self._error(400, "няма такъв пакет или план")
            return
        account = app.store.account(user["id"])
        if mode == "subscription" and account["plan"]:
            self._error(409, "вече имаш абонамент")
            return
        kw = {"opener": app.stripe_opener} if app.stripe_opener is not None else {}
        try:
            url = billing.create_checkout(app.stripe, user_id=user["id"], email=user["email"],
                                          offer=offer, mode=mode, **kw)
        except billing.StripeError:
            self._error(502, "плащането не отговаря — опитай пак след малко")
            return
        self._json(200, {"url": url})


# socketserver слуша с опашка 5: при 20 едновременни заявки Linux
# (syncookies) приема връзката и после я реже — клиентът получава
# ConnectionResetError вместо 409/429 (CI 3.13, 2026-10-11).
class _Server(ThreadingHTTPServer):
    request_queue_size = 128


def make_server(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server


# ── командите на оператора ───────────────────────────────────────────────────
# Само от командния ред, без HTTP (2026-10-11): до сървъра се стига през shell,
# който и без това е на оператора; админ адрес в уеба е още нещо за пазене.

def _who(store: Store, email: str) -> dict[str, Any] | None:
    user = store.user_by_email(email)
    if user is None:
        print(f"няма такъв човек: {email.strip().lower()}", file=sys.stderr)
    return user


def _admin(a: argparse.Namespace, store: Store) -> int:
    if a.cmd == "users":
        print(f"{'id':>4}  {'имейл':<32} {'спрян':<6} {'баланс':>10} {'месец':>10} "
              f"{'днес':>5}  план")
        for u in store.users():
            acc = store.account(u["id"])
            print(f"{u['id']:>4}  {u['email']:<32} {'да' if acc['disabled'] else '':<6} "
                  f"{billing.fmt_credits(store.balance(u['id'])):>10} "
                  f"{billing.fmt_credits(store.month_spent(u['id'])):>10} "
                  f"{store.turns_today(u['id']):>5}  {acc['plan']}")
        return 0
    if a.cmd == "invite":
        try:
            credits = billing.parse_credits(a.credits)
            if credits < 0:
                raise ValueError("кредитите на покана не са отрицателни")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        public = (a.public_url or os.environ.get("GENESIS_PUBLIC_URL", "")).strip().rstrip("/")
        for code in store.new_invites(a.count, credits, a.note):
            print(f"{code}  {public}/?invite={code}" if public else code)
        return 0
    if a.cmd == "reconcile":
        try:
            prices = billing.load_prices(a.prices)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        n = billing.reconcile(store, prices, a.gateway_usage)
        print(f"таксувани ходове: {n}")
        return 0
    user = _who(store, a.email)
    if user is None:
        return 1
    uid = user["id"]
    if a.cmd == "balance":
        print(f"{user['email']}: {billing.fmt_credits(store.balance(uid))} кредита "
              f"(този месец похарчени {billing.fmt_credits(store.month_spent(uid))})")
        for row in store.ledger_entries(uid, 20):
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(row["created"]))
            print(f"  {when}  {row['kind']:<12} {billing.fmt_credits(row['amount']):>10}  "
                  f"{row['event_id']}  {row['ref'][:60] if row['kind'] != 'charge' else ''}")
        return 0
    if a.cmd == "grant":
        try:
            amount = billing.parse_credits(a.credits)
            if amount == 0:
                raise ValueError("нула кредита не е промяна")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        store.add_entry(uid, f"admin:{uuid.uuid4()}", "grant" if amount > 0 else "adjust",
                        amount, ref=a.note)
        print(f"{user['email']}: {billing.fmt_credits(amount)} → баланс "
              f"{billing.fmt_credits(store.balance(uid))} кредита")
        return 0
    if a.cmd in ("disable", "enable"):
        store.set_disabled(uid, a.cmd == "disable")
        print(f"{user['email']}: {'спрян, сесиите са затворени' if a.cmd == 'disable' else 'пуснат'}")
        return 0
    # limits
    fields: dict[str, Any] = {}
    if a.clear:
        fields = {"turns_per_day": None, "max_tokens_per_turn": None, "monthly_cap": None}
    if a.turns_per_day is not None:
        fields["turns_per_day"] = a.turns_per_day
    if a.max_tokens is not None:
        fields["max_tokens_per_turn"] = a.max_tokens
    if a.monthly_cap is not None:
        try:
            fields["monthly_cap"] = billing.parse_credits(a.monthly_cap)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    store.set_account(uid, **fields)
    acc = store.account(uid)
    cap = acc["monthly_cap"]
    print(f"{user['email']}: ходове на ден {acc['turns_per_day'] or 'подразбиране'}, "
          f"токени на ход {acc['max_tokens_per_turn'] or 'подразбиране'}, "
          f"месечен таван {billing.fmt_credits(cap) if cap is not None else 'подразбиране'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Genesis онлайн — вход, чат, файлове.")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user", help="нов човек (бета — само ръчно)")
    add.add_argument("email")
    add.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    admin = {
        "users": sub.add_parser("users", help="всички хора: баланс, месец, днес, план"),
        "balance": sub.add_parser("balance", help="баланс и последните 20 реда"),
        "grant": sub.add_parser("grant", help="кредити на човек (отрицателно = корекция)"),
        "disable": sub.add_parser("disable", help="спира човека и затваря сесиите му"),
        "enable": sub.add_parser("enable", help="пуска спрян човек"),
        "limits": sub.add_parser("limits", help="лимити на един човек"),
        "invite": sub.add_parser("invite", help="нови покани (кодове за еднократна регистрация)"),
        "reconcile": sub.add_parser("reconcile", help="таксува ходовете без такса от шлюза"),
    }
    for name, sp in admin.items():
        sp.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
        if name in ("balance", "grant", "disable", "enable", "limits"):
            sp.add_argument("email")
    admin["grant"].add_argument("credits", help="напр. 12.5 или -3")
    admin["grant"].add_argument("--note", default="")
    admin["limits"].add_argument("--turns-per-day", type=int)
    admin["limits"].add_argument("--max-tokens", type=int)
    admin["limits"].add_argument("--monthly-cap", help="кредити")
    admin["limits"].add_argument("--clear", action="store_true", help="обратно към подразбирането")
    admin["invite"].add_argument("--count", type=int, default=1)
    admin["invite"].add_argument("--credits", default="0", help="подарени кредити при регистрация")
    admin["invite"].add_argument("--note", default="")
    admin["invite"].add_argument("--public-url", default="")
    admin["reconcile"].add_argument("--gateway-usage", type=Path, required=True)
    admin["reconcile"].add_argument("--prices", type=Path, default=billing.PRICES)
    srv = sub.add_parser("serve")
    srv.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    srv.add_argument("--jobs", type=Path, default=Path("/srv/jobs"))
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--workers", type=int, default=2, help="контейнери наведнъж")
    srv.add_argument("--seconds", type=int, default=launch.Limits.seconds)
    srv.add_argument("--env-file", help="API ключовете (KEY=value на ред)")
    srv.add_argument("--runtime", help="напр. runsc (gVisor)")
    srv.add_argument("--gateway-usage", type=Path,
                     help="шлюз за моделите: JSONL на шлюза (ключовете не влизат в контейнера)")
    srv.add_argument("--insecure-cookie", action="store_true",
                     help="само за локален тест по http://")
    srv.add_argument("--trust-proxy", action="store_true",
                     help="IP от последния адрес в X-Forwarded-For (зад Caddy/nginx)")
    srv.add_argument("--trust-cloudflare", action="store_true",
                     help="IP от CF-Connecting-IP (само зад Cloudflare Tunnel)")
    srv.add_argument("--billing", action="store_true",
                     help="плащане с кредити (иска --gateway-usage)")
    srv.add_argument("--prices", type=Path, default=billing.PRICES,
                     help="цените (JSON), по подразбиране cloud/web/prices.json")
    srv.add_argument("--turns-per-day", type=int, default=TURNS_PER_DAY,
                     help="ходове на човек на ден (UTC), важи и без плащане")
    srv.add_argument("--max-tokens-per-turn", type=int, default=launch.TASK_BUDGET,
                     help="таван на токените на ход в шлюза")
    srv.add_argument("--monthly-cap", default=str(MONTHLY_CAP // MICRO),
                     help="месечен таван на харченето в кредити (с плащане)")
    srv.add_argument("--public-url", default="",
                     help="https://адресът на услугата (за Stripe; иначе GENESIS_PUBLIC_URL)")
    a = p.parse_args(argv)
    # README-то казва „никога и двете“, а кодът приемаше и вярваше на
    # CF-Connecting-IP — зад Caddy клиентът си го пише сам и спирачката на
    # входа не спираше нищо (одит 2026-10-11: 12 × 401, нито един 429).
    if a.cmd == "serve" and a.trust_proxy and a.trust_cloudflare:
        print("--trust-proxy и --trust-cloudflare са взаимно изключващи се — избери едното",
              file=sys.stderr)
        return 2
    store = Store(a.data / "web.db")
    if a.cmd == "add-user":
        password = secrets.token_urlsafe(12)
        try:
            store.add_user(a.email, password)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{a.email.strip().lower()}  парола: {password}")
        return 0
    if a.cmd != "serve":
        return _admin(a, store)
    try:
        monthly_cap = billing.parse_credits(a.monthly_cap)
    except ValueError as e:
        print(f"--monthly-cap: {e}", file=sys.stderr)
        return 2
    prices = None
    if a.billing:
        if not a.gateway_usage:
            print("плащането иска --gateway-usage: без шлюза токените ги казва кодът в контейнера",
                  file=sys.stderr)
            return 2
        try:
            prices = billing.load_prices(a.prices)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    try:
        stripe = billing.StripeConfig.from_env(os.environ)
        if stripe is not None:
            if prices is None:
                raise ValueError("Stripe ключовете са зададени, а плащането не е (--billing)")
            public = a.public_url or os.environ.get("GENESIS_PUBLIC_URL", "")
            stripe = dataclasses.replace(stripe, public_url=billing.check_public_url(
                public, allow_http=a.insecure_cookie))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    gateway = None
    if a.gateway_usage:
        if not a.env_file:
            print("--gateway-usage иска --env-file с GATEWAY_SECRET", file=sys.stderr)
            return 2
        try:
            gateway = launch.Gateway.from_keys_file(Path(a.env_file), a.gateway_usage)
            if prices is not None:
                billing.check_models(prices, gateway.models)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    app = App(store, a.jobs, workers=a.workers, limits=launch.Limits(seconds=a.seconds),
              env_file=a.env_file, runtime=a.runtime, secure_cookie=not a.insecure_cookie,
              trust_proxy="cloudflare" if a.trust_cloudflare else a.trust_proxy, gateway=gateway,
              recover=False, prices=prices, turns_per_day=a.turns_per_day,
              max_tokens_per_turn=a.max_tokens_per_turn, monthly_cap=monthly_cap, stripe=stripe)
    try:
        server = make_server(app, a.host, a.port)
    except OSError as e:
        # Портът е зает (друг `serve` върви): нищо от неговото не се пипа.
        print(f"{a.host}:{a.port} не може да се заеме: {e}", file=sys.stderr)
        app.pool.shutdown(wait=False)
        return 1
    app.recover()
    # systemd спира услугата със SIGTERM; без обработчик процесът умираше
    # веднага и контейнерите оставаха (деплой одит 2026-10-11).
    signal.signal(signal.SIGTERM, _interrupt)
    print(f"genesis web: http://{a.host}:{server.server_address[1]}", flush=True)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        server.server_close()
        app.shutdown()
    return 0


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
