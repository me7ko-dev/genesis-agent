"""genesis_agent.remote_server — `genesis serve`: Genesis от телефона.

Агентът остава на компютъра (там са файловете, shell-ът, ключовете);
телефонът (mobile/, Android и iOS) е само прозорец към него — както Claude
Code на телефона управлява сесия, която тече другаде. iOS и без това не
позволява на приложение да пуска процеси, тоест агент на самия телефон не
би могъл да прави нищо полезно.

Сигурност. Това е отдалечено изпълнение на команди на машината — затова:

  * Всяка заявка и всеки отговор са криптирани и автентикирани с
    ChaCha20-Poly1305 и 32-байтов ключ, който се ражда тук и стига до
    телефона САМО през QR кода на екрана (във фрагмента на адреса, `#k=`,
    който браузърите не пращат по мрежата). По мрежата не минава нито ключ,
    нито токен — само шифротекст. Който слуша в същия Wi-Fi, не може нито да
    чете разговора, нито да изпрати команда.
  * Повторено (записано и пуснато пак) съобщение се отказва: всяка заявка
    носи час и еднократен идентификатор.
  * Отговорът е вързан за заявката (AAD съдържа нейния идентификатор), тоест
    не може да бъде подменен с друг, стар отговор.
  * Опасните команди минават през същия sandbox и питат оператора — само
    че въпросът отива на телефона, а без отговор до 5 минути е „не".
  * `genesis serve --reset` сменя ключа: всички сдвоени телефони губят
    достъп.

Протокол v1 (mobile/src/protocol.ts е другата страна):

    GET  /api/hello  → {"app":"genesis","v":1,"key_id":"…","name":"…"}   явно
    POST /api/v1     ← {"v":1,"n":b64(nonce),"c":b64(шифротекст)}
                       AAD "genesis/1/req"; открит текст:
                       {"ts":ms,"rid":"…","op":"status|send|events|confirm|stop|clear", …}
                     → {"v":1,"n":…,"c":…}, AAD "genesis/1/res:" + rid
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROTOCOL = 1
DEFAULT_PORT = 8765
_REQ_AAD = b"genesis/1/req"
_RES_AAD = b"genesis/1/res:"
_MAX_SKEW_MS = 5 * 60 * 1000
_MAX_BODY = 256 * 1024
_MAX_WAIT_S = 25.0
_CONFIRM_TIMEOUT_S = 300.0
_EVENT_LOG = 1000
_TOOL_PREVIEW = 4000


class ProtocolError(Exception):
    """Заявка, която не се приема — отговорът е явна грешка без подробности."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_id(key: bytes) -> str:
    """Отпечатък на ключа — телефонът проверява, че говори с СВОЯ компютър."""
    return hashlib.sha256(key).hexdigest()[:16]


# ── ключ и настройки ────────────────────────────────────────────────────────

def _config_path() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return GENESIS_HOME / "remote.json"


def load_or_create_key(*, reset: bool = False) -> bytes:
    """Ключът за сдвояване. Живее в ~/.genesis/remote.json (само за собственика)."""
    path = _config_path()
    if not reset:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            key = _b64d(data["key"])
            if len(key) == 32:
                return key
        except (OSError, ValueError, KeyError, TypeError):
            pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"key": _b64e(key), "created": int(time.time())}, f)
    os.replace(tmp, path)
    return key


def lan_addresses() -> list[str]:
    """IPv4 адресите, на които телефонът в същата мрежа може да стигне компютъра.

    Първият е този, през който минава маршрутът навън (UDP „connect" не праща
    нищо — само пита таблицата с маршрути). После останалите от името на
    машината. Loopback и link-local отпадат: телефонът не може да ги ползва.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return [a for a in found if not a.startswith(("127.", "169.254.", "0."))]


def pairing_url(host: str, port: int, key: bytes, name: str) -> str:
    """Какво носи QR кодът. Ключът е във фрагмента (след `#`): браузърът
    не го праща към сървъра, а приложението го чете само от кода."""
    from urllib.parse import quote
    return f"http://{host}:{port}/#k={_b64e(key)}&n={quote(name)}"


# ── криптиране ─────────────────────────────────────────────────────────────

class Cipher:
    """ChaCha20-Poly1305 със случаен 96-битов nonce на всяко съобщение."""

    def __init__(self, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        if len(key) != 32:
            raise ValueError("ключът трябва да е 32 байта")
        self._aead = ChaCha20Poly1305(key)

    def seal(self, payload: dict, aad: bytes) -> dict:
        nonce = secrets.token_bytes(12)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return {"v": PROTOCOL, "n": _b64e(nonce), "c": _b64e(self._aead.encrypt(nonce, raw, aad))}

    def open(self, envelope: Any, aad: bytes) -> dict:
        from cryptography.exceptions import InvalidTag
        if not isinstance(envelope, dict) or envelope.get("v") != PROTOCOL:
            raise ProtocolError("unsupported envelope")
        try:
            nonce = _b64d(str(envelope["n"]))
            data = _b64d(str(envelope["c"]))
            plain = self._aead.decrypt(nonce, data, aad)
            payload = json.loads(plain.decode("utf-8"))
        except (KeyError, ValueError, InvalidTag, UnicodeDecodeError) as e:
            raise ProtocolError("cannot open") from e
        if not isinstance(payload, dict):
            raise ProtocolError("payload is not an object")
        return payload


class ReplayGuard:
    """Отказва заявка извън ±5 минути или с вече виждан идентификатор."""

    def __init__(self, max_skew_ms: int = _MAX_SKEW_MS) -> None:
        self._max_skew_ms = max_skew_ms
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, payload: dict, now_ms: float | None = None) -> str:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        rid, ts = payload.get("rid"), payload.get("ts")
        if not isinstance(rid, str) or not (8 <= len(rid) <= 64):
            raise ProtocolError("bad rid")
        if not isinstance(ts, (int, float)) or abs(now_ms - ts) > self._max_skew_ms:
            raise ProtocolError("stale request (check the phone's clock)")
        with self._lock:
            horizon = now_ms - 2 * self._max_skew_ms
            for old in [r for r, t in self._seen.items() if t < horizon]:
                del self._seen[old]
            if rid in self._seen:
                raise ProtocolError("replayed request")
            self._seen[rid] = now_ms
        return rid


# ── сесията: събития, ход на агента, потвърждения ──────────────────────────

@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    allow: bool = False


TurnRunner = Callable[[str, Any], None]


class RemoteSession:
    """Един разговор, споделен от всички сдвоени телефони.

    Ходовете вървят един по един в отделна нишка; всичко, което агентът
    показва, става събитие с пореден номер. Телефонът пита „какво има след
    N" (дълго чакане до 25 s) — ако е бил офлайн, получава пропуснатото.
    """

    def __init__(self, runner: TurnRunner, *, on_event: Callable[[dict], None] | None = None) -> None:
        self._runner = runner
        self._on_event = on_event
        self._events: deque[dict] = deque(maxlen=_EVENT_LOG)
        self._seq = 0
        self._cond = threading.Condition()
        self._busy = False
        self._stop = threading.Event()
        self._pending: dict[str, _Pending] = {}
        self.epoch = secrets.token_hex(4)

    # -- събития --
    def emit(self, kind: str, **data: Any) -> dict:
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "ts": int(time.time() * 1000), "type": kind, **data}
            self._events.append(event)
            self._cond.notify_all()
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass
        return event

    def events_after(self, after: int, wait: float = 0.0) -> dict:
        deadline = time.monotonic() + max(0.0, min(wait, _MAX_WAIT_S))
        with self._cond:
            while self._seq <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            first = self._events[0]["seq"] if self._events else self._seq + 1
            # Пропуснатото вече не е в дневника → телефонът получава целия
            # наличен дневник и знае, че трябва да го покаже наново.
            reset = after + 1 < first or after > self._seq
            items = list(self._events) if reset else [e for e in self._events if e["seq"] > after]
            return {"events": items, "last": self._seq, "reset": reset,
                    "busy": self._busy, "epoch": self.epoch}

    # -- ход --
    @property
    def busy(self) -> bool:
        return self._busy

    def send(self, text: str) -> bool:
        text = text.strip()
        if not text:
            raise ProtocolError("empty message")
        with self._cond:
            if self._busy:
                return False
            self._busy = True
            self._stop.clear()
        self.emit("user", text=text)
        threading.Thread(target=self._run, args=(text,), daemon=True, name="genesis-turn").start()
        return True

    def _run(self, text: str) -> None:
        self.emit("busy", busy=True)
        try:
            self._runner(text, RemoteTurnUI(self))
        except Exception as e:
            self.emit("error", text=f"{type(e).__name__}: {e}")
        finally:
            with self._cond:
                self._busy = False
            self.emit("busy", busy=False)

    def request_stop(self) -> None:
        self._stop.set()
        for pending in list(self._pending.values()):
            pending.allow = False
            pending.event.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- потвърждения от sandbox-а --
    def confirm(self, operation: str, reasons: list[str], timeout: float = _CONFIRM_TIMEOUT_S) -> bool:
        cid = secrets.token_hex(6)
        pending = self._pending[cid] = _Pending()
        self.emit("confirm", id=cid, operation=operation[:1000], reasons=list(reasons)[:10])
        answered = pending.event.wait(timeout)
        self._pending.pop(cid, None)
        allow = answered and pending.allow and not self._stop.is_set()
        self.emit("confirm_done", id=cid, allow=allow,
                  note="" if answered else "няма отговор — отказано")
        return allow

    def answer(self, cid: str, allow: bool) -> bool:
        pending = self._pending.get(cid)
        if pending is None:
            return False
        pending.allow = bool(allow)
        pending.event.set()
        return True


class RemoteTurnUI:
    """TurnUI (genesis_terminal_agent) за телефона: всичко става събитие."""

    def __init__(self, session: RemoteSession) -> None:
        self._s = session

    def thinking(self, label: str, spinner: str = "dots"):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self._s.emit("thinking", label=label)
            yield
        return _cm()

    def assistant(self, text: str) -> None:
        if text.strip():
            self._s.emit("assistant", text=text)

    def tool(self, name: str, result: str) -> None:
        clipped = len(result) > _TOOL_PREVIEW
        self._s.emit("tool", name=name, result=result[:_TOOL_PREVIEW], clipped=clipped)

    def asked(self, question: str) -> None:
        self._s.emit("asked", text=question)

    def spinning(self, note: str) -> None:
        self._s.emit("warn", text=note)

    def warn(self, text: str) -> None:
        self._s.emit("warn", text=text)

    def info(self, text: str) -> None:
        self._s.emit("info", text=text)

    def cancelled(self) -> bool:
        return self._s.stopped()


# ── HTTP ────────────────────────────────────────────────────────────────────

class RemoteServer:
    """Сглобява ключ, сесия и HTTP сървър. `serve_forever` блокира."""

    def __init__(self, key: bytes, session: RemoteSession, *, name: str,
                 status: Callable[[], dict] | None = None,
                 clear: Callable[[], None] | None = None,
                 commands: Callable[[str, dict], dict] | None = None,
                 web_root: Path | None = None) -> None:
        self.key = key
        self.cipher = Cipher(key)
        self.replay = ReplayGuard()
        self.session = session
        self.name = name
        self.status = status or (dict)
        self.clear = clear
        self.commands = commands
        self.web_root = web_root
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -- заявки --
    def hello(self) -> dict:
        from genesis_agent import __version__
        return {"app": "genesis", "v": PROTOCOL, "key_id": key_id(self.key),
                "name": self.name, "version": __version__,
                "features": ["commands"] if self.commands else []}

    def handle(self, envelope: Any) -> tuple[dict, str]:
        """(отговор, rid) за вече декодиран JSON плик. Хвърля ProtocolError."""
        payload = self.cipher.open(envelope, _REQ_AAD)
        rid = self.replay.check(payload)
        return self._dispatch(payload), rid

    def _dispatch(self, p: dict) -> dict:
        op = p.get("op")
        s = self.session
        if op == "status":
            return {"ok": True, **self.hello(), "busy": s.busy, **self.status()}
        if op == "send":
            text = str(p.get("text") or "")
            if len(text) > 20000:
                return {"ok": False, "error": "too_long"}
            if text.strip().lower() in ("/clear", "/нов"):
                return self._dispatch({"op": "clear"})
            return {"ok": True} if s.send(text) else {"ok": False, "error": "busy"}
        if op == "events":
            try:
                after = int(p.get("after") or 0)
                wait = float(p.get("wait") or 0)
            except (TypeError, ValueError) as e:
                raise ProtocolError("bad events args") from e
            return {"ok": True, **s.events_after(after, wait)}
        if op == "confirm":
            return {"ok": s.answer(str(p.get("id") or ""), bool(p.get("allow")))}
        if op == "stop":
            s.request_stop()
            return {"ok": True}
        if op == "clear":
            if s.busy:
                return {"ok": False, "error": "busy"}
            if self.clear is not None:
                self.clear()
            s.emit("cleared")
            return {"ok": True}
        if op == "command" and self.commands is not None:
            arg = p.get("arg")
            return self.commands(str(p.get("name") or ""), arg if isinstance(arg, dict) else {})
        raise ProtocolError("unknown op")

    def seal(self, response: dict, rid: str) -> dict:
        return self.cipher.seal(response, _RES_AAD + rid.encode("ascii"))

    # -- защита от налучкване --
    def too_many_failures(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < 60]
            self._failures[ip] = recent
            return len(recent) >= 30

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.monotonic())

    def make_http(self, host: str, port: int) -> ThreadingHTTPServer:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "genesis"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # тихо: дисплеят е събитията
                return

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # Уеб клиентът се сервира от същия адрес; CORS е за Expo в
                # режим на разработка (друг порт). Съдържанието е шифровано,
                # тоест чужд сайт не печели нищо от достъпа.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: dict) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def do_OPTIONS(self) -> None:
                self._send(204, b"")

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/api/hello":
                    self._json(200, server.hello())
                    return
                self._static(path)

            def _static(self, path: str) -> None:
                root = server.web_root
                if root is None or not (root / "index.html").is_file():
                    self._send(200, _LANDING.encode("utf-8"), "text/html; charset=utf-8")
                    return
                rel = path.lstrip("/") or "index.html"
                target = (root / rel).resolve()
                if root.resolve() not in target.parents or not target.is_file():
                    target = root / "index.html"  # едностранично приложение
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)

            def do_POST(self) -> None:
                ip = self.client_address[0]
                if self.path.split("?", 1)[0] != "/api/v1":
                    self._json(404, {"error": "not_found"})
                    return
                if server.too_many_failures(ip):
                    self._json(429, {"error": "slow_down"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 < length <= _MAX_BODY:
                    self._json(413, {"error": "size"})
                    return
                try:
                    envelope = json.loads(self.rfile.read(length).decode("utf-8"))
                    response, rid = server.handle(envelope)
                except (ProtocolError, ValueError, UnicodeDecodeError) as e:
                    server.record_failure(ip)
                    # Подробността е само вида на отказа („cannot open",
                    # „stale request"): полезна на своя телефон, безполезна
                    # на непознат, който няма ключа.
                    self._json(401, {"error": "rejected", "detail": str(e)[:80]})
                    return
                self._json(200, server.seal(response, rid))

        httpd = _QuietServer((host, port), Handler)
        httpd.daemon_threads = True
        return httpd


class _QuietServer(ThreadingHTTPServer):
    """Телефон, който заспи или смени мрежата насред дългото чакане, затваря
    връзката — това е нормално, не грешка за печатане в терминала."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".ttf": "font/ttf", ".woff2": "font/woff2", ".map": "application/json",
}

_LANDING = """<!doctype html><html lang="bg"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Genesis</title>
<body style="font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1>Genesis е тук</h1>
<p>Отвори приложението <b>Genesis Remote</b> на телефона и сканирай QR кода от
терминала на компютъра. Уеб версията не е включена в тази инсталация.</p>
</body></html>"""


# ── `genesis serve` ─────────────────────────────────────────────────────────

def _print_qr(console: Any, url: str) -> None:
    try:
        import qrcode  # type: ignore[import-untyped]
    except ImportError:
        console.print("[yellow](за QR код: pip install qrcode — или въведи адреса ръчно)[/]")
        return
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    # Два реда модули на един ред текст (▀ ▄ █): кодът е квадратен и се
    # чете от телефон и в малък прозорец. Цветовете са зададени изрично, за
    # да е тъмно-на-светло и в тъмна тема на терминала.
    lines = []
    for y in range(0, len(matrix), 2):
        row = []
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < len(matrix) else False
            # Тъмният модул е черен знак на бял фон — обърнат код камерата
            # на iPhone не чете надеждно.
            row.append({(True, True): "█", (True, False): "▀",
                        (False, True): "▄", (False, False): " "}[(top, bottom)])
        lines.append("".join(row))
    console.print("\n".join(lines), style="black on white", highlight=False)


def _web_root() -> Path | None:
    from genesis_agent.paths import PACKAGE_DIR
    root = PACKAGE_DIR / "web"
    return root if (root / "index.html").is_file() else None


@dataclass
class ServeOptions:
    port: int = DEFAULT_PORT
    host: str = ""          # адресът в QR кода; празно — първият LAN адрес
    bind: str = "0.0.0.0"   # къде слуша сървърът
    reset: bool = False


def parse_serve_args(args: list[str]) -> ServeOptions | int:
    """Опциите на `genesis serve` — или код за изход (--help, грешна опция)."""
    opts = ServeOptions()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            i += 1
            try:
                opts.port = int(args[i])
            except ValueError:
                print(f"--port иска число, не {args[i]!r}")
                return 2
        elif a == "--host" and i + 1 < len(args):
            i += 1
            opts.host = args[i]
        elif a == "--bind" and i + 1 < len(args):
            i += 1
            opts.bind = args[i]
        elif a == "--reset":
            opts.reset = True
        elif a in ("-h", "--help"):
            print(SERVE_USAGE)
            return 0
        else:
            print(f"Непозната опция: {a}\n\n{SERVE_USAGE}")
            return 2
        i += 1
    return opts


def serve(args: list[str]) -> int:
    """`genesis serve [--port N] [--host IP] [--bind IP] [--reset]`."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("`genesis serve` иска cryptography:  pip install \"genesis-agent[mobile]\"")
        return 2

    opts = parse_serve_args(args)
    if isinstance(opts, int):
        return opts
    port, host_override, bind, reset = opts.port, opts.host, opts.bind, opts.reset
    # Слуша само на един адрес (127.0.0.1 за Genesis Desktop) → той е и в QR кода.
    if not host_override and bind not in ("", "0.0.0.0"):
        host_override = bind

    key = load_or_create_key(reset=reset)
    from rich.panel import Panel
    from rich.text import Text

    import genesis_terminal_agent as gta

    console = gta.console
    system_prompt, _briefing = gta.build_system_prompt()
    state: dict[str, Any] = {"messages": deque([{"role": "system", "content": system_prompt}],
                                               maxlen=gta._HISTORY_MAXLEN)}

    def runner(text: str, ui: Any) -> None:
        state["messages"] = gta.run_turn(state["messages"], text, ui)

    def clear() -> None:
        state["messages"] = deque([{"role": "system", "content": system_prompt}],
                                  maxlen=gta._HISTORY_MAXLEN)
        # Нов разговор → нов файл в историята и нулеви броячи, както `/clear`
        # в терминала; иначе следващият ход презаписва току-що изчистената сесия.
        gta.session_start_time = time.time()
        gta.total_input_tokens = gta.total_output_tokens = 0

    def status() -> dict:
        return {"model": f"{gta.current_provider}/{gta.current_model_id}",
                "workspace": str(gta.WORKSPACE)}

    def show(event: dict) -> None:
        kind = event["type"]
        if kind == "user":
            console.print(f"\n[bold green]📱 ❯[/] {event['text']}")
        elif kind == "assistant":
            gta.RICH_UI.assistant(event["text"])
        elif kind == "tool":
            gta.RICH_UI.tool(event["name"], event["result"])
        elif kind in ("warn", "asked", "error"):
            console.print(f"[yellow]⚠ {event['text']}[/]")
        elif kind == "confirm":
            console.print(f"[bold yellow]📱 Чака потвърждение от телефона:[/] {event['operation'][:200]}")
        elif kind == "confirm_done":
            console.print(f"[dim]   → {'разрешено' if event['allow'] else 'отказано'} {event.get('note', '')}[/]")

    session = RemoteSession(runner, on_event=show)

    from genesis_agent import sandbox as _sandbox
    _sandbox.set_policy(_sandbox.SandboxPolicy(
        mode="interactive",
        confirm_fn=lambda op, verdict: session.confirm(op, list(verdict.reasons))))

    name = socket.gethostname()
    from genesis_agent import desktop_commands

    def set_messages(messages: Any) -> None:
        state["messages"] = messages

    ctx = desktop_commands.CommandContext(
        get_messages=lambda: state["messages"], set_messages=set_messages,
        busy=lambda: session.busy, emit=session.emit)
    server = RemoteServer(key, session, name=name, status=status, clear=clear,
                          commands=lambda n, a: desktop_commands.run(n, a, ctx),
                          web_root=_web_root())
    try:
        httpd = server.make_http(bind, port)
    except OSError as e:
        print(f"Не мога да слушам на порт {port}: {e}. Друг порт: genesis serve --port 8766")
        return 1

    hosts = [host_override] if host_override else (lan_addresses() or ["127.0.0.1"])
    url = pairing_url(hosts[0], port, key, name)
    console.print(Panel(Text.assemble(
        ("Сканирай с телефона (приложението Genesis Remote или камерата):\n", "bold"),
        ("Работна папка: ", "dim"), (str(gta.WORKSPACE), ""),
        ("\nАдрес: ", "dim"), (f"http://{hosts[0]}:{port}", "cyan"),
        (("\nКлючът е само в QR кода. Нов ключ (отнема достъпа на всички телефони): "
          "genesis serve --reset"), "dim")),
        title="📱 Genesis за телефона", border_style="cyan"))
    _print_qr(console, url)
    # Същото като текст — за поставяне в приложението, ако камерата не
    # хване кода. Терминалът е на машината на оператора, не в мрежата.
    console.print(f"[dim]Връзка:[/] {url}", highlight=False, soft_wrap=True)
    if len(hosts) > 1:
        console.print(f"[dim]Други адреси на тази машина: {', '.join(hosts[1:])} "
                      "(genesis serve --host <адрес>)[/]")
    if os.name == "nt":
        console.print("[dim]Windows пита за защитната стена при първото пускане — "
                      "разреши „Private networks“, иначе телефонът няма да стигне дотук.[/]")
    console.print("[dim]Ctrl+C спира.[/]")

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        session.request_stop()
        try:
            from genesis_agent import workspace_memory as _wm
            convo = [m for m in state["messages"] if m.get("role") in ("user", "assistant")]
            if len(convo) >= 2:
                _wm.auto_capture(list(convo))
        except Exception:
            pass
    console.print("\n[dim]Сървърът за телефона е спрян.[/]")
    return 0


SERVE_USAGE = """Употреба: genesis serve [--port N] [--host IP] [--bind IP] [--reset]

Пуска Genesis за телефона (приложението Genesis Remote за Android и iOS):
показва QR код, който сдвоява телефона с тази машина.

  --port N    порт (по подразбиране 8765)
  --host IP   адрес в QR кода — напр. Tailscale адрес за достъп извън дома
  --bind IP   на кой адрес да слуша (по подразбиране 0.0.0.0; 127.0.0.1 —
              само тази машина, както го пуска Genesis Desktop)
  --reset     нов ключ; всички сдвоени телефони губят достъп"""
