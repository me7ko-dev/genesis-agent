"""Етап 2: вход, уеб чат и сваляне на файловете от задачата.

    python -m cloud.web.server add-user ivan@example.com        # отпечатва парола
    python -m cloud.web.server serve --data /srv/genesis/web --jobs /srv/jobs \\
        --env-file /srv/genesis/keys.env                          # 127.0.0.1:8080

Всяко съобщение в разговор е нов контейнер (cloud/runner/launch.py) върху
СЪЩАТА папка — файловете остават, историята е в /work/.genesis (task.py).
Само стандартната библиотека: услугата стои зад Cloudflare/Caddy с HTTPS, тук
е само приложението.

Какво пази:
  - вход с имейл и парола (scrypt); бисквитка HttpOnly + SameSite=Strict (+ Secure);
  - POST иска JSON и Origin от същия хост — чужда страница не може да праща
    от името на влязъл човек;
  - 5 грешни пароли за 15 мин → 429 (на имейл и на IP);
  - разговорите и файловете на друг човек изглеждат като липсващи (404);
  - по един ход на човек, общо най-много `--workers` контейнера наведнъж;
  - файловете се сваля само като прикачени (никога не се показват в нашия
    произход), без символни връзки (кодът в контейнера е чужд) и само докато
    в разговора не върви ход.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cloud.runner import launch
from cloud.web.store import SESSION_SECONDS, Store

STATIC = Path(__file__).with_name("static")
COOKIE = "genesis_session"
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
MAX_FILES = 1000
MAX_ZIP_BYTES = 200 * 1024 * 1024
LOGIN_WINDOW = 15 * 60
LOGIN_FAILURES = 5
HIDDEN = ".genesis"          # историята на разговора (task.py) — не е за сваляне

Runner = Callable[..., launch.Result]

_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class App:
    """Всичко без HTTP: може да се тества и да се пусне с фалшив runner."""

    def __init__(self, store: Store, jobs_dir: Path, *, runner: Runner = launch.run_task,
                 workers: int = 2, limits: launch.Limits | None = None,
                 env_file: str | None = None, runtime: str | None = None,
                 secure_cookie: bool = True, trust_proxy: bool = False,
                 gateway: launch.Gateway | None = None) -> None:
        self.store = store
        self.jobs_dir = jobs_dir
        self.runner = runner
        self.limits = limits or launch.Limits()
        self.env_file = env_file
        self.runtime = runtime
        self.secure_cookie = secure_cookie
        self.trust_proxy = trust_proxy
        self.gateway = gateway
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="turn")
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        store.fail_unfinished("сървърът беше рестартиран по време на задачата")

    # ── вход ────────────────────────────────────────────────────────────────
    def _recent_failures(self, key: str) -> list[float]:
        now = time.time()
        with self._lock:
            kept = [t for t in self._failures.get(key, []) if now - t < LOGIN_WINDOW]
            self._failures[key] = kept
            return kept

    def login_blocked(self, email: str, ip: str) -> bool:
        return any(len(self._recent_failures(k)) >= LOGIN_FAILURES
                   for k in (f"e:{email.lower()}", f"ip:{ip}"))

    def login(self, email: str, password: str, ip: str) -> str | None:
        user_id = self.store.check_password(email, password)
        if user_id is None:
            with self._lock:
                for k in (f"e:{email.lower()}", f"ip:{ip}"):
                    self._failures.setdefault(k, []).append(time.time())
            return None
        with self._lock:
            self._failures.pop(f"e:{email.lower()}", None)
        return self.store.new_session(user_id)

    # ── ходове ──────────────────────────────────────────────────────────────
    def workspace(self, user_id: int, chat_id: str) -> Path:
        return self.jobs_dir / str(user_id) / chat_id

    def start_turn(self, user: dict[str, Any], chat_id: str, text: str) -> int | None:
        """Нов ход, или None ако човекът вече чака отговор (по един на човек)."""
        with self._lock:
            if self.store.busy(user_id=user["id"]):
                return None
            turn_id = self.store.add_turn(chat_id, text)
        self.pool.submit(self._run, turn_id, text, self.workspace(user["id"], chat_id))
        return turn_id

    def _run(self, turn_id: int, text: str, workspace: Path) -> None:
        self.store.set_status(turn_id, "running")
        try:
            res = self.runner(text, workspace, limits=self.limits,
                              on_event=lambda e: self.store.add_event(turn_id, e),
                              env_file=self.env_file, runtime=self.runtime,
                              gateway=self.gateway)
            self.store.finish_turn(turn_id, ok=res.ok, error=res.error,
                                   tokens=res.tokens, seconds=res.seconds)
        except Exception as e:
            self.store.finish_turn(turn_id, ok=False, error=f"{type(e).__name__}: {e}",
                                   tokens={}, seconds=0.0)


# ── файлове: кодът в контейнера е чужд, всичко от /work е недоверено ────────

def list_files(workspace: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not workspace.is_dir():
        return out
    for root, dirs, files in os.walk(workspace, followlinks=False):
        rel_root = Path(root).relative_to(workspace)
        dirs[:] = sorted(d for d in dirs if not (rel_root == Path(".") and d == HIDDEN)
                         and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            p = Path(root) / name
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue  # символна връзка, fifo, устройство — не се дава
            out.append({"path": (rel_root / name).as_posix(), "size": st.st_size})
            if len(out) >= MAX_FILES:
                return out
    return out


def safe_file(workspace: Path, rel: str) -> Path | None:
    """Пътят до обикновен файл вътре в папката, или None. Без `..`, без
    абсолютни пътища, без символна връзка по пътя, без скритата история."""
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")  # не PurePosixPath: той мълчаливо маха "." и "//"
    if parts[0] == HIDDEN or any(p in ("", ".", "..") for p in parts):
        return None
    p = workspace
    for part in parts:
        p = p / part
        try:
            if stat.S_ISLNK(p.lstat().st_mode):
                return None
        except OSError:
            return None
    if not stat.S_ISREG(p.lstat().st_mode):
        return None
    if not p.resolve().is_relative_to(workspace.resolve()):
        return None
    return p


def read_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def zip_files(workspace: Path) -> bytes | None:
    """Всички файлове в един zip, или None ако са над тавана."""
    files = list_files(workspace)
    if sum(f["size"] for f in files) > MAX_ZIP_BYTES:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            p = safe_file(workspace, f["path"])
            if p is not None:
                z.writestr(f["path"], read_file(p))
    return buf.getvalue()


# ── HTTP ─────────────────────────────────────────────────────────────────────

_CHAT = re.compile(r"^/api/chats/([0-9a-f]{32})$")
_TURNS = re.compile(r"^/api/chats/([0-9a-f]{32})/turns$")
_FILES = re.compile(r"^/api/chats/([0-9a-f]{32})/files$")
_ZIP = re.compile(r"^/api/chats/([0-9a-f]{32})/files\.zip$")
_FILE = re.compile(r"^/api/chats/([0-9a-f]{32})/files/(.+)$")
_EVENTS = re.compile(r"^/api/turns/(\d+)/events$")


class Handler(BaseHTTPRequestHandler):
    server_version = "genesis-web"
    sys_version = ""
    app: App  # закача се от make_server

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} {format % args}\n")

    # ── помощни ──
    def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**_SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _ip(self) -> str:
        if self.app.trust_proxy:
            fwd = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else ""

    def _user(self) -> dict[str, Any] | None:
        return self.app.store.session_user(self._token())

    def _cookie(self, value: str, max_age: int) -> str:
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.app.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def _body(self) -> dict[str, Any] | None:
        """JSON тялото на POST, или None (и отговорът вече е пратен)."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(415, "очаква се JSON")
            return None
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self._error(403, "чужд произход")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._error(413, "твърде голяма заявка")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._error(400, "невалиден JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "очаква се обект")
            return None
        return data

    # ── GET ──
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html", "/app.js", "/app.css"):
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            self._send(200, f.read_bytes(), _STATIC_TYPES[f.suffix])
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        user = self._user()
        if user is None:
            self._error(401, "нужен е вход")
            return
        store = self.app.store
        if path == "/api/me":
            self._json(200, {"email": user["email"]})
        elif path == "/api/chats":
            self._json(200, {"chats": store.chats(user["id"])})
        elif m := _CHAT.match(path):
            chat = store.chat(user["id"], m[1])
            if chat is None:
                self._error(404, "няма такъв разговор")
                return
            turns = store.turns(chat["id"])
            for t in turns:
                t["events"] = store.events(t["id"])
            self._json(200, {**chat, "turns": turns})
        elif m := _EVENTS.match(path):
            turn = store.turn(user["id"], int(m[1]))
            if turn is None:
                self._error(404, "няма такъв ход")
                return
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json(200, {"status": turn["status"], "error": turn["error"],
                             "tokens": turn["tokens"], "seconds": turn["seconds"],
                             "events": store.events(turn["id"], after)})
        elif (m := _FILES.match(path)) or (m := _ZIP.match(path)) or (m := _FILE.match(path)):
            self._files(user, m)
        else:
            self._error(404, "няма такъв адрес")

    def _files(self, user: dict[str, Any], m: re.Match[str]) -> None:
        store = self.app.store
        chat = store.chat(user["id"], m[1])
        if chat is None:
            self._error(404, "няма такъв разговор")
            return
        ws = self.app.workspace(user["id"], chat["id"])
        if m.re is _FILES:
            self._json(200, {"files": list_files(ws), "busy": bool(store.busy(chat_id=chat["id"]))})
            return
        # Докато върви ход, кодът в контейнера може да подмени файл с връзка
        # между проверката и четенето. Затова се сваля само между ходовете.
        if store.busy(chat_id=chat["id"]):
            self._error(409, "задачата още върви — файловете ще са готови след нея")
            return
        if m.re is _ZIP:
            data = zip_files(ws)
            if data is None:
                self._error(413, "файловете са твърде големи за един архив")
                return
            name = "genesis-files.zip"
        else:
            rel = unquote(m[2])
            p = safe_file(ws, rel)
            if p is None:
                self._error(404, "няма такъв файл")
                return
            data = read_file(p)
            name = PurePosixPath(rel).name
        self._send(200, data, "application/octet-stream", {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ── POST ──
    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._body()
        if body is None:
            return
        if path == "/api/login":
            email, password = str(body.get("email", "")), str(body.get("password", ""))
            ip = self._ip()
            if self.app.login_blocked(email, ip):
                self._error(429, "твърде много опити — опитай пак след 15 минути")
                return
            token = self.app.login(email, password, ip)
            if token is None:
                self._error(401, "грешен имейл или парола")
                return
            self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(token, SESSION_SECONDS)})
            return
        user = self._user()
        if user is None:
            self._error(401, "нужен е вход")
            return
        store = self.app.store
        if path == "/api/logout":
            store.end_session(self._token())
            self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
        elif path == "/api/chats":
            title = str(body.get("title") or "Нов разговор").strip()
            self._json(201, {"id": store.new_chat(user["id"], title)})
        elif m := _TURNS.match(path):
            chat = store.chat(user["id"], m[1])
            if chat is None:
                self._error(404, "няма такъв разговор")
                return
            text = str(body.get("text", "")).strip()
            if not text or len(text) > MAX_TEXT:
                self._error(400, f"съобщението трябва да е между 1 и {MAX_TEXT} знака")
                return
            turn_id = self.app.start_turn(user, chat["id"], text)
            if turn_id is None:
                self._error(409, "предишната ти задача още върви")
                return
            self._json(202, {"turn_id": turn_id})
        else:
            self._error(404, "няма такъв адрес")


def make_server(app: App, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Genesis онлайн — вход, чат, файлове.")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user", help="нов човек (бета — само ръчно)")
    add.add_argument("email")
    add.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    srv = sub.add_parser("serve")
    srv.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    srv.add_argument("--jobs", type=Path, default=Path("/srv/jobs"))
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--workers", type=int, default=2, help="контейнери наведнъж")
    srv.add_argument("--seconds", type=int, default=launch.Limits.seconds)
    srv.add_argument("--env-file", help="API ключовете (KEY=value на ред)")
    srv.add_argument("--runtime", help="напр. runsc (gVisor)")
    srv.add_argument("--gateway-usage", type=Path,
                     help="шлюз за моделите: JSONL на шлюза (ключовете не влизат в контейнера)")
    srv.add_argument("--insecure-cookie", action="store_true",
                     help="само за локален тест по http://")
    srv.add_argument("--trust-proxy", action="store_true",
                     help="IP от CF-Connecting-IP / X-Forwarded-For (зад Cloudflare/Caddy)")
    a = p.parse_args(argv)
    store = Store(a.data / "web.db")
    if a.cmd == "add-user":
        password = secrets.token_urlsafe(12)
        try:
            store.add_user(a.email, password)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{a.email.strip().lower()}  парола: {password}")
        return 0
    gateway = None
    if a.gateway_usage:
        if not a.env_file:
            print("--gateway-usage иска --env-file с GATEWAY_SECRET", file=sys.stderr)
            return 2
        gateway = launch.Gateway.from_keys_file(Path(a.env_file), a.gateway_usage)
    app = App(store, a.jobs, workers=a.workers, limits=launch.Limits(seconds=a.seconds),
              env_file=a.env_file, runtime=a.runtime, secure_cookie=not a.insecure_cookie,
              trust_proxy=a.trust_proxy, gateway=gateway)
    server = make_server(app, a.host, a.port)
    print(f"genesis web: http://{a.host}:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
