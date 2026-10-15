"""
genesis_agent.debug_log — GENESIS_DEBUG=1 → ~/.genesis/debug.log.

Местата, които нарочно не спират работата при грешка (памет, известия,
незадължителни модули), я записват с `log.debug(..., exc_info=True)`. Без
handler тези записи не отиват никъде; този модул е единственият ключ, който
ги прави видими. Изключен по подразбиране, слага се веднъж от `cli.main`.

Файлът е ограничен (5 MB × 2 резервни) и минава през `notifier.redact`:
съобщения и следи на грешки носят адреси с токени и заглавки с ключове, а
debug.log е файл, който хората прикачват към бъгрепорти.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Всички модули ползват logging.getLogger("genesis.<име>"). "genesis_agent"
# хваща и бъдещ модул, който вземе logging.getLogger(__name__).
LOGGER_NAMES = ("genesis", "genesis_agent")
FILE_NAME = "debug.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 2

_handler: RotatingFileHandler | None = None
_added: list[tuple[logging.Logger, logging.Handler]] = []
_levels: dict[str, int] = {}


class _RedactingFormatter(logging.Formatter):
    """Маскира тайните в целия запис — и в съобщението, и в следата."""

    def format(self, record: logging.LogRecord) -> str:
        from genesis_agent.notifier import redact
        return redact(super().format(record))


def enabled() -> bool:
    return os.environ.get("GENESIS_DEBUG") == "1"


def install_from_env() -> Path | None:
    """Слага handler-а, ако GENESIS_DEBUG=1. Повторно извикване не добавя втори.
    Връща пътя до файла или None, когато е изключено."""
    if not enabled():
        return None
    try:
        return install()
    except OSError as e:
        # Ключ за диагностика не бива да спира самия `genesis` (папка без
        # право на запис, GENESIS_HOME сочи файл).
        print(f"[genesis] debug.log не се отвори: {e}", file=sys.stderr)
        return None


def install() -> Path:
    global _handler
    if _handler is not None:
        return Path(_handler.baseFilename)
    from genesis_agent import paths
    path = paths.ensure_genesis_home() / FILE_NAME
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                  encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_RedactingFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"))
    # Предупрежденията досега стигаха до конзолата през logging.lastResort,
    # който се ползва САМО докато няма нито един handler по веригата. С файла
    # той млъква — затова го закачаме изрично: с GENESIS_DEBUG=1 конзолата
    # вижда точно каквото виждаше и без него.
    console = logging.lastResort if not logging.getLogger().handlers else None
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        _levels.setdefault(name, logger.level)
        logger.setLevel(logging.DEBUG)
        for h in (handler, console):
            if h is not None and h not in logger.handlers:
                logger.addHandler(h)
                _added.append((logger, h))
    _handler = handler
    return path


def uninstall() -> None:
    """Маха каквото install() е сложил (тестовете — между тестовете)."""
    global _handler
    for logger, h in _added:
        logger.removeHandler(h)
    _added.clear()
    for name, level in _levels.items():
        logging.getLogger(name).setLevel(level)
    _levels.clear()
    if _handler is not None:
        _handler.close()
        _handler = None
