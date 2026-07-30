"""
genesis_agent.browser — истинска интерактивна browser автоматизация (design note, 2026-07-25).

Playwright, headless Chromium, ИЗОЛИРАН ефемерен профил (НЕ реалния Chrome
профил на потребителя — няма достъп до неговите бисквитки/запазени пароли/сесии).
Всяко действие минава през genesis_agent.sandbox risk-класификация:

    навигация / четене на страница  → SAFE, изпълнява се директно.
    клик / попълване на поле        → CONFIRM (зависи от policy mode, като
                                        chmod/sudo — пита в interactive, автоматично
                                        в allow, отказва в deny).
    парола / данни за карта / бутон
    за плащане-поръчка              → BLOCKED ВИНАГИ, независимо от mode.
                                        Genesis НИКОГА не въвежда пароли/данни
                                        за плащане и не кликва "потвърди поръчка"
                                        автономно — категорично ограничение,
                                        не конфигурируемо, дори при изрично искане.

Интерактивните елементи на страницата се "маркират" с data-genesis-idx атрибут
при всяко сканиране и се адресират по номер (или fuzzy текст match) — Genesis
вижда номерирана листа, не пише CSS селектори наслуки.
"""
from __future__ import annotations

import os

from genesis_agent import sandbox

_ALLOWED_SCHEMES = ("http://", "https://")
_MAX_TEXT_CHARS = 4000
_MAX_ELEMENTS = 150

_SCAN_JS = """
() => {
    const sel = 'a, button, input, textarea, select, [role="button"], [onclick]';
    const els = Array.from(document.querySelectorAll(sel));
    let i = 0;
    const out = [];
    for (const el of els) {
        const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        if (!visible) continue;
        el.setAttribute('data-genesis-idx', String(i));
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                      el.getAttribute('placeholder') || '').trim().slice(0, 80);
        const name = el.getAttribute('name') || el.id || '';
        const r = el.getBoundingClientRect();
        out.push({i, tag, type, text, name,
                   x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)});
        i += 1;
        if (i >= %d) break;
    }
    return out;
}
""" % _MAX_ELEMENTS  # noqa: UP031 — JS literal is full of {}; %-format avoids escaping every brace

# ── Персистентна (в рамките на процеса) headless сесия ──────────────────────
_pw = None
_browser = None
_page = None
_last_elements: list[dict] = []


def _ensure_page():
    global _pw, _browser, _page
    if _page is not None:
        return _page
    from playwright.sync_api import sync_playwright
    _pw = sync_playwright().start()
    # Ефемерен, изолиран профил — без cookies/пароли от реалния Chrome на потребителя.
    # Headless по подразбиране (безопасно, работи и без дисплей на сървър) —
    # GENESIS_BROWSER_VISIBLE=1 показва реален прозорец, за да гледаш агента
    # докато навигира (design note, 2026-07-29, изрична заявка на METKO).
    visible = os.environ.get("GENESIS_BROWSER_VISIBLE") == "1"
    _browser = _pw.chromium.launch(headless=not visible)
    _page = _browser.new_page()
    _page.set_default_timeout(15000)
    return _page


def close() -> None:
    """Затваря браузъра (извиква се при изход от терминалния чат)."""
    global _pw, _browser, _page
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None


def _scan() -> list[dict]:
    global _last_elements
    page = _ensure_page()
    try:
        _last_elements = page.evaluate(_SCAN_JS)
    except Exception:
        _last_elements = []
    return _last_elements


def _format_elements(elements: list[dict]) -> str:
    if not elements:
        return "(няма кликаеми/попълваеми елементи)"
    lines = []
    for e in elements:
        label = e["text"] or e["name"] or "(без текст)"
        extra = f" name={e['name']}" if e["name"] and e["name"] != e["text"] else ""
        typ = f" type={e['type']}" if e["type"] else ""
        lines.append(f"  [{e['i']}] <{e['tag']}{typ}>{extra} \"{label}\"")
    return "\n".join(lines)


def _page_state_summary() -> str:
    page = _ensure_page()
    try:
        title = page.title()
    except Exception:
        title = "(?)"
    url = page.url
    try:
        text = page.inner_text("body")[:_MAX_TEXT_CHARS]
    except Exception:
        text = "(няма достъпен текст)"
    elements = _scan()
    return (
        f"Заглавие: {title}\nURL: {url}\n\n"
        f"Текст (откъс):\n{text}\n\n"
        f"Кликаеми/попълваеми елементи:\n{_format_elements(elements)}"
    )


def _resolve_element(target: str, *, kind: str = "any") -> dict | None:
    """target = индекс от последното сканиране ИЛИ текст за fuzzy match."""
    target = target.strip()
    if target.isdigit():
        idx = int(target)
        for e in _last_elements:
            if e["i"] == idx:
                return e
        return None
    tl = target.lower()
    candidates = _last_elements
    if kind == "field":
        candidates = [e for e in candidates if e["tag"] in ("input", "textarea", "select")]
    best = None
    for e in candidates:
        hay = f"{e['text']} {e['name']}".lower()
        if tl in hay or hay in tl:
            best = e
            break
    return best


def navigate(url: str) -> str:
    """SAFE — зарежда URL и връща състоянието на страницата."""
    url = url.strip()
    if not url.lower().startswith(_ALLOWED_SCHEMES):
        return f"[BROWSE] Отказано — само http(s) адреси са позволени: {url}"
    page = _ensure_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        return f"[BROWSE] Грешка при зареждане на {url}: {e}"
    return f"[BROWSE: {url}]\n{_page_state_summary()}"


def read() -> str:
    """SAFE — препрочита текущата страница (полезно след клик/скрол/попълване)."""
    if _page is None:
        return "[BROWSER_READ] Няма отворена страница — първо използвай [BROWSE: url]."
    return f"[BROWSER_READ]\n{_page_state_summary()}"


def click(target: str) -> str:
    """CONFIRM (или BLOCKED за плащане/поръчка бутони) — клик по индекс/текст."""
    if _page is None:
        return "[BROWSER_CLICK] Няма отворена страница — първо използвай [BROWSE: url]."
    el = _resolve_element(target)
    if el is None:
        return f"[BROWSER_CLICK] Не намерих елемент, съвпадащ с: {target!r}. Виж номерата от [BROWSER_READ]."

    label = el["text"] or el["name"] or target
    is_submit_near_password = False
    try:
        is_submit_near_password = bool(_page.evaluate(
            "(idx) => { const el = document.querySelector(`[data-genesis-idx=\"${idx}\"]`); "
            "const form = el && el.closest('form'); "
            "return !!(form && form.querySelector('input[type=password]')); }",
            el["i"],
        ))
    except Exception:
        pass

    verdict = sandbox.assess_browser_click(label, is_submit_near_password)
    allowed, reason = sandbox._decide(f"BROWSER_CLICK {label}", verdict, sandbox.get_policy())
    if not allowed:
        return f"[BROWSER_CLICK: {label}] {reason}"

    try:
        _page.locator(f'[data-genesis-idx="{el["i"]}"]').click(timeout=5000)
        _page.wait_for_timeout(500)  # кратка пауза за евентуална навигация/render
    except Exception as e:
        return f"[BROWSER_CLICK: {label}] Грешка при клик: {e}"
    return f"[BROWSER_CLICK: {label}] ✓ кликнато\n\n{_page_state_summary()}"


def type_text(arg: str) -> str:
    """CONFIRM (или BLOCKED за пароли/чувствителни полета) — arg = 'target | текст'."""
    if _page is None:
        return "[BROWSER_TYPE] Няма отворена страница — първо използвай [BROWSE: url]."
    if "|" not in arg:
        return "[BROWSER_TYPE] Формат: [BROWSER_TYPE: индекс_или_текст_на_поле | текст за въвеждане]"
    target, text = arg.split("|", 1)
    target, text = target.strip(), text.strip()

    el = _resolve_element(target, kind="field")
    if el is None:
        return f"[BROWSER_TYPE] Не намерих поле, съвпадащо с: {target!r}. Виж номерата от [BROWSER_READ]."

    field_label = el["name"] or el["text"] or target
    verdict = sandbox.assess_browser_field(el["type"], field_label)
    allowed, reason = sandbox._decide(f"BROWSER_TYPE {field_label}", verdict, sandbox.get_policy())
    if not allowed:
        return f"[BROWSER_TYPE: {field_label}] {reason}"

    try:
        _page.locator(f'[data-genesis-idx="{el["i"]}"]').fill(text, timeout=5000)
    except Exception as e:
        return f"[BROWSER_TYPE: {field_label}] Грешка при въвеждане: {e}"
    return f"[BROWSER_TYPE: {field_label}] ✓ въведено"
