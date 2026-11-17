#!/usr/bin/env python3
"""
genesis_agent.research — [RESEARCH: въпрос]: бързо проучване с източници.

2026-11-17 (scripts/bench_research.py, 20 въпроса с проверим отговор): старият
RESEARCH отговаряше вярно на 2/20 за ~6 s. Търсеше само в DuckDuckGo Lite (на
кирилица — CAPTCHA) и четеше само описанието под резултата (≤300 знака), като
питаше модела отделно за всеки източник — бавно и почти винаги „НЕ Е ОТКРИТО“.

Сега, като човек, който проучва:
  1. Търси в няколко търсачки наведнъж (search_engines.multi_search).
  2. Отваря първите страници паралелно (WEB_FETCH със същата защита от
     вътрешни адреси; страница, рисувана с JS — през браузъра).
  3. Реже ги на откъси и подрежда откъсите по близост до въпроса (BM25,
     локално, без модел) — до модела стигат ~6000 знака, не цели страници.
     Затова работи и с малкия локален модел.
  4. ЕДНО извикване на модела: отговор само от откъсите, с [номер] след
     всяко твърдение; разминаване между източниците се казва, не се решава.
  5. Няма ли отговор — втори кръг с други думи (едно търсене на английски).
     Пак няма и има ключ за Groq — неговото вградено търсене (бавно, много
     токени — затова последно).
Източниците (адресите) ги пише кодът, не моделът — не могат да бъдат измислени.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import math
import os
import re
import time
from collections import Counter
from datetime import date

log = logging.getLogger("genesis.research")

READ_PAGES = 5            # колко страници се отварят
FETCH_TIMEOUT = 9.0       # за всички страници заедно
CONTEXT_CHARS = 6500      # откъсите до модела
# Локален модел (GENESIS_LOCAL_ONLY=1): GTX 1650 чете входа с ~80 токена/с, така
# че времето е почти само дължината на откъсите. Бенч 2026-11-17, genesis-local:9b,
# 20 въпроса: 6500 → 20/20, медиана 38 s; 2200 → 20/20, 17 s; 1300 → 20/20 но
# 19/20 с източник и втори кръг до 36 s. 2200 е границата без загуба.
LOCAL_CONTEXT_CHARS = 2200
CHUNK_CHARS = 700
MAX_PER_SOURCE = 3
NOT_FOUND = "НЕ Е ОТКРИТО В ИЗТОЧНИЦИТЕ"

_SYNTH_SYS = (
    "Ти си проучвател. Отговаряш САМО по дадените откъси от източници — не ползвай "
    "свое знание. Правила:\n"
    "1. Започни с прекия отговор на въпроса, кратко, на езика на въпроса.\n"
    "2. След всяко твърдение сложи номера на източника в квадратни скоби, напр. [1] или [2][3].\n"
    "3. Ако източниците си противоречат — кажи го и посочи кой какво твърди; не избирай наслука.\n"
    f"4. Ако отговорът го няма в откъсите — напиши точно „{NOT_FOUND}“ и после какво все пак пише.\n"
    "5. Без въведение и без повтаряне на въпроса. Числа, дати и имена — точно както са в източника."
)

_QUERIES_SYS = (
    "Помагаш с търсене в интернет. Дай ДВЕ различни кратки заявки за търсачка, с които "
    "най-вероятно ще се намери отговорът на въпроса: първата — ключови думи на езика на "
    "въпроса, втората — на английски. Само заявките, по една на ред, без номера и кавички."
)

_STOP = frozenset((  # noqa: SIM905 — един низ се чете и поправя по-лесно от 100 думи в кавички
    "и в на е се от за с да по ли ще са има или че не как кой коя кое кои какъв каква какво "
    "какви колко кога къде защо който която което които това този тази тези към при през след "
    "до ги го я му ѝ им бил била било били съм си сме сте най още вече само "
    "the a an of in on at is are was were what who which when where why how to for and or "
    "does did do by with from that this it its as be been"
).split())


# ─── Откъси и подреждане ─────────────────────────────────────────────────────

def _terms(text: str) -> list[str]:
    """Думите за сравнение: малки букви, без служебни, отрязани до 5 знака —
    груб стемер, който стига за „височина/височината“, „община/общини“."""
    out = []
    for w in re.findall(r"[^\W_]+", text.lower()):
        if w in _STOP or (len(w) < 2 and not w.isdigit()):
            continue
        out.append(w if w.isdigit() else w[:5])
    return out


def _clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)            # картинки
    text = re.sub(r"\[([^\]]*)\]\((?:[^)(]|\([^)]*\))*\)", r"\1", text)  # [текст](адрес) → текст
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Откъси по ~size знака, по граници на редове/изречения."""
    parts: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            parts.append(para)
            continue
        for sent in re.split(r"(?<=[.!?…])\s+", para):
            while len(sent) > size:
                parts.append(sent[:size])
                sent = sent[size:]
            parts.append(sent)
    out: list[str] = []
    cur = ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > size:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return out


def rank_passages(question: str, sources: list[dict], *, budget: int = CONTEXT_CHARS,
                  per_source: int = MAX_PER_SOURCE) -> list[tuple[int, str]]:
    """[(номер на източника, откъс)] — най-близките до въпроса, в бюджета знаци.
    BM25 върху всички откъси от всички източници (заглавието се брои към откъса)."""
    q = Counter(_terms(question))
    if not q:
        return []
    docs: list[tuple[int, str, list[str]]] = []
    for i, src in enumerate(sources):
        title = src.get("title", "")
        pieces = []
        if src.get("snippet"):
            pieces.append(src["snippet"])
        pieces += _chunks(src.get("text", ""))
        seen: set[str] = set()
        for p in pieces:
            key = p[:120]
            if key in seen:
                continue
            seen.add(key)
            docs.append((i, p, _terms(f"{title} {p}")))
    if not docs:
        return []
    n = len(docs)
    avg = sum(len(d[2]) for d in docs) / n or 1.0
    df: Counter[str] = Counter()
    for _i, _p, toks in docs:
        df.update(set(toks))
    k1, b = 1.5, 0.75
    scored = []
    for idx, (i, p, toks) in enumerate(docs):
        tf = Counter(toks)
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(toks) / avg))
        # По-високо в търсачката = малко предимство при равенство.
        s *= 1.0 + 0.15 / (1 + i)
        if s > 0:
            scored.append((s, idx, i, p))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: list[tuple[int, int, str]] = []
    used: Counter[int] = Counter()
    total = 0
    for _s, idx, i, p in scored:
        if used[i] >= per_source:
            continue
        if total + len(p) > budget and picked:
            continue
        picked.append((idx, i, p))
        used[i] += 1
        total += len(p)
    picked.sort(key=lambda x: (x[1], x[0]))   # по източник, в реда на страницата
    return [(i, p) for _idx, i, p in picked]


# ─── Четене на страниците ────────────────────────────────────────────────────

def _fetch_text(url: str) -> tuple[str, str]:
    """(заглавие, текст) на страницата; ("", "") ако не се отвори."""
    from genesis_agent.web_fetch import (
        FetchError,
        _check_url,
        _is_loopback_name,
        fetch,
        html_to_text,
    )
    title = text = ""
    try:
        # Преглед за сигурност 2026-11-17: fetch() пуска localhost, когато самият
        # адрес е localhost („операторът го поиска“). Тук адресът идва от
        # търсачката — чужд резултат не бива да чете услуги на лаптопа.
        if _is_loopback_name(_check_url(url)):
            return "", ""
    except FetchError:
        return "", ""
    try:
        final, ctype, body = fetch(url)
        if "html" in ctype.lower() or body.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
            title, text = html_to_text(body, final)
        else:
            text = body
    except FetchError as e:
        log.info("RESEARCH: %s не се отвори: %s", url[:120], e)
    if len(text.strip()) < 400:
        # Страница, която рисува текста с JavaScript, или сайт, който спира
        # заявки без браузър.
        try:
            from genesis_agent import search_browser as sb
            if sb.available():
                _final, btitle, btext = sb.fetch_text(url)
                if len(btext.strip()) > len(text.strip()):
                    title, text = btitle or title, btext
        except Exception:
            log.debug("браузърът не прочете %s", url[:120], exc_info=True)
    return title, _clean(text)


def _read_pages(hits: list[dict], limit: int) -> list[dict]:
    """Първите `limit` резултата, с текста на страницата (паралелно)."""
    sources = [dict(h) for h in hits]
    todo = [s for s in sources if str(s.get("url", "")).startswith(("http://", "https://"))][:limit]
    if not todo:
        return sources
    pool = cf.ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="genesis-research")
    futs = {pool.submit(_fetch_text, s["url"]): s for s in todo}
    try:
        for fut in cf.as_completed(futs, timeout=FETCH_TIMEOUT):
            src = futs[fut]
            try:
                title, text = fut.result()
            except Exception:
                log.debug("четенето на %s се провали", src.get("url"), exc_info=True)
                continue
            src["text"] = text
            if title and not src.get("title"):
                src["title"] = title
    except cf.TimeoutError:
        log.info("RESEARCH: %d страници не се отвориха навреме", sum(not f.done() for f in futs))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return sources


# ─── Модел ───────────────────────────────────────────────────────────────────

def _ask(messages: list[dict], *, light: bool = False) -> str:
    """Текстът от модела; "" при грешка (Brain връща „Error: …“, не хвърля)."""
    from genesis_agent.brain import Brain
    try:
        brain = Brain(light=True) if light else Brain()
        reply = brain.complete(messages)
    except Exception:
        log.debug("моделът не отговори", exc_info=True)
        return ""
    text = (getattr(reply, "raw_text", "") or "").strip()
    return "" if text.startswith("Error:") else text


def _alt_queries(question: str) -> list[str]:
    text = _ask([{"role": "system", "content": _QUERIES_SYS},
                 {"role": "user", "content": question}], light=True)
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("\"'„“")
        if 2 < len(line) < 200 and line.lower() != question.lower():
            out.append(line)
    return out[:2]


def _numbered(passages: list[tuple[int, str]], sources: list[dict]) -> tuple[str, list[int]]:
    """Откъсите за модела, номерирани 1..k по реда на източниците."""
    order: list[int] = []
    for i, _p in passages:
        if i not in order:
            order.append(i)
    blocks = []
    for n, i in enumerate(order, 1):
        src = sources[i]
        body = "\n…\n".join(p for j, p in passages if j == i)
        blocks.append(f"[{n}] {src.get('title') or src['url']} — {src['url']}\n{body}")
    return "\n\n".join(blocks), order


def _not_found(answer: str) -> bool:
    # Само с главни букви, както го иска промптът: „404 Not Found“ или „не е
    # открито до 1500 г.“ са отговори, не отказ (одит 2026-10-07, същият капан).
    return "НЕ Е ОТКРИТО" in answer


def _format(question: str, answer: str, sources: list[dict], order: list[int], t0: float) -> str:
    # gpt-oss понякога цитира с 【1】 (мерено 2026-11-17, Белмекен) — същото като [1].
    answer = re.sub(r"【\s*(\d{1,2})\s*(?:†[^】]*)?】", r"[\1]", answer)
    cited = sorted({int(m) for m in re.findall(r"\[(\d{1,2})\]", answer) if 0 < int(m) <= len(order)})
    missing = _not_found(answer)
    shown = list(range(1, len(order) + 1)) if missing or not cited else cited
    lines = [f"[{n}] {sources[order[n - 1]].get('title') or ''} — {sources[order[n - 1]]['url']}"
             for n in shown]
    if missing:
        # „Не е открито“ не е потвърждение от източник (одит 2026-10-07).
        note = f"отговорът НЕ е открит в {len(order)} прочетени източника — не го измисляй"
    elif cited:
        note = f"цитирани {len(cited)} от {len(order)} прочетени източника"
    else:
        note = f"{len(order)} прочетени източника, без цитат в отговора"
    return (f"[RESEARCH] {question}\n\n{answer}\n\nИзточници:\n" + "\n".join(lines)
            + f"\n({note} · {time.monotonic() - t0:.1f} s)")


def _search(query: str, n: int) -> list[dict]:
    from genesis_agent import web_search
    hits = web_search.search(query, max_results=n) or []
    return [h for h in hits if not str(h.get("title", "")).startswith("Търсачката отказа")]


def _groq_web_answer(question: str) -> str:
    """Последна резерва: моделът на Groq с вграденото търсене (browser_search).
    Мерено 2026-11-17: верен отговор, но 24 s и ~575 хил. входни токена на
    въпрос — затова само когато всичко друго не намери нищо."""
    import json
    import urllib.error
    import urllib.request

    from genesis_agent.search_engines import _key, cool_down, cooling
    key = _key("GROQ_API_KEY")
    if not key or cooling("groq_browser_search"):
        return ""
    body = {"model": "openai/gpt-oss-120b", "tool_choice": "required",
            "tools": [{"type": "browser_search"}],
            "messages": [{"role": "user", "content": (
                f"{question}\n\nОтговори кратко на езика на въпроса и посочи адресите на източниците.")}]}
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "genesis-agent"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        cool_down("groq_browser_search", 3600.0 if e.code in (401, 403, 413, 429) else 300.0)
        return ""
    except (OSError, ValueError):
        log.debug("Groq browser_search не отговори", exc_info=True)
        return ""
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = re.sub(r"【[^】]*】", "", str(msg.get("content") or "")).strip()
    return text


# ─── Входът ──────────────────────────────────────────────────────────────────

def _context_budget() -> int:
    """По-къси откъси, когато мисли локалният модел (/local_model_* или
    GENESIS_LOCAL_ONLY=1) — там дължината на входа е цялото чакане."""
    return LOCAL_CONTEXT_CHARS if os.environ.get("GENESIS_LOCAL_ONLY") == "1" else CONTEXT_CHARS


def grounded_research(question: str, *, top_n: int = READ_PAGES) -> str:
    """Безопасно (не хвърля) — при мрежова грешка връща ясно съобщение."""
    t0 = time.monotonic()
    question = question.strip()
    if not question:
        return "[RESEARCH] Празен въпрос."

    try:
        hits = _search(question, 8)
    except Exception as e:
        log.debug("търсенето се провали", exc_info=True)
        return f"[RESEARCH] Грешка при търсене: {e}"

    budget = _context_budget()
    sources = _read_pages(hits, top_n) if hits else []
    passages = rank_passages(question, sources, budget=budget)
    answer = ""
    order: list[int] = []
    if passages:
        context, order = _numbered(passages, sources)
        answer = _ask([{"role": "system", "content": _SYNTH_SYS},
                       {"role": "user", "content": (
                           f"Днес е {date.today().isoformat()}.\nВъпрос: {question}\n\nОткъси:\n{context}")}])

    if not passages or (answer and _not_found(answer)):
        # Втори кръг: други думи и английски — за въпрос на български често
        # по-добрите страници са на английски, а търсачките вадят различно.
        seen = {s.get("url") for s in sources}
        extra: list[dict] = []
        for q in _alt_queries(question):
            try:
                extra += [h for h in _search(q, 6) if h.get("url") not in seen]
            except Exception:
                log.debug("второто търсене се провали", exc_info=True)
            seen.update(h.get("url") for h in extra)
        if extra:
            sources = sources + _read_pages(extra, max(3, top_n - 2))
            more = rank_passages(question, sources, budget=budget)
            if more:
                passages = more
                context, order = _numbered(passages, sources)
                answer = _ask([{"role": "system", "content": _SYNTH_SYS},
                               {"role": "user", "content": (
                                   f"Днес е {date.today().isoformat()}.\nВъпрос: {question}\n\nОткъси:\n{context}")}])

    if not hits and not passages:
        groq = _groq_web_answer(question)
        if groq:
            return (f"[RESEARCH] {question}\n\n{groq}\n\n(отговор от Groq с търсене в интернет — "
                    f"търсачките тук не върнаха нищо · {time.monotonic() - t0:.1f} s)")
        return f"[RESEARCH] Няма намерени резултати за: {question}"

    if not passages:
        return (f"[RESEARCH] {question}\n\nНамерените страници не съдържат текст по въпроса — "
                "НЕ Е ОТКРИТО. Не го измисляй; кажи, че не е проверено.")

    if not answer:
        # Откъсите са намерени, моделът — недостъпен: даваме ги сурови, ясно
        # казано, вместо да лепим „проверено“ върху нищо (bug fix 2026-08-12).
        context, order = _numbered(passages, sources)
        return (f"[RESEARCH] {question}\n\n⚠️ Обобщението НЕ бе направено (моделът е недостъпен) — "
                "по-долу са най-подходящите откъси, БЕЗ проверка:\n\n" + context)

    if _not_found(answer):
        groq = _groq_web_answer(question)
        if groq:
            return (f"[RESEARCH] {question}\n\n{groq}\n\n(отговор от Groq с търсене в интернет — "
                    f"прочетените тук страници не го съдържаха · {time.monotonic() - t0:.1f} s)")

    return _format(question, answer, sources, order, t0)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    q = " ".join(sys.argv[1:]) or "какво е Python GIL?"
    print(grounded_research(q))
