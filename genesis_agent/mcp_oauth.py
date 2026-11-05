"""
genesis_agent.mcp_oauth — вход с OAuth за MCP сървъри по HTTP (2026-10-09).

Хостнатите MCP сървъри (Linear, Notion, Sentry, GitHub…) не искат токен в
headers, а вход: отговарят 401 и казват къде е сървърът за вход. Досега
Genesis казваше само „провери headers в mcp.json“. Тук е потокът от
спецификацията на MCP (2025-06-18, Authorization), както в Claude Code:

  1. 401 → `WWW-Authenticate: Bearer resource_metadata="…"` (RFC 9728), или
     `/.well-known/oauth-protected-resource` на сървъра;
  2. метаданните на сървъра за вход (RFC 8414, или OpenID discovery);
  3. регистрация на клиента (RFC 7591), ако сървърът я предлага;
  4. вход в браузъра: authorization code + PKCE S256, `resource` (RFC 8707),
     обратен адрес на 127.0.0.1 — кодът никога не минава през мрежата;
  5. токенът (и refresh токенът) — в ~/.genesis/mcp_tokens.json (0600).

`/mcp login <име>` го пуска в терминала; от телефона — не (браузърът е на
компютъра). Изтекъл токен се подновява сам с refresh токена.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

log = logging.getLogger("genesis.mcp_oauth")

LOGIN_TIMEOUT = 300.0
_HTTP_TIMEOUT = 15
# Колко чака ключалката на файла с токените друг процес (одит 2026-10-31).
_LOCK_WAIT = 60.0
# Метаданните и отговорите с токени са няколко KB. Без таван 100 MB тяло от
# сървъра за токени влизаше цялото в паметта (RSS от 29 на 329 MB) и в
# mcp_tokens.json (одит 2026-10-31, възпроизведено).
_MAX_BODY = 256 * 1024
# Общият срок на една размяна със сървъра за вход (одит 2026-11-04 — същото
# като горе: /token, който капе по байт на 0.5 s, държеше refresh() над 40 s).
# Подразбиращият се срок е по-малкото от него и _HTTP_TIMEOUT.
_HTTP_DEADLINE = 20.0


class OAuthError(RuntimeError):
    """Входът не стана — текстът е за оператора."""


# ── токените на диска ───────────────────────────────────────────────────────

def _store_path() -> Path:
    from genesis_agent import paths
    return Path(paths.GENESIS_HOME) / "mcp_tokens.json"


def _key(url: str) -> str:
    """Токенът е за точно този адрес (схема, хост, порт, път)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def _load_all() -> dict[str, dict]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_all(data: dict[str, dict]) -> None:
    import tempfile
    path = _store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Свое временно име: общото `.tmp` при два процеса даваше FileNotFoundError
    # и изгубени записи (одит 2026-10-09).
    fd, tmp = tempfile.mkstemp(prefix=".mcp_tokens.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_lock = threading.Lock()
# Токен, който не можа да се запише (пълен диск, заключен файл): поне този
# процес го пази — сървърът вече е завъртял refresh токена (одит 2026-10-09).
_memory: dict[str, dict] = {}


class _FileLock:
    """Между процесите (`genesis` и `genesis serve` с общ ~/.genesis): прочети →
    поднови → запиши е едно цяло, иначе завъртян refresh токен се губеше
    и единият процес искаше нов вход (одит 2026-10-09: 5 от 10).

    flock / msvcrt: ключалката е на отворения файл и пада сама, ако процесът
    умре — без „кражба“ по възраст, която пускаше двама вътре (втори одит).

    `timeout` (2026-10-31): колко най-много да се чака — и в процеса, и между
    процесите; None — в процеса докогато трябва, другия процес _LOCK_WAIT."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def __enter__(self) -> None:
        start = time.monotonic()
        if self.timeout is None:
            _lock.acquire()
        elif not _lock.acquire(timeout=max(0.0, self.timeout)):
            raise OAuthError("файлът с токените е зает (подновява се)")
        self.fh = None
        try:
            path = _store_path().with_suffix(".lock")
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fh = open(path, "a+b")
            except OSError:
                # ~/.genesis само за четене: поне в процеса е заключено, а
                # токенът остава в _memory (както преди — трети одит).
                return
            deadline = start + (_LOCK_WAIT if self.timeout is None else self.timeout)
            while True:
                try:
                    _lock_file(fh)
                    self.fh = fh
                    return
                except OSError as e:
                    if e.errno not in _BUSY:
                        # flock не работи на тази файлова система (ENOLCK на
                        # NFS без lockd): друг процес няма, чакането беше 60 s,
                        # „зает от друг процес“ и изхвърлен токен след входа
                        # (одит 2026-10-31, възпроизведено). Както при
                        # read-only: заключено само в процеса.
                        log.debug("ключалката на файла с токените не работи: %s", e)
                        fh.close()
                        return
                    if time.monotonic() > deadline:
                        fh.close()
                        raise OAuthError("файлът с токените е зает от друг процес") from None
                    time.sleep(0.05)
        except BaseException:
            # Ctrl-C в чакането: без това _lock оставаше взета завинаги (одит).
            _lock.release()
            raise

    def __exit__(self, *exc: object) -> None:
        try:
            if self.fh is not None:
                _unlock_file(self.fh)
                self.fh.close()
        finally:
            _lock.release()


# „Зает“ — ключалката е у друг (одит 2026-10-31): flock (EWOULDBLOCK/EAGAIN, EACCES при някои
# системи), msvcrt.locking на Windows (EACCES; EDEADLOCK при LK_LOCK).
_BUSY = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES,
                   getattr(errno, "EDEADLOCK", errno.EAGAIN)})


def _lock_file(fh: Any) -> None:
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fh: Any) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def saved(url: str) -> dict | None:
    key = _key(url)
    if key in _memory:
        return dict(_memory[key])
    entry = _load_all().get(key)
    return entry if isinstance(entry, dict) else None


def _save(url: str, entry: dict) -> None:
    """Само под _FileLock."""
    key = _key(url)
    try:
        data = _load_all()
        data[key] = entry
        _save_all(data)
        _memory.pop(key, None)
    except OSError:
        _memory[key] = dict(entry)


def forget(url: str) -> bool:
    key = _key(url)
    with _FileLock():
        # Вход само в паметта (read-only ~/.genesis, пълен диск) също е вход:
        # /mcp logout казваше „Няма вход“ точно след успешен вход, а махаше
        # ползвания токен (одит 2026-10-31, възпроизведено).
        gone = _memory.pop(key, None) is not None
        data = _load_all()
        if data.pop(key, None) is not None:
            _save_all(data)
            gone = True
    return gone


# ── откриване ──────────────────────────────────────────────────────────────

def _covers(resource: str, server_url: str) -> bool:
    """Ресурсът е този сървър или негов „родител“ на същия адрес: метаданни в
    корена на домейна дават `resource` = `https://host` за `https://host/mcp`
    (RFC 9728 §3.1, както MCP SDK-то; втори одит). Друг хост — никога."""
    r, s = urlsplit(_key(resource)), urlsplit(_key(server_url))
    if (r.scheme, r.netloc) != (s.scheme, s.netloc):
        return False
    rp, sp = r.path.rstrip("/"), s.path.rstrip("/")
    return sp == rp or sp.startswith(rp + "/")


def _shown(url: str) -> str:
    """Схемата и хостът (с порта) — за съобщенията. `/mcp login` подава
    разгърнатия адрес (`${VAR}` вече заместени) и ключ в заявката или в пътя
    (Zapier, Smithery) излизаше на екрана в „❌ Входът … не стана“ (прегледът
    на одита, 2026-11-05); mcp_client пази същото с `_netloc()`."""
    try:
        parts = urlsplit(str(url or ""))
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return "<адрес>"
    if not host:
        return "<адрес>"
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _has_controls(text: str) -> bool:
    """Управляващи и невидими знаци (ESC, CR, U+202E…) и интервали."""
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp") for ch in text)


def _plain(text: object, limit: int = 300) -> str:
    """Текст от сървъра за терминала: без управляващи знаци, на един ред, кратък."""
    raw = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in str(text or ""))
    return " ".join(raw.split())[:limit]


def _safe_url(url: str, what: str) -> str:
    """HTTPS, или http само към този компютър: токенът и кодът не тръгват
    по мрежата открито."""
    # Адресът се показва на оператора и се отваря в браузъра. Одит
    # 2026-11-04: OSC 52 (запис в клипборда) и „изчисти екрана“ в
    # authorization_endpoint стигаха до терминала непокътнати (Rich с
    # markup=False пропуска ESC), а urlsplit проверяваше адрес без \t\r\n —
    # не този, който се показва и отваря.
    if _has_controls(str(url or "")):
        raise OAuthError(f"{what}: {_shown(_plain(url, 200))!r} съдържа управляващи знаци — отказано")
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return url
    if parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return url
    raise OAuthError(f"{what}: {_shown(url)!r} не е https — отказано")


def _shutdown(resp: Any) -> None:
    """Прекъсва връзката изпод четящата нишка (както mcp_client._abort):
    shutdown() на сокета връща recv веднага; detach() — затваря го нишката."""
    import socket
    try:
        sock = socket.socket(fileno=resp.raw.fileno())
    except (OSError, ValueError, AttributeError, TypeError):
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    finally:
        sock.detach()


def _http(method: str, url: str, timeout: float | None = None, **kw: Any) -> tuple[int, bytes]:
    """Една HTTP размяна със сървъра за вход: (статус, тяло), с ОБЩ срок и
    таван _MAX_BODY; OAuthError — не стана.

    Срокът на requests е за едно recv, не за цялата размяна: сървър за токени,
    който капе по байт, държеше refresh() — и _lock с него — над 45 s; после
    /mcp logout чакаше завинаги, а `genesis` не излизаше, защото stop_all()
    при atexit подновяваше (одит 2026-10-31, възпроизведено). Размяната е в
    нишка: извикващият продължава в срока, а връзката се прекъсва."""
    import requests
    limit = min(_HTTP_TIMEOUT, _HTTP_DEADLINE) if timeout is None else max(0.0, timeout)
    done: queue.Queue = queue.Queue()
    guard = threading.Lock()
    cancelled = threading.Event()
    live: list[Any] = []   # отговорът, докато нишката го чете

    def work() -> None:
        resp = None
        try:
            resp = requests.request(method, url, timeout=max(limit, 0.1), stream=True,
                                    allow_redirects=False, **kw)
            with guard:
                if cancelled.is_set():
                    return
                live.append(resp)
            body = bytearray()
            for chunk in resp.iter_content(chunk_size=16384):
                body += chunk
                if len(body) > _MAX_BODY:
                    raise OAuthError(f"отговорът е над {_MAX_BODY // 1024} KB — отказан")
            done.put((resp.status_code, bytes(body)))
        except Exception as e:  # noqa: BLE001 — предава се на викащия, който я хвърля като OAuthError
            done.put(e)
        finally:
            # Под guard: сокетът не се затваря, докато основната нишка го
            # прекъсва — иначе номерът му можеше да е вече на друга връзка.
            with guard:
                live.clear()
                if resp is not None:
                    resp.close()

    threading.Thread(target=work, name="genesis-oauth-http", daemon=True).start()
    end = time.monotonic() + limit
    while True:
        try:
            # На парчета: на Windows (до 3.14) едно дълго чакане не се буди от
            # Ctrl-C (одит 2026-11-04).
            item = done.get(timeout=max(0.0, min(0.25, end - time.monotonic())))
            break
        except queue.Empty:
            if time.monotonic() >= end:
                with guard:
                    cancelled.set()
                    for resp in live:
                        _shutdown(resp)
                raise OAuthError(f"няма отговор за {limit:g} s") from None
    if isinstance(item, OAuthError):
        raise item
    if isinstance(item, Exception):
        raise OAuthError(str(item) or type(item).__name__) from item
    status, body = item
    return status, body


def _get_json(url: str) -> dict | None:
    try:
        status, body = _http("GET", url, headers={"Accept": "application/json"})
        data = json.loads(body) if status == 200 else None
    except (OAuthError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_METADATA_HINT = re.compile(r'resource_metadata="([^"]+)"')


def _well_known(base: str, name: str) -> list[str]:
    """`/.well-known/<name>` с пътя отзад, после без него (RFC 8414 / 9728)."""
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    found = [f"{root}/.well-known/{name}{path}"] if path else []
    return [*found, f"{root}/.well-known/{name}"]


def discover(server_url: str, www_authenticate: str = "") -> dict:
    """Къде и как се влиза за този MCP сървър."""
    _safe_url(server_url, "MCP сървърът")
    candidates = []
    m = _METADATA_HINT.search(www_authenticate or "")
    if m:
        candidates.append(m.group(1))
    candidates += _well_known(server_url, "oauth-protected-resource")
    resource_meta = None
    for url in candidates:
        try:
            _safe_url(url, "метаданните на сървъра")
        except OAuthError:
            continue
        resource_meta = _get_json(url)
        if resource_meta:
            break
    issuers = (resource_meta or {}).get("authorization_servers") or []
    if not issuers:
        # По-старите сървъри (2025-03-26): сървърът за вход е самият той.
        parts = urlsplit(server_url)
        issuers = [urlunsplit((parts.scheme, parts.netloc, "", "", ""))]
    issuer = _safe_url(str(issuers[0]), "сървърът за вход")
    meta = None
    # RFC 8414 (пътят след .well-known), после OpenID Discovery и в двата вида —
    # Keycloak и др. са `…/realms/x/.well-known/openid-configuration` (одит).
    # Формите в корена на хоста са последни (редът от спецификацията на MCP
    # за издател с път): там е друг издател — при Okta `…/oauth2/default` е
    # свой сървър за вход, а коренът описва този на организацията.
    oauth_forms, oidc_forms = (_well_known(issuer, "oauth-authorization-server"),
                               _well_known(issuer, "openid-configuration"))
    tries = [*oauth_forms[:-1], *oidc_forms[:-1]]
    if urlsplit(issuer).path.rstrip("/"):
        tries.append(issuer.rstrip("/") + "/.well-known/openid-configuration")
    tries += [oauth_forms[-1], oidc_forms[-1]]
    foreign = None
    for url in dict.fromkeys(tries):
        meta = _get_json(url)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            # RFC 8414 §3.3: метаданните са за същия сървър за вход, не за
            # чужд. Чуждите се прескачат, не спират търсенето: при Okta
            # коренът (друг издател) спираше откриването и верният адрес не
            # се питаше (одит 2026-10-31, възпроизведено).
            if str(meta.get("issuer") or issuer).rstrip("/") == issuer.rstrip("/"):
                break
            foreign = foreign or meta.get("issuer")
        meta = None
    if meta is None and foreign:
        raise OAuthError(f"метаданните са за {foreign!r}, а не за {issuer!r} — отказано")
    if meta is None:
        raise OAuthError(f"{issuer} не казва как се влиза (няма метаданни за OAuth)")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise OAuthError("сървърът за вход не поддържа PKCE S256 — отказано")
    # Без `resource` от сървъра — каноничният адрес (RFC 8707: без заявка и
    # фрагмент), както _key. С `server_url` като е, `…/mcp?tenant=acme` никога
    # не влизаше: отказът по-долу хващаше собствения ни адрес (одит
    # 2026-10-31, възпроизведено). Отказът остава за `resource` от сървъра.
    resource = str((resource_meta or {}).get("resource") or _key(server_url))
    # RFC 9728 §3.3: метаданните трябва да са за ТОЗИ сървър. Иначе зъл сървър
    # сочи чужд (`resource` = жертвата) и получава нейния токен: операторът
    # вижда истинската страница за вход, а токенът отива при злия (одит
    # 2026-10-09, възпроизведено).
    if not _covers(resource, server_url) or urlsplit(resource).query or urlsplit(resource).fragment:
        theirs, ours = _shown(resource), _shown(server_url)
        if theirs == ours:
            theirs += " (друг път или заявка)"
        raise OAuthError(f"сървърът иска токен за {theirs!r}, а е {ours!r} — отказано")
    resource = _key(resource).rstrip("/") if urlsplit(resource).path in ("", "/") else _key(resource)
    return {
        "issuer": issuer,
        "authorization_endpoint": _safe_url(meta["authorization_endpoint"], "адресът за вход"),
        "token_endpoint": _safe_url(meta["token_endpoint"], "адресът за токени"),
        "registration_endpoint": meta.get("registration_endpoint") or "",
        "scopes": (resource_meta or {}).get("scopes_supported") or [],
        "resource": str(resource),
    }


_CLIENT_AUTH = ("none", "client_secret_post", "client_secret_basic")


def _register(endpoint: str, redirect_uri: str) -> tuple[str, str, str]:
    """(client_id, client_secret, token_endpoint_auth_method).

    Искаме публичен клиент (`none`), но RFC 7591 §3.2.1 позволява на сървъра
    да го смени: сървър без публични клиенти връща client_secret и
    client_secret_post/basic. Тайната се губеше и размяната на кода падаше с
    401 СЛЕД входа в браузъра (одит 2026-10-31, възпроизведено) — сега се
    ползва, а метод, който не можем, се отказва преди браузъра."""
    _safe_url(endpoint, "регистрацията")
    try:
        status, body = _http("POST", endpoint, json={
            "client_name": "Genesis", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        data = json.loads(body) if status in (200, 201) else {}
    except (OAuthError, ValueError) as e:
        raise OAuthError(f"регистрацията не стана: {e}") from e
    client_id = data.get("client_id") if isinstance(data, dict) else None
    if not client_id:
        raise OAuthError(f"регистрацията не стана (HTTP {status})")
    secret = data.get("client_secret")
    secret = secret if isinstance(secret, str) else ""
    # Без метод в отговора: с тайна — client_secret_basic (по подразбиране в
    # RFC 7591 §2), без — публичен.
    method = str(data.get("token_endpoint_auth_method")
                 or ("client_secret_basic" if secret else "none"))
    if method not in _CLIENT_AUTH:
        raise OAuthError(f"сървърът за вход иска удостоверяване на клиента „{method}“, "
                         "което Genesis не поддържа — сложи токен в headers на mcp.json")
    if method != "none" and not secret:
        raise OAuthError(f"регистрацията иска {method}, но не даде client_secret")
    return str(client_id), (secret if method != "none" else ""), method


# ── вход ───────────────────────────────────────────────────────────────────

def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _Callback(HTTPServer):
    result: dict[str, str]
    state: str


def _callback_server() -> _Callback:
    class Handler(BaseHTTPRequestHandler):
        # Свързване, което не праща нищо (preconnect на браузъра, друг процес),
        # държеше входа и чата завинаги (одит 2026-10-09).
        timeout = 5

        def log_message(self, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            srv: _Callback = self.server  # type: ignore[assignment]
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            ok = parts.path == "/callback" and query.get("state") == srv.state and "code" in query
            if parts.path == "/callback" and query.get("state") == srv.state:
                srv.result = query
            text = ("Входът е готов — върни се в терминала на Genesis." if ok else
                    "Входът не стана — виж терминала на Genesis.")
            body = f"<!doctype html><meta charset=utf-8><p>{text}</p>".encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _Callback(("127.0.0.1", 0), Handler)
    srv.result = {}
    srv.state = secrets.token_urlsafe(24)
    srv.timeout = 1.0
    return srv


def _token_request(endpoint: str, form: dict[str, str], *, secret: str = "",
                   auth_method: str = "", timeout: float | None = None) -> dict:
    headers = {"Accept": "application/json"}
    form = dict(form)
    if secret and auth_method == "client_secret_post":
        form["client_secret"] = secret
    elif secret:
        # client_secret_basic (RFC 6749 §2.3.1): id и тайна, кодирани като във
        # форма, в Authorization; client_id не се дублира в тялото (MCP SDK).
        pair = f"{quote(form.pop('client_id', ''), safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    try:
        status, body = _http("POST", endpoint, data=form, headers=headers, timeout=timeout)
    except OAuthError as e:
        raise OAuthError(f"токенът не дойде: {e}") from e
    # Статусът е най-важното: HTML страница 502 от прокси даваше само
    # „Expecting value: line 1 column 1“, а 2 MB error_description отиваше
    # цяло в терминала (одит 2026-10-31, възпроизведено).
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        if data is None:
            raise OAuthError(f"токенът не дойде (HTTP {status}, отговорът не е JSON)")
        said = [data.get("error_description"), data.get("error")] if isinstance(data, dict) else []
        # Текстът е на сървъра, а отива в терминала (одит 2026-11-04: ESC).
        err = next((_plain(s) for s in said if isinstance(s, str) and s.strip()), "")
        raise OAuthError(f"токенът не дойде (HTTP {status}) {err}".strip())
    return data


def _entry(info: dict, client_id: str, tokens: dict, old: dict | None = None, *,
           secret: str = "", auth_method: str = "") -> dict:
    try:
        lifetime = float(tokens.get("expires_in") or 0)   # и "3600" като низ
    except (TypeError, ValueError):
        lifetime = 0.0
    entry = {
        "access_token": str(tokens["access_token"]),
        "refresh_token": str(tokens.get("refresh_token") or (old or {}).get("refresh_token") or ""),
        "expires_at": time.time() + lifetime if lifetime > 0 else 0,
        "lifetime": lifetime,
        "client_id": client_id, "token_endpoint": info["token_endpoint"],
        "resource": info["resource"], "issuer": info["issuer"],
    }
    if secret:   # поверителен клиент (виж _register) — нужна е и за refresh
        entry["client_secret"] = secret
        entry["token_endpoint_auth_method"] = auth_method
    return entry


def no_browser() -> bool:
    """По SSH или без графична среда браузър на тази машина няма."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    return sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _from_pasted(text: str) -> dict[str, str]:
    """Адресът, на който браузърът стигна (`…/callback?code=…&state=…`)."""
    query = urlsplit(text.strip()).query or text.strip().lstrip("?")
    return {k: v[0] for k, v in parse_qs(query).items()}


def login(server_url: str, *, www_authenticate: str = "",
          open_browser: Callable[[str], Any] | None = None,
          say: Callable[[str], None] = print, timeout: float = LOGIN_TIMEOUT,
          paste: Callable[[str], str] | None = None) -> dict:
    """Целият вход. `open_browser` по подразбиране е webbrowser.open; адресът
    се казва и на оператора — ако браузърът не се отвори, го копира сам.

    `paste` (2026-10-09): без браузър тук (SSH) — операторът отваря адреса
    където има браузър и поставя адреса, на който е стигнал; кодът и `state`
    се взимат от него. Обратният адрес на 127.0.0.1 там не отговаря — това е
    очаквано."""
    info = discover(server_url, www_authenticate)
    srv = _callback_server()
    redirect_uri = f"http://127.0.0.1:{srv.server_address[1]}/callback"
    try:
        client_id, secret, auth_method = _register(info["registration_endpoint"], redirect_uri) \
            if info["registration_endpoint"] else ("", "", "")
        if not client_id:
            raise OAuthError("сървърът за вход не позволява регистрация на клиент — "
                             "сложи токен в headers на mcp.json")
        verifier, challenge = _pkce()
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                  "code_challenge": challenge, "code_challenge_method": "S256",
                  "state": srv.state, "resource": info["resource"]}
        if info["scopes"]:
            params["scope"] = " ".join(str(s) for s in info["scopes"])
        sep = "&" if "?" in info["authorization_endpoint"] else "?"
        url = info["authorization_endpoint"] + sep + urlencode(params)
        if paste is not None:
            say("Отвори този адрес в браузър (на който и да е компютър) и влез:\n" + url)
            result = _from_pasted(paste(
                "После постави тук адреса, на който те прати браузърът "
                "(http://127.0.0.1:…/callback?code=…, страницата може да не се отвори): "))
            if not result:
                raise OAuthError("не е поставен адрес с код")
            if result.get("state") != srv.state:
                raise OAuthError("поставеният адрес не е от този вход (state не съвпада)")
        else:
            say(f"Вход в браузъра (ако не се отвори, копирай адреса):\n{url}")
            threading.Thread(target=open_browser or _open, args=(url,), daemon=True).start()
            deadline = time.monotonic() + timeout
            while not srv.result and time.monotonic() < deadline:
                srv.handle_request()
            result = srv.result
    finally:
        srv.server_close()
    if not result:
        raise OAuthError("входът не завърши навреме")
    if "code" not in result:
        why = result.get("error_description") or result.get("error") or "?"
        raise OAuthError(f"входът е отказан: {_plain(why)}")
    tokens = _token_request(info["token_endpoint"], {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier, "resource": info["resource"]},
        secret=secret, auth_method=auth_method)
    entry = _entry(info, client_id, tokens, secret=secret, auth_method=auth_method)
    try:
        with _FileLock():
            _save(server_url, entry)
    except OAuthError:
        # Файлът е зает над _LOCK_WAIT: токенът е издаден (кодът е похарчен) —
        # поне този процес го пази, вместо входът да се изхвърли (одит
        # 2026-10-31: изгубен токен след 60 s чакане).
        log.warning("токенът за %s остава само в паметта: файлът е зает", _key(server_url))
        with _lock:
            _memory[_key(server_url)] = dict(entry)
    return entry


def _open(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        log.debug("браузърът не се отвори", exc_info=True)


def _expired(entry: dict) -> bool:
    if not entry.get("expires_at"):
        return False
    # Отстъп до 60 s, но не повече от половината живот: при expires_in=30
    # всяка заявка подновяваше (одит 2026-10-09).
    skew = min(60.0, float(entry.get("lifetime") or 120) / 2)
    return float(entry["expires_at"]) - skew < time.time()


def _for_this_server(entry: dict, server_url: str) -> bool:
    return not entry.get("resource") or _covers(str(entry["resource"]), server_url)


def access_token(server_url: str, timeout: float | None = None) -> str:
    """Валиден токен за сървъра, подновен при нужда; "" — няма (нужен е вход).

    `timeout` (2026-10-31) — колко най-много да чака подновяването: срокът на
    извикващия (mcp_client._post). 0 — без подновяване, изтекъл токен се
    връща какъвто е: stop() затваря сесията с него, DELETE не чака сървъра за
    вход (stop_all() при atexit подновяваше по 15 s на сървър — измерено)."""
    entry = saved(server_url)
    if not entry or not entry.get("access_token") or not _for_this_server(entry, server_url):
        return ""
    if _expired(entry):
        if timeout is not None and timeout <= 0:
            return str(entry["access_token"])
        return refresh(server_url, stale=str(entry["access_token"]), timeout=timeout) or ""
    return str(entry["access_token"])


def refresh(server_url: str, stale: str = "", timeout: float | None = None) -> str | None:
    """Нов токен с refresh токена; None — не стана (нужен е нов вход).
    `stale` — токенът, който е отказан: ако друг процес/нишка вече го е
    подновил, ползваме неговия, вместо да харчим refresh токена втори път.
    `timeout` — общ срок за чакането на ключалката и заявката (None — както
    досега: ключалката докогато трябва, заявката _HTTP_TIMEOUT)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _FileLock(timeout):
        entry = saved(server_url)
        if not entry or not _for_this_server(entry, server_url):
            return None
        if stale and entry.get("access_token") and entry["access_token"] != stale \
                and not _expired(entry):
            return str(entry["access_token"])
        if not entry.get("refresh_token"):
            return None
        left = None if deadline is None else min(_HTTP_TIMEOUT, deadline - time.monotonic())
        if left is not None and left <= 0:
            return None
        secret = str(entry.get("client_secret") or "")
        auth_method = str(entry.get("token_endpoint_auth_method") or "")
        try:
            tokens = _token_request(_safe_url(entry["token_endpoint"], "адресът за токени"), {
                "grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                "client_id": entry.get("client_id", ""), "resource": entry.get("resource", "")},
                secret=secret, auth_method=auth_method, timeout=left)
        except OAuthError:
            return None
        new = _entry({"token_endpoint": entry["token_endpoint"],
                      "resource": entry.get("resource", ""), "issuer": entry.get("issuer", "")},
                     entry.get("client_id", ""), tokens, entry,
                     secret=secret, auth_method=auth_method)
        _save(server_url, new)
        return str(new["access_token"])
