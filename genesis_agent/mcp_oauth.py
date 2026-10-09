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
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

LOGIN_TIMEOUT = 300.0
_HTTP_TIMEOUT = 15


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
    умре — без „кражба“ по възраст, която пускаше двама вътре (втори одит)."""

    def __enter__(self) -> None:
        _lock.acquire()
        self.fh = None
        try:
            path = _store_path().with_suffix(".lock")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fh = open(path, "a+b")
            deadline = time.monotonic() + 60
            while True:
                try:
                    _lock_file(fh)
                    self.fh = fh
                    return
                except OSError:
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
    with _FileLock():
        _memory.pop(_key(url), None)
        data = _load_all()
        gone = data.pop(_key(url), None) is not None
        if gone:
            _save_all(data)
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


def _safe_url(url: str, what: str) -> str:
    """HTTPS, или http само към този компютър: токенът и кодът не тръгват
    по мрежата открито."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return url
    if parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return url
    raise OAuthError(f"{what}: {url!r} не е https — отказано")


def _get_json(url: str) -> dict | None:
    import requests
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"Accept": "application/json"},
                            allow_redirects=False)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
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
    tries = [*_well_known(issuer, "oauth-authorization-server"),
             *_well_known(issuer, "openid-configuration"),
             issuer.rstrip("/") + "/.well-known/openid-configuration"]
    for url in dict.fromkeys(tries):
        meta = _get_json(url)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
        meta = None
    if meta is None:
        raise OAuthError(f"{issuer} не казва как се влиза (няма метаданни за OAuth)")
    # RFC 8414 §3.3: метаданните са за същия сървър за вход, не за чужд.
    if str(meta.get("issuer") or issuer).rstrip("/") != issuer.rstrip("/"):
        raise OAuthError(f"метаданните са за {meta.get('issuer')!r}, а не за {issuer!r} — отказано")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise OAuthError("сървърът за вход не поддържа PKCE S256 — отказано")
    resource = str((resource_meta or {}).get("resource") or server_url)
    # RFC 9728 §3.3: метаданните трябва да са за ТОЗИ сървър. Иначе зъл сървър
    # сочи чужд (`resource` = жертвата) и получава нейния токен: операторът
    # вижда истинската страница за вход, а токенът отива при злия (одит
    # 2026-10-09, възпроизведено).
    if not _covers(resource, server_url):
        raise OAuthError(f"сървърът иска токен за {resource!r}, а е {server_url!r} — отказано")
    return {
        "issuer": issuer,
        "authorization_endpoint": _safe_url(meta["authorization_endpoint"], "адресът за вход"),
        "token_endpoint": _safe_url(meta["token_endpoint"], "адресът за токени"),
        "registration_endpoint": meta.get("registration_endpoint") or "",
        "scopes": (resource_meta or {}).get("scopes_supported") or [],
        "resource": str(resource),
    }


def _register(endpoint: str, redirect_uri: str) -> str:
    import requests
    _safe_url(endpoint, "регистрацията")
    try:
        resp = requests.post(endpoint, json={
            "client_name": "Genesis", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"},
            timeout=_HTTP_TIMEOUT, allow_redirects=False)
        data = resp.json() if resp.status_code in (200, 201) else {}
    except (requests.RequestException, ValueError) as e:
        raise OAuthError(f"регистрацията не стана: {e}") from e
    client_id = data.get("client_id") if isinstance(data, dict) else None
    if not client_id:
        raise OAuthError(f"регистрацията не стана (HTTP {resp.status_code})")
    return str(client_id)


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


def _token_request(endpoint: str, form: dict[str, str]) -> dict:
    import requests
    try:
        resp = requests.post(endpoint, data=form, timeout=_HTTP_TIMEOUT, allow_redirects=False,
                             headers={"Accept": "application/json"})
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise OAuthError(f"токенът не дойде: {e}") from e
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        err = data.get("error_description") or data.get("error") if isinstance(data, dict) else ""
        raise OAuthError(f"токенът не дойде (HTTP {resp.status_code}) {err or ''}".strip())
    return data


def _entry(info: dict, client_id: str, tokens: dict, old: dict | None = None) -> dict:
    try:
        lifetime = float(tokens.get("expires_in") or 0)   # и "3600" като низ
    except (TypeError, ValueError):
        lifetime = 0.0
    return {
        "access_token": str(tokens["access_token"]),
        "refresh_token": str(tokens.get("refresh_token") or (old or {}).get("refresh_token") or ""),
        "expires_at": time.time() + lifetime if lifetime > 0 else 0,
        "lifetime": lifetime,
        "client_id": client_id, "token_endpoint": info["token_endpoint"],
        "resource": info["resource"], "issuer": info["issuer"],
    }


def login(server_url: str, *, www_authenticate: str = "",
          open_browser: Callable[[str], Any] | None = None,
          say: Callable[[str], None] = print, timeout: float = LOGIN_TIMEOUT) -> dict:
    """Целият вход. `open_browser` по подразбиране е webbrowser.open; адресът
    се казва и на оператора — ако браузърът не се отвори, го копира сам."""
    info = discover(server_url, www_authenticate)
    srv = _callback_server()
    redirect_uri = f"http://127.0.0.1:{srv.server_address[1]}/callback"
    try:
        client_id = _register(info["registration_endpoint"], redirect_uri) \
            if info["registration_endpoint"] else ""
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
        raise OAuthError(f"входът е отказан: {result.get('error_description') or result.get('error') or '?'}")
    tokens = _token_request(info["token_endpoint"], {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier, "resource": info["resource"]})
    entry = _entry(info, client_id, tokens)
    with _FileLock():
        _save(server_url, entry)
    return entry


def _open(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _expired(entry: dict) -> bool:
    if not entry.get("expires_at"):
        return False
    # Отстъп до 60 s, но не повече от половината живот: при expires_in=30
    # всяка заявка подновяваше (одит 2026-10-09).
    skew = min(60.0, float(entry.get("lifetime") or 120) / 2)
    return float(entry["expires_at"]) - skew < time.time()


def _for_this_server(entry: dict, server_url: str) -> bool:
    return not entry.get("resource") or _covers(str(entry["resource"]), server_url)


def access_token(server_url: str) -> str:
    """Валиден токен за сървъра, подновен при нужда; "" — няма (нужен е вход)."""
    entry = saved(server_url)
    if not entry or not entry.get("access_token") or not _for_this_server(entry, server_url):
        return ""
    if _expired(entry):
        return refresh(server_url, stale=str(entry["access_token"])) or ""
    return str(entry["access_token"])


def refresh(server_url: str, stale: str = "") -> str | None:
    """Нов токен с refresh токена; None — не стана (нужен е нов вход).
    `stale` — токенът, който е отказан: ако друг процес/нишка вече го е
    подновил, ползваме неговия, вместо да харчим refresh токена втори път."""
    with _FileLock():
        entry = saved(server_url)
        if not entry or not _for_this_server(entry, server_url):
            return None
        if stale and entry.get("access_token") and entry["access_token"] != stale \
                and not _expired(entry):
            return str(entry["access_token"])
        if not entry.get("refresh_token"):
            return None
        try:
            tokens = _token_request(_safe_url(entry["token_endpoint"], "адресът за токени"), {
                "grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                "client_id": entry.get("client_id", ""), "resource": entry.get("resource", "")})
        except OAuthError:
            return None
        new = _entry({"token_endpoint": entry["token_endpoint"],
                      "resource": entry.get("resource", ""), "issuer": entry.get("issuer", "")},
                     entry.get("client_id", ""), tokens, entry)
        _save(server_url, new)
        return str(new["access_token"])
