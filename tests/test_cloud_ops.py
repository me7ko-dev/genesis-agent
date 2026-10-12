"""cloud/backup.py (копие на web.db и usage.jsonl, докато сървърът пише) и
cloud/check.py (готовност) — с фалшив Docker от tests/conftest.py."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import pytest

from cloud import backup, check
from cloud.web.store import Store


def _live(tmp_path):
    data = tmp_path / "web"
    store = Store(data / "web.db")
    store.add_user("ana@example.com", "correct-horse-battery")
    usage = tmp_path / "usage.jsonl"
    usage.write_text('{"job": "genesis-task-1", "total_tokens": 10}\n', encoding="utf-8")
    return data, store, usage


def test_a_backup_of_a_live_database_is_whole(tmp_path) -> None:
    """Деплой одит 2026-10-11: нищо не копираше web.db и сметката."""
    data, store, usage = _live(tmp_path)
    uid = store.add_user("bob@example.com", "correct-horse-battery")
    chat = store.new_chat(uid, "t")
    stop = threading.Event()

    def writer() -> None:                       # сървърът пише през цялото време
        while not stop.is_set():
            store.add_event(store.add_turn(chat, "x"), {"kind": "info", "text": "y" * 500})

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.05)
        line = backup.run(data, usage, tmp_path / "bk", keep_days=14)
    finally:
        stop.set()
        t.join()
    assert "2 души, integrity ok" in line
    (copy,) = list((tmp_path / "bk").iterdir())
    db = sqlite3.connect(copy / "web.db")
    try:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        db.close()
    assert (copy / "usage.jsonl").read_bytes() == usage.read_bytes()
    if os.name != "nt":   # web.db има scrypt хешове — само за собственика
        assert (copy / "web.db").stat().st_mode & 0o077 == 0
        assert copy.stat().st_mode & 0o077 == 0


def test_old_backups_are_pruned_and_nothing_else(tmp_path) -> None:
    dest = tmp_path / "bk"
    for name in ("20260901-000000", "20261010-120000", "notes", "2026-x"):
        (dest / name).mkdir(parents=True)
    now = time.mktime(time.strptime("20261011-120000", "%Y%m%d-%H%M%S"))
    assert backup.prune(dest, keep_days=14, now=now) == ["20260901-000000"]
    assert sorted(p.name for p in dest.iterdir()) == ["2026-x", "20261010-120000", "notes"]


def test_a_missing_database_fails_and_leaves_no_half_copy(tmp_path, capsys) -> None:
    code = backup.main(["--data", str(tmp_path / "none"), "--usage", str(tmp_path / "u.jsonl"),
                        "--dest", str(tmp_path / "bk")])
    assert code == 1 and "ГРЕШКА" in capsys.readouterr().err
    assert list((tmp_path / "bk").iterdir()) == []


def test_no_gateway_yet_is_not_an_error(tmp_path, capsys) -> None:
    data, _, _ = _live(tmp_path)
    assert backup.main(["--data", str(data), "--usage", str(tmp_path / "none.jsonl"),
                        "--dest", str(tmp_path / "bk")]) == 0
    assert "шлюзът не е ползван" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="umask")
def test_the_process_umask_is_restored(tmp_path) -> None:
    data, _, usage = _live(tmp_path)
    before = os.umask(0o022)
    try:
        backup.run(data, usage, tmp_path / "bk", keep_days=1)
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


# ── cloud/check.py: готовност по истинския път на задача ─────────────────────

def _ready(fake_docker, answers: dict | None = None) -> None:
    fake_docker.answers.update({("info",): (0, "29.8.2", ""), ("image",): (0, "sha256:x", ""),
                                ("inspect",): (0, "true false", "")})
    fake_docker.answers.update(answers or {})


def test_check_fails_when_no_helper_runs(fake_docker, tmp_path) -> None:
    """Деплой одит 2026-10-11: /healthz беше 200, а нито една задача не можеше да тръгне."""
    _ready(fake_docker, {("inspect",): (1, "", "No such object")})
    with pytest.raises(check.CheckFailed, match="up.sh"):
        check.run_checks(tmp_path, None, 0, say=lambda s: None)


def test_check_fails_on_a_restarting_gateway(fake_docker, tmp_path) -> None:
    """up.sh печаташе „готово“, докато шлюзът беше в restarting."""
    _ready(fake_docker, {("inspect",): (0, "true true", "")})
    with pytest.raises(check.CheckFailed, match="genesis-gateway не е Up"):
        check.run_checks(tmp_path, None, 0, say=lambda s: None)


def test_check_fails_when_a_task_can_get_out(fake_docker, tmp_path) -> None:
    probe = {"uid": 10001, "direct": "open", "rootfs": "read-only", "cap_eff": 0,
             "no_new_privs": 1, "helper": 200}
    _ready(fake_docker, {("run",): (0, json.dumps(probe), "")})
    with pytest.raises(check.CheckFailed, match="пряк изход"):
        check.run_checks(tmp_path, None, 0, say=lambda s: None)
    # пробата върви в мрежа на задача, с тавани, и мрежата се маха
    run = next(c for c in fake_docker.calls if c[0] == "run")
    net = run[run.index("--network") + 1]
    assert net.startswith("genesis-job-") and "--read-only" in run and "--cap-drop" in run
    assert ["network", "rm", net] in fake_docker.calls


def test_check_main_says_what_failed(fake_docker, tmp_path, capsys) -> None:
    fake_docker.answers[("info",)] = (1, "", "Cannot connect to the Docker daemon")
    assert check.main(["--jobs", str(tmp_path)]) == 1
    assert "Docker не отговаря" in capsys.readouterr().err
