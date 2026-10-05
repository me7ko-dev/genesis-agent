"""
genesis_agent.web_check — бърза проверка на записан .html/.css/.js, върната
в резултата на WRITE_FILE/EDIT_FILE (както ruff за .py).

Защо (NEXT_STEPS, план Г.11; измерено 2026-09-28): задача „красив уеб сайт“ —
моделът записа три файла, прочете index.html, потърси `</article` и обяви, че
всичко „се отваря без грешки в браузър“. В страницата имаше og:image към
example.com, aria-label върху div без роля, цени в лева девет месеца след
еврото. Нито едно от тези не се вижда от unit тест, а всяко се вижда от
парсване на самия файл — за милисекунди, без браузър и без модел.

Само stdlib (+ `node --check`, ако node е на PATH). Нищо не блокира записа:
находките отиват при модела като бележка, той решава. Празен низ = чисто.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("genesis.web_check")

WEB_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs"}
_MAX_FINDINGS = 12
_NODE_TIMEOUT = 10

# Елементи без затварящ таг и такива, чийто край HTML позволява да се пропусне
# — за тях липсващ `</x>` не е грешка.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr", "path", "circle", "rect", "line", "polyline",
         "polygon", "ellipse", "stop", "use"}
_OPTIONAL_END = {"p", "li", "dt", "dd", "option", "optgroup", "tr", "td", "th",
                 "thead", "tbody", "tfoot", "colgroup", "rt", "rp", "html", "head", "body"}
_LOCAL_REF_ATTRS = {("link", "href"), ("script", "src"), ("img", "src"), ("source", "src"),
                    ("video", "src"), ("audio", "src"), ("a", "href"), ("iframe", "src"),
                    ("use", "href")}
_PLACEHOLDER_HOST = re.compile(r"(^|\.)example\.(com|org|net)$|^(your|my)?domain\.", re.IGNORECASE)
# Цена в лева: „120 лв“, „120 лв.“, „BGN 120“. От 1 януари 2026 валутата е еврото
# (виж проверено знание bg_euro_bgn_conversion).
_LEV_PRICE = re.compile(r"\d[\d\s.,]*\s?(лв\.?|лева|BGN)(?![а-яa-z])|\bBGN\s?\d", re.IGNORECASE)


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.mismatched: list[str] = []
        self.ids: dict[str, int] = {}
        self.refs: list[tuple[str, str]] = []
        self.external: list[str] = []
        self.img_no_alt = 0
        self.labels_for: set[str] = set()
        self.inputs: list[tuple[str, bool]] = []  # (id, has aria-label/title)
        self.html_lang: str | None = None
        self.has_viewport = self.has_title = self.has_description = self.has_charset = False
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        line = self.getpos()[0]
        if "id" in attrs:
            self.ids[attrs["id"]] = self.ids.get(attrs["id"], 0) + 1
        if tag == "html":
            self.html_lang = attrs.get("lang", "")
        elif tag == "meta":
            name = attrs.get("name", "").lower()
            self.has_viewport |= name == "viewport"
            self.has_description |= name == "description"
            self.has_charset |= "charset" in attrs or attrs.get("http-equiv", "").lower() == "content-type"
            prop = attrs.get("property", "").lower()
            if prop in ("og:image", "og:url") or name == "twitter:image":
                self.external.append(attrs.get("content", ""))
        elif tag == "title":
            self.has_title = True
        elif tag == "img" and "alt" not in attrs:
            self.img_no_alt += 1
        elif tag == "label" and attrs.get("for"):
            self.labels_for.add(attrs["for"])
        elif tag in ("input", "textarea", "select") and attrs.get("type") not in ("hidden", "submit", "button"):
            self.inputs.append((attrs.get("id", ""), bool(attrs.get("aria-label") or attrs.get("title"))))
        for (t, a) in _LOCAL_REF_ATTRS:
            if tag == t and attrs.get(a):
                self.refs.append((a, attrs[a]))
        if tag not in _VOID:
            self.stack.append((tag, line))

    def handle_startendtag(self, tag: str, attrs_list) -> None:
        # `<img ... />`: обработва се като начален таг, но не влиза в стека.
        self.handle_starttag(tag, attrs_list)
        if self.stack and self.stack[-1][0] == tag and tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        names = [t for t, _ in self.stack]
        if tag not in names:
            self.mismatched.append(f"ред {self.getpos()[0]}: </{tag}> без отварящ таг")
            return
        while self.stack:
            open_tag, line = self.stack.pop()
            if open_tag == tag:
                break
            if open_tag not in _OPTIONAL_END:
                self.mismatched.append(f"ред {line}: <{open_tag}> не е затворен преди </{tag}>")

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data)


def _is_local(ref: str) -> bool:
    parts = urlsplit(ref)
    return not (parts.scheme or ref.startswith(("//", "#", "{", "$")))


# Примерни данни, не сайт: скрейпърът си пише `sample.html` за тестовете. Измерено
# 2026-09-28 (bench fuel-prices): бележките за viewport/description и браузърната
# проверка го накараха да „разкраси“ примера от 769 на 6 996 знака, тестовете
# паднаха — 532 s и 215 хил. токена вместо ~100 s и ~46 хил.
_DATA_NAME = re.compile(r"sample|fixture|mock|dummy|example|test|data|page_?\d", re.IGNORECASE)
_DATA_DIRS = {"test", "tests", "fixtures", "fixture", "samples", "data", "testdata", "__snapshots__"}
_STYLED = re.compile(r"<link[^>]+stylesheet|<style[\s>]|<script[\s>]", re.IGNORECASE)


def is_site_page(path: Path, content: str) -> bool:
    """Страница, която хората ще отворят (стилизирана, не в tests/ и не „sample“),
    а не HTML като данни за друга програма."""
    if _DATA_NAME.search(path.stem) or _DATA_DIRS & {p.lower() for p in path.parts[:-1]}:
        return False
    return bool(_STYLED.search(content))


def check_html(path: Path, content: str) -> list[str]:
    c = _Collector()
    try:
        c.feed(content)
        c.close()
    except Exception as e:  # HTMLParser почти не хвърля; ако все пак — казваме го
        log.debug("HTMLParser хвърли — казва се като находка", exc_info=True)
        return [f"HTML не се парсва: {e}"]
    found: list[str] = []
    found += c.mismatched[:4]
    found += [f"<{t}> на ред {ln} не е затворен" for t, ln in c.stack
              if t not in _OPTIONAL_END][:3]
    if not is_site_page(path, content):
        return found  # данни: само структурата, без SEO/достъпност/цени
    return found + _link_findings(path, c) + _page_findings(c, content) + _lev_findings(c)


def _link_findings(path: Path, c: _Collector) -> list[str]:
    found: list[str] = []
    for ref_attr, ref in c.refs:
        if ref.startswith("#"):
            if len(ref) > 1 and unquote(ref[1:]) not in c.ids:
                found.append(f'връзка {ref_attr}="{ref}" води към id, което го няма')
            continue
        if not _is_local(ref) or ref.startswith(("mailto:", "tel:", "data:", "javascript:")):
            continue
        target = (path.parent / unquote(urlsplit(ref).path)).resolve()
        if urlsplit(ref).path and not target.exists():
            found.append(f'{ref_attr}="{ref}" — няма такъв файл до {path.name} (още ли предстои?)')
    for url in c.external:
        host = urlsplit(url).hostname or ""
        if not url or not urlsplit(url).scheme or _PLACEHOLDER_HOST.search(host):
            found.append(f'og/twitter мета сочи към заместител „{url or "(празно)"}" — счупена връзка')
    return found


def _page_findings(c: _Collector, content: str) -> list[str]:
    found: list[str] = []
    dup = [i for i, n in c.ids.items() if n > 1]
    if dup:
        found.append(f"повтарящи се id: {', '.join(dup[:5])}")
    if c.img_no_alt:
        found.append(f"{c.img_no_alt} <img> без alt")
    unlabeled = [i or "(без id)" for i, aria in c.inputs if not aria and i not in c.labels_for]
    if unlabeled:
        found.append(f"полета без <label for>: {', '.join(unlabeled[:5])}")
    if c.html_lang is not None and not c.html_lang:
        found.append("<html> без lang")
    if "<html" not in content.lower():  # фрагмент, не пълен документ
        return found
    missing = [(c.has_viewport, 'няма <meta name="viewport"> — на телефон ще е ситно'),
               (c.has_charset, ('няма <meta charset="utf-8"> — без него кирилицата може да '
                                'излезе като „Ð”Ð¾…“')),
               (c.has_title, "няма <title>"),
               (c.has_description, 'няма <meta name="description"> (търсачките показват него)')]
    return found + [note for present, note in missing if not present]


def _lev_findings(c: _Collector) -> list[str]:
    # Всички наведнъж: с една цена в бележката моделът оправяше по една на рунд
    # (2026-09-28: 4 рунда за 4 стаи).
    lev = [m.group(0).strip() for m in _LEV_PRICE.finditer(" ".join(c.text))]
    if not lev:
        return []
    return [(f"{len(lev)} {'цена' if len(lev) == 1 else 'цени'} в лева "
             f"({', '.join(lev[:6])}) — от 1 януари 2026 "
             "валутата в България е еврото; текущите цени са в € (EUR)")]


def check_css(content: str) -> list[str]:
    body = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    body = re.sub(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", body)
    opened, closed = body.count("{"), body.count("}")
    return [f"CSS: {opened} „{{“ срещу {closed} „}}“ — незатворен блок"] if opened != closed else []


def check_js(path: Path) -> list[str]:
    node = shutil.which("node")
    if not node:
        return []
    try:
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=_NODE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode == 0:
        return []
    lines = [ln for ln in (r.stderr or r.stdout).splitlines() if ln.strip()]
    err = next((ln for ln in lines if "Error" in ln), lines[0] if lines else "синтактична грешка")
    where = next((ln for ln in lines if str(path.name) in ln), "")
    return [f"JS не се парсва ({where.strip()}): {err.strip()}" if where else f"JS не се парсва: {err.strip()}"]


# Файловете, за които последната бележка имаше находки. Когато станат чисти,
# това се казва изрично: 2026-09-28 бележката просто изчезваше, моделът не
# разбираше, че е оправил всичко, и пусна XML парсер (lxml.etree.parse) върху
# HTML5 — фалшиви грешки, „поправки“ на здрав код, 9 рунда до тавана.
_HAD_FINDINGS: set[Path] = set()


def web_note(path: Path) -> str:
    """Бележка за резултата на WRITE_FILE/EDIT_FILE; "" ако няма какво да се каже."""
    suffix = path.suffix.lower()
    if suffix not in WEB_SUFFIXES:
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if suffix in (".html", ".htm"):
        found = check_html(path, content)
    elif suffix == ".css":
        found = check_css(content)
    else:
        found = check_js(path)
    key = path.resolve()
    if not found:
        if key in _HAD_FINDINGS:
            _HAD_FINDINGS.discard(key)
            return (f"\n[уеб проверка] {path.name}: вече е чисто ✓ — бележките са оправени, "
                    "не е нужна още проверка на файла (XML парсер като lxml.etree.parse дава "
                    "фалшиви грешки за валиден HTML5).")
        return ""
    _HAD_FINDINGS.add(key)
    shown = found[:_MAX_FINDINGS]
    more = f"\n  … още {len(found) - len(shown)}" if len(found) > len(shown) else ""
    return ("\n[уеб проверка] " + path.name + ":\n" + "\n".join(f"  • {f}" for f in shown) + more
            + "\n(Браузър не е пускан — това е само проверка на файла.)")
