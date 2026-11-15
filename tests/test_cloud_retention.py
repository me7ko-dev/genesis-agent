"""Срокът на разговорите (2026-11-15): поверителността обещава, че разговорите и
файловете се пазят до {{СРОК_РАЗГОВОРИ}} (напр. 90 дни след последния ход), а
нищо не триеше нито редовете в базата, нито `/srv/jobs/<човек>/<разговор>`. С
качването на файлове (фактури — лични данни, до 1 GiB на човек на ден) дискът
растеше без край.

`serve --keep-days N` трие разговорите без ход от N дни: редовете (с
secure_delete, като `forget`) и папката. Сметката (ledger) остава. Не пипа
разговор с ход в ход, със сваляне/качване или с неокончателна сметка.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from cloud.web import server as web
from cloud.web.store import Store

DAY = 86400.0
LATER = 100 * DAY          # „сега“ в теста: 100 дни след създаването


class NoRunner:
    def __call__(self, *a, **kw):  # pragma: no cover — тук ходове не тръгват
        raise AssertionError("ход не се пуска")


def _app(tmp: Path, keep_days: int = 90) -> web.App:
    return web.App(Store(tmp / "web.db"), tmp / "jobs", runner=NoRunner(), keep_days=keep_days)


def _chat(app: web.App, uid: int, text: str = "тайна задача") -> tuple[str, int, Path]:
    chat = app.store.new_chat(uid, "разговор")
    turn = app.store.add_turn(chat, text)
    app.store.add_event(turn, {"type": "answer", "text": "тайният отговор"})
    app.store.finish_turn(turn, ok=True, error="", tokens={}, seconds=1.0)
    folder = app.workspace(uid, chat)
    folder.mkdir(parents=True)
    (folder / "invoice.pdf").write_bytes(b"%PDF fake")
    (folder / "out").mkdir()
    (folder / "out" / "sums.csv").write_text("1,2\n", encoding="utf-8")
    return chat, turn, folder


def _count(app: web.App, sql: str, *args: object) -> int:
    with sqlite3.connect(app.store.path) as c:
        return int(c.execute(sql, args).fetchone()[0])


@pytest.fixture
def app(tmp_path):
    a = _app(tmp_path)
    yield a
    a.pool.shutdown(wait=True)


def test_an_old_chat_is_deleted_with_its_folder_but_the_ledger_stays(app) -> None:
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    app.store.add_entry(uid, "stripe:cs_1", "purchase", 500 * 1_000_000)
    chat, turn, folder = _chat(app, uid)

    assert app.expire(now=time.time() + LATER) == 1

    assert not folder.exists() and app.store.chat(uid, chat) is None
    assert _count(app, "SELECT COUNT(*) FROM turns WHERE chat_id = ?", chat) == 0
    assert _count(app, "SELECT COUNT(*) FROM events WHERE turn_id = ?", turn) == 0
    assert app.store.balance(uid) == 500 * 1_000_000
    raw = Path(app.store.path).read_bytes()
    assert "тайна задача".encode() not in raw and "тайният отговор".encode() not in raw


def test_a_recent_chat_and_an_old_chat_with_a_recent_turn_stay(app) -> None:
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    old, _, old_folder = _chat(app, uid)
    recent, _, recent_folder = _chat(app, uid)
    revived, _, _ = _chat(app, uid)
    now = time.time() + LATER
    with sqlite3.connect(app.store.path) as c:
        c.execute("UPDATE chats SET created = ?", (now - 200 * DAY,))
        c.execute("UPDATE turns SET created = ? WHERE chat_id = ?", (now - 120 * DAY, old))
        c.execute("UPDATE turns SET created = ? WHERE chat_id != ?", (now - 10 * DAY, old))
    os.utime(old_folder, (now - 120 * DAY, now - 120 * DAY))
    os.utime(recent_folder, (now - 10 * DAY, now - 10 * DAY))

    assert app.expire(now=now) == 1
    assert app.store.chat(uid, old) is None
    assert app.store.chat(uid, recent) is not None and app.store.chat(uid, revived) is not None
    assert recent_folder.exists()


def test_a_file_added_after_the_last_turn_keeps_the_chat(app) -> None:
    """Качен файл не е ход: денят на качването е в папката, не в базата."""
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    chat, _, folder = _chat(app, uid)
    now = time.time() + LATER
    with sqlite3.connect(app.store.path) as c:
        c.execute("UPDATE turns SET created = ?", (now - 120 * DAY,))
        c.execute("UPDATE chats SET created = ?", (now - 120 * DAY,))
    os.utime(folder, (now - 5 * DAY, now - 5 * DAY))
    assert app.expire(now=now) == 0
    assert folder.exists() and app.store.chat(uid, chat) is not None


def test_a_busy_chat_is_not_touched(app) -> None:
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    running = app.store.new_chat(uid, "върви")
    app.store.add_turn(running, "още върви")                 # статус queued
    unsettled, turn, _ = _chat(app, uid)
    app.store.set_job(turn, "genesis-task-1", 100)
    app.store.job_started(turn, 0.0)                         # сметката не е окончателна
    downloading, _, folder = _chat(app, uid)
    app._downloading[downloading] += 1                      # сваляне (и качване) в ход

    assert app.expire(now=time.time() + LATER) == 0
    for chat in (running, unsettled, downloading):
        assert app.store.chat(uid, chat) is not None
    assert folder.exists()

    app._downloading[downloading] -= 1
    app.store.mark_settled(turn)
    assert app.expire(now=time.time() + LATER) == 2          # ходът в ход остава


def test_without_keep_days_nothing_is_deleted(tmp_path) -> None:
    app = _app(tmp_path, keep_days=0)
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    chat, _, folder = _chat(app, uid)
    assert app.expire(now=time.time() + 10 * 365 * DAY) == 0
    assert folder.exists() and app.store.chat(uid, chat) is not None
    app.pool.shutdown(wait=True)


def test_a_folder_left_without_its_chat_is_swept_when_old(app) -> None:
    """Триенето на папката е след базата; ако е спряло (EBUSY, срив), следващият
    проход я намира по това, че разговорът ѝ го няма."""
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    user_dir = app.jobs_dir / str(uid)
    old = user_dir / ("a" * 32)
    new = user_dir / ("b" * 32)
    other = user_dir / "notes"                               # не е папка на разговор
    for d in (old, new, other):
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    now = time.time() + LATER
    os.utime(old, (now - 120 * DAY, now - 120 * DAY))
    os.utime(new, (now - 1 * DAY, now - 1 * DAY))
    os.utime(other, (now - 120 * DAY, now - 120 * DAY))

    app.expire(now=now)
    assert not old.exists() and new.exists() and other.exists()


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="връзки — само POSIX")
def test_a_chat_folder_that_is_a_link_is_not_followed(app, tmp_path) -> None:
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    chat = app.store.new_chat(uid, "x")
    turn = app.store.add_turn(chat, "x")
    app.store.finish_turn(turn, ok=True, error="", tokens={}, seconds=1.0)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    folder = app.workspace(uid, chat)
    folder.parent.mkdir(parents=True)
    folder.symlink_to(outside, target_is_directory=True)

    assert app.expire(now=time.time() + LATER) == 1
    assert (outside / "keep.txt").exists()


def test_a_turn_in_a_chat_deleted_meanwhile_is_refused(app) -> None:
    """Разговорът е изтрит между проверката в обработчика и start_turn: ходът
    не тръгва в разговор без ред (сирачета в turns)."""
    uid = app.store.add_user("ana@example.com", "correct-horse-battery")
    chat, _, _ = _chat(app, uid)
    user = {"id": uid, "email": "ana@example.com"}
    app.expire(now=time.time() + LATER)
    with pytest.raises(web.TurnRefused) as e:
        app.start_turn(user, chat, "още нещо")
    assert e.value.status == 404
    assert _count(app, "SELECT COUNT(*) FROM turns WHERE chat_id = ?", chat) == 0


def test_serve_takes_keep_days() -> None:
    a = web.build_parser().parse_args(["serve", "--keep-days", "90"])
    assert a.keep_days == 90
    assert web.build_parser().parse_args(["serve"]).keep_days == 0
