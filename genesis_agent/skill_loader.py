#!/usr/bin/env python3
"""
Skill loader — reads the Markdown skill format used by this project.

Предоставя:
 - skill_view(name)         → метаданни + код от .md файл
 - run_skill(name, **kwargs) → изпълнява кода в изолиран subprocess
 - search_skills(query)     → търси умения по trigger keywords
 - reload_skills_index()    → hot-reload при нови умения
 - resolve_skill(query)     → точно име или fuzzy match по свободен текст
 - use_skill(query, driver) → РЕАЛНО изпълнение (умение + driver код) в sandbox
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import yaml

from genesis_agent.config import SKILLS_DIR

# `file_path` entries in skills.json are stored relative to this, not to
# PROJECT_ROOT — the two coincide for a git checkout, but diverge for an
# installed copy, where SKILLS_DIR is redirected to ~/.genesis/skills (see
# config._default_skills_dir). Resolving against PROJECT_ROOT there would
# look inside site-packages for a file that actually lives under the user's
# home directory.
SKILLS_ROOT = SKILLS_DIR.parent
_SKILLS_INDEX_CACHE: dict[str, dict[str, Any]] | None = None


def reload_skills_index() -> dict[str, dict[str, Any]]:
    """Принудително презарежда skills.json индекса от диска."""
    global _SKILLS_INDEX_CACHE
    index_path = SKILLS_DIR / "skills.json"
    if not index_path.exists():
        _SKILLS_INDEX_CACHE = {}
        return _SKILLS_INDEX_CACHE
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        skills_list = data if isinstance(data, list) else data.get("skills", [])
        _SKILLS_INDEX_CACHE = {s["name"]: s for s in skills_list if "name" in s}
    except Exception:
        _SKILLS_INDEX_CACHE = {}
    return _SKILLS_INDEX_CACHE


def load_skills_index() -> dict[str, dict[str, Any]]:
    """Зарежда (с кеш) skills.json индекса."""
    if _SKILLS_INDEX_CACHE is None:
        reload_skills_index()
    return _SKILLS_INDEX_CACHE  # type: ignore[return-value]


def format_skill_list(width: int = 70) -> str:
    """Списък с уменията за човек — `genesis skills` и `/skills` в чата.

    Без модел: на живо „кажи какви умения имаш" изгори два рунда в
    USE_SKILL заявки, които нямаше как да сработят."""
    index = load_skills_index()
    verified = sum(1 for s in index.values() if s.get("verified"))
    lines = [f"{len(index)} умения, {verified} verified"]
    for name in sorted(index):
        entry = index[name]
        desc = " ".join((entry.get("description") or "").split())
        if len(desc) > width:
            desc = desc[:width - 1] + "…"
        lines.append(f"  {'✓' if entry.get('verified') else '·'} {name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


_CODE_FENCE = re.compile(r"^(`{3,})python\n(.*?)\n\1[ \t]*$", re.DOTALL | re.MULTILINE)


def extract_code(content: str) -> str | None:
    """Кодът от първия ```python блок — същият низ, който се подписва.

    Затварящата ограда е със СЪЩАТА дължина като отварящата и на свой ред:
    код с ред, започващ с ``` (Markdown шаблон), се пише в ````python и не
    се отрязва при първия такъв ред (одит 2026-10-07: подписано и проверено
    умение се отказваше като „подправено“, а неподписано пускаше отрязан код).
    """
    text = content.replace("\r\n", "\n")
    # След ПОСЛЕДНОТО заглавие „## Python Код“ (преглед 2026-10-07): целта на
    # умението се записва дословно в „## Описание“ над него, и ```python блок в
    # нея ставаше „кодът“ — подписан и пуснат вместо проверения.
    head = text.rfind("\n## Python Код")
    if head != -1:
        text = text[head:]
    m = _CODE_FENCE.search(text)
    return m.group(2).strip() if m else None


def skill_view(name: str, *, file_path: Path | None = None) -> dict[str, Any]:
    """
    Зарежда .md файл на умение и връща YAML метаданни + код.

    Raises:
        FileNotFoundError: ако файлът не съществува.
        ValueError:        ако няма Python код блок в .md.
    """
    index = load_skills_index()
    skill_meta = index.get(name)

    if not skill_meta and not file_path:
        # Опит по точно имe на файл
        guessed = SKILLS_DIR / f"{name}.md"
        if guessed.exists():
            file_path = guessed
        else:
            raise FileNotFoundError(f"Умение '{name}' не е намерено в skills.json.")

    if file_path:
        md_path = Path(file_path)
    elif skill_meta:
        md_path = SKILLS_ROOT / skill_meta["file_path"]
    else:
        raise FileNotFoundError(f"Умение '{name}' няма нито файл, нито запис в индекса.")
    if not md_path.exists():
        raise FileNotFoundError(f"Файлът на умението не съществува: {md_path}")

    content = md_path.read_text(encoding="utf-8", errors="replace")

    # Извличане на YAML frontmatter
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    metadata: dict[str, Any] = {}
    if fm_match:
        try:
            metadata = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
    metadata.setdefault("name", name)

    # Извличане на Python код
    code = extract_code(content)
    if code is None:
        raise ValueError(f"Няма Python код блок в: {md_path}")

    # Signature check (design note, 2026-08-12): skills_manager.save_skill
    # signs NEW skills going forward — see its comment for why. Deliberately
    # asymmetric: a MISSING signature (every skill saved before this, or one
    # saved while `cryptography` was unavailable) loads exactly as it always
    # has, no backfill, no forced re-sign. A signature that IS present but no
    # longer matches the code just read from disk means the .md file (or the
    # index entry) was edited after signing — that skill is refused, not
    # silently executed, because everything that calls skill_view() (USE_SKILL,
    # run_skill) feeds its "code" straight into sandbox
    # execution. Fails OPEN only on the crypto tooling itself being
    # unavailable (ImportError etc.) — an optional integrity check must not
    # turn into an outage for every already-working skill the moment the
    # `cryptography` package goes missing; a genuine signature MISMATCH still
    # refuses regardless.
    signature = (skill_meta or {}).get("signature") or ""
    if signature:
        try:
            from genesis_agent.cryptography_utils import have_keys, verify_signature
            # Липсващ публичен ключ НЕ е провалена проверка (bug found
            # end-to-end, 2026-08-12): verify_signature() връща False и в двата
            # случая, а третирането им еднакво значи, че всяко клониране без
            # ключовете — или само сгрешен GENESIS_HOME — прави ЦЯЛАТА
            # библиотека незаредима, при това с обвинение в подправяне.
            # Липсата на ключ значи "не мога да преценя", не "открих намеса";
            # тогава се държим точно както при неподписаните умения.
            # Несъвпадение ПРИ наличен ключ си остава твърд отказ. Изтрит
            # public_key.pem при наличен частен ключ не изключва проверката —
            # публичният се извежда от частния (одит 2026-10-07: код, пуснат
            # от умение, трие public_key.pem и подменя умението трайно).
            if not have_keys():
                sig_ok = True
            else:
                sig_ok = verify_signature(code, signature)
        except Exception:
            sig_ok = True
        if not sig_ok:
            from genesis_agent.cryptography_utils import KEY_DIR as _KEY_DIR
            raise ValueError(
                f"Умение '{name}' носи подпис, който НЕ съвпада с текущото съдържание на "
                f"{md_path.name}. Две възможни причини: (1) файлът е променян след "
                f"подписването, или (2) зареждаш го с ДРУГ ключ от този, с който е "
                f"подписано (ключовете са в {_KEY_DIR} — провери дали GENESIS_HOME сочи "
                f"натам и от двете среди, ако ползваш Genesis и от WSL, и от Windows). "
                f"Отказвам да го заредя, докато не е ясно кое от двете е."
            )

    return {
        "metadata": metadata,
        "code": code,
        "file_path": str(md_path.relative_to(SKILLS_ROOT)),
    }


# Кратки/общи думи не носят сигнал за релевантност (напр. "a" в "reverse a string"
# съвпада с половината библиотека) — без филтър keyword search връща боклук.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "for", "with", "and", "or", "is",
    "that", "this", "using", "use", "from", "by", "as", "at", "be", "it", "its",
    # goal_engine-ните шаблони обличат ПОЧТИ ВСЯКА цел в едно и също изречение
    # ("...with type hints, docstring, and an assert-based self-test that
    # prints OK", "stdlib only", "from scratch", "production-grade utility в
    # pure Python") — тези думи съвпадат между напълно несвързани цели и
    # надуваха score-а изкуствено (измерено 2026-07-27: "изчисли повърхнина на
    # тор" излизаше score=5 съвпадение с "exponential backoff retry" само
    # заради "test/assert/self/type/hints"). Без този филтър прагът по-долу
    # в build_context е безполезен — боклукът минаваше прага само с шаблонни думи.
    "implement", "build", "create", "develop", "write", "design",
    "type", "hints", "docstring", "assert", "self", "test", "tests", "prints",
    "ok", "stdlib", "only", "pure", "python", "production", "grade", "utility",
    "inline", "scratch", "based", "given",
    # Български: операторът пише на български, тоест и заявките, и целите от
    # goals_from_real_work идват на български. Първият ред са граматични думи,
    # вторият — същите шаблонни глаголи като английските отгоре ("направи" е
    # "build", "напиши" е "write"); без тях две несвързани цели съвпадат само
    # защото и двете започват със "Направи".
    "или", "като", "това", "този", "тази", "тези", "който", "която", "което",
    "които", "със", "над", "под", "без", "през", "след", "преди", "при",
    "все", "още", "ако", "защото", "там", "тук", "има", "бъде", "нещо",
    "направи", "напиши", "създай", "искам", "трябва", "моля", "добави",
}


# Окончания на български, най-дългите първи. Не е граматика, а сгъване на
# формите, които операторът реално пише за едно и също: „фактура/фактурите“,
# „провери/проверка“, „уебсайт/уебсайта“, „работни/работните/работен“,
# „умение/уменията“. Измерено 2026-10-07 (domain_context върху заявки като
# „провери ЕГН-то на клиента“, „направи уебсайта на пекарната“, „провери
# IBAN-ите“, „извлечи полетата от фактурите“): без него и четирите не получаваха
# провереното знание, макар темата да е точно тя — само заради формата на думата.
_BG_SUFFIXES = tuple(sorted({
    "ение", "ения", "ие", "ия", "ове", "еве", "ища", "ане", "яне", "ания",
    "ни", "на", "но", "ен", "ена", "ено", "ени",
    "ам", "ям", "аме", "яме", "ате", "яте", "ат", "ах", "ях", "еше", "аше",
    "ете", "ем", "еш", "иш", "им", "ал", "ала", "ало", "али", "ял", "яла", "яли",
    "ка", "ки", "а", "я", "о", "е", "и", "у", "ю",
}, key=len, reverse=True))
_BG_VOWELS = set("аъоуеияю")
# Остатъкът от „IBAN-ите“, „ЕГН-то“: `\w+` реже на тирето и членът остава
# като отделна „дума“, която съвпада с нищо смислено.
_BG_FRAGMENTS = {"ите", "ата", "ото", "ият", "ния"}
_CYRILLIC_WORD = re.compile(r"^[а-яѝ]+$")


def _stem(word: str) -> str:
    """Основата на българска дума: първо членът, после едно окончание;
    основата остава поне 3 букви.

    Членът „-та/-то/-те“ се маха само след гласна („фактура|та“), иначе
    „-а“ е окончанието („уебсайт|а“, не „уебсай|та“). Латиница и думи под
    4 букви (`егн`, `ддс`, `iban`) не се пипат — там формата е една и
    съкращаването само би слепило различни неща.
    """
    if len(word) < 4 or not _CYRILLIC_WORD.match(word):
        return word
    article = word.endswith(("ът", "ят")) or (
        word.endswith(("та", "то", "те")) and word[-3] in _BG_VOWELS)
    if article and len(word) >= 5:
        word = word[:-2]
    for suffix in _BG_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _light_stem(word: str) -> str:
    """Само членът и окончанието на съществително — без глаголните окончания.

    За проверката „цял тригер“ в domain_context. Пълната основа (_stem) слива
    „работи“ (глагол) с „работни“ — и „скриптът не работи вече 3 дни“ получаваше
    правилата за работни дни (одит 2026-10-07); „основните полета“ — правилата
    за фактури („данъчна основа“). Тук „работи“ и „работни“ остават различни,
    а „фактурите“/„фактура“, „уебсайта“/„уебсайт“, „номерата“/„номер“ се срещат.
    """
    if len(word) < 4 or not _CYRILLIC_WORD.match(word):
        return word
    if word.endswith(("ът", "ят")) and len(word) >= 5:
        return word[:-2]
    if word.endswith(("та", "то", "те")) and len(word) >= 5 and word[-3] in _BG_VOWELS:
        word = word[:-2]
    if word[-1] in "ая" and word[-2] not in _BG_VOWELS and len(word) >= 5:
        word = word[:-1]
    return word


def _light_keywords(text: str) -> set[str]:
    return {
        _light_stem(w) for w in re.findall(r"\w+", text.lower())
        if len(w) >= 3 and w not in _STOPWORDS and w not in _BG_FRAGMENTS
    }


def _keywords(text: str) -> set[str]:
    r"""Думите, по които се мери съвпадение. `\w` вместо `[a-z0-9_]` (2026-09-20):
    старият клас беше само ASCII, тоест всяка дума на кирилица беше невидима.
    Измерено преди поправката — заявка изцяло на български даваше ПРАЗНО
    множество, значи score 0 за всяко умение, значи нито едно не можеше да се
    преизползва никога. А преизползването е целият смисъл на библиотеката.

    Българските думи се свеждат до основата си (_stem) — и в заявката, и в
    тригерите, затова „фактурите“ съвпада с тригер „фактура“.
    """
    return {
        _stem(w) for w in re.findall(r"\w+", text.lower())
        if len(w) >= 3 and w not in _STOPWORDS and w not in _BG_FRAGMENTS
    }


def search_skills(query: str, top_n: int = 5, *, use_semantic: bool = True) -> list[dict[str, Any]]:
    """
    Търси умения по keyword overlap (бързо, точно за буквални съвпадения), после
    допълва с семантично търсене (genesis_agent.embeddings) за перифразирани заявки,
    които keyword match пропуска — напр. "invert character order" ~ "reverse a string".
    Ако embeddings не са налични, поведението е същото както преди (чист keyword).
    """
    index = load_skills_index()
    query_words = _keywords(query)
    scored: list[tuple[int, dict[str, Any]]] = []

    for skill in index.values():
        triggers = skill.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        trigger_words = {w for t in triggers for w in _keywords(t)}
        # Добавяме думи от name и description
        name_words = _keywords(skill.get("name", ""))
        desc_words = _keywords(skill.get("description", ""))
        all_words = trigger_words | name_words | desc_words
        score = len(query_words & all_words)
        if score > 0:
            # _kw_score е ПРЕХОДЕН ключ (не се пази в skills.json) — брой РЕАЛНИ
            # (пост-stopword) съвпадащи думи. build_context() го ползва, за да
            # не инжектира пълен код при съвпадение само по случайност/шаблон.
            scored.append((score, {**skill, "_kw_score": score}))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [s for _, s in scored]
    seen = {s["name"] for s in results if "name" in s}

    if use_semantic and len(results) < top_n:
        try:
            from genesis_agent.embeddings import SEARCH_THRESHOLD, semantic_search
            for name, sim in semantic_search(query, top_k=top_n * 3):
                if sim < SEARCH_THRESHOLD or name in seen or name not in index:
                    continue
                # Дошло е през embedding прага (SEARCH_THRESHOLD), не през keyword
                # overlap — маркираме отделно, _kw_score=0 тук НЕ значи "слабо".
                results.append({**index[name], "_semantic_hit": True})
                seen.add(name)
        except Exception:
            pass  # embeddings недостъпни — просто чист keyword резултат

    return results[:top_n]


def domain_context(query: str) -> str:
    """Провереното знание по темата на заявката — за чата, само категория `domain`.

    Защо (измерено 2026-09-25, проект за ЕГН): и четирите безплатни модела
    във веригата твърдяха 3/3 пъти, че четна девета цифра е жена (обратното е
    вярно). Кодът и тестовете им бяха еднакво грешни, тоест зелени. Ред в
    промпта „провери правилото“ не помогна (0/3 преди, 0/3 след) — моделът е
    сигурен и не търси. Затова знанието идва от библиотеката: проверено
    умение със самотест, подадено на модела наготово.

    Само `category: domain` — общите умения (event bus, rate limiter…) не се
    подават в чата: там съвпадение по две думи е случайно твърде често
    (виж build_context). Празен низ = нищо не се добавя, нула токена.
    """
    try:
        hits = search_skills(query, top_n=10, use_semantic=False)
    except Exception:
        return ""
    # Мярката е само по ТРИГЕРИТЕ, не по описанието (2026-09-25): „България“ и
    # „модул“ от описанието на IBAN умението стигнаха за 2 думи — и задача за
    # работни дни получи правилата за IBAN; „знака“ + „число“ биха го подали и
    # на CSV отчет. Тригерите са думите на темата, описанието е проза.
    query_words = _keywords(query)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for h in hits:
        verified = h.get("verified") or h.get("verification", {}).get("verified")
        # `min_score` (по подразбиране 2): уеб ръководството се подава и на „направи
        # сайт за пекарна“ — една, но недвусмислена дума. Затова тригерите му са
        # сайт/уебсайт/лендинг, не „html“ и „страница“ (скрейпърът в bench-а ги има).
        need = int(h.get("min_score", 2))
        if h.get("category") not in ("domain", "web") or not verified or h.get("_kw_score", 0) < need:
            continue
        triggers = h.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        # `not_with`: думи, които отменят знанието. bench car-ads (2026-10-06):
        # „scraper.py … обяви от сайт за коли“ — „сайт“ подаваше уеб
        # ръководството на скрейпър; да събираш от сайт не е да правиш сайт.
        blockers = h.get("not_with", [])
        if isinstance(blockers, str):
            blockers = [blockers]
        if query_words & {w for b in blockers for w in _keywords(b)}:
            continue
        score = len(query_words & {w for t in triggers for w in _keywords(t)})
        # Поне един тригер — целият. bench library-pkg (2026-10-06): „проверка
        # на контролната цифра“ + „номер на читателя“ за ISBN дадоха 3 думи от
        # тригерите на ЕГН („проверка на егн контролна цифра“, „единен граждански
        # номер“) без самото „егн“ — и правилата за ЕГН отиваха в задача за книги.
        light_query = _light_keywords(query)
        whole = any(kw and kw <= light_query for kw in (_light_keywords(t) for t in triggers))
        if whole and score >= need:
            ranked.append((score, h))
    ranked.sort(key=lambda x: x[0], reverse=True)
    for _, h in ranked:
        if h.get("category") == "web":
            body = _guide_body(h)
            if body:
                return (f"## Проверено ръководство от библиотеката: {h['name']}\n"
                        "Основата отдолу минава браузърната проверка без находки — започни от нея "
                        "и я пригоди към заявката (съдържание, илюстрации, цвят на акцента), "
                        "вместо да пишеш стиловете от нулата.\n\n" + body)
            continue
        try:
            code = skill_view(h["name"])["code"]
        except (OSError, ValueError, KeyError):
            continue
        # „Правилата оттук, имената от заявката“: първата версия само казваше
        # „ползвай кода“ и моделът го копира дословно — с validate_egn/parse_egn
        # вместо поисканите validate/parse; правилни правила, счупен интерфейс
        # (скритите тестове не можаха да го импортират, 2026-09-25).
        return (f"## Проверено знание от библиотеката: {h['name']}\n"
                "ПРАВИЛАТА в този код са проверени със самотест — вземи ги оттук (и "
                "очакваните стойности в тестовете), не по памет. ИМЕНАТА, файловете и "
                "интерфейса вземи от заявката на оператора, не от този код.\n"
                f"```python\n{code.strip()}\n```")
    return ""


def _guide_body(meta: dict[str, Any]) -> str:
    """Тялото на умение-ръководство (category: web) без frontmatter и без
    „Описание“ — то е за хората (защо, кога е мерено), не за модела."""
    try:
        text = (SKILLS_ROOT / meta.get("file_path", f"skills/{meta['name']}.md")).read_text(encoding="utf-8")
    except (OSError, KeyError):
        return ""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return re.sub(r"## Описание\n.*?(?=\n## )", "", text, flags=re.DOTALL).strip()


def _extract_signatures(code: str) -> list[str]:
    """Топ-ниво def/class сигнатури от кода на умение — какво реално може да
    се извика, показано на модела ПРЕДИ да пише driver код по памет/предположение."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    sigs: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sigs.append(f"{prefix} {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")]
            sigs.append(f"class {node.name}({', '.join(methods)})")
    return sigs


# Колко РЕАЛНИ (пост-stopword) съвпадащи думи трябват, за да сметнем свободен
# текст за наистина сочещ към конкретно умение. Същият праг като в
# Brain.build_context — виж resolve_skill за защо тук е дори по-важен.
_MIN_FUZZY_KW_SCORE = 2


def resolve_skill(name_or_query: str) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Резолвира умение по ТОЧНО име (skills.json ключ), а ако липсва — по
    свободен текст (fuzzy/семантично търсене през search_skills).
    Връща (resolved_name или None, списък кандидати от search — при отказ това
    са БЛИЗКИТЕ, но недостатъчно убедителни попадения, за да може викащият да
    ги покаже, вместо да мълчи).

    Прагът (design note, 2026-08-12, хванат на живо): `search_skills` връща
    ВСЯКО умение с overlap ≥ 1 дума, а дотук се вземаше просто candidates[0],
    колкото и слабо да е съвпадението. Реален случай от проследена мисия:
    заявка "in-process job queue retry exponential backoff" резолвваше до
    `build_a_stdlib_only_an_in_process_event_bus_pub` — цялото съвпадение е
    думата "process" — и USE_SKILL връщаше self-test-а на EventBus с "OK",
    все едно наистина е намерил job queue. Моделът повярва, че такова умение
    съществува, и изгори 5 от 8 рунда да го разпитва, преди anti-starvation
    предпазителят да го принуди да пише код (мисия за 1 рунд стана 8).

    Брат ѝ `Brain.build_context` ползва точно този праг (`_kw_score >= 2`) от
    2026-07-27 по същата причина. Тук е дори по-остро: build_context само
    ИНЖЕКТИРА код в промпта, докато USE_SKILL реално ИЗПЪЛНЯВА умението и
    представя изхода му като отговор на заявката.

    Чисто семантично попадение (`_semantic_hit`, без keyword overlap) също не
    стига за авто-изпълнение — build_context съзнателно не му се доверява за
    скъпия път, защото cosine прагът е замърсен от шаблонната опашка в
    описанията ("...with type hints, docstring and an assert-based self-test").
    То обаче остава в списъка кандидати: точното име винаги резолвва, така че
    ако наистина е това умението, моделът е на едно извикване разстояние.
    """
    idx = load_skills_index()
    q = name_or_query.strip()
    if q in idx:
        return q, []
    candidates = search_skills(q, top_n=3)
    confident = [c for c in candidates
                 if c.get("_kw_score", 0) >= _MIN_FUZZY_KW_SCORE and c.get("name")]
    if confident:
        return confident[0]["name"], candidates
    return None, candidates


# Тестовете (conftest) я пренасочват; иначе — системната временна папка.
SKILL_HOME_ROOT: Path | None = None
_operator_env: dict[str, str] | None = None


def _home_roots() -> list[Path]:
    import os
    import tempfile
    if SKILL_HOME_ROOT is not None:
        return [Path(SKILL_HOME_ROOT)]
    roots = [Path(tempfile.gettempdir())]
    if os.name != "nt":
        roots += [Path("/tmp"), Path("/var/tmp")]
    try:
        real = Path.home().resolve()
        # TMPDIR в домашната папка връщаше точно това, което се маха: истинския
        # HOME сред родителите (втори одит 2026-10-09). На Windows няма друго.
        outside = [r for r in roots if r.is_dir() and r.resolve() != real
                   and real not in r.resolve().parents]
        return outside or roots
    except (OSError, RuntimeError):
        return roots


def _ours(path: Path) -> bool:
    import os
    import stat
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if hasattr(os, "getuid"):
        if st.st_uid != os.getuid():
            return False
        os.chmod(path, 0o700)
    return True


def skill_home() -> Path:
    """Домашната папка на кода на уменията: във временната папка на системата,
    по една на потребител, 0700 и наша. Не в ~/.genesis: оттам
    `Path.home().parent` беше точно .env, ключовете и mcp_tokens.json, а
    `.parents[1]` — истинската домашна папка (одит 2026-10-09)."""
    import os
    import tempfile
    root = _home_roots()[0]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Чужда/подменена папка с това име (споделен /tmp): следващото стабилно
    # име, не нова случайна папка при всяко извикване — кешът студен и папки
    # изтичаха в /tmp (втори одит 2026-10-09).
    for n in range(10):
        home = root / (f"genesis-skill-home-{uid}" + (f"-{n}" if n else ""))
        if _ours(home):
            return home
    return Path(tempfile.mkdtemp(prefix="genesis-skill-home-", dir=str(root)))


def _operator_settings() -> dict[str, str]:
    """Каквото уменията губят с нова домашна папка и наистина им трябва:
    кой подписва git комитите и откъде pip тегли (корпоративен индекс).
    Само тези стойности, не файловете (одит 2026-10-09)."""
    global _operator_env
    if _operator_env is not None:
        return dict(_operator_env)
    import os
    import subprocess
    out: dict[str, str] = {}
    for key, names in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                       ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            value = subprocess.run(["git", "config", "--get", key], capture_output=True,
                                   text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
        if value:
            out.update({n: value for n in names})
    home = Path.home()
    appdata = os.environ.get("APPDATA", "")
    for conf in (os.environ.get("PIP_CONFIG_FILE", ""), str(home / ".config" / "pip" / "pip.conf"),
                 str(home / ".pip" / "pip.conf"),
                 str(Path(appdata) / "pip" / "pip.ini") if appdata else "",
                 str(home / "Library" / "Application Support" / "pip" / "pip.conf")):
        if conf and Path(conf).is_file():
            out["PIP_CONFIG_FILE"] = conf
            break
    _operator_env = out
    return dict(out)


def _workspace() -> list[Path]:
    """Работната папка на чата: пазачът я пуска и когато е в ~/.genesis
    (`~/.genesis/workspace` при старт от домашната папка — одит 2026-10-09)."""
    import sys
    gs = sys.modules.get("genesis_skills")
    ws = getattr(gs, "_WORKSPACE", None) if gs is not None else None
    return [Path(ws)] if ws else []


def skill_env() -> dict[str, str]:
    """Средата на кода на умение: собствена домашна папка (skill_home).

    Драйверът на USE_SKILL е код от модела. С HOME на оператора
    `Path.home() / ".ssh"` или `expanduser("~/.aws")` стигаха до тайните без
    нито един буквален път, който sandbox-ът да види (NEXT_STEPS, одит
    2026-10-07). Папката е постоянна, затова кешовете на pip/matplotlib в нея
    остават топли — заради тях досега HOME не беше сменян. PYTHONUSERBASE
    сочи истинския user site: пакет от `pip install --user` пак се внася
    (2026-10-09).

    Не е стена: `pwd.getpwuid()`, а и RUN_CMD знаят истинската папка —
    истинската граница е отделен потребител/контейнер."""
    import os
    import site
    home = skill_home()
    for sub in (".cache", ".config", ".local/share", "AppData/Roaming", "AppData/Local"):
        (home / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {"HOME": str(home), "USERPROFILE": str(home),
           "XDG_CACHE_HOME": str(home / ".cache"), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local" / "share"),
           "MPLCONFIGDIR": str(home / ".config" / "matplotlib"),
           "APPDATA": str(home / "AppData" / "Roaming"),
           "LOCALAPPDATA": str(home / "AppData" / "Local"),
           **_operator_settings()}
    try:
        env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE") or site.getuserbase()
    except Exception:
        pass
    return env


def use_skill(name_or_query: str, driver_code: str = "") -> str:
    """
    Намира умение (по точно име или свободна заявка) и го изпълнява РЕАЛНО —
    не само го инжектира като текст в промпта. driver_code (ако е зададен) се
    добавя СЛЕД кода на умението в един и същ файл, затова функциите/класовете
    на умението са директно достъпни по име, без import — резултатът е истинско
    изпълнено действие, не поредна регенерация на същата логика от нула.

    Ако driver_code е празен, само зарежда умението и показва наличните
    функции/класове (self-test-ът на умението пак се пуска — потвърждава, че
    работи).
    """
    from genesis_agent import sandbox

    resolved, candidates = resolve_skill(name_or_query)
    if not resolved:
        msg = (f"[USE_SKILL: {name_or_query.strip()}] Няма достатъчно близко умение — "
               "напиши кода сам, не търси повече.")
        # Слабите попадения се ПОКАЗВАТ, но не се изпълняват (виж resolve_skill).
        # Директивата стои ПРЕДИ тях нарочно: целият този праг съществува, за да
        # не се горят рундове в търсене, а списък от имена с покана да се пробват
        # е точно такава покана (наблюдавано веднага след поправката — моделът
        # изяде още един рунд да извика най-близкото по име, за да се увери).
        near = [c["name"] for c in candidates if c.get("name")]
        if near:
            msg += ("\n(Само повърхностно съвпадение, почти сигурно НЕ вършат работа: "
                    + ", ".join(near) + ")")
        return msg

    try:
        data = skill_view(resolved)
    except (FileNotFoundError, ValueError) as e:
        return f"[USE_SKILL: {resolved}] Грешка при зареждане: {e}"

    sigs = _extract_signatures(data["code"])
    header = [f"[USE_SKILL: {resolved}]"]
    if resolved != name_or_query.strip():
        header.append(f"(намерено по заявка '{name_or_query.strip()}')")
    if sigs:
        header.append("Достъпни: " + "; ".join(sigs))
    others = [c["name"] for c in candidates if c.get("name") and c["name"] != resolved]
    if others:
        header.append("Други близки умения: " + ", ".join(others))

    script = data["code"]
    if driver_code.strip():
        script += "\n\n# --- USE_SKILL driver ---\n" + driver_code

    res = sandbox.run_python(script, timeout=60, env_extra=skill_env(), allow=_workspace())
    if res.blocked:
        return "\n".join(header) + "\n" + res.stderr

    body = (res.stdout or "").strip()
    if res.stderr and not res.ok:
        body += ("\n" if body else "") + "stderr:\n" + res.stderr.strip()[:2000]
        body += ("\n\nГРЕШКА — driver кодът не се изпълни. Провери 'Достъпни' по-горе "
                 "за точното име на функцията/класа и опитай ОТНОВО с коригиран driver "
                 "в нов [USE_SKILL: ...] таг. НЕ съобщавай резултат, който не е реално "
                 "отпечатан по-горе.")
    if not body:
        body = "(няма изход)" if res.ok else f"грешка (rc={res.returncode})"
    return "\n".join(header) + "\n" + body


def run_skill(name: str, **exec_kwargs) -> str:
    """
    Изпълнява умение през genesis_agent.sandbox (защитната бариера).
    exec_kwargs се подават като env var SKILL_ARGS (JSON) — през whitelist-a,
    затова кодът на умението не вижда API ключовете на процеса-родител.
    Връща stdout на процеса.
    """
    import json as _json

    from genesis_agent import sandbox

    data = skill_view(name)
    code = data["code"]

    res = sandbox.run_python(
        code,
        timeout=120,
        env_extra={**skill_env(), "SKILL_ARGS": _json.dumps(exec_kwargs)},
        allow=_workspace(),
    )
    if res.blocked:
        raise RuntimeError(res.stderr)
    if not res.ok:
        raise RuntimeError(f"Грешка при изпълнение:\n{res.stderr[:2000]}")
    return res.stdout.strip()


if __name__ == "__main__":
    idx = load_skills_index()
    print(f"✅ Заредени умения: {len(idx)}")

    results = search_skills("retry decorator")
    print(f"\n🔎 Търсене 'retry decorator' → {len(results)} резултата:")
    for r in results:
        print(f"  - {r['name']} ({r.get('category', '?')})")