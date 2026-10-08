#!/usr/bin/env python3
"""
Самостоятелна браузърна проверка на една страница — пуска се от
genesis_agent.page_check като ОТДЕЛЕН процес, с Python-а, който има playwright
(на лаптопа това е системният, не pipx venv-ът). Затова тук е само stdlib +
playwright и нищо от genesis_agent.

    python page_check_runner.py <index.html> [--shots DIR]

Сервира папката на страницата на 127.0.0.1 (не file:// — модули и fetch там
не вървят), отваря я в Chromium на 1440 и 390 px, в светла и тъмна тема, и
печата JSON: {"findings": [...], "shots": [...]}. Находките са на български,
с числа — моделът не вижда страницата, вижда само това.

Контрастът се мери по ПИКСЕЛИТЕ зад текста (текстът се прави прозрачен и
страницата се снима), не по CSS: фонът на hero секцията често е отделен
елемент, ::before или SVG, а измерено 2026-09-28 проверката по родителите
видя „бял текст върху бяло“ там, където беше бял текст върху светлозелен SVG.
"""
from __future__ import annotations

import argparse
import base64
import functools
import http.server
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path

# Всичко за един изглед; Python решава кое е находка.
_MEASURE = r"""
() => {
  const W = window.innerWidth;
  const parse = c => { const m = (c || '').match(/[\d.]+/g); if (!m || m.length < 3) return null;
    return {rgb: m.slice(0, 3).map(Number), a: m.length > 3 ? Number(m[3]) : 1}; };
  const desc = el => { const cls = (el.className && typeof el.className === 'string') ? '.' + el.className.trim().split(/\s+/)[0] : '';
    const t = (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return el.tagName.toLowerCase() + cls + (t ? ` „${t}“` : ''); };
  const visible = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return !(s.display === 'none' || s.visibility === 'hidden' || r.width < 2 || r.height < 2); };
  // Анимация, вързана за скрола (animation-timeline: view()), държи елемента
  // невидим, докато не влезе в екрана — това е замисълът, не грешка.
  const scrollLinked = e => e.getAnimations().some(a => a.timeline && a.timeline.constructor.name !== 'DocumentTimeline');
  const opacity = el => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
    if (scrollLinked(e)) return 1; o *= Number(getComputedStyle(e).opacity); } return o; };
  const texts = [], invisible = [];
  for (const el of document.body.querySelectorAll('*')) {
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 1);
    if (!own || !visible(el) || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'TITLE'].includes(el.tagName)) continue;
    if (opacity(el) < 0.1) { invisible.push(desc(el)); continue; }
    const st = getComputedStyle(el);
    const c = parse(el instanceof SVGElement ? st.fill : st.color); if (!c || c.a < 0.05) continue;
    const r = el.getBoundingClientRect(); const size = parseFloat(st.fontSize);
    if (r.top + scrollY < 0 || r.left + scrollX < 0) continue;  // skip link и др. — скрити извън страницата до фокус
    // Кутията на СЪДЪРЖАНИЕТО (без padding и рамка) — там са буквите. Цялата
    // кутия на бутон-„хапче“ носи ъглите с фона на страницата: на телефон те
    // бяха над 10% от пробите и бутон 5.5:1 се отчиташе 1.17:1 (2026-09-28).
    const px = k => parseFloat(st[k]) || 0;
    const l = px('paddingLeft') + px('borderLeftWidth'), t = px('paddingTop') + px('borderTopWidth');
    const cw = r.width - l - px('paddingRight') - px('borderRightWidth'), ch = r.height - t - px('paddingBottom') - px('borderBottomWidth');
    const box = cw >= 2 && ch >= 2 ? {x: r.left + l, y: r.top + t, w: cw, h: ch} : {x: r.left, y: r.top, w: r.width, h: r.height};
    if (texts.length < 300) texts.push({x: box.x + scrollX, y: box.y + scrollY, w: box.w, h: box.h,
      rgb: c.rgb, a: c.a, large: size >= 24 || (size >= 18.6 && Number(st.fontWeight) >= 700), desc: desc(el)});
  }
  const overflow = [];
  if (document.documentElement.scrollWidth > W + 1) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > W + 1 && r.width > 0 && visible(el)) overflow.push(desc(el) + ` (до ${Math.round(r.right)} px)`);
      if (overflow.length >= 3) break;
    }
  }
  const small = [];
  for (const el of document.querySelectorAll('a, button, input, select, textarea, [role=button]')) {
    if (!visible(el) || el.type === 'hidden') continue;
    if (el.tagName === 'A' && el.closest('p, li') && !el.closest('nav')) continue;  // връзка в текст — изключение по WCAG 2.5.8
    const r = el.getBoundingClientRect(); if (r.height < 24 || r.width < 24) small.push(desc(el) + ` ${Math.round(r.width)}×${Math.round(r.height)}`);
  }
  const placeholders = [];
  const re = /placeholder|lorem ipsum|\btodo\b|заместител|coming soon|очаквайте скоро|снимка \d|image \d/i;
  for (const el of document.body.querySelectorAll('*')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
    if (re.test(own) && visible(el)) placeholders.push(desc(el));
  }
  // Голяма кутия почти без съдържание: само емоджи или едноцветна SVG (2026-09-28:
  // стаи и галерия — 🛏️ 🔥 🎵 в бежови полета 260×170 px; преди — сиви правоъгълници).
  const sparse = [];
  const painted = s => (parse(s.backgroundColor)?.a || 0) > 0.05 || s.backgroundImage !== 'none'
    || parseFloat(s.borderTopWidth) > 0 || s.boxShadow !== 'none';
  for (const el of document.body.querySelectorAll('div, figure, span, a, li, article, picture')) {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    if (!visible(el) || r.width < 150 || r.height < 110 || ['absolute', 'fixed'].includes(s.position)) continue;
    if ((el.innerText || '').replace(/\s+/g, '').length > 3) continue;
    const area = r.width * r.height, big = m => { const q = m.getBoundingClientRect(); return q.width * q.height > area * 0.2; };
    if ([...el.querySelectorAll('img, video, canvas, iframe, picture, input, textarea')].some(big)) continue;
    const svgs = [...el.querySelectorAll('svg')].filter(big);
    if (svgs.some(v => v.querySelectorAll('path, polygon, circle, ellipse, rect, line, polyline, image, use').length >= 3)) continue;
    if (!painted(s) && !svgs.length) continue;
    sparse.push({el, d: desc(el) + ` ${Math.round(r.width)}×${Math.round(r.height)}`});
  }
  // Най-вътрешните: „div.gallery-item ×4“ казва какво да се поправи, „div.gallery-grid“ — не.
  const inner = sparse.filter(o => !sparse.some(q => q !== o && o.el.contains(q.el)));
  const header = document.querySelector('header, [role=banner]');
  return {
    scrollWidth: document.documentElement.scrollWidth, width: W, texts, invisible, overflow,
    small: small.slice(0, 5), smallCount: small.length, placeholders: placeholders.slice(0, 4),
    sparse: inner.map(o => o.d).slice(0, 4), sparseCount: inner.length,
    h1: document.querySelectorAll('h1').length,
    headerHeight: header ? Math.round(header.getBoundingClientRect().height) : 0,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    htmlBg: getComputedStyle(document.documentElement).backgroundColor,
  };
}
"""

# Без скролбар по време на мерене: снимката на цялата страница го маха, ширината
# се сменя с ~15 px, текстът се пренарежда и кутиите се разминават с пикселите
# (2026-09-28: бутон „Изпрати“ отчетен 1.17:1, реално 5.5:1).
_NO_SCROLLBAR = "html{scrollbar-width:none}::-webkit-scrollbar{display:none}"

_NO_ANIMATION = "*,*::before,*::after{animation:none!important}"

# Текстът прозрачен — на снимката остава само това, което е ЗАД него.
_HIDE_TEXT = ("*,*::before,*::after{color:transparent!important;-webkit-text-fill-color:transparent!important;"
              "text-shadow:none!important;transition:none!important}svg text{fill:transparent!important}")

# Контраст по пикселите: за всеки текст — 10-и и 90-и перцентил на яркостта на
# фона в неговата кутия (не единичен пиксел: рамки и икони не бива да решават).
_CONTRAST_FROM_PIXELS = r"""
async ({img, texts}) => {
  const im = new Image(); im.src = 'data:image/png;base64,' + img; await im.decode();
  const cv = document.createElement('canvas'); cv.width = im.width; cv.height = im.height;
  const cx = cv.getContext('2d', {willReadFrequently: true}); cx.drawImage(im, 0, 0);
  const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const out = [];
  for (const t of texts) {
    const x0 = Math.max(0, Math.floor(t.x)), y0 = Math.max(0, Math.floor(t.y));
    const w = Math.min(cv.width - x0, Math.ceil(t.w)), h = Math.min(cv.height - y0, Math.ceil(t.h));
    if (w < 2 || h < 2) continue;
    const d = cx.getImageData(x0, y0, w, h).data;
    const step = Math.max(1, Math.floor(Math.sqrt(w * h / 400)));
    const px = [];
    for (let y = 0; y < h; y += step) for (let x = 0; x < w; x += step) { const i = (y * w + x) * 4; px.push([d[i], d[i + 1], d[i + 2]]); }
    if (!px.length) continue;
    const avg = [0, 1, 2].map(k => px.reduce((s, p) => s + p[k], 0) / px.length);
    const fg = t.a >= 1 ? t.rgb : t.rgb.map((v, k) => v * t.a + avg[k] * (1 - t.a));
    const L = px.map(lum).sort((a, b) => a - b);
    const tl = lum(fg);
    const r = Math.min(...[L[Math.floor(L.length * 0.1)], L[Math.floor(L.length * 0.9)]]
      .map(b => (Math.max(tl, b) + 0.05) / (Math.min(tl, b) + 0.05)));
    if (r < (t.large ? 3 : 4.5)) out.push({el: t.desc, ratio: Math.round(r * 100) / 100,
      bg: `фон ≈ rgb(${avg.map(Math.round).join(',')})`});
  }
  return out;
}
"""

_THEME_TOGGLE = "button, [role=button], input[type=checkbox]"
_THEME_WORDS = r"тем|theme|dark|light|тъмн|светл|🌙|☀|☾"


_UTF8_TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
               ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
               ".mjs": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


# Какво сървърът НЕ дава на страницата (одит 2026-10-07): папката на сайта
# често е коренът на проекта (Vite), до index.html стои `.env` — и всеки скрипт
# на страницата, включително от CDN, можеше да го вземе с fetch('/.env').
# `node_modules/` остава достъпен: страница без бъндлър го реферира законно
# (`/node_modules/chart.js/dist/chart.umd.js`), а 404 там е фалшива находка.
_PRIVATE = re.compile(r"(^|/)(\.[^/]*|__pycache__|venv|env)(/|$)"
                      r"|\.(pem|key|p12|pfx|jks|keystore|sqlite3?|db|env|sql)$|(^|/)(id_rsa|id_ed25519)"
                      r"|(^|/)(credentials|service[-_]?account|client_secret)[^/]*\.json$",
                      re.IGNORECASE)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):  # type: ignore[no-untyped-def]
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        local = Path(self.translate_path(self.path))
        root = Path(self.directory).resolve()
        try:
            inside = local.resolve().is_relative_to(root)
        except OSError:
            inside = False
        if _PRIVATE.search(path) or not inside:
            self.send_error(404)
            return None
        return super().send_head()

    def __init__(self, *args, **kwargs) -> None:
        # Без charset браузърът чете страница без <meta charset> като windows-1252 —
        # кирилицата става „Ð”Ð¾Ð±…“. Хостингите (Pages, Vercel) пращат utf-8.
        # Преди super(): той обработва заявката още в конструктора.
        self.extensions_map = {**self.extensions_map, **_UTF8_TYPES}
        super().__init__(*args, **kwargs)

    def log_message(self, *_args) -> None:
        pass


def _serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _scroll_through(page, height: int) -> None:
    """Превърта цялата страница, за да тръгнат reveal анимациите. `instant`:
    при `scroll-behavior: smooth` обикновеното scrollTo чакаше анимацията —
    0.56 s на стъпка, 18 от 35 s на цялата проверка (2026-09-28)."""
    total = page.evaluate("document.documentElement.scrollHeight")
    for y in range(0, total + height, max(200, height * 2 // 3)):
        page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
        page.wait_for_timeout(50)
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(700)  # reveal анимациите да свършат


def _measure(page) -> dict:
    bar = page.add_style_tag(content=_NO_SCROLLBAR)
    page.wait_for_timeout(60)
    m = page.evaluate(_MEASURE)
    m["lowContrast"] = []
    if m["texts"]:
        style = page.add_style_tag(content=_HIDE_TEXT)
        page.wait_for_timeout(60)
        png = page.screenshot(full_page=True)
        style.evaluate("el => el.remove()")
        m["lowContrast"] = page.evaluate(_CONTRAST_FROM_PIXELS,
                                         {"img": base64.b64encode(png).decode(), "texts": m["texts"]})
    bar.evaluate("el => el.remove()")
    return m


def _bg(m: dict) -> str:
    return m["bodyBg"] if "0, 0, 0, 0" not in m["bodyBg"] else m["htmlBg"]


def _examples(low: list[dict]) -> str:
    return "; ".join(f"{c['el']} {c['ratio']}:1 ({c['bg']})" for c in low[:3])


def _theme_and_form(page, before: dict) -> tuple[list[str], bool]:
    """Находките от бутона за тема и от празно изпращане на формата; и дали
    изобщо има бутон за тема."""
    out: list[str] = []
    buttons = page.locator(_THEME_TOGGLE)
    target = None
    for i in range(min(buttons.count(), 30)):
        el = buttons.nth(i)
        if not el.is_visible():
            continue
        label = " ".join(filter(None, [el.get_attribute("aria-label"), el.get_attribute("title"),
                                       el.inner_text()]))
        if re.search(_THEME_WORDS, label, re.IGNORECASE):
            target = el
            break
    if target is not None:
        target.click()
        page.wait_for_timeout(400)
        after = _measure(page)
        if _bg(after) == _bg(before):
            out.append("бутонът за тема не сменя фона")
        elif after["lowContrast"]:
            out.append(f"след бутона за тема: {len(after['lowContrast'])} текста с нисък "
                       f"контраст — напр. {_examples(after['lowContrast'])}")
        target.click()
        page.wait_for_timeout(300)
    form = page.locator("form").first
    if form.count() and form.locator("[required]").count():
        novalidate = form.get_attribute("novalidate") is not None
        submit = form.locator("button[type=submit], input[type=submit], button:not([type])").first
        if novalidate and submit.count():
            text_before = page.evaluate("document.body.innerText")
            submit.click()
            page.wait_for_timeout(500)
            invalid = page.locator("[aria-invalid=true]").count()
            if not invalid and page.evaluate("document.body.innerText") == text_before:
                out.append("празна форма: при „Изпрати“ не се показва грешка "
                           "(novalidate, а собствена проверка няма)")
    return out, target is not None


def check(html: Path, shots: Path | None, single: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    srv, port = _serve(html.parent)
    url = f"http://127.0.0.1:{port}/{html.name}"
    findings: list[str] = []
    saved: list[str] = []
    views = [("компютър", (1440, 900), "light"), ("компютър, тъмна тема", (1440, 900), "dark"),
             ("телефон", (390, 844), "light")]
    measured: dict[str, dict] = {}
    has_toggle = False
    try:
        with sync_playwright() as p:
            # --no-proxy-server: без него Chromium под Windows търси proxy (WPAD).
            # --single-process (само Windows): там всеки нов процес на Chromium минава
            # през Smart App Control/Defender — първият раздел отваряше за 5 s, с
            # флага за 0.03 s (2026-09-28). Не е официално поддържан режим, затова
            # при срив check() се вика пак без него (виж main).
            browser = p.chromium.launch(args=["--no-proxy-server"] + (["--single-process"] if single else []))
            # ЕДИН раздел за трите изгледа; размерът и темата се сменят на място.
            page = browser.new_context().new_page()
            errors: list[str] = []
            failed: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("requestfailed", lambda r: failed.append(r.url))
            page.on("response", lambda r: failed.append(f"{r.url} ({r.status})") if r.status >= 400 else None)
            for name, (w, h), scheme in views:
                page.set_viewport_size({"width": w, "height": h})
                page.emulate_media(color_scheme=scheme)
                page.goto(url, wait_until="load", timeout=20000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
                _scroll_through(page, h)
                m = _measure(page)
                measured[name] = m
                if shots:
                    # Снимката е за хора: анимациите, вързани за скрола, иначе оставят
                    # всичко под първия екран празно (елементите още „не са влезли“).
                    frozen = page.add_style_tag(content=_NO_ANIMATION)
                    page.wait_for_timeout(150)
                    shot = shots / f"{'mobile' if w < 500 else 'desktop'}-{scheme}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    saved.append(str(shot))
                    frozen.evaluate("el => el.remove()")
                if name == "компютър":
                    for e in dict.fromkeys(errors):
                        findings.append(f"грешка в конзолата: {e[:160]}")
                    local = [f for f in dict.fromkeys(failed) if f"127.0.0.1:{port}" in f]
                    other = [f for f in dict.fromkeys(failed) if f not in local]
                    for f in local[:4]:
                        findings.append(f"не се зарежда: {f.replace(f'http://127.0.0.1:{port}/', '')}")
                    for f in other[:2]:
                        findings.append(f"външна заявка не мина: {f[:120]}")
                    if m["h1"] != 1:
                        findings.append(f"<h1> е {m['h1']} пъти (трябва точно 1)")
                    if m["headerHeight"] > 110:
                        findings.append(f"header-ът е висок {m['headerHeight']} px на компютър — "
                                        "лого, навигация и бутоните не са на един ред")
                    extra, has_toggle = _theme_and_form(page, m)
                    findings += extra
                    # Бутонът за тема помни избора — следващият изглед не бива да го наследи.
                    page.evaluate("try { localStorage.clear(); sessionStorage.clear() } catch (e) {}")
            browser.close()
    finally:
        srv.shutdown()

    # Един и същ проблем обикновено се вижда в няколко изгледа — казва се веднъж,
    # с изброени изгледи, вместо три почти еднакви реда.
    per_view: dict[str, list[str]] = {}
    for name, m in measured.items():
        bodies = []
        if m["lowContrast"]:
            bodies.append(f"{len(m['lowContrast'])} текста с нисък контраст (нужно ≥4.5:1, за "
                          f"едър ≥3:1) — напр. {_examples(m['lowContrast'])}")
        if m["invisible"]:
            bodies.append(f"{len(m['invisible'])} текста остават невидими (opacity≈0) и след "
                          f"скрол — напр. {m['invisible'][0]}; анимацията не се задейства")
        if m["overflow"]:
            bodies.append(f"страницата е по-широка от екрана ({m['scrollWidth']} px при "
                          f"{m['width']}) — {', '.join(m['overflow'])}")
        if m["placeholders"]:
            bodies.append(f"видим заместител — {', '.join(m['placeholders'][:3])}; направи "
                          "истинско съдържание (SVG, текст)")
        if m["sparseCount"]:
            bodies.append(f"{m['sparseCount']} големи полета почти без съдържание (само емоджи или "
                          f"едноцветна форма) — напр. {', '.join(m['sparse'][:3])}; нарисувай SVG "
                          "сцена със слоеве/градиенти по темата")
        for b in bodies:
            per_view.setdefault(b, []).append(name)
    findings += [f"{' / '.join(names)}: {body}" for body, names in per_view.items()]
    phone = measured.get("телефон")
    if phone and phone["smallCount"]:
        findings.append(f"телефон: {phone['smallCount']} бутона/връзки под 24×24 px (трудни за "
                        f"натискане) — напр. {', '.join(phone['small'][:3])}")
    light, dark = measured.get("компютър"), measured.get("компютър, тъмна тема")
    if light and dark and _bg(light) == _bg(dark) and not has_toggle:
        findings.append("тъмна тема: няма бутон, а при prefers-color-scheme: dark фонът не се сменя")
    return {"findings": list(dict.fromkeys(findings)), "shots": saved}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html", type=Path)
    ap.add_argument("--shots", type=Path)
    a = ap.parse_args()
    if a.shots:
        a.shots.mkdir(parents=True, exist_ok=True)
    try:
        try:
            res = check(a.html.resolve(), a.shots, single=sys.platform == "win32")
        except Exception:
            if sys.platform != "win32":
                raise
            res = check(a.html.resolve(), a.shots)  # без --single-process
    except Exception as e:  # браузърът не тръгна и т.н. — извикващият решава
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
