"""
genesis_agent.mcp_client — tools from MCP servers (Model Context Protocol), the
way Claude Code uses them: GitHub, databases, Slack, a browser, anything that
ships an MCP server.

    ~/.genesis/mcp.json      — the operator's servers, every project
    <project>/.mcp.json      — the project's (Claude Code's file, same format);
                               only after `/mcp trust`, pinned to its content:
                               starting a server is running a program.

    {"mcpServers": {
      "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_TOKEN": "..."}},
      "db": {"command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "app.db"],
             "autoApprove": ["read_query", "list_tables"]}
    }}

Only the stdio transport (a local program speaking JSON-RPC on stdin/stdout)
— what nearly every server offers; no extra dependency.

The model sees each tool as `mcp__<server>__<tool>` (native tool calling) or
`[MCP: server.tool | {"arg": 1}]` (text tags). A tool the server marks
read-only (annotations.readOnlyHint) or the operator lists in `autoApprove`
runs at once; any other asks like a risky command does (the sandbox's
confirm: the terminal asks, autonomous mode refuses) — an MCP tool acts on an
outside system (creates issues, writes rows), and the agent must not do that
unasked. In plan mode only the read-only ones are offered.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
_START_TIMEOUT = 20.0
_CALL_TIMEOUT = 120.0
_MAX_RESULT = 20000
_MAX_DESCRIPTION = 800
_NAME_OK = re.compile(r"[^A-Za-z0-9_-]")


class MCPError(RuntimeError):
    pass


@dataclass
class Tool:
    server: str
    name: str
    description: str
    schema: dict
    read_only: bool
    # `mcp__server__tool` — уникално за сесията (виж _assign_names): `get.item` и
    # `get_item` ставаха едно и също име и вторият инструмент беше недостъпен.
    qualified: str = ""


def _safe(name: str) -> str:
    return _NAME_OK.sub("_", name)


# Средата на MCP сървъра: колкото да тръгне (като MCP SDK-тата), без API
# ключовете на Genesis — сървърът получава своите от `env` в конфигурацията
# (одит 2026-10-08: всеки сървър виждаше ANTHROPIC/OPENAI/AWS ключовете).
_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
             "TMPDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME",
             "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
             "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "PROCESSOR_ARCHITECTURE",
             "WINDIR", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
_MAX_LINE = 8_000_000


def _server_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(extra)
    return env


def _resolve_command(command: str, env: dict[str, str], forbidden: list[Path]) -> str:
    """The program to start, found ONLY on PATH — never in the current folder.

    On Windows shutil.which looks in the current directory first: a cloned
    repository with its own `npx.cmd` replaced the operator's `npx` and ran
    as the chat started (audit 2026-10-08). PATH entries inside the project
    are skipped for the same reason."""
    if os.path.isabs(command):
        return command
    if os.sep in command or (os.altsep and os.altsep in command):
        raise MCPError(f"относителен път към програма ({command}) — дай абсолютен път")
    exts = [""]
    if os.name == "nt":
        exts = [""] + [e.lower() for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for folder in env.get("PATH", "").split(os.pathsep):
        if not folder or folder in (".", "./"):
            continue
        try:
            real_folder = Path(folder).resolve()
        except OSError:
            continue
        if any(real_folder == f or f in real_folder.parents for f in forbidden):
            continue
        for ext in exts:
            candidate = Path(folder) / (command + ext)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    raise MCPError(f"„{command}“ не е намерен в PATH")


@dataclass
class Server:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    auto_approve: set[str]
    source: Path
    proc: subprocess.Popen | None = None
    tools: list[Tool] = field(default_factory=list)
    error: str = ""
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _inbox: queue.Queue = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── transport ─────────────────────────────────────────────────────────
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, forbidden: list[Path] | None = None) -> None:
        env = _server_env(self.env)
        exe = _resolve_command(self.command, env, forbidden or [])
        try:
            self.proc = subprocess.Popen(
                [exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=str(_workspace()),
                start_new_session=(os.name == "posix"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as e:
            raise MCPError(f"не тръгна ({self.command}): {e}") from e
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # Целият старт (initialize + tools/list) — в един срок: сървър, който
        # праща известия всяка секунда, иначе никога не изтичаше и чатът не
        # тръгваше (одит 2026-10-08).
        deadline = time.monotonic() + _START_TIMEOUT
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "genesis", "version": _version()}}, deadline=deadline)
        self.notify("notifications/initialized")
        self.tools = self._list_tools(deadline)

    def _read(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        while True:
            raw = proc.stdout.readline(_MAX_LINE)
            if not raw:
                break
            if not raw.endswith(b"\n") and len(raw) >= _MAX_LINE:
                while True:  # прекалено дълъг ред — изхвърля се до края му
                    rest = proc.stdout.readline(_MAX_LINE)
                    if not rest or rest.endswith(b"\n"):
                        break
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # сървър, който печата лог в stdout — пропускаме реда
            if isinstance(msg, dict) and "method" in msg and "id" in msg:
                self._answer_server_request(msg)
            elif isinstance(msg, dict):
                self._inbox.put(msg)
        self._inbox.put({"_closed": True})

    def _drain_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        for _ in proc.stderr:  # иначе пълен буфер спира сървъра
            pass

    def _send(self, msg: dict, timeout: float = 10.0) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPError("сървърът не работи")
        # Само ASCII (кирилицата като \uXXXX): сървър на Windows, който чете
        # stdin с cp1252, иначе получаваше „Ð·Ð´…“ вместо „здравей“ (CI, 2026-10-08).
        data = (json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii")
        failed: list[BaseException] = []

        def write() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                failed.append(e)
        # Записът в нишка със срок: сървър, който не чете stdin, блокираше
        # write() завинаги — и с него всяко следващо извикване (одит 2026-10-08).
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(max(0.1, timeout))
        if writer.is_alive():
            self.error = "не чете входа си — спрян"
            self.stop()
            raise MCPError(self.error)
        if failed:
            raise MCPError(f"връзката прекъсна: {failed[0]}")

    def _answer_server_request(self, msg: dict) -> None:
        """Сървърът пита клиента (ping, roots/list…): отговаряме, за да не чака."""
        try:
            self._send(_reply_for(msg))
        except MCPError:
            pass

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None, deadline: float | None = None) -> dict:
        if deadline is None:
            deadline = time.monotonic() + (_CALL_TIMEOUT if timeout is None else timeout)
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise MCPError(f"сървърът е зает с друго извикване ({method})")
        try:
            rid = next(self._ids)
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                       timeout=max(0.1, deadline - time.monotonic()))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MCPError(f"няма отговор навреме ({method})")
                try:
                    msg = self._inbox.get(timeout=left)
                except queue.Empty:
                    raise MCPError(f"няма отговор навреме ({method})") from None
                if msg.get("_closed"):
                    raise MCPError("сървърът спря")
                if msg.get("id") != rid:
                    continue  # известие или закъснял отговор
                if "error" in msg:
                    err = msg["error"]
                    text = err.get("message") if isinstance(err, dict) else None
                    raise MCPError(str(text or err)[:500])
                result = msg.get("result")
                return result if isinstance(result, dict) else {}
        finally:
            self._lock.release()

    def _list_tools(self, deadline: float) -> list[Tool]:
        tools: list[Tool] = []
        cursor = None
        for _ in range(20):  # страници
            result = self.request("tools/list", {"cursor": cursor} if cursor else {},
                                  deadline=deadline)
            items = result.get("tools")
            for t in items if isinstance(items, list) else []:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                notes = t.get("annotations")
                schema = t.get("inputSchema")
                if not isinstance(schema, dict) or not isinstance(schema.get("properties", {}), dict):
                    schema = {"type": "object", "properties": {}}
                tools.append(Tool(
                    server=self.name, name=str(t["name"]),
                    description=" ".join(str(t.get("description") or "").split())[:_MAX_DESCRIPTION],
                    schema=schema,
                    read_only=bool(notes.get("readOnlyHint")) if isinstance(notes, dict) else False))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def stop(self) -> None:
        """Спира сървъра с цялото му дърво (npx → node): само прекият процес
        оставяше истинския сървър сирак и нишките му живи (одит 2026-10-08)."""
        proc, self.proc = self.proc, None
        if proc is None:
            return

        def close_stdin() -> None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
        # close() чака блокиран write() в друга нишка (сървър, който не чете) —
        # в своя нишка и със срок; убиването по-долу го освобождава.
        closer = threading.Thread(target=close_stdin, daemon=True)
        closer.start()
        closer.join(1)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        from genesis_agent import sandbox
        sandbox.stop_process(proc)
        if sys.platform != "win32" and proc.returncode is not None:
            try:  # децата в групата, ако прекият процес вече е излязъл
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


def _reply_for(msg: dict) -> dict:
    if msg.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    if msg.get("method") == "roots/list":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": [
            {"uri": _workspace().resolve().as_uri(), "name": _workspace().name}]}}
    return {"jsonrpc": "2.0", "id": msg["id"],
            "error": {"code": -32601, "message": "not supported by Genesis"}}


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    """`${GITHUB_TOKEN}` / `${PORT:-8080}` от средата — както .mcp.json на Claude Code."""
    return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)


def _static_auth(headers: dict[str, str]) -> bool:
    """mcp.json вече дава вход (Authorization, X-API-Key, Cookie, …-Token)? Тогава
    401 значи „провери headers“; иначе — OAuth. `X-Tenant` не е вход (одити)."""
    # Цели думи от името (X-Api-Key → x, api, key), не поднизове: X-Session-Id,
    # X-Bypass-Cache, X-Compass-Region не са вход (одит 2026-10-09).
    login = {"authorization", "auth", "cookie", "key", "apikey", "token", "secret",
             "password", "passwd", "credential", "credentials", "signature", "bearer"}
    for name in headers:
        parts = set(re.split(r"[-_\s]+", name.lower()))
        if parts & login or ("session" in parts and parts & {"token", "key", "cookie"}):
            return True
    return False


@dataclass
class HttpServer(Server):
    """MCP по HTTP (Streamable HTTP, 2025-06-18): всяко съобщение е POST към
    един адрес; отговорът е JSON или SSE поток, в който може да има и въпроси
    от сървъра (ping) преди самия отговор. Сесията идва в `Mcp-Session-Id` от
    initialize и се праща при всяка следваща заявка. Хостнатите сървъри
    (GitHub и др.) са такива — stdio е само за локални програми."""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    _ready: bool = False
    # 401 без токен: какво каза сървърът (за /mcp login — mcp_oauth.discover).
    www_authenticate: str = ""
    needs_login: bool = False

    def alive(self) -> bool:
        return self._ready and not self.error

    def start(self, forbidden: list[Path] | None = None) -> None:
        deadline = time.monotonic() + _START_TIMEOUT
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "genesis", "version": _version()}}, deadline=deadline)
        self._ready = True
        self.notify("notifications/initialized")
        self.tools = self._list_tools(deadline)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             **{k: _expand(v) for k, v in self.headers.items()}}
        # Токен от /mcp login (mcp_oauth) — освен ако mcp.json не дава свой.
        if not any(k.lower() == "authorization" for k in h):
            from genesis_agent import mcp_oauth
            try:
                token = mcp_oauth.access_token(_expand(self.url))
            except Exception:   # „никога не хвърля“ — без токен, сървърът ще каже 401
                token = ""
            if token:
                h["Authorization"] = f"Bearer {token}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self._ready:
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return h

    def _post(self, msg: dict, deadline: float, *, retried: bool = False):
        import requests
        left = max(0.5, deadline - time.monotonic())
        headers = self._headers()
        try:
            resp = requests.post(_expand(self.url), data=json.dumps(msg, ensure_ascii=True),
                                 headers=headers, timeout=(min(10.0, left), left),
                                 stream=True)
        except requests.RequestException as e:
            raise MCPError(f"не се свърза: {e}") from e
        challenge = resp.headers.get("WWW-Authenticate", "")
        # OAuth (2026-10-09) само ако mcp.json не дава свои headers: с X-API-Key
        # „/mcp login“ беше грешен съвет (одит) — там остава „провери headers“.
        if resp.status_code == 401 and not _static_auth(self.headers):
            self.www_authenticate = challenge
            resp.close()
            from genesis_agent import mcp_oauth
            try:
                fresh = (not retried and "Authorization" in headers
                         and mcp_oauth.refresh(_expand(self.url),
                                               stale=headers["Authorization"][7:]))
            except Exception:
                fresh = None
            if fresh:
                return self._post(msg, deadline, retried=True)
            self.needs_login = True
            raise MCPError(f"нужен е вход — /mcp login {self.name}")
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code == 404 and self.session_id and msg.get("method") != "initialize":
            resp.close()
            self.error = "сесията изтече — /mcp restart"
            raise MCPError(self.error)
        if resp.status_code in (401, 403):
            resp.close()
            login = (f" (или ги махни и /mcp login {self.name})"
                     if challenge.lower().startswith("bearer") else "")
            raise MCPError(f"HTTP {resp.status_code} — провери headers (токена) в mcp.json{login}")
        if resp.status_code >= 400:
            body = resp.text[:300]
            resp.close()
            raise MCPError(f"HTTP {resp.status_code}: {body}")
        return resp

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        self._post(msg, time.monotonic() + 15).close()

    def _messages(self, resp, deadline: float):
        """JSON-RPC съобщенията от отговора: един JSON (или списък), или SSE поток."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" not in ctype:
            try:
                data = json.loads(resp.content.decode("utf-8", errors="replace") or "null")
            except ValueError:
                return
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict):
                    yield item
            return
        buf: list[str] = []
        for raw in resp.iter_lines(decode_unicode=False):
            if time.monotonic() > deadline:
                raise MCPError("няма отговор навреме")
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
            elif not line.strip() and buf:
                try:
                    item = json.loads("\n".join(buf))
                except ValueError:
                    item = None
                buf = []
                if isinstance(item, dict):
                    yield item

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None, deadline: float | None = None) -> dict:
        if deadline is None:
            deadline = time.monotonic() + (_CALL_TIMEOUT if timeout is None else timeout)
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise MCPError(f"сървърът е зает с друго извикване ({method})")
        try:
            rid = next(self._ids)
            resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                               "params": params or {}}, deadline)
            try:
                for msg in self._messages(resp, deadline):
                    if "method" in msg and "id" in msg:   # въпрос от сървъра посред отговора
                        try:
                            self._post(_reply_for(msg), deadline).close()
                        except MCPError:
                            pass
                        continue
                    if msg.get("id") != rid:
                        continue
                    if "error" in msg:
                        err = msg["error"]
                        text = err.get("message") if isinstance(err, dict) else None
                        raise MCPError(str(text or err)[:500])
                    result = msg.get("result")
                    return result if isinstance(result, dict) else {}
            finally:
                resp.close()
            raise MCPError(f"сървърът не върна отговор ({method})")
        finally:
            self._lock.release()

    def stop(self) -> None:
        if self._ready and self.session_id:
            try:
                import requests
                requests.delete(_expand(self.url), headers=self._headers(), timeout=3)
            except Exception:
                pass
        self._ready = False


# ── registry ──────────────────────────────────────────────────────────────

_servers: dict[str, Server] = {}
_started = False
workspace: Path | None = None


def _workspace() -> Path:
    if workspace is not None:
        return Path(workspace)
    try:
        import genesis_skills
        return Path(genesis_skills._WORKSPACE)
    except Exception:
        return Path.cwd()


def _version() -> str:
    try:
        from genesis_agent.version_info import __version__  # type: ignore[attr-defined]
        return str(__version__)
    except Exception:
        return "dev"


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


def project_file(ws: Path | None = None) -> Path:
    from genesis_agent.project_instructions import _project_root
    return _project_root(Path(ws or _workspace()).resolve()) / ".mcp.json"


def _parse(path: Path) -> list[Server]:
    """Servers from one file. Every field is checked: one wrong value
    (`"autoApprove": true`, `"args": "foo"`) used to raise and silently
    switch off every server (audit 2026-10-08)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        return [Server(name=path.name, command="", args=[], env={}, auto_approve=set(),
                       source=path, error=f"{path} не се чете: {e}")]
    table = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    out = []
    for name, spec in (table.items() if isinstance(table, dict) else []):
        if not isinstance(spec, dict) or spec.get("disabled") is True:
            continue

        def bad(why: str, name: str = str(name)) -> Server:
            return Server(name=name, command="", args=[], env={}, auto_approve=set(),
                          source=path, error=why)
        kind = str(spec.get("type") or ("http" if spec.get("url") and not spec.get("command")
                                        else "stdio")).lower()
        if kind == "sse":
            out.append(bad("SSE е старият транспорт — сървърът почти сигурно дава и "
                           "\"type\": \"http\" на същия адрес (обикновено …/mcp)"))
            continue
        if kind in ("http", "streamable-http", "streamable_http"):
            url, headers = spec.get("url"), spec.get("headers", {})
            if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
                out.append(bad("url трябва да е http(s) адрес"))
                continue
            if not isinstance(headers, dict):
                out.append(bad("headers трябва да е обект"))
                continue
            approve = spec.get("autoApprove", spec.get("alwaysAllow", []))
            if not isinstance(approve, list):
                out.append(bad("autoApprove трябва да е списък с имена на инструменти"))
                continue
            out.append(HttpServer(str(name), "", [], {}, {str(x) for x in approve}, path,
                                  url=url.strip(),
                                  headers={str(k): str(v) for k, v in headers.items()}))
            continue
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            out.append(bad("няма command (stdio) или url (\"type\": \"http\")"))
            continue
        args = spec.get("args", [])
        if isinstance(args, str):
            args = [args]
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
            out.append(bad("args трябва да е списък от низове"))
            continue
        env = spec.get("env", {})
        if not isinstance(env, dict):
            out.append(bad("env трябва да е обект"))
            continue
        approve = spec.get("autoApprove", spec.get("alwaysAllow", []))
        if not isinstance(approve, list):
            out.append(bad("autoApprove трябва да е списък с имена на инструменти"))
            continue
        out.append(Server(str(name), command.strip(), [str(a) for a in args],
                          {str(k): str(v) for k, v in env.items()},
                          {str(x) for x in approve}, path))
    return out


def configured(ws: Path | None = None) -> tuple[list[Server], Path | None]:
    """The servers in force (not started), and the project file when it is not trusted."""
    from genesis_agent import hooks
    servers = _parse(_home() / "mcp.json")
    project = project_file(ws)
    untrusted = None
    if project.is_file():
        from genesis_agent.project_instructions import _project_root
        root = _project_root(Path(ws or _workspace()).resolve())
        if hooks.stays_in_project(project, root) and hooks.is_trusted(project):
            names = {s.name for s in servers}
            servers += [s for s in _parse(project) if s.name not in names]
        else:
            untrusted = project
    return servers, untrusted


last_start: list[str] = []


def start_all(ws: Path | None = None) -> list[str]:
    """Start every configured server, all at once (a dead server cost every
    chat start its 20 s, one after another — audit 2026-10-08); returns lines
    for the operator."""
    global _started, last_start
    stop_all()
    _started = True
    lines: list[str] = []
    servers, untrusted = configured(ws)
    from genesis_agent.project_instructions import _project_root
    root = _project_root(Path(ws or _workspace()).resolve())
    forbidden = [root, Path(ws or _workspace()).resolve()]

    def run(srv: Server) -> None:
        try:
            # Програма от проекта — само ако сървърът е от доверения .mcp.json.
            srv.start([] if srv.source == project_file(ws) else forbidden)
        except Exception as e:  # всеки сървър сам за себе си
            srv.error = str(e) or type(e).__name__
            srv.stop()

    threads = []
    for srv in servers:
        _servers[srv.name] = srv
        if not srv.error:
            t = threading.Thread(target=run, args=(srv,), daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join(_START_TIMEOUT + 15)
    _assign_names()
    for srv in servers:
        if srv.error:
            lines.append(f"⚠ MCP {srv.name}: {srv.error}")
        else:
            lines.append(f"🔌 MCP {srv.name}: {len(srv.tools)} инструмента")
    if untrusted:
        lines.append(f"⚠ {untrusted} има MCP сървъри, но не е доверен — прегледай го и /mcp trust.")
    last_start = lines
    return lines


_by_name: dict[str, tuple[Server, Tool]] = {}
_MAX_SCHEMAS = 80  # заедно с вградените под тавана от 128 инструмента на OpenAI-съвместимите


def _assign_names() -> None:
    """Unique `mcp__server__tool` names within 64 characters."""
    _by_name.clear()
    import hashlib
    for srv in _servers.values():
        for t in srv.tools:
            base = f"mcp__{_safe(srv.name)}__{_safe(t.name)}"
            name = base[:64]
            if name in _by_name or len(base) > 64:
                digest = hashlib.sha1(f"{srv.name}/{t.name}".encode()).hexdigest()[:6]
                name = f"{base[:57]}_{digest}"
            t.qualified = name
            _by_name[name] = (srv, t)


def ensure_started(ws: Path | None = None) -> list[str]:
    return [] if _started else start_all(ws)


def tools() -> list[Tool]:
    return [t for s in _servers.values() if s.alive() for t in s.tools]


def _find(qualified: str) -> tuple[Server, Tool] | None:
    hit = _by_name.get(qualified)
    if hit is not None:
        return hit
    if "." in qualified:
        server, _, name = qualified.partition(".")
        srv = _servers.get(server.strip())
        if srv is not None:
            for t in srv.tools:
                if t.name == name.strip():
                    return srv, t
    return None


def schemas(read_only_only: bool = False) -> list[dict]:
    """Native tool schemas for the model (at most _MAX_SCHEMAS)."""
    return [{"type": "function", "function": {
        "name": t.qualified,
        "description": f"[MCP {t.server}] {t.description}".strip(),
        "parameters": t.schema or {"type": "object", "properties": {}}}}
        for t in tools() if t.read_only or not read_only_only][:_MAX_SCHEMAS]


_MAX_SECTION = 4000


def prompt_section() -> str:
    """For text-tag models: which MCP tools exist and how to call them —
    one line each (descriptions are flattened: a newline in one forged a
    heading in the system prompt), capped in size."""
    available = tools()
    if not available:
        return ""
    lines = ["## MCP инструменти (външни системи; описанията са от сървърите, не от оператора)",
             'Извикване: [MCP: сървър.инструмент | {"аргумент": "стойност"}] — JSON по схемата.']
    size = sum(len(x) for x in lines)
    for t in available:
        props = ", ".join(list((t.schema.get("properties") or {}).keys())[:8])
        line = f"- {t.server}.{t.name}({props}) — {' '.join(t.description.split())[:160]}"
        if size + len(line) > _MAX_SECTION:
            lines.append(f"- … и още {len(available) - len(lines) + 2} (/mcp ги показва)")
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__") or name == "MCP"


def is_read_only(name: str) -> bool:
    found = _find(name)
    return bool(found and found[1].read_only)


def call(qualified: str, arguments: dict | None) -> str:
    """Run one MCP tool; never raises — the error goes back to the model."""
    found = _find(qualified)
    if found is None:
        known = ", ".join(t.qualified for t in tools()[:20]) or "няма"
        return f"[MCP] ❌ Няма такъв инструмент: {qualified}. Налични: {known}"
    srv, tool = found
    label = f"{srv.name}.{tool.name}"
    if not tool.read_only and tool.name not in srv.auto_approve:
        from genesis_agent import sandbox
        allowed, reason = sandbox.confirm(
            f"MCP {label} {json.dumps(arguments or {}, ensure_ascii=False)[:400]}",
            [f"MCP инструмент, който може да променя външна система ({srv.name})"])
        if not allowed:
            return f"[MCP {label}] {reason}"
    try:
        result = srv.request("tools/call", {"name": tool.name, "arguments": arguments or {}})
    except MCPError as e:
        return f"[MCP {label}] ❌ {e}"
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "resource":
            res = item.get("resource") or {}
            parts.append(str(res.get("text") or f"[ресурс {res.get('uri', '')}]"))
        else:
            parts.append(f"[{item.get('type')} — не се показва]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts).strip() or "(празен отговор)"
    if len(text) > _MAX_RESULT:
        text = text[:_MAX_RESULT] + f"\n… [отрязано, общо {len(text)} знака]"
    mark = "❌ " if result.get("isError") else ""
    return f"[MCP {label}] {mark}{text}"


def call_text_tag(arg: str) -> str:
    """`server.tool | {json}` from a text tag."""
    head, _, raw = arg.partition("|")
    head = head.strip()
    args: dict = {}
    if raw.strip():
        try:
            from genesis_agent.tool_schemas import load_tool_arguments
            args = load_tool_arguments(raw.strip())
        except (ValueError, TypeError):
            return f"[MCP {head}] ❌ Аргументите не са валиден JSON: {raw.strip()[:200]}"
    return call(head, args)


def summary() -> str:
    if not _servers:
        return ("Няма MCP сървъри. Опиши ги в ~/.genesis/mcp.json "
                '({"mcpServers": {"име": {"command": "...", "args": [...]}}}) или в .mcp.json '
                "на проекта (иска /mcp trust), после /mcp restart.")
    lines = []
    for s in _servers.values():
        state = f"❌ {s.error}" if s.error else ("✓ върви" if s.alive() else "спрян")
        lines.append(f"{s.name}: {state}, {len(s.tools)} инструмента ({s.source})")
        for t in s.tools[:15]:
            mark = "👁" if t.read_only else ("✓" if t.name in s.auto_approve else "?")
            lines.append(f"   {mark} {t.name} — {t.description[:90]}")
    lines.append("👁 само чете · ✓ без питане (autoApprove) · ? пита преди изпълнение")
    return "\n".join(lines)


@atexit.register
def stop_all() -> None:
    global _started
    for srv in list(_servers.values()):
        srv.stop()
    _servers.clear()
    _by_name.clear()
    _started = False
