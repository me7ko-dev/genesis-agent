"""Paths and limits for Genesis Agent."""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from genesis_agent.paths import env_path as _env_path

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
    import hashlib
    import json
    import shutil

    from genesis_agent.file_lock import locked

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    user_dir.mkdir(parents=True, exist_ok=True)
    index_path = user_dir / "skills.json"
    try:
        shipped_index = json.loads((shipped / "skills.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        shipped_index = {"skills": []}
    shipped_entries = {e.get("name"): e for e in shipped_index.get("skills") or []
                       if isinstance(e, dict)}

    # Под заключване: две стартиращи копия (чат + `genesis serve`) пишат
    # същия индекс (одит 2026-10-07).
    with locked(index_path):
        try:
            current = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(current, dict) or not isinstance(current.get("skills"), list):
                raise TypeError("unexpected shape")
        except FileNotFoundError:
            current = {"version": "1.0", "skills": []}
        except (ValueError, TypeError):
            # Повреден индекс: настрани, не върху него — той е картата към
            # уменията на оператора (одит 2026-10-07: изтриваше се без следа).
            from genesis_agent.skills_manager import _free_corrupt_name
            kept = _free_corrupt_name(index_path)
            index_path.replace(kept)
            log.warning("skills.json е нечетим — запазен като %s", kept.name)
            current = {"version": "1.0", "skills": []}
        entries: list[dict] = list(current["skills"])
        by_name = {e.get("name"): e for e in entries if isinstance(e, dict)}

        changed: list[str] = []
        for md in sorted(shipped.glob("*.md")):
            name, target = md.stem, user_dir / md.name
            new_sha = sha(md)
            entry = shipped_entries.get(name)
            if not target.exists():
                shutil.copy2(md, target)
            else:
                have = sha(target)
                mine = by_name.get(name) or {}
                # Непипано копие на по-стара доставка → новата версия. Само
                # когато знаем какво сме доставили (shipped_sha): иначе не можем
                # да различим редакция на оператора и тя печели.
                untouched_old = mine.get("shipped_sha") == have and have != new_sha
                if untouched_old:
                    shutil.copy2(md, target)
                elif have != new_sha:
                    continue
                # Същото съдържание без запис (копиран, но записът на индекса
                # се провали) → долу влиза в индекса. Същото съдържание СЪС
                # запис → пак долу: записът на доставката (not_with, min_score,
                # triggers) може да е нов, макар .md да е същият (одит
                # 2026-10-27: `not_with` на web_site_2026 от dfa1b47 не
                # стигаше до инсталация, засята преди него).
                elif name in by_name:
                    if entry is not None and any(mine.get(k) != v for k, v in entry.items()):
                        # Полетата на оператора извън доставката (signature) остават.
                        fresh = {**mine, **entry, "shipped_sha": new_sha}
                        entries[entries.index(by_name[name])] = fresh
                        by_name[name] = fresh
                        changed.append(name)
                    elif mine.get("shipped_sha") != new_sha:
                        mine["shipped_sha"] = new_sha    # същото съдържание: запомни го
                        changed.append("")
                    continue
            if entry is None:
                changed.append(name)
                continue
            fresh = dict(entry, shipped_sha=new_sha)
            if name in by_name:
                entries[entries.index(by_name[name])] = fresh
            else:
                entries.append(fresh)
            by_name[name] = fresh
            changed.append(name)
        if not changed:
            return 0
        current["skills"] = entries
        # Пише се през временен файл: прекъснат запис на индекса прави ЦЯЛАТА
        # библиотека незаредима, а това се случва при стартиране.
        tmp = index_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(index_path)
        except OSError:
            log.warning("не можах да обновя %s — новите умения са копирани, но не са в индекса; "
                        "ще опитам пак при следващото стартиране", index_path)
        return len([n for n in changed if n])


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
# Празна GENESIS_SANDBOX_DIR = подразбиращата се (одит 2026-10-22: беше „.“,
# тоест пясъчникът ставаше текущата папка на потребителя).
SANDBOX_DIR: Path = _env_path("GENESIS_SANDBOX_DIR",
    (Path.home() / ".genesis" / "sandbox_run") if _INSTALLED else PROJECT_ROOT / ".sandbox_run")
LOGS_DIR: Path = (Path.home() / ".genesis" / "logs") if _INSTALLED else PROJECT_ROOT / "logs"

# Where the SQLite state lives: workspace memory, episodic memory, conversation
# history, the embeddings cache. These modules historically wrote next to
# their own __file__ — fine in a checkout (that's a writable repo directory),
# but for an installed copy __file__ is under site-packages, and a database
# nobody can write survives exactly until the next `pip install --upgrade`
# deletes it anyway. Same path as always in checkout mode: zero behavior
# change for anyone running from a git clone.
#
# GENESIS_DATA_DIR премества и състоянието на веригата (2026-10-11): e2e тестът
# на bench-а пуска истинския чат срещу фалшив доставчик и той записа
# budget_log.jsonl, provider_stats.json и model_check.json в genesis_agent/ на
# checkout-а — тоест в истинското състояние на оператора, от което веригата
# после решава кой модел е мъртъв. GENESIS_MEMORY_DIR покриваше само паметта.
_DATA_OVERRIDE = os.environ.get("GENESIS_DATA_DIR", "").strip()
DATA_DIR: Path = (Path(_DATA_OVERRIDE).expanduser() if _DATA_OVERRIDE else
                  (Path.home() / ".genesis" / "data") if _INSTALLED else (PROJECT_ROOT / "genesis_agent"))
if _INSTALLED or _DATA_OVERRIDE:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# What Genesis remembers — conversations, episodes, work threads, decisions,
# preferences, the knowledge graph — apart from the state of the model chain
# (provider_stats, model_check, budget_log). bench runs every trial with an
# empty one: on 2026-09-28 a site task's "preferences" (SEO, dark theme,
# index.html) were injected into every later trial, even a Python function.
# Without the variable it is DATA_DIR, as before.
MEMORY_DIR: Path = Path(os.environ.get("GENESIS_MEMORY_DIR") or DATA_DIR)
if MEMORY_DIR != DATA_DIR:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

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

# B.12 (2026-10-11): в ИЗПРАТЕНОТО копие съдържанието на стари WRITE_FILE/
# EDIT_FILE аргументи се заменя с маркер (budget._elide_old_writes). Офлайн
# scripts/bench_history.py (seed 7, K=6): 5/15 рунда −29.6%, 5/24 −25.8%,
# 8/40 −23.4%, 10/60 −24.5% промпт токени — но делът файлове, видими в
# текущия си вид, пада средно за хода 92% → 65% (5/15) и 61% → 41% (8/40):
# старият READ_FILE вече е свит и изпуснатият запис е бил единственото пълно
# копие. Само правилото „по-нов запис на същия файл“ (K=∞) дава −1..−7% без
# загуба на видимост. A/B с истински модел (2026-10-12, claude_cli/sonnet, bench_projects
# 16 проекта ×1): включено 15/16, 1 034 407 токена, 1247 s; изключено 14/16 (едната
# разлика — шрифтов бъг на теста, не на модела), 1 038 287 токена, 1220 s. Верните не
# падат; в кратките задачи няма печалба (свиването почва след K рунда), в дългите е
# офлайн −23…−30%. Правилото на плана — „по-добро или равно → включва се“ — затова е
# ВКЛЮЧЕНО; GENESIS_ELIDE_OLD_WRITES=0 го спира. Пазачът срещу преписан маркер е в кода.
ELIDE_OLD_WRITES: int = _int_env("GENESIS_ELIDE_OLD_WRITES", 1)
ELIDE_WRITE_ROUNDS: int = _int_env("GENESIS_ELIDE_WRITE_ROUNDS", 6, minimum=2)

# Доставчик с prompt caching (Anthropic) получава историята САМО ДОПЪЛВАНА, като
# Claude Code (2026-10-11): кешът е съвпадение по префикс, а свиването на още
# един стар резултат и плъзгащият се с по едно прозорец сменяха префикса на
# всеки рунд — bench egn-check с claude-opus-5: 218 849 от 315 468 входни токена
# платени изцяло, $1.60 на пуск. Кеширан токен струва 0.1×, свит но изпратен
# наново — 1×. Над CACHE_HISTORY_MAX_CHARS отпада наведнъж цяло парче от
# CACHE_TRIM_CHUNK съобщения (не по едно), за да остане префиксът същият до
# следващото парче.
CACHE_HISTORY_MAX_CHARS: int = _int_env("GENESIS_CACHE_HISTORY_MAX_CHARS", 300_000)
CACHE_TRIM_CHUNK: int = _int_env("GENESIS_CACHE_TRIM_CHUNK", 20, minimum=2)
# Безплатните доставчици кешират сами по префикс, без маркери (budget_log до
# 2026-10-11: ollama_cloud gpt-oss-120b 69% от входа, groq 7%, nvidia 3%,
# openrouter 6%) — и при тях свиването сменя префикса всеки рунд. Тези от
# списъка получават историята само допълвана; таванът е по-нисък, защото
# прозорецът им е по-малък (gpt-oss: 131k токена), а кирилицата е ~2-3 знака на
# токен. Anthropic е винаги вътре (виж brain._NATIVE_PROVIDERS).
# Измерено 2026-10-11 (egn-check + iban-check, по 1 пуск, всички верни): само
# допълвана история — ollama_cloud 69% → 83% от кеша, некеширани токени на
# заявка 2 770 → 1 126.
# Реплей по модел (2026-10-11, 14 рунда растяща история с инструменти, свиване
# срещу само допълване): ollama_cloud gpt-oss-120b 73% → 88% от кеша, некеширани
# на заявка 3 093 → 1 653; nvidia nemotron-3-super 44% → 81%, 7 036 → 3 059;
# nvidia nemotron-3-ultra 17% → 35%, 10 644 → 10 241. Groq (безплатният план)
# не кешира: три еднакви заявки към gpt-oss-20b — без cached_tokens, лимитът в
# минута се изяжда цял всеки път; там по-малкото пратено е икономията, затова
# groq остава на свиването. Запис е доставчик („nvidia“) или един модел
# („groq::openai/gpt-oss-120b“).
def _names(raw: str) -> frozenset[str]:
    """"a, b" → {"a", "b"}; празен низ → празно множество."""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


CACHE_PROVIDERS: frozenset[str] = _names(os.environ.get("GENESIS_CACHE_PROVIDERS", "ollama_cloud,nvidia"))
CACHE_HISTORY_MAX_CHARS_FREE: int = _int_env("GENESIS_CACHE_HISTORY_MAX_CHARS_FREE", 120_000)

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
