"""Резервно копие на данните на услугата: web.db и сметката на шлюза.

    python3 -m cloud.backup --data /srv/genesis/web \\
        --usage /srv/genesis/gateway/usage.jsonl --dest /srv/backup/genesis --keep-days 14

Пуска се от таймер (cloud/deploy/genesis-backup.timer, на 6 часа), докато
сървърът работи. Само стандартната библиотека — върви и на Ubuntu 22.04.

Защо (деплой одит 2026-10-11): web.db държи хората, сесиите и ходовете, а
usage.jsonl е единственият запис за сметката (таксува шлюзът, не контейнерът)
— нищо не ги копираше. web.db е с rollback journal (journal_mode=delete) и
онлайн backup API-то на sqlite е безопасно, докато сървърът пише: измерено тук
— integrity_check 'ok' върху копие на жива база.

Копието е в <dest>/<ГГГГММДД-ЧЧММСС>/ с права само за собственика (umask 077:
web.db има scrypt хешове). Извън машината го праща операторът (rsync към
Storage Box) — виж cloud/README.md. Възстановяване: спри genesis-web, сложи
web.db от копието в --data, пусни genesis-web, влез.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_STAMP = re.compile(r"^\d{8}-\d{6}$")


def backup_db(src: Path, dst: Path) -> int:
    """Онлайн копие на SQLite базата + проверка. Връща броя хора в копието."""
    if not src.is_file():
        raise RuntimeError(f"няма {src}")
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"integrity_check на копието: {check}")
            return int(target.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            target.close()
    finally:
        source.close()


def prune(dest: Path, keep_days: float, now: float | None = None) -> list[str]:
    """Трие копията по-стари от `keep_days` — само папки с името на копие."""
    limit = (now if now is not None else time.time()) - keep_days * 86400
    removed = []
    for d in sorted(dest.iterdir()) if dest.is_dir() else []:
        if not (d.is_dir() and not d.is_symlink() and _STAMP.match(d.name)):
            continue
        try:
            made = time.mktime(time.strptime(d.name, "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if made < limit:
            shutil.rmtree(d)
            removed.append(d.name)
    return removed


def run(data: Path, usage: Path | None, dest: Path, keep_days: float) -> str:
    old = os.umask(0o077)
    try:
        return _run(data, usage, dest, keep_days)
    finally:
        os.umask(old)


def _run(data: Path, usage: Path | None, dest: Path, keep_days: float) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = dest / stamp
    out.mkdir()
    try:
        users = backup_db(data / "web.db", out / "web.db")
        notes = [f"web.db: {users} души, integrity ok"]
        if usage is not None:
            if usage.is_file():
                # Само се добавя към него — копие посред запис има най-много
                # един недописан последен ред, който usage_for прескача.
                shutil.copyfile(usage, out / "usage.jsonl")
                notes.append(f"usage.jsonl: {(out / 'usage.jsonl').stat().st_size} байта")
            else:
                notes.append(f"usage.jsonl: няма {usage} (шлюзът не е ползван)")
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    gone = prune(dest, keep_days)
    if gone:
        notes.append(f"изтрити стари: {', '.join(gone)}")
    return f"{out}: " + "; ".join(notes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Резервно копие на web.db и usage.jsonl.")
    p.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    p.add_argument("--usage", type=Path, default=Path("/srv/genesis/gateway/usage.jsonl"))
    p.add_argument("--dest", type=Path, default=Path("/srv/backup/genesis"))
    p.add_argument("--keep-days", type=float, default=14)
    a = p.parse_args(argv)
    try:
        print(run(a.data, a.usage, a.dest, a.keep_days))
    except (OSError, sqlite3.Error, RuntimeError) as e:
        print(f"ГРЕШКА: копието не стана: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
