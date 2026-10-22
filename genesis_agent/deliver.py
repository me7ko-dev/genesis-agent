"""
genesis_agent.deliver — the project, handed over: a zip plus a report.

NEXT_STEPS Д.13: what the client receives is not the chat but the project —
the files, how to run them, what was assumed, and whether its tests pass right
now. Every one of those answers is taken from the project itself at the moment
of export (the README, the files, a real test run, the work memory of that
folder), never from what the model said it did: a report written from the
conversation would repeat whatever the conversation claimed.

    genesis export [PATH] [-o OUT.zip] [--no-tests]
    /export                       (in the chat — the current workspace)

Keys stay behind. A file the sandbox treats as a secret (`.env`, `*.pem`,
`id_rsa`, ...) is left out of the zip and the report says it was, by name, so
the recipient knows to supply their own.
"""
from __future__ import annotations

import contextlib
import fnmatch
import functools
import logging
import os
import re
import subprocess
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from genesis_agent.code_edit import read_text_any

log = logging.getLogger("genesis.deliver")

REPORT_NAME = "GENESIS_REPORT.md"
# A project above this is not a deliverable; it is a disk image with code in it.
_MAX_INPUT_MB = 200

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".venv", "venv", "env", "dist", "build", "target",
    ".next", ".tox", ".gradle", ".verify_libs", ".idea", ".vscode", ".terraform",
}
# Имена, които са и обичайни папки с код (`mypkg/build/builder.py`): само в
# корена са артефакт; по-навътре — само ако си личи (venv, кеш, Cargo target).
# Пропускани на всяка дълбочина, те липсваха от архива без дума (одит 2026-10-09).
_SKIP_ONLY_AT_TOP = {"build", "dist", "env", "target"}


def _skip_dir(parent: Path, name: str, depth: int) -> bool:
    if name not in _SKIP_DIRS:
        return False
    if depth == 0 or name not in _SKIP_ONLY_AT_TOP:
        return True
    here = parent / name
    return ((here / "pyvenv.cfg").is_file() or (here / "CACHEDIR.TAG").is_file()
            or (name == "target" and (parent / "Cargo.toml").is_file()))
_SKIP_SUFFIXES = {".pyc", ".pyo"}
# `.git` като ФАЙЛ е указателят на submodule (`gitdir: ../.git/modules/…`) —
# пътят е от машината на оператора и у клиента не води наникъде (2026-10-11).
_SKIP_NAMES = {".DS_Store", "Thumbs.db", ".git"}

# Тайни, които образецът на sandbox-а не познава (той пази команди, не
# предаване). Одит 2026-10-07: `.envrc`, `.streamlit/secrets.toml`,
# `certs/server.key`, `token.json`, `client_secret_*.json`, `master.key`,
# `service-account.json`, `.htpasswd` влизаха в архива.
_EXPORT_SECRETS = re.compile(
    r"(^|/)(\.envrc|\.htpasswd|token\.json|master\.key|credentials\.json|\.git-credentials"
    r"|\.pgpass|\.dockercfg|terraform\.tfstate(\.backup)?)$"
    r"|\.(key|jks|keystore|kdbx|ppk|p8|ovpn)$"
    r"|(^|/)secrets?\.[a-z]+$|(^|/)client_secret[^/]*\.json$|service[-_]?account[^/]*\.json$",
    re.IGNORECASE)
# И по съдържание — частен ключ в иначе невинно име (`config.json`, `deploy.txt`).
_SECRET_CONTENT = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\"private_key\"\s*:\s*\"-----BEGIN")
# Файлът се чете ЦЯЛ, на парчета от _SCAN_BYTES с припокриване (ключ на
# границата иначе се цепи на две). Одит 2026-10-22: четяха се само първите
# 256 KB — .ipynb с една графика (~300 KB base64) и клетка `OpenAI(api_key=
# "sk-proj-…")` след нея заминаваше с ключа, а 360 KB .py с RSA ключ накрая —
# също. По-нагоре от тавана на проекта не се чете: такъв файл и без това
# спира export-а.
_SCAN_BYTES = 256 * 1024
_SCAN_OVERLAP = 4 * 1024


@functools.lru_cache(maxsize=1)
def _api_token_bytes() -> re.Pattern[bytes]:
    from genesis_agent.notifier import API_TOKEN_RE
    return re.compile(API_TOKEN_RE.pattern.encode())


# Образецът от notifier.py е за маскиране, където да хване повече не вреди;
# тук хванатото изпада от архива. Преглед 2026-10-11: README с
# `sk-your-api-key-here`, .env.example с `sk-your-key-here`, index.html и CSS
# със SpinKit класове (`sk-fading-circle`, `sk-circle12`) и Firebase web
# конфиг (`apiKey: "AIza…"`, публичен по замисъл) — всички задържани. Истински
# ключ е случаен низ: има цифра, `sk-` ключът има и главна буква и поне 20
# знака след `sk-`, и няма думи-образци.
_PLACEHOLDER_WORDS = (b"your", b"here", b"example", b"xxxx", b"placeholder", b"dummy",
                      b"changeme", b"replace", b"insert", b"redacted", b"fake")


def _real_key(tok: bytes, data: bytes, at: int) -> bool:
    low = tok.lower()
    if any(w in low for w in _PLACEHOLDER_WORDS) or not re.search(rb"[0-9]", tok):
        return False
    if tok.startswith(b"sk-"):
        return len(tok) >= 23 and re.search(rb"[A-Z]", tok) is not None
    if tok.startswith(b"AIza"):
        return not _firebase_web_key(data, at)
    return True


# Точните форми на ключовете по доставчик — втори път до горния, а не вместо
# него: файлът се задържа, ако който и да е от двата намери ключ. Одит
# 2026-10-11: горното правило иска цифра (и главна буква за `sk-`), а истински
# ключове без такива заминаваха към клиента — DeepSeek `sk-<32 hex>`,
# OpenRouter `sk-or-v1-<64 hex>` (доставчик на самия Genesis), Stripe
# `sk_live_`/`rk_live_`, Cerebras `csk-`, всеки Hugging Face `hf_` (34 букви,
# цифра няма), AWS `AKIA` без цифра. Формата е толкова тясна, че цифра не
# трябва; образците се познават по думите в тях.
_KEY_SHAPES = re.compile(
    rb"(?<![A-Za-z0-9_-])(?:"
    rb"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}"
    rb"|sk-ant-(?:api|admin)\d\d-[A-Za-z0-9_-]{80,}"
    rb"|sk-or-v1-[0-9a-f]{64}"
    rb"|sk-[A-Za-z0-9]{48}"
    rb"|sk-[0-9a-f]{32}"
    rb"|csk-[a-z0-9]{40,}"
    rb"|gsk_[A-Za-z0-9]{52}"
    rb"|AIza[0-9A-Za-z_-]{35}"
    rb"|gh[pousr]_[A-Za-z0-9]{36}"
    rb"|github_pat_[A-Za-z0-9_]{82}"
    rb"|[sr]k_live_[A-Za-z0-9]{24,}"
    rb"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    rb"|nvapi-[A-Za-z0-9_-]{64}"
    rb"|hf_[A-Za-z]{34}"
    # Одит 2026-10-22: GitLab `glpat-`, npm `npm_`, Telegram бот (`<id>:AA…`,
    # и в адреса `…/bot<id>:AA…` — Genesis сам има Telegram известия) и
    # SendGrid `SG.<22>.<43>` заминаваха; в .npmrc ги пазеше само името.
    rb"|glpat-[A-Za-z0-9_-]{20,}"
    rb"|npm_[A-Za-z0-9]{36}"
    # Две алтернативи, не `(?:bot)?`: незадължителната група удвояваше времето
    # на целия образец (0.18 → 0.36 s на 4 MB, 2026-10-22), а файлът вече се
    # чете цял.
    rb"|bot\d{8,10}:AA[A-Za-z0-9_-]{33}|\d{8,10}:AA[A-Za-z0-9_-]{33}"
    rb"|SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"
    rb")(?![A-Za-z0-9_-])")
# Образец: думи-образци навсякъде, `your`/`here`/`fake`/`key` като отделна част
# (`sk-your-api-key-here`), или 6 еднакви знака подред (`AIzaSyXXXXXX…`,
# `ghp_xxxxxx…`). В случаен ключ последното е ~1 на 10^4 и по-рядко.
_PLACEHOLDER_RE = re.compile(
    rb"(?i:example|placeholder|changeme|redacted|replace|insert|dummy)"
    rb"|(?i:(?:^|[-_])(?:your|here|fake|key)(?:[-_]|$))"
    rb"|(.)\1{5}")


def _shaped_key(m: re.Match[bytes], data: bytes) -> bool:
    tok = m.group(0)
    if _PLACEHOLDER_RE.search(tok):
        return False
    return not (tok.startswith(b"AIza") and _firebase_web_key(data, m.start()))


# Firebase web конфигът е публичен по замисъл, но изключението важи само за
# него, не за целия файл. Одит 2026-10-11: `authDomain` където и да е във
# файла (дори в коментар) пускаше всеки `AIza…` в него — и Gemini ключ до
# firebaseConfig заминаваше към клиента. Сега ключът е публичен, само ако е
# стойност на поле `apiKey` и най-близката обхващаща скоба — балансирана
# `{…}` или `(…)`, така че `${env}` и FlutterFire `FirebaseOptions(...)` не я
# объркват — съдържа поле на Firebase.
_FIREBASE_FIELD_RE = re.compile(rb"authDomain|messagingSenderId|firebaseapp\.com")
_APIKEY_FIELD_RE = re.compile(rb"""(?<![A-Za-z0-9_$])["']?apiKey["']?\s*[:=]\s*["'`]$""")


def _enclosing(data: bytes, at: int) -> bytes:
    """Текстът на най-близката незатворена `{…}`/`(…)` около позиция `at`."""
    depth, i = 0, at - 1
    while i >= 0:
        c = data[i]
        if c in b"})":
            depth += 1
        elif c in b"{(":
            if depth == 0:
                break
            depth -= 1
        i -= 1
    if i < 0:
        return b""
    depth, j = 0, at
    while j < len(data):
        c = data[j]
        if c in b"{(":
            depth += 1
        elif c in b"})":
            if depth == 0:
                break
            depth -= 1
        j += 1
    return data[i:j + 1]


def _firebase_web_key(data: bytes, at: int) -> bool:
    line_start = data.rfind(b"\n", 0, at) + 1
    return (_APIKEY_FIELD_RE.search(data[line_start:at]) is not None
            and _FIREBASE_FIELD_RE.search(_enclosing(data, at)) is not None)


def _find_key(data: bytes) -> re.Match[bytes] | None:
    """Първият истински ключ в `data` по който и да е от двата пътя."""
    found = [m for m in (
        next((m for m in _KEY_SHAPES.finditer(data) if _shaped_key(m, data)), None),
        next((m for m in _api_token_bytes().finditer(data)
              if _real_key(m.group(0), data, m.start())), None),
    ) if m is not None]
    return min(found, key=lambda m: m.start()) if found else None


# Последната част от името на променлива (флаг, параметър в адрес), която
# носи тайна — цели части (`DB_PASSWORD`, `GITHUB_TOKEN`, `APIKEY`), не
# поднизове: `PATH` и `COMPASS_URL` не са тайни. Ревю 2026-10-11: всяка част
# от името спираше целия .mcp.json — `GOOGLE_APPLICATION_CREDENTIALS` (път),
# `SSH_AUTH_SOCK`, `AUTH_MODE`, `TOKEN_URL`; затова само последната, а
# CREDENTIALS вън — вписан JSON ключ хваща _SECRET_CONTENT.
_SECRET_ENV_LAST = frozenset({"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASS", "PAT",
                              "DSN", "AUTH"})


def _secret_env_name(name: str) -> bool:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name.upper()) if p]
    return bool(parts) and (parts[-1] in _SECRET_ENV_LAST
                            or parts[-1].endswith(("KEY", "TOKEN", "SECRET")))


def _mcp_literal(value: object) -> str:
    """Каквото mcp_client ще прати буквално: `${VAR}` е препратка, но
    `${VAR:-тайна}` праща тайната, ако VAR липсва (mcp_client._expand) — ревю
    2026-10-11. Цялата стойност `$API_KEY` и самото `Bearer` не са тайни."""
    from genesis_agent.mcp_client import _VAR
    text = _VAR.sub(lambda m: m.group(2) or "", str(value or "")).strip()
    if re.fullmatch(r"\$[A-Z_][A-Z0-9_]*", text) or text.lower() in ("bearer", "basic", "token"):
        return ""
    return text


def _url_secret(text: str) -> bool:
    """Парола в адреса (`postgres://admin:S3cr3t@db`) или `?api_key=…`."""
    from urllib.parse import parse_qsl, urlsplit
    if "://" not in text:
        return False
    try:
        parts = urlsplit(text)
        password = parts.password
    except ValueError:
        return False
    return bool(password) or any(v and _secret_env_name(k) for k, v in parse_qsl(parts.query))


# Парола в адреса във ВСЕКИ файл, не само в .mcp.json. Одит 2026-10-22:
# `git+https://deploy:<парола>@gitlab…` в requirements.txt и `url =
# https://ci-bot:<парола>@git…` в .gitmodules заминаваха. Образците остават:
# препратка (`${DB_PASS}`, `<password>`), парола като името (`postgres:postgres`),
# думите от _URL_PLACEHOLDER_PW и думите-образци, под 6 знака (`u:p@host`).
_URL_USERINFO = re.compile(
    rb"\b[A-Za-z][A-Za-z0-9+.-]*://([^/\s:@'\"<>`]*):([^/\s@'\"<>`]+)@(?=[A-Za-z0-9\[])")
_URL_PLACEHOLDER_PW = frozenset({b"password", b"passwd", b"pass", b"pwd", b"secret", b"postgres",
                                 b"root", b"admin", b"user", b"test", b"guest", b"mysql", b"redis"})


def _url_passwords(data: bytes) -> Iterator[re.Match[bytes]]:
    """Адресите с истинска парола; group(2) е самата парола."""
    if b"://" not in data:
        return
    for m in _URL_USERINFO.finditer(data):
        user, pw = m.group(1), m.group(2)
        low = pw.lower()
        if (len(pw) < 6 or pw == user or low in _URL_PLACEHOLDER_PW
                or re.search(rb"[${}<>*]|%\(", pw) or _PLACEHOLDER_RE.search(pw)
                or any(w in low for w in _PLACEHOLDER_WORDS)):
            continue
        yield m


def _mcp_literal_secret(data: bytes) -> str | None:
    """`сървър.headers.Authorization` / `сървър.env.DB_PASSWORD` / `сървър.args`
    / `сървър.url`, ако .mcp.json на проекта носи самата тайна, а не `${ПРОМЕНЛИВА}`.

    E2e 2026-10-11: MCP сървърите на проекта (mcp_client) се описват в
    .mcp.json, а export го пращаше на клиента с `"Authorization": "Bearer
    9f8e…"` и `"DB_PASSWORD": "Tr0ub4dor&3xyz"` — ключовете нямат формата,
    която _find_key познава. Ревю 2026-10-11: и парола в DATABASE_URL, токен в
    args (`--token ghp_…`) и в адреса (`?api_key=…`) минаваха."""
    import json

    from genesis_agent.code_edit import decode_text
    from genesis_agent.mcp_client import _static_auth
    try:
        config = json.loads(decode_text(data)[0].lstrip("\ufeff"))
    except ValueError:
        return None
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return None
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        for field_name, is_secret in (("headers", lambda k: _static_auth({k: ""})),
                                      ("env", _secret_env_name)):
            table = server.get(field_name)
            if not isinstance(table, dict):
                continue
            for key, value in table.items():
                literal = _mcp_literal(value)
                if literal and (is_secret(str(key)) or _url_secret(literal)):
                    return f"{name}.{field_name}.{key}"
        if _url_secret(_mcp_literal(server.get("url"))):
            return f"{name}.url"
        args = server.get("args")
        args = [str(a) for a in args] if isinstance(args, list) else []
        for i, arg in enumerate(args):
            # `--token X` и `--token=X`; `--token-file X` не е тайна.
            flag, eq, value = arg.partition("=")
            if not eq:
                value = args[i + 1] if i + 1 < len(args) else ""
                value = "" if value.startswith("-") else value
            if (flag.startswith("-") and _secret_env_name(flag) and _mcp_literal(value)) \
                    or _url_secret(_mcp_literal(arg)):
                return f"{name}.args"
    return None


def _secret_file(p: Path, rel: str, *, whole: bool = True) -> str | None:
    """None — файлът може да замине; иначе как да се впише в „извън архива“.
    `whole=False` — само първите _SCAN_BYTES (остатъкът — в _scan_whole)."""
    from genesis_agent import sandbox
    if sandbox.sensitive_path_reason(rel) or sandbox.sensitive_path_reason(p.name):
        return rel
    if _EXPORT_SECRETS.search(rel):
        return rel
    is_mcp = p.name.lower() in (".mcp.json", "mcp.json")
    try:
        with p.open("rb") as f:
            for i, (lines, data) in enumerate(_chunks(f, whole)):
                if _SECRET_CONTENT.search(data):
                    return rel
                if is_mcp and i == 0:
                    where = _mcp_literal_secret(data)
                    if where:
                        return f"{rel} (вписан ключ в {where} — сложи ${{ПРОМЕНЛИВА}} вместо него)"
                # API ключ, вписан направо в кода или конфига (`GROQ_API_KEY =
                # "gsk_…"`, `ghp_…` в settings.json, `AKIA…` в aws.cfg). Одит
                # 2026-10-11: и трите заминаваха към клиента — пазеше се само блок
                # PRIVATE KEY. Казва се къде е ключът (без самия него), за да го
                # изнесе човекът в .env.
                m = _find_key(data)
                if m:
                    line = lines + data.count(b"\n", 0, m.start()) + 1
                    return (f"{rel} (съдържа ключ {m.group(0)[:4].decode('ascii', 'replace')}… "
                            f"на ред {line})")
                u = next(_url_passwords(data), None)
                if u:
                    line = lines + data.count(b"\n", 0, u.start()) + 1
                    return f"{rel} (парола в адрес на ред {line})"
    except OSError:
        return None
    return None


def _chunks(f: BinaryIO, whole: bool = True) -> Iterator[tuple[int, bytes]]:
    """(редове преди парчето, парче) за целия файл — виж _SCAN_BYTES;
    `whole=False` — само първото парче."""
    lines = 0
    chunk = f.read(_SCAN_BYTES)
    total = len(chunk)
    while chunk:
        yield lines, chunk
        more = (f.read(_SCAN_BYTES) if whole and total < _MAX_INPUT_MB * 1024 * 1024
                else b"")
        if not more:
            return
        total += len(more)
        cut = max(len(chunk) - _SCAN_OVERLAP, 0)
        lines += chunk.count(b"\n", 0, cut)
        chunk = chunk[cut:] + more


_RUN_HEADINGS = ("how to run", "running", "usage", "quick start", "quickstart",
                 "getting started", "install", "как се пуска", "пускане",
                 "стартиране", "употреба", "инсталация", "как да")
_ASSUME_HEADINGS = ("assumption", "допускания", "допускане", "предположения")


@dataclass
class Delivery:
    zip_path: Path
    report: str
    files: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    tests_passed: bool | None = None


# ── Files ────────────────────────────────────────────────────────────────────

_IGNORED = "игнориран от .gitignore"


def _git_ls(root: Path) -> list[str] | None:
    """`git ls-files -co --exclude-standard` в `root`; None — git отказа или липсва.
    `core.fsmonitor=false`: вложеното хранилище е писано от модела, а
    fsmonitor в неговия .git/config е команда, която git би пуснал."""
    try:
        proc = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root),
                               "ls-files", "-co", "--exclude-standard", "-z"],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [e for e in proc.stdout.decode("utf-8", "replace").split("\0") if e]


# Колко нива вложени хранилища се обхождат (submodule в submodule…).
_NESTED_REPO_DEPTH = 8


def _git_kept(root: Path) -> tuple[set[str], tuple[str, ...]] | None:
    """Файловете, които git би запазил (следени + неследени неигнорирани),
    спрямо `root`, и папките, приети цели. None — проектът не е в git или
    git липсва."""
    entries = _git_ls(root)
    if not entries:
        # Папка, която обхващащо хранилище игнорира цялата (dotfiles репо в
        # $HOME с `*` в .gitignore): git не казва нищо за проекта — тогава
        # решава собственият му .gitignore, а не „всичко е игнорирано“.
        return None
    return _kept_from(root, entries, 0)


def _kept_from(root: Path, entries: list[str], depth: int) -> tuple[set[str], tuple[str, ...]]:
    # Вложено хранилище git изрежда като папка: комитнато (gitlink/submodule)
    # — `lib`, неследено — `sub/`. Преглед 2026-10-11: то не влизаше в
    # приетите папки и цялото изпадаше като „игнорирано“. Одит 2026-10-11:
    # прието цяло, то носеше и игнорираните си server.log и db.sqlite3 — затова
    # вътре решава собственият му git. Цяло остава само ако git там откаже
    # (None); празен списък значи „нищо за пазене“, не „пази всичко“.
    files = {e for e in entries if not e.endswith("/") and not (root / e).is_dir()}
    whole: list[str] = []
    for entry in sorted(set(entries) - files):
        d = entry.rstrip("/")
        inner = (_git_ls(root / d) if depth < _NESTED_REPO_DEPTH and (root / d / ".git").exists()
                 else None)
        if inner is None:
            whole.append(d + "/")
            continue
        sub_files, sub_whole = _kept_from(root / d, inner, depth + 1)
        files.update(f"{d}/{f}" for f in sub_files)
        whole.extend(f"{d}/{w}" for w in sub_whole)
    return files, tuple(whole)


def _gitignore_rules(root: Path) -> list[tuple[str, bool, bool]]:
    """Минимален прочит на `.gitignore` в корена за проект без git:
    (образец, само папка, закотвен). Отрицания (`!`) не се поддържат —
    така се задържа повече, не по-малко."""
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules = []
    for line in text.splitlines():
        pat = line.strip()
        if not pat or pat.startswith(("#", "!")):
            continue
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        anchored = "/" in pat
        pat = pat.lstrip("/")
        if pat:
            rules.append((pat, dir_only, anchored))
    return rules


def _matches_gitignore(rules: list[tuple[str, bool, bool]], rel: str, is_dir: bool) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pat, dir_only, anchored in rules:
        if dir_only and not is_dir:
            continue
        # `**/` в git пасва и на нула папки (`**/*.log` хваща и `app.log` в
        # корена, `a/**/b` — и `a/b`); fnmatch иска поне една `/`. Преглед
        # 2026-10-11: логът в корена оставаше в архива без git.
        pats = {pat, pat.replace("/**/", "/")}
        if pat.startswith("**/"):
            pats.add(pat[3:])
        if any(fnmatch.fnmatch(rel if anchored else name, p) for p in pats):
            return True
    return False


def project_files(root: Path, *, exclude: Path | None = None) -> tuple[list[Path], list[str]]:
    """(files to ship, secret files withheld) — both relative-sortable."""
    keep: list[Path] = []
    withheld: list[str] = []
    # Каквото проектът сам не пуска в git — локалната база, логовете, качените
    # от потребителите файлове, .coverage — не е за клиента. Одит 2026-10-11:
    # db.sqlite3 с ЕГН, server.log с токени и uploads/passport_scan.jpg
    # влизаха в архива, а отчетът мълчеше за тях.
    git = _git_kept(root)
    rules = [] if git is not None else _gitignore_rules(root)
    kept_dirs: set[str] = set()
    if git is not None:
        # И родителите на приетите цели папки: submodule в `vendor/u` иначе
        # изпадаше цял като „vendor/ (игнориран)“ (одит 2026-10-11).
        for f in (*git[0], *git[1]):
            parts = f.rstrip("/").split("/")[:-1]
            kept_dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))

    def ignored(rel: str, is_dir: bool) -> bool:
        if git is None:
            return bool(rules) and _matches_gitignore(rules, rel, is_dir)
        if any(rel.startswith(d) or rel + "/" == d for d in git[1]):
            return False
        return rel not in (kept_dirs if is_dir else git[0])

    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        kept_dirs_here = []
        for d in sorted(dirnames):
            if _skip_dir(Path(dirpath), d, depth):
                continue
            rel_d = (Path(dirpath) / d).relative_to(root).as_posix()
            if ignored(rel_d, True):
                with contextlib.suppress(OSError):
                    if any(os.scandir(Path(dirpath) / d)):
                        withheld.append(f"{rel_d}/ ({_IGNORED})")
                continue
            if (Path(dirpath) / d).is_symlink():
                withheld.append(f"{(Path(dirpath) / d).relative_to(root).as_posix()}/ "
                                "(символна връзка — не е включена)")
                continue
            kept_dirs_here.append(d)
        dirnames[:] = kept_dirs_here
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name in _SKIP_NAMES or p.suffix in _SKIP_SUFFIXES:
                continue
            if exclude is not None and p.resolve() == exclude:
                continue
            rel = p.relative_to(root).as_posix()
            # Казва се какво липсва: връзките и имената, които не са UTF-8,
            # изпадаха без дума, а едно такова име сриваше целия export.
            if p.is_symlink():
                withheld.append(f"{rel} (символна връзка — не е включена)")
                continue
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError:
                withheld.append(f"{rel.encode('utf-8', 'replace').decode()} (името не е UTF-8)")
                continue
            if name == REPORT_NAME and p.parent == root:
                continue  # regenerated below; an old one would be stale
            secret = _secret_file(p, rel, whole=False)
            if secret:
                withheld.append(secret)
                continue
            if ignored(rel, False):
                withheld.append(f"{rel} ({_IGNORED})")
                continue
            keep.append(p)
    return _scan_whole(root, keep, withheld)


def _scan_whole(root: Path, keep: list[Path], withheld: list[str]) -> tuple[list[Path], list[str]]:
    """Остатъкът след първите _SCAN_BYTES — само на файловете, които заминават.
    Ревю 2026-10-22: четено до края още в обхождането, игнорираното (и без
    това извън архива) правеше project_files 12.4 s вместо 0.05 s при 60 MB
    db.sqlite3 и 40 MB server.log, а проект с 3 × 100 MB .bin чакаше отказа
    за тавана 32 s (сега 0.14 s) — над тавана не се чете нищо повече."""
    sizes: dict[Path, int] = {}
    for p in keep:
        with contextlib.suppress(OSError):
            sizes[p] = p.stat().st_size
    if sum(sizes.values()) > _MAX_INPUT_MB * 1024 * 1024:
        return keep, withheld          # export отказва
    out: list[Path] = []
    for p in keep:
        if sizes.get(p, 0) > _SCAN_BYTES:
            secret = _secret_file(p, p.relative_to(root).as_posix())
            if secret:
                withheld.append(secret)
                continue
        out.append(p)
    return out, withheld


# ── README ───────────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")   # същото като в mentions.py


def readme_sections(root: Path) -> dict[str, str]:
    """Heading (lowercased) → body, for the project's README."""
    for name in ("README.md", "README.MD", "Readme.md", "readme.md", "README.txt", "README"):
        p = root / name
        if p.is_file():
            try:
                text = read_text_any(p)
            except OSError:
                return {}
            break
    else:
        return {}
    lines = text.splitlines()
    # `# install deps` в ```bash блок не е заглавие (одит 2026-10-11): иначе
    # „Как се пуска“ в отчета ставаше само „```bash“ — незатворена ограда,
    # която поглъщаше останалото, а самите команди липсваха.
    heads: list[tuple[int, int, str]] = []
    fenced = False
    for i, ln in enumerate(lines):
        if _FENCE.match(ln):
            fenced = not fenced
            continue
        m = None if fenced else _HEADING.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip().lower()))
    out: dict[str, str] = {}
    for k, (i, level, title) in enumerate(heads):
        # Тялото стига до следващото заглавие на същото или по-високо ниво —
        # подзаглавията („### Windows“ под „## Как се пуска“) са част от него.
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        out.setdefault(title, "\n".join(lines[i + 1:end]).strip())
    return out


def _section(sections: dict[str, str], keys: tuple[str, ...]) -> str:
    for heading, body in sections.items():
        if body and any(k in heading for k in keys):
            return body
    return ""


# ── How to run, when the README does not say ─────────────────────────────────

def _guess_run(root: Path, files: list[str]) -> list[str]:
    steps: list[str] = []
    names = set(files)
    if "requirements.txt" in names:
        steps.append("pip install -r requirements.txt")
    elif "pyproject.toml" in names:
        steps.append("pip install -e .")
    if "package.json" in names:
        steps.append("npm install")
        try:
            import json
            scripts = json.loads(read_text_any(root / "package.json")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        for s in ("start", "dev"):
            if s in scripts:
                steps.append(f"npm run {s}" if s != "start" else "npm start")
                break
    for entry in ("main.py", "app.py", "cli.py", "manage.py", "server.py"):
        if entry in names:
            steps.append(f"python {entry}" + (" runserver" if entry == "manage.py" else ""))
            break
    else:
        top = [f for f in files if f.count("/") == 1]
        mains = sorted(f.split("/")[0] for f in top if f.endswith("/__main__.py"))
        clis = sorted(f[:-3].replace("/", ".") for f in top if f.endswith("/cli.py")
                      and "__main__" in _read(root / f))
        if mains:
            steps.append(f"python -m {mains[0]}")
        elif clis:
            steps.append(f"python -m {clis[0]}")
    if "index.html" in names and not steps:
        steps.append("отвори index.html в браузър")
    return steps


def _read(p: Path) -> str:
    try:
        return read_text_any(p)
    except OSError:
        return ""


def _dependencies(root: Path, files: list[str]) -> list[str]:
    names = set(files)
    if "requirements.txt" in names:
        try:
            lines = read_text_any(root / "requirements.txt").splitlines()
        except OSError:
            return []
        return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", "-"))]
    if "pyproject.toml" in names:
        try:
            import tomllib  # type: ignore[import-not-found]  # 3.11+
        except ImportError:
            return []
        try:
            data = tomllib.loads(read_text_any(root / "pyproject.toml"))
        except (OSError, ValueError):
            return []
        return [str(d) for d in data.get("project", {}).get("dependencies", [])]
    if "package.json" in names:
        try:
            import json
            data = json.loads(read_text_any(root / "package.json"))
        except (OSError, ValueError):
            return []
        deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
        return [f"{k} {v}" for k, v in deps.items()]
    return []


# ── Tests ────────────────────────────────────────────────────────────────────

_SUMMARY = re.compile(r"\b\d+ (passed|failed|error|errors)\b.*")


def _portable(command: str) -> str:
    """`/home/x/.venv/bin/python -m pytest` → `python -m pytest`: пътят до
    интерпретатора е на тази машина, получателят има свой."""
    return re.sub(r'^("[^"]*[/\\]python[\d.]*(\.exe)?"|[^\s"]*[/\\]python[\d.]*(\.exe)?)(?=\s)',
                  "python", command, flags=re.IGNORECASE)


def _test_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        m = _SUMMARY.search(line)
        if m:
            return line.strip(" =")
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return lines[-1].strip()[:200] if lines else ""


# ── Work memory of this folder ───────────────────────────────────────────────

def _memory(root: Path) -> tuple[list[dict], list[dict]]:
    """(decisions, open threads) recorded for exactly this folder."""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception:
        log.debug("workspace_memory не се внася — без памет в описанието", exc_info=True)
        return [], []
    previous = wm._workspace
    try:
        wm.set_workspace(root)
        decisions = wm.list_decisions(15)
        threads = wm.list_threads("open", 15) + wm.list_threads("blocked", 15)
    except Exception:
        log.debug("паметта за папката не се прочете", exc_info=True)
        return [], []
    finally:
        wm._workspace = previous
    # Отчетът заминава при клиента: ключ, записан с REMEMBER преди
    # маскирането при запис (2026-10-22), влизаше дословно в GENESIS_REPORT.md.
    clean = [{k: wm.mask_secrets(v) if isinstance(v, str) else v for k, v in row.items()}
             for row in decisions + threads]
    return clean[:len(decisions)], clean[len(decisions):]


# ── Тайни в отчета ───────────────────────────────────────────────────────────
# Отчетът е единственият файл, който е СИГУРНО в архива, а носи чужд текст:
# изхода на падащите тестове, разделите от README-то, решенията от паметта.
# Одит 2026-10-22: pytest показа ключа от задържания .env в assert-а
# (`'sk-proj-…'.startswith`), а README, задържан заради ключа си, го даде в
# „Как се пуска“. Затова целият отчет минава през същите проверки, по които
# файл се задържа, плюс стойностите от задържаните файлове с тайни — парола
# без позната форма (`DB_PASSWORD=…` в .env) pytest показва също толкова лесно.
# Не notifier.redact: неговото `ИМЕ: стойност` прави `KeyError: 'x'` и
# `token == x` в изхода на тестовете на `KeyError=***`, `token=***`.
_PRIVATE_BLOCK = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:\s|\\n)+[A-Za-z0-9+/=]{16,})*"
    rb"(?:(?:\s|\\n)*-----END [A-Z ]*PRIVATE KEY-----)?")
_ENV_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:]\s*(.+?)\s*$")


def _withheld_values(root: Path, withheld: list[str]) -> set[str]:
    """Стойностите на `ИМЕ=стойност` с тайно име в задържаните по име файлове
    (`.env`, `secrets.toml`, …)."""
    from genesis_agent import sandbox
    out: set[str] = set()
    for entry in withheld:
        rel = entry.split(" (", 1)[0]
        p = root / rel
        if not (sandbox.sensitive_path_reason(rel) or sandbox.sensitive_path_reason(p.name)
                or _EXPORT_SECRETS.search(rel)):
            continue
        try:
            if p.is_symlink() or not p.is_file() or p.stat().st_size > _SCAN_BYTES:
                continue
            text = read_text_any(p)
        except OSError:
            continue
        for line in text.splitlines():
            m = _ENV_ASSIGN.match(line)
            if not m or not _secret_env_name(m.group(1)):
                continue
            raw = m.group(2)
            if raw[:1] in ("'", '"') and raw[0] in raw[1:]:
                value = raw[1:raw.index(raw[0], 1)]
            else:
                value = raw.split(" #", 1)[0].strip()
            if len(value) >= 6 and not value.startswith("$"):
                out.add(value)
    return out


def _scrub(text: str, values: set[str] | None = None) -> str:
    """`text` с `***` на мястото на всеки ключ, парола в адрес, частен ключ и
    стойност от `values`."""
    for v in sorted(values or (), key=len, reverse=True):
        text = text.replace(v, "***")
    data = text.encode("utf-8", "surrogatepass")
    spans = [m.span() for m in _KEY_SHAPES.finditer(data) if _shaped_key(m, data)]
    spans += [m.span() for m in _api_token_bytes().finditer(data)
              if _real_key(m.group(0), data, m.start())]
    spans += [m.span(2) for m in _url_passwords(data)]
    spans += [m.span() for m in _PRIVATE_BLOCK.finditer(data)]
    if not spans:
        return text
    out: list[bytes] = []
    pos = 0
    for start, end in sorted(spans):
        if start < pos:
            pos = max(pos, end)
            continue
        out += [data[pos:start], b"***"]
        pos = end
    out.append(data[pos:])
    return b"".join(out).decode("utf-8", "surrogatepass")


# ── The report ───────────────────────────────────────────────────────────────

def _size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    n /= 1024
    return f"{n:.1f} KB" if n < 1024 else f"{n / 1024:.1f} MB"


def build_report(root: Path, files: list[Path], withheld: list[str], *,
                 run_tests: bool = True) -> tuple[str, bool | None]:
    """Markdown report and whether the tests passed (None: not run / none)."""
    from genesis_agent.repo_map import detect_project

    rel = [f.relative_to(root).as_posix() for f in files]
    info = detect_project(root)
    secrets = _withheld_values(root, withheld)
    sections = readme_sections(root)
    lines = [f"# {root.name} — отчет за предаване",
             "",
             (f"Изготвен от Genesis Agent на {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
              "Всичко по-долу е взето от самия проект в този момент — файловете, README-то, "
              "истинско пускане на тестовете — не от разговора."),
             ""]

    lines += ["## Какво има", ""]
    total = 0
    code_lines = 0
    for f, r in zip(files, rel):
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        total += size
        if f.suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".go", ".rs", ".java", ".rb", ".php"):
            try:
                code_lines += f.read_text(encoding="utf-8", errors="replace").count("\n")
            except OSError:
                pass
    lines.append(f"{len(rel)} файла, {_size(total)}, ~{code_lines} реда код. "
                 f"Език: {info.language}.")
    lines.append("")
    shown = rel[:60]
    lines += [f"- `{r}`" for r in shown]
    if len(rel) > len(shown):
        lines.append(f"- … и още {len(rel) - len(shown)}")
    lines.append("")

    lines += ["## Как се пуска", ""]
    how = _section(sections, _RUN_HEADINGS)
    if how:
        lines += ["(от README-то на проекта)", "", how, ""]
    else:
        guess = _guess_run(root, rel)
        if guess:
            lines += ["README-то не го казва; по файловете на проекта:", "", "```"]
            lines += guess + ["```", ""]
        else:
            lines += ["Нито README-то, нито файловете казват как — **липсва**.", ""]
    deps = _dependencies(root, rel)
    if deps:
        lines += ["Зависимости: " + ", ".join(f"`{d}`" for d in deps[:30]), ""]
    try:
        from genesis_agent.py_deps import undeclared
        missing = undeclared(root)
    except Exception:
        log.debug("проверката за незаписани зависимости се провали", exc_info=True)
        missing = []
    if missing:
        lines += [("⚠️ Кодът внася пакети, които не са записани в requirements.txt / "
                   "pyproject.toml — на друга машина няма да тръгне без тях:"), ""]
        lines += [f"- `{pip}` (`import {mod}` в {', '.join(files[:3])})" for mod, pip, files in missing]
        lines.append("")

    lines += ["## Тестове", ""]
    passed: bool | None = None
    if not info.test_command:
        lines += ["Проектът няма тестове, които да се пуснат.", ""]
    elif not run_tests:
        lines += [f"Не са пускани при този експорт. Команда: `{_portable(info.test_command)}`", ""]
    else:
        from genesis_agent.repo_agent import run_tests as _run
        res = _run(root, info.test_command)
        # Изходът се чисти ПРЕДИ да се отреже: ревю 2026-10-22 — срязът на
        # [-1500:] насред паролата оставяше `ub4dor-3xyz-Qq` от
        # `Tr0ub4dor-3xyz-Qq`, а такъв къс вече не пасва на нищо.
        output = _scrub(res.output, secrets)
        if not res.ran:
            lines += [f"Не можаха да се пуснат: {output[:300]}", ""]
        else:
            passed = res.passed
            verdict = "✅ минават" if res.passed else "❌ падат"
            lines += [f"{verdict} — `{_test_summary(output)}`",
                      "", f"Команда: `{_portable(info.test_command)}`", ""]
            if not res.passed:
                lines += ["```", output[-1500:], "```", ""]

    assumptions = _section(sections, _ASSUME_HEADINGS)
    decisions, threads = _memory(root)
    lines += ["## Допускания и решения", ""]
    if assumptions:
        lines += ["(от README-то)", "", assumptions, ""]
    if decisions:
        for d in decisions:
            why = f" — {d['why']}" if d.get("why") else ""
            lines.append(f"- {d['what']}{why}")
        lines.append("")
    if not assumptions and not decisions:
        lines += ["Не са записани нито в README-то, нито в паметта на работата.", ""]

    if threads:
        lines += ["## Недовършено", ""]
        for t in threads:
            nxt = f" → следва: {t['next_step']}" if t.get("next_step") else ""
            lines.append(f"- [{t['status']}] {t['title']}{nxt}")
        lines.append("")

    if withheld:
        lines += ["## Нарочно извън архива", "",
                  ("Файлове с ключове или тайни (получателят слага своите), игнорираното "
                   "от .gitignore (локални данни, логове, качени файлове), символни връзки "
                   "и имена, които не стават в zip:"), ""]
        lines += [f"- `{w}`" for w in withheld]
        lines.append("")
    report = _scrub("\n".join(lines), secrets)
    return report.rstrip() + "\n", passed


# ── Export ───────────────────────────────────────────────────────────────────

def export(project: str | Path, out: str | Path | None = None, *,
           run_tests: bool = True) -> Delivery:
    """Zip `project` with GENESIS_REPORT.md at its root. Raises ValueError
    with a human sentence when there is nothing sensible to export."""
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Няма такава папка: {root}")
    default_name = f"{root.name}-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    zip_path = (Path(out).expanduser() if out else root.parent / default_name).resolve()
    # Папка за цел (`/export ~/Desktop/`) — архивът влиза в нея. Беше
    # ~/Desktop.zip до папката (одит 2026-10-22).
    if out and (zip_path.is_dir() or str(out).endswith(("/", os.sep))):
        zip_path = zip_path / default_name
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_suffix(zip_path.suffix + ".zip")
    files, withheld = project_files(root, exclude=zip_path)
    if not files:
        raise ValueError(f"В {root} няма файлове за предаване.")
    total = sum(f.stat().st_size for f in files if f.exists())
    if total > _MAX_INPUT_MB * 1024 * 1024:
        raise ValueError(f"Проектът е {total // (1024 * 1024)} MB (таван {_MAX_INPUT_MB} MB) — "
                         "това не е предаване, а образ на диска. Изчисти данните/артефактите.")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".part")
    # Файловете влизат в архива ПРЕДИ тестовете: тест, който пипа или трие файл,
    # иначе сриваше export-а (оставяйки .part) или пращаше състоянието след
    # теста (одит 2026-10-09). Докладът се добавя след тях.
    try:
        shipped: list[Path] = []
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                try:
                    z.write(f, f"{root.name}/{f.relative_to(root).as_posix()}")
                except FileNotFoundError:
                    continue
                shipped.append(f)
        files = shipped
        report, passed = build_report(root, files, withheld, run_tests=run_tests)
        with zipfile.ZipFile(tmp, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{root.name}/{REPORT_NAME}", report)
        tmp.replace(zip_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Delivery(zip_path, report, [f.relative_to(root).as_posix() for f in files],
                    withheld, passed)


def summary(d: Delivery) -> str:
    """The few lines the terminal prints."""
    tests = {True: "тестовете минават ✅", False: "тестовете ПАДАТ ❌",
             None: "без пуснати тестове"}[d.tests_passed]
    out = [f"📦 {d.zip_path}", f"   {len(d.files)} файла + {REPORT_NAME}, {tests}"]
    if d.withheld:
        out.append(f"   🔒 извън архива: {', '.join(d.withheld[:5])}"
                   + (" …" if len(d.withheld) > 5 else ""))
    return "\n".join(out)


_USAGE = """Употреба:
  genesis export [път-до-проекта] [-o изход.zip] [--no-tests]

Zip на проекта + GENESIS_REPORT.md: файловете, как се пуска, зависимостите,
резултатът от тестовете (пускат се сега), допусканията и недовършеното.
Ключове и тайни (.env, *.pem, …) остават извън архива.
По подразбиране: текущата папка → <папка>-<дата>.zip до нея.
Изходен код 1, ако тестовете падат (архивът пак се прави)."""


def main(args: list[str]) -> int:
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    project, out, run = ".", None, True
    i = 0
    positional: list[str] = []
    while i < len(args):
        a = args[i]
        if a in ("-o", "--out") and i + 1 < len(args):
            i += 1
            out = args[i]
        elif a == "--no-tests":
            run = False
        elif a.startswith("-"):
            print(f"Непозната опция: {a}\n\n{_USAGE}")
            return 2
        else:
            positional.append(a)
        i += 1
    if len(positional) > 1:
        print(_USAGE)
        return 2
    if positional:
        project = positional[0]
    try:
        d = export(project, out, run_tests=run)
    except (ValueError, OSError) as e:
        print(f"❌ {e}")
        return 1
    print(summary(d))
    return 0 if d.tests_passed is not False else 1
