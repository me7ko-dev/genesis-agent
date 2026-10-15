"""Тестовете не пишат в genesis_agent/ (2026-10-15).

В checkout DATA_DIR по подразбиране е самата папка на пакета — там е
истинската памет на оператора. memory и episodic_memory създават базите си
при внасяне, тоест преди фикстурите в conftest да са пренасочили пътя:
пълният пакет оставяше episodes.db, persistent_memory.db и -wal/-shm в
genesis_agent/. Освен тези проверки conftest._package_dir_untouched пада
след всеки тест, който остави файл там.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from genesis_agent import config, episodic_memory, memory

PACKAGE_DIR = Path(config.__file__).resolve().parent


def _inside_package(path: Path | str) -> bool:
    p = Path(path).resolve()
    return p == PACKAGE_DIR or PACKAGE_DIR in p.parents


def test_data_dir_of_the_test_session_is_not_the_package() -> None:
    assert not _inside_package(config.DATA_DIR), config.DATA_DIR
    assert not _inside_package(config.MEMORY_DIR), config.MEMORY_DIR


def test_databases_created_at_import_were_not_in_the_package() -> None:
    # Пътищата, с които модулите са се внесли (conftest ги подменя после за
    # всеки тест — затова се гледа DATA_DIR, от който са изведени).
    assert not (PACKAGE_DIR / "episodes.db").exists()
    assert not (PACKAGE_DIR / "persistent_memory.db").exists()
    assert not _inside_package(episodic_memory.DB_PATH)
    assert not _inside_package(memory.DB_PATH)


def test_a_subprocess_inherits_the_isolation() -> None:
    before = set(os.listdir(PACKAGE_DIR))
    code = ("import genesis_agent.memory, genesis_agent.episodic_memory, "
            "genesis_agent.config as c; print(c.DATA_DIR)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                         timeout=120, check=True).stdout.strip()
    assert not _inside_package(out), out
    assert set(os.listdir(PACKAGE_DIR)) - before == set()
