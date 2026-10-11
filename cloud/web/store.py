"""Данните на уеб услугата в един SQLite файл: хора, сесии, разговори, ходове,
сметка (кредити), профили (спрян, лимити, Stripe клиент) и покани.

Паролите са scrypt със сол; сесиите и поканите се пазят като SHA-256, тоест
изтекла база не дава вход. Всяка операция отваря своя връзка — сървърът е
многонишков, а при този мащаб (бета с 10 души) това е по-просто от пул.

Новите таблици (2026-10-11) са само CREATE ... IF NOT EXISTS, без ALTER на
`users`/`turns`: вече пусната web.db се допълва сама при първото отваряне.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_SECONDS = 14 * 24 * 3600
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}

_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(16)))
    return _DUMMY[0]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | ok | failed
    error TEXT NOT NULL DEFAULT '',
    tokens TEXT NOT NULL DEFAULT '{}',
    seconds REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    turn_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (turn_id, seq)
);
-- Сметката е сбор от редове, не колона с баланс (2026-10-11): колоната губи
-- историята, а повторен Stripe webhook я удвоява. Редът се пише веднъж по
-- event_id и не се пипа — поправка е нов ред 'adjust'. Сумите са цели µc.
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_id TEXT UNIQUE NOT NULL,  -- turn:<id> | stripe:<cs_|in_> | admin:<uuid> | invite:<hash16>
    kind TEXT NOT NULL CHECK (kind IN ('charge','purchase','subscription','grant','adjust')),
    amount INTEGER NOT NULL,
    turn_id INTEGER,
    ref TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, created);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    disabled INTEGER NOT NULL DEFAULT 0,
    turns_per_day INTEGER,          -- NULL = подразбирането на сървъра
    max_tokens_per_turn INTEGER,    -- NULL = подразбирането на сървъра
    monthly_cap INTEGER,            -- µc; NULL = подразбирането на сървъра
    stripe_customer TEXT UNIQUE,
    plan TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS turn_jobs (
    turn_id INTEGER PRIMARY KEY,
    job TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY,
    credits INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    used_by INTEGER,
    used_at REAL
);
"""

KINDS = ("charge", "purchase", "subscription", "grant", "adjust")
MICRO = 1_000_000          # µc в един кредит
_ACCOUNT_FIELDS = ("disabled", "turns_per_day", "max_tokens_per_turn", "monthly_cap",
                   "stripe_customer", "plan")
_ACCOUNT_DEFAULTS: dict[str, Any] = {"disabled": False, "turns_per_day": None,
                                     "max_tokens_per_turn": None, "monthly_cap": None,
                                     "stripe_customer": None, "plan": ""}


def _day_start(now: float | None = None) -> float:
    t = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _month_start(now: float | None = None) -> float:
    t = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _check_new_user(email: str, password: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(password) < 10:
        raise ValueError("нужни са имейл и парола от поне 10 знака")
    return email


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(got.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    # ── хора и сесии ────────────────────────────────────────────────────────
    def add_user(self, email: str, password: str) -> int:
        email = _check_new_user(email, password)
        try:
            with self._db() as c:
                cur = c.execute("INSERT INTO users (email, password, created) VALUES (?,?,?)",
                                (email, hash_password(password), time.time()))
        except sqlite3.IntegrityError:
            raise ValueError(f"{email} вече съществува") from None
        return int(cur.lastrowid or 0)

    def check_password(self, email: str, password: str) -> int | None:
        with self._db() as c:
            row = c.execute("SELECT u.id, u.password, COALESCE(a.disabled, 0) AS disabled "
                            "FROM users u LEFT JOIN accounts a ON a.user_id = u.id "
                            "WHERE u.email = ?", (email.strip().lower(),)).fetchone()
        if row is None:
            # Същото време като при грешна парола — да не издава кои имейли има.
            # ЕДНО scrypt, като при съществуващ имейл: hash_password + verify бяха
            # две и непознатият имейл се отказваше 2× по-бавно (одит 2026-10-07).
            verify_password(password, _dummy_hash())
            return None
        ok = verify_password(password, row["password"])
        # Спрян профил (2026-10-11): същото едно scrypt и после отказ — времето
        # не казва дали профилът е спрян, а отговорът е като при грешна парола.
        return int(row["id"]) if ok and not row["disabled"] else None

    def new_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            c.execute("INSERT INTO sessions VALUES (?,?,?)",
                      (_token_hash(token), user_id, now + SESSION_SECONDS))
        return token

    def session_user(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._db() as c:
            # Спрян профил няма сесия, дори да е останала стара бисквитка.
            row = c.execute(
                "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
                "LEFT JOIN accounts a ON a.user_id = u.id "
                "WHERE s.token_hash = ? AND s.expires > ? AND COALESCE(a.disabled, 0) = 0",
                (_token_hash(token), time.time()),
            ).fetchone()
        return {"id": int(row["id"]), "email": row["email"]} if row else None

    def end_session(self, token: str) -> None:
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # ── разговори и ходове ─────────────────────────────────────────────────
    def new_chat(self, user_id: int, title: str) -> str:
        chat_id = uuid.uuid4().hex
        with self._db() as c:
            c.execute("INSERT INTO chats VALUES (?,?,?,?)",
                      (chat_id, user_id, title[:80], time.time()))
        return chat_id

    def chats(self, user_id: int) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT id, title, created FROM chats WHERE user_id = ? "
                             "ORDER BY created DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def chat(self, user_id: int, chat_id: str) -> dict[str, Any] | None:
        """Разговорът, само ако е на този човек — чуждият изглежда като липсващ."""
        with self._db() as c:
            row = c.execute("SELECT id, title, created FROM chats WHERE id = ? AND user_id = ?",
                            (chat_id, user_id)).fetchone()
        return dict(row) if row else None

    def add_turn(self, chat_id: str, text: str) -> int:
        with self._db() as c:
            cur = c.execute("INSERT INTO turns (chat_id, text, status, created) "
                            "VALUES (?,?,'queued',?)", (chat_id, text, time.time()))
        return int(cur.lastrowid or 0)

    def set_status(self, turn_id: int, status: str) -> None:
        with self._db() as c:
            c.execute("UPDATE turns SET status = ? WHERE id = ?", (status, turn_id))

    def add_event(self, turn_id: int, event: dict[str, Any]) -> None:
        with self._db() as c:
            c.execute("INSERT INTO events (turn_id, seq, data) VALUES "
                      "(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE turn_id = ?), ?)",
                      (turn_id, turn_id, json.dumps(event, ensure_ascii=False)))

    def finish_turn(self, turn_id: int, *, ok: bool, error: str,
                    tokens: dict[str, int], seconds: float) -> None:
        with self._db() as c:
            c.execute("UPDATE turns SET status = ?, error = ?, tokens = ?, seconds = ? WHERE id = ?",
                      ("ok" if ok else "failed", error[:500], json.dumps(tokens), seconds, turn_id))

    # Цената на хода (µc, ≥ 0) — от реда в сметката, ако има такъв.
    _TURN_SELECT = ("SELECT t.*, -l.amount AS cost_micro FROM turns t "
                    "LEFT JOIN ledger l ON l.event_id = 'turn:' || t.id ")

    def turn(self, user_id: int, turn_id: int) -> dict[str, Any] | None:
        with self._db() as c:
            row = c.execute(self._TURN_SELECT + "JOIN chats ch ON ch.id = t.chat_id "
                            "WHERE t.id = ? AND ch.user_id = ?", (turn_id, user_id)).fetchone()
        return self._turn_dict(row) if row else None

    def turns(self, chat_id: str) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute(self._TURN_SELECT + "WHERE t.chat_id = ? ORDER BY t.id",
                             (chat_id,)).fetchall()
        return [self._turn_dict(r) for r in rows]

    def events(self, turn_id: int, after: int = 0) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT seq, data FROM events WHERE turn_id = ? AND seq > ? "
                             "ORDER BY seq", (turn_id, after)).fetchall()
        # Номерът е на базата: `seq` от контейнера не бива да го подменя — иначе
        # клиентът показваше събития два пъти или спираше да вижда нови (одит 2026-10-09).
        return [{**json.loads(r["data"]), "seq": r["seq"]} for r in rows]

    def busy(self, *, user_id: int | None = None, chat_id: str | None = None) -> int:
        """Незавършени ходове (на човек, на разговор или общо)."""
        sql = ("SELECT COUNT(*) FROM turns t JOIN chats ch ON ch.id = t.chat_id "
               "WHERE t.status IN ('queued', 'running')")
        args: list[Any] = []
        if user_id is not None:
            sql += " AND ch.user_id = ?"
            args.append(user_id)
        if chat_id is not None:
            sql += " AND t.chat_id = ?"
            args.append(chat_id)
        with self._db() as c:
            return int(c.execute(sql, args).fetchone()[0])

    def fail_unfinished(self, reason: str) -> int:
        """При старт: ходовете, прекъснати от спиране на сървъра, не висят вечно."""
        with self._db() as c:
            cur = c.execute("UPDATE turns SET status = 'failed', error = ? "
                            "WHERE status IN ('queued', 'running')", (reason,))
        return cur.rowcount

    @staticmethod
    def _turn_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["tokens"] = json.loads(d.get("tokens") or "{}")
        if d.get("cost_micro") is None:
            d.pop("cost_micro", None)
        return d

    # ── сметка (само добавяне) ─────────────────────────────────────────────
    @staticmethod
    def _add_entry(c: sqlite3.Connection, user_id: int, event_id: str, kind: str, amount: int,
                   turn_id: int | None, ref: str) -> bool:
        # type(), не isinstance: True е int, а 0.1 като float губи µc.
        if type(amount) is not int:
            raise ValueError(f"сумата трябва да е цяло число µc, а е {type(amount).__name__}")
        if kind not in KINDS:
            raise ValueError(f"непознат вид запис: {kind}")
        if not event_id:
            raise ValueError("липсва event_id")
        # ON CONFLICT(event_id), не INSERT OR IGNORE: OR IGNORE мълчаливо
        # прескача и нарушен CHECK/NOT NULL, а тук само повторът е „вече е“.
        cur = c.execute("INSERT INTO ledger (user_id, event_id, kind, amount, turn_id, ref, created) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                        (user_id, event_id, kind, amount, turn_id, ref[:2000], time.time()))
        return cur.rowcount == 1

    def add_entry(self, user_id: int, event_id: str, kind: str, amount: int, *,
                  turn_id: int | None = None, ref: str = "") -> bool:
        """True ако записът е нов; False — събитието вече е отчетено (не е грешка)."""
        with self._db() as c:
            return self._add_entry(c, user_id, event_id, kind, amount, turn_id, ref)

    def has_entry(self, event_id: str) -> bool:
        with self._db() as c:
            return c.execute("SELECT 1 FROM ledger WHERE event_id = ?",
                             (event_id,)).fetchone() is not None

    def balance(self, user_id: int) -> int:
        with self._db() as c:
            return int(c.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = ?",
                                 (user_id,)).fetchone()[0])

    def month_spent(self, user_id: int, now: float | None = None) -> int:
        """Похарченото (µc, ≥ 0) от началото на календарния месец по UTC."""
        with self._db() as c:
            return -int(c.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = ? "
                                  "AND kind = 'charge' AND created >= ?",
                                  (user_id, _month_start(now))).fetchone()[0])

    def ledger_entries(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT * FROM ledger WHERE user_id = ? "
                             "ORDER BY created DESC, id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ── профили и лимити ───────────────────────────────────────────────────
    def account(self, user_id: int) -> dict[str, Any]:
        with self._db() as c:
            row = c.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        out = dict(_ACCOUNT_DEFAULTS)
        if row is not None:
            out.update({k: row[k] for k in _ACCOUNT_FIELDS})
            out["disabled"] = bool(row["disabled"])
        return out

    def set_account(self, user_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(_ACCOUNT_FIELDS)
        if unknown:
            raise ValueError(f"непознати полета: {', '.join(sorted(unknown))}")
        if not fields:
            return
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        # Имената на колоните са само от _ACCOUNT_FIELDS (проверено горе).
        sql = (f"INSERT INTO accounts (user_id, {cols}) VALUES (?, {marks}) "
               f"ON CONFLICT(user_id) DO UPDATE SET {updates}")
        with self._db() as c:
            c.execute(sql, (user_id, *values))

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        """Спирането затваря и всички сесии — в същата транзакция."""
        with self._db() as c:
            c.execute("INSERT INTO accounts (user_id, disabled) VALUES (?, ?) "
                      "ON CONFLICT(user_id) DO UPDATE SET disabled = excluded.disabled",
                      (user_id, int(disabled)))
            if disabled:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def users(self) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT id, email FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._db() as c:
            row = c.execute("SELECT id, email FROM users WHERE email = ?",
                            (email.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def user_exists(self, user_id: int) -> bool:
        with self._db() as c:
            return c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None

    def user_by_customer(self, customer: str) -> int | None:
        if not customer:
            return None
        with self._db() as c:
            row = c.execute("SELECT user_id FROM accounts WHERE stripe_customer = ?",
                            (customer,)).fetchone()
        return int(row["user_id"]) if row else None

    def turns_today(self, user_id: int, now: float | None = None) -> int:
        """Ходовете от полунощ UTC — всякакви, и провалените: спам с грешни
        задачи пак пуска контейнери."""
        with self._db() as c:
            return int(c.execute("SELECT COUNT(*) FROM turns t JOIN chats ch ON ch.id = t.chat_id "
                                 "WHERE ch.user_id = ? AND t.created >= ?",
                                 (user_id, _day_start(now))).fetchone()[0])

    def set_job(self, turn_id: int, job: str) -> None:
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO turn_jobs (turn_id, job) VALUES (?, ?)", (turn_id, job))

    def unbilled_turns(self) -> list[dict[str, Any]]:
        """Завършени ходове с име на задача в шлюза и без ред в сметката."""
        with self._db() as c:
            rows = c.execute(
                "SELECT t.id AS turn_id, ch.user_id AS user_id, j.job AS job FROM turns t "
                "JOIN turn_jobs j ON j.turn_id = t.id JOIN chats ch ON ch.id = t.chat_id "
                "WHERE t.status IN ('ok', 'failed') AND NOT EXISTS "
                "(SELECT 1 FROM ledger l WHERE l.event_id = 'turn:' || t.id) ORDER BY t.id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── покани ─────────────────────────────────────────────────────────────
    def new_invites(self, n: int, credits_micro: int = 0, note: str = "") -> list[str]:
        if type(credits_micro) is not int or credits_micro < 0:
            raise ValueError("кредитите на поканата са цяло число µc ≥ 0")
        codes = [secrets.token_urlsafe(12) for _ in range(n)]
        now = time.time()
        with self._db() as c:
            c.executemany("INSERT INTO invites (code_hash, credits, note, created) VALUES (?,?,?,?)",
                          [(_token_hash(code), credits_micro, note[:200], now) for code in codes])
        return codes

    def signup(self, code: str, email: str, password: str) -> int:
        """Нов човек срещу покана — всичко в една транзакция.

        Поканата се заема първа (UPDATE ... WHERE used_by IS NULL): заключването
        за запис на SQLite подрежда паралелните опити и загубилият вижда 0 реда.
        Дублиран имейл вдига грешка, транзакцията се отменя и поканата остава."""
        email = _check_new_user(email, password)
        hashed = hash_password(password)   # scrypt извън транзакцията
        code_hash = _token_hash(code)
        now = time.time()
        with self._db() as c:
            cur = c.execute("UPDATE invites SET used_by = -1, used_at = ? "
                            "WHERE code_hash = ? AND used_by IS NULL", (now, code_hash))
            if cur.rowcount != 1:
                raise ValueError("поканата е невалидна или вече е използвана")
            try:
                cur = c.execute("INSERT INTO users (email, password, created) VALUES (?,?,?)",
                                (email, hashed, now))
            except sqlite3.IntegrityError:
                raise ValueError(f"{email} вече съществува") from None
            user_id = int(cur.lastrowid or 0)
            c.execute("UPDATE invites SET used_by = ? WHERE code_hash = ?", (user_id, code_hash))
            credits = int(c.execute("SELECT credits FROM invites WHERE code_hash = ?",
                                    (code_hash,)).fetchone()[0])
            if credits > 0:
                self._add_entry(c, user_id, f"invite:{code_hash[:16]}", "grant", credits,
                                None, "покана")
        return user_id
