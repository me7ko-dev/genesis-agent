"""GENESIS_DEBUG=1 → ~/.genesis/debug.log (2026-10-15).

Местата, които нарочно не спират работата при грешка, я записват с
log.debug(exc_info=True). Без handler тези записи изчезваха — по същия начин,
по който изчезваше и самата грешка при `except Exception: pass`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from genesis_agent import cli, debug_log, paths


def _log_text() -> str:
    for name in debug_log.LOGGER_NAMES:
        for h in logging.getLogger(name).handlers:
            h.flush()
    return (paths.GENESIS_HOME / "debug.log").read_text(encoding="utf-8")


def test_conftest_keeps_it_out_of_the_operator_home(tmp_path) -> None:
    # Операторът може да е с export GENESIS_DEBUG=1 — тестовете не го наследяват.
    assert "GENESIS_DEBUG" not in os.environ
    assert Path(paths.GENESIS_HOME).is_relative_to(tmp_path)


def test_off_by_default_no_file(monkeypatch) -> None:
    assert cli.main(["--version"]) == 0
    assert not (paths.GENESIS_HOME / "debug.log").exists()
    assert debug_log._handler is None


def test_installed_once(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_DEBUG", "1")
    cli.main(["--version"])
    cli.main(["--version"])
    logger = logging.getLogger("genesis")
    assert sum(isinstance(h, logging.FileHandler) for h in logger.handlers) == 1


def test_size_is_capped(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_DEBUG", "1")
    cli.main(["--version"])
    h = debug_log._handler
    assert h is not None and h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 2


def test_name_based_loggers_arrive_too(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_DEBUG", "1")
    cli.main(["--version"])
    logging.getLogger("genesis_agent.some_module").debug("от __name__")
    logging.getLogger("genesis.other").debug("от genesis.other")
    text = _log_text()
    assert "от __name__" in text and "от genesis.other" in text


def test_secrets_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_DEBUG", "1")
    cli.main(["--version"])
    log = logging.getLogger("genesis.test")
    log.debug("заявка към https://me:hunter2pass@example.com с ключ %s",
              "sk-ant-abcdefghijklmnop")
    try:
        raise RuntimeError("Authorization: Bearer ghp_abcdefghijklmnopqrstuv")
    except RuntimeError:
        log.debug("провал", exc_info=True)
    text = _log_text()
    assert "hunter2pass" not in text
    assert "sk-ant-abcdefghijklmnop" not in text
    assert "ghp_abcdefghijklmnopqrstuv" not in text
    assert "провал" in text and "RuntimeError" in text


def test_warnings_still_reach_the_console(monkeypatch, capsys) -> None:
    # Без GENESIS_DEBUG предупрежденията стигат до stderr през lastResort;
    # файлът не бива да ги открадне от конзолата.
    monkeypatch.setenv("GENESIS_DEBUG", "1")
    # Както в `genesis`: коренът е без handler-и (pytest слага свои за caplog).
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    cli.main(["--version"])
    capsys.readouterr()
    logging.getLogger("genesis.test").warning("видимо предупреждение")
    logging.getLogger("genesis.test").debug("само във файла")
    err = capsys.readouterr().err
    assert "видимо предупреждение" in err
    assert "само във файла" not in err


@pytest.mark.parametrize("value", ["", "0", "true"])
def test_only_exactly_1_turns_it_on(monkeypatch, value) -> None:
    monkeypatch.setenv("GENESIS_DEBUG", value)
    assert debug_log.install_from_env() is None
    assert not (paths.GENESIS_HOME / "debug.log").exists()
