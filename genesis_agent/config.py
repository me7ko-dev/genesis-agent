"""Paths and limits for Genesis Agent."""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("genesis.config")

stop_event = threading.Event()


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    """Цяло число от променлива на средата, с връщане към стойността по
    подразбиране вместо срив.

    Всички долу бяха голи `int(os.environ.get(...))`, тоест се изчисляваха при
    ВНАСЯНЕ на този модул — а него го внася всичко. Празна стойност беше
    достатъчна, за да умре целият агент с ValueError, преди каквото и да е:

        $ GENESIS_TOOL_ROUNDS= genesis
        ValueError: invalid literal for int() with base 10: ''

    „Зададена, но празна“ не е рядкост — `docker run -e GENESIS_TOOL_ROUNDS`
    без стойност прави точно това, както и ред в shell профила, от който е
    махната стойността. Сгрешена настройка трябва да даде предупреждение и
    разумна стойност, не мъртъв агент със стектрейс.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("%s=%r не е цяло число — ползвам %d", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%d е под допустимото (%d) — ползвам %d",
                    name, value, minimum, default)
        return default
    return value


# The directory containing the genesis_agent package. For a git checkout
# this is the repo root — writable, fine to use directly. For a `pip install`
# it is site-packages, which a mission has no business writing into (often
# not even permitted, and wiped on the next reinstall/upgrade).
PACKAGE_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = PACKAGE_DIR.parent
# The native Windows build (genesis.exe) counts as installed: its package
# directory is inside the install directory, which every update replaces.
_INSTALLED = (PROJECT_ROOT.name in ("site-packages", "dist-packages")
              or bool(getattr(sys, "frozen", False)))


def seed_user_skills(shipped: Path, user_dir: Path) -> int:
    """Донася в личната папка доставените умения, които ги няма там. Връща броя.

    САМО добавя. Файл, който вече съществува, не се пипа — операторът може да
    е променил доставено умение и неговата версия печели.

    Защо не е „копирай веднъж, ако папката липсва", както беше: тогава всяка
    вече съществуваща инсталация спираше да получава умения завинаги. Измерено
    на живо преди поправката: прясна инсталация → 20 умения; същата версия при
    налична ~/.genesis/skills с едно умение → 1 умение. Тоест кодът се
    обновяваше, а библиотеката не — и нищо не го казваше.

    Съзнателна цена: доставено умение, което операторът е ИЗТРИЛ, се връща при
    следващото обновяване. Доставеният набор е част от версията; който иска да
    го няма, го празни, а не го трие.
    """
    import json
    import shutil

    user_dir.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    for md in sorted(shipped.glob("*.md")):
        target = user_dir / md.name
        if not target.exists():
            shutil.copy2(md, target)
            added.append(md.stem)
    if not added:
        return 0

    # Индексът се слива по име. Записът на оператора за същото име печели —
    # той сочи неговия файл, който току-що НЕ презаписахме.
    index_path = user_dir / "skills.json"
    try:
        current = json.loads(index_path.read_text(encoding="utf-8"))
        entries = list(current.get("skills") or [])
    except (OSError, ValueError):
        current, entries = {"version": "1.0"}, []
    known = {e.get("name") for e in entries}
    try:
        shipped_index = json.loads((shipped / "skills.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shipped_index = {"skills": []}
    for entry in shipped_index.get("skills") or []:
        if entry.get("name") in added and entry.get("name") not in known:
            entries.append(entry)
    current["skills"] = entries
    # Пише се през временен файл: прекъснат запис на индекса прави ЦЯЛАТА
    # библиотека незаредима, а това се случва при стартиране.
    tmp = index_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(index_path)
    except OSError:
        log.warning("не можах да обновя %s — новите умения са копирани, но не са в индекса",
                    index_path)
    return len(added)


def _default_skills_dir() -> Path:
    shipped = PACKAGE_DIR / "skills"
    if not _INSTALLED:
        return shipped
    # Installed copy: skills a mission writes go to ~/.genesis/skills instead.
    # The shipped set is seeded there — copied, never written back into
    # site-packages — and topped up on every start, so an upgrade actually
    # delivers the new skills instead of only the new code.
    from genesis_agent.paths import GENESIS_HOME
    user_dir = GENESIS_HOME / "skills"
    if shipped.exists():
        try:
            seed_user_skills(shipped, user_dir)
        except OSError as e:
            # Достъпът до диска не бива да спира стартирането: по-добре със
            # старите умения, отколкото никак.
            log.warning("не можах да обновя уменията в %s: %s", user_dir, e)
    else:
        user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


SKILLS_DIR: Path = Path(os.environ.get("GENESIS_SKILLS_DIR") or _default_skills_dir())
SANDBOX_DIR: Path = Path(os.environ.get("GENESIS_SANDBOX_DIR",
    str((Path.home() / ".genesis" / "sandbox_run") if _INSTALLED else PROJECT_ROOT / ".sandbox_run")))
LOGS_DIR: Path = (Path.home() / ".genesis" / "logs") if _INSTALLED else PROJECT_ROOT / "logs"

# Where the SQLite state lives: workspace memory, episodic memory, conversation
# history, the embeddings cache. These modules historically wrote next to
# their own __file__ — fine in a checkout (that's a writable repo directory),
# but for an installed copy __file__ is under site-packages, and a database
# nobody can write survives exactly until the next `pip install --upgrade`
# deletes it anyway. Same path as always in checkout mode: zero behavior
# change for anyone running from a git clone.
DATA_DIR: Path = (Path.home() / ".genesis" / "data") if _INSTALLED else (PROJECT_ROOT / "genesis_agent")
if _INSTALLED:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Removed here: ENGINE_EXE / MODEL_PATH / ENGINE_PORT / LLM_BASE_URL /
# LLM_API_KEY / LLM_MODEL / GENESIS_MODE. They described a llama-server and
# LM Studio setup that no longer exists — nothing in the codebase read any of
# them, and they pointed at a specific .gguf on a specific machine. The local
# model is configured with GENESIS_LOCAL_MODEL and talked to over Ollama's
# OpenAI-compatible endpoint; see genesis_agent/brain.py.

# За локални малки модели (DeepSeek 8B, Qwen 7B и т.н.) намали ретрите!
# Малък модел = бърз провал, не 300 рунда по 130 секунди
MAX_LLM_RETRIES: int = _int_env("GENESIS_MAX_RETRIES", 8, minimum=1)
EXEC_TIMEOUT_SEC: int = _int_env("GENESIS_EXEC_TIMEOUT", 120, minimum=1)

# ─── Бюджет на контекста: щедро към свежото, безмилостно към старото ───────
#
# Двата лимита не се борят — разделят си работата по възраст на резултата:
#
# TOOL_RESULT_MAX_CHARS е предпазителят на ВХОДА. Пази историята (и файла на
# сесията) от абсурд — `cat` на бинарен файл, лог от 5 MB — но нарочно е
# щедър: точно този резултат е онова, върху което моделът работи В МОМЕНТА,
# и да го отрежеш на две е да го накараш да гадае или да пусне командата пак,
# което струва повече от символите, които си спестил.
#
# STALE_TOOL_RESULT_MAX_CHARS е истинската икономия и се прилага при ИЗХОДА
# (genesis_agent.budget.budget_history, извикан от Brain.complete). Един tool
# резултат се праща наново на ВСЕКИ следващ рунд, докато не изпадне от
# прозореца — 10 рунда по-късно същите 40 000 символа са платени десет пъти,
# а вече не вършат работа: моделът е реагирал на тях преди осем хода. Затова
# всичко освен последните FRESH_TOOL_RESULTS резултата се свива агресивно.
# Нетно: моделът вижда ПОВЕЧЕ от това, което му трябва сега, и много по-малко
# от това, което вече е изиграло ролята си.
#
# 0 изключва съответния таван (старото поведение).
TOOL_RESULT_MAX_CHARS: int = _int_env("GENESIS_TOOL_RESULT_MAX_CHARS", 40000)
STALE_TOOL_RESULT_MAX_CHARS: int = _int_env("GENESIS_STALE_TOOL_RESULT_MAX_CHARS", 2000)
FRESH_TOOL_RESULTS: int = _int_env("GENESIS_FRESH_TOOL_RESULTS", 2)

# Колко tool рунда има правото да направи агентът за ЕДНО съобщение, преди
# цикълът да спре и да върне контрола. Това е предпазителят срещу зацикляне,
# НЕ бюджет за работа — а на 8 беше точно бюджет: реална многостъпкова задача
# (диагностицирай → поправи → пусни тестовете → поправи пак → потвърди) тихо
# се удряше в тавана по средата и потребителят получаваше половин работа с
# обяснение — точно поведението, което целият tool-loop беше добавен да спре
# (виж коментара при цикъла в genesis_terminal_agent.py). Цената на рунд вече
# е ограничена отделно, през TOOL_RESULT_MAX_CHARS.
TOOL_ROUND_CAP: int = _int_env("GENESIS_TOOL_ROUNDS", 25, minimum=1)

# Optional: set GENESIS_OPERATOR=<your-name> for an audit trail (CLI --operator).
# GENESIS_STRICT_AUTHORITY=1 requires sovereign operator to start the autonomous loop.
# Red Zone manual approval contract: GENESIS_RED_ZONE_SECRET (host) + GENESIS_RED_ZONE_TOKEN (process) must match.

# Storage safety: total project tree size
STORAGE_THRESHOLD_BYTES: int = _int_env(
    "GENESIS_STORAGE_THRESHOLD_GB", 100, minimum=1) * (1024**3)
