"""
genesis_agent.page_check — браузърна проверка на уеб страниците, писани в
текущия ход (NEXT_STEPS, план Г.11 — втората половина).

Защо (измерено 2026-09-28, задача „сайт за къща за гости“): след web_check
файловете бяха чисти, а страницата — не: бял текст върху светлозелен фон
(1.65:1 при нужни 3:1), 17 нечетими текста в тъмната тема, сива кутия „Карта
(SVG placeholder)“, бутони под 24 px на телефон. Моделът не вижда страницата;
вижда само числа — тук те се мерят в истински Chromium.

Кога: веднъж, когато моделът каже „готово“ (ход без инструменти) и в хода е
записан .html/.css/.js — както тестовете след поправка в `genesis fix`. Най-много
MAX_RUNS пъти на ход; находките отиват обратно при модела.

Браузърът: playwright в отделен процес (page_check_runner.py) с първия Python,
който го има — този на Genesis или този на проекта (на лаптопа: системният).
Няма ли — проверката мълчи; нищо не бива да спира работа заради нея.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).with_name("page_check_runner.py")
MAX_RUNS = 2
_TIMEOUT = 150
_WEB = {".html", ".htm", ".css", ".js", ".mjs"}
_WRITTEN = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): ([^\]\n]+)\] ✓", re.MULTILINE)
# Средата на проверката: без ключовете на Genesis — страницата изпълнява чужд JS.
# `PWD$` (MYSQL_PWD) и `WEBHOOK` (SLACK_WEBHOOK_URL, DISCORD_WEBHOOK,
# MONITOR_WEBHOOK_URL — адрес с токен в пътя) стигаха до браузъра (одит 2026-10-27).
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD$|CREDENTIAL|AUTH|DSN|COOKIE|SESSION"
                         r"|WEBHOOK|_PAT$|^PAT$|_SK$|DATABASE_URL", re.IGNORECASE)

_python_cache: dict[str, str] = {}


# Стойност с вградени данни за вход (`postgres://u:парола@…`) е тайна, каквото
# и да е името ѝ (DATABASE_URL, SENTRY_DSN). Проксито остава — без него страница
# зад корпоративен прокси не зарежда нищо от CDN.
_CREDENTIAL_URL = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_KEEP = re.compile(r"^(https?_proxy|no_proxy|all_proxy)$", re.IGNORECASE)


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if _KEEP.match(k) or not (_SECRET_ENV.search(k) or _CREDENTIAL_URL.search(v))}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def browser_python(project: Path | None = None) -> str:
    """Python с playwright, или "" ако няма такъв."""
    key = str(project or "")
    if key in _python_cache:
        return _python_cache[key]
    from genesis_agent.paths import project_python
    found = ""
    for py in dict.fromkeys([sys.executable, project_python(project)]):
        if not py:
            continue
        try:
            r = subprocess.run([py, "-c", "import playwright.sync_api"], capture_output=True,
                               timeout=30, env=_clean_env(), check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            found = py
            break
    _python_cache[key] = found
    return found


def run(html: Path, shots: Path | None = None) -> tuple[list[str] | None, str]:
    """(находки, бележка). Находки None = проверката не можа да тръгне (бележката
    казва защо); [] = чисто."""
    py = browser_python(html.parent)
    if not py:
        return None, "няма Python с playwright (pip install playwright && playwright install chromium)"
    cmd = [py, str(RUNNER), str(html)] + (["--shots", str(shots)] if shots else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=_TIMEOUT, env=_clean_env(), check=False)
    except subprocess.TimeoutExpired:
        return None, f"браузърът не отговори за {_TIMEOUT} s"
    except OSError as e:
        return None, f"не тръгна: {e}"
    try:
        res = json.loads(r.stdout or "{}")
    except ValueError:
        return None, f"неразбираем изход: {(r.stderr or r.stdout)[-200:]}"
    if res.get("error"):
        return None, f"браузърът не тръгна: {res['error']}"
    return list(res.get("findings") or []), ""


def written_web_files(result: str) -> list[Path]:
    """Уеб файловете, които този резултат на инструмент доказва като записани."""
    return [Path(p.strip()) for p in _WRITTEN.findall(result or "")
            if Path(p.strip()).suffix.lower() in _WEB]


def pages_for(files: set[Path]) -> list[Path]:
    """Кои страници да се отворят: записаните .html, а за записан само CSS/JS —
    index.html в същата папка (ако го има)."""
    pages: dict[Path, None] = {}
    for f in sorted(files):
        if f.suffix.lower() in (".html", ".htm"):
            pages[f] = None
        elif (f.parent / "index.html").is_file():
            pages[f.parent / "index.html"] = None
    from genesis_agent.web_check import is_site_page

    def site(p: Path) -> bool:
        try:
            return is_site_page(p, p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
    return [p for p in pages if p.is_file() and site(p)][:3]


# Снимките към модела: компютър и телефон, светла тема — тъмната удвоява
# цената (~1.5K токена картинка) за същото оформление.
_SHOT_NAMES = ("desktop-light.png", "mobile-light.png")
_MAX_SHOTS = 2


def _top_of(data: bytes, ratio: float) -> bytes:
    """Горната част на снимка на цялата страница: 1440×6000 смалена до 1568 px
    по дългата страна е 376 px широка — нечетима. С Pillow се реже до
    височина ≤ ширина × ratio; без него — цялата (vision я смалява/отказва)."""
    try:
        from PIL import Image
    except ImportError:
        return data
    import io
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if h <= w * ratio:
            return data
        out = io.BytesIO()
        img.crop((0, 0, w, int(w * ratio))).save(out, format="PNG")
        return out.getvalue()
    # DecompressionBombError не е OSError (ревю 2026-10-15): страница над
    # ~124 000 px при 1440 px сваляше хода; vision.image_part после я отказва.
    except (OSError, ValueError, Image.DecompressionBombError):
        return data


def _shots_of(folder: Path, page: str) -> list[dict]:
    from genesis_agent import vision
    parts = []
    for name in _SHOT_NAMES:
        try:
            data = (folder / name).read_bytes()
        except OSError:
            continue
        ratio = 2.2 if name.startswith("mobile") else 1.0
        try:
            parts.append(vision.image_part(_top_of(data, ratio), "image/png", f"{page} {name}"))
        except vision.ImageError:
            continue
    return parts


class FinalCheck:
    """Проверката в края на хода. Цикълът на агента вика `observe` за всеки
    резултат на инструмент и `check()`, когато моделът спре да вика инструменти."""

    def __init__(self, runner=None, *, shots: bool = False) -> None:
        self._run = runner  # None → модулният `run`, търсен при всяко извикване
        # shots (2026-10-15): моделът вижда картинки → с находките идват и
        # снимките на страницата (`images`, части за vision.attachment_message).
        # Числата казват „контраст 1.65:1“; снимката показва кое и къде.
        self.shots = shots
        self.images: list[dict] = []
        self._dirty: set[Path] = set()
        self.runs = 0
        self.passed = False  # последната проверка е чиста — доказва „проверено в браузър“
        # Находки, върнати на модела, а той не е записал нищо след тях. Измерено
        # 2026-09-28: отговори „All set.“ и ходът свърши с нечетим текст в тъмната
        # тема. Напомня се веднъж — може и да има причина, но да я каже.
        self._unanswered = False
        self._reminded = False

    def observe(self, result: str) -> None:
        written = written_web_files(result)
        if written:
            self._dirty.update(written)
            self._unanswered = False

    def due(self) -> bool:
        return ((bool(self._dirty) and self.runs < MAX_RUNS)
                or (self._unanswered and not self._reminded))

    def check(self) -> tuple[str, str]:
        """(бележка за модела, ред за оператора). Празна бележка = няма какво
        да се поправя (чисто, пропуснато или нищо уеб в хода)."""
        self.images = []
        if not self.due():
            return "", ""
        if not self._dirty:
            self._reminded = True
            return ("[проверка в браузър] Находките от браузъра по-горе не са поправени — след "
                    "тях не е записан нито един файл. Поправи ги с EDIT_FILE или кажи изрично "
                    "защо не са проблем; финалният отговор — на български."), ""
        pages = pages_for(self._dirty)
        self._dirty.clear()
        if not pages:
            return "", ""
        self.runs += 1
        notes: list[str] = []
        shots: list[dict] = []
        for page in pages:
            with tempfile.TemporaryDirectory(prefix="genesis-shots-") as tmp:
                if self.shots:
                    findings, why = (self._run or run)(page, Path(tmp))
                else:
                    findings, why = (self._run or run)(page)
                if findings is None:
                    return "", f"[проверка в браузър] пропусната — {why}"
                if findings and self.shots and len(shots) < _MAX_SHOTS:
                    shots += _shots_of(Path(tmp), page.name)[:_MAX_SHOTS - len(shots)]
            if findings:
                notes.append(f"{page.name}:\n" + "\n".join(f"  • {f}" for f in findings[:12]))
        names = ", ".join(p.name for p in pages)
        if not notes:
            self.passed = True
            self._unanswered = False
            return "", (f"[проверка в браузър] {names}: чисто ✓ "
                        "(Chromium: 1440 px светла и тъмна тема, 390 px телефон)")
        self.passed = False
        self._unanswered = True
        self.images = shots
        last = self.runs >= MAX_RUNS
        body = "\n".join(notes)
        note = ("[проверка в браузър] Отворих страницата в Chromium (1440 px светла и тъмна "
                "тема, 390 px телефон) и измерих:\n" + body
                + "\nОправи ги в CSS/HTML"
                + (", после дай финалния отговор и кажи кое остава." if last
                   else " — после ще проверя пак."))
        # Операторът вижда същите находки, не само броя им.
        return note, f"[проверка в браузър] {sum(n.count('•') for n in notes)} находки\n{body}"
