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
import shutil
import subprocess
import threading
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

    @property
    def qualified(self) -> str:
        """`mcp__server__tool`, within OpenAI's 64-character name limit."""
        return f"mcp__{_safe(self.server)}__{_safe(self.name)}"[:64]


def _safe(name: str) -> str:
    return _NAME_OK.sub("_", name)


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
    def start(self) -> None:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.env.items()})
        # `npx` на Windows е npx.cmd — Popen със списък не го намира сам.
        exe = shutil.which(self.command, path=env.get("PATH")) or self.command
        try:
            self.proc = subprocess.Popen(
                [exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=str(_workspace()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise MCPError(f"не тръгна ({self.command}): {e}") from e
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "genesis", "version": _version()}}, timeout=_START_TIMEOUT)
        self.notify("notifications/initialized")
        self.tools = self._list_tools()

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
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
        assert self.proc is not None and self.proc.stderr is not None
        for _ in self.proc.stderr:  # иначе пълен буфер спира сървъра
            pass

    def _send(self, msg: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise MCPError("сървърът не работи")
        # Само ASCII (кирилицата като \uXXXX): сървър на Windows, който чете
        # stdin с cp1252, иначе получаваше „Ð·Ð´…“ вместо „здравей“ (CI, 2026-10-08).
        data = (json.dumps(msg, ensure_ascii=True) + "\n").encode("ascii")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except OSError as e:
            raise MCPError(f"връзката прекъсна: {e}") from e

    def _answer_server_request(self, msg: dict) -> None:
        """Сървърът пита клиента (ping, roots/list…): отговаряме, за да не чака."""
        if msg.get("method") == "ping":
            reply: dict = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        elif msg.get("method") == "roots/list":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": [
                {"uri": _workspace().resolve().as_uri(), "name": _workspace().name}]}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": "not supported by Genesis"}}
        try:
            self._send(reply)
        except MCPError:
            pass

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict | None = None,
                timeout: float = _CALL_TIMEOUT) -> dict:
        with self._lock:
            rid = next(self._ids)
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            while True:
                try:
                    msg = self._inbox.get(timeout=timeout)
                except queue.Empty:
                    raise MCPError(f"няма отговор за {timeout:.0f} s ({method})") from None
                if msg.get("_closed"):
                    raise MCPError("сървърът спря")
                if msg.get("id") != rid:
                    continue  # известие или закъснял отговор
                if "error" in msg:
                    err = msg["error"] or {}
                    raise MCPError(str(err.get("message") or err)[:500])
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

    def _list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        cursor = None
        for _ in range(20):  # страници
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            for t in result.get("tools") or []:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                notes = t.get("annotations") or {}
                schema = t.get("inputSchema")
                tools.append(Tool(
                    server=self.name, name=str(t["name"]),
                    description=str(t.get("description") or "")[:_MAX_DESCRIPTION],
                    schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                    read_only=bool(notes.get("readOnlyHint")) if isinstance(notes, dict) else False))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.proc = None


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
        if not isinstance(spec, dict):
            continue
        if spec.get("disabled"):
            continue
        command = str(spec.get("command") or "")
        if not command:
            out.append(Server(name=str(name), command="", args=[], env={}, auto_approve=set(),
                              source=path, error="няма command (само stdio сървъри се поддържат)"))
            continue
        args = [str(a) for a in spec.get("args") or [] if isinstance(a, (str, int, float))]
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()} \
            if isinstance(spec.get("env"), dict) else {}
        approve = {str(x) for x in spec.get("autoApprove") or spec.get("alwaysAllow") or []}
        out.append(Server(str(name), command, args, env, approve, path))
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
    """Start every configured server once; returns lines for the operator."""
    global _started, last_start
    stop_all()
    _started = True
    lines = []
    servers, untrusted = configured(ws)
    for srv in servers:
        _servers[srv.name] = srv
        if srv.error:
            lines.append(f"⚠ MCP {srv.name}: {srv.error}")
            continue
        try:
            srv.start()
            lines.append(f"🔌 MCP {srv.name}: {len(srv.tools)} инструмента")
        except MCPError as e:
            srv.error = str(e)
            srv.stop()
            lines.append(f"⚠ MCP {srv.name}: {e}")
    if untrusted:
        lines.append(f"⚠ {untrusted} има MCP сървъри, но не е доверен — прегледай го и /mcp trust.")
    last_start = lines
    return lines


def ensure_started(ws: Path | None = None) -> list[str]:
    return [] if _started else start_all(ws)


def tools() -> list[Tool]:
    return [t for s in _servers.values() if s.proc is not None for t in s.tools]


def _find(qualified: str) -> tuple[Server, Tool] | None:
    for srv in _servers.values():
        for t in srv.tools:
            if t.qualified == qualified or f"{srv.name}.{t.name}" == qualified:
                return srv, t
    return None


def schemas(read_only_only: bool = False) -> list[dict]:
    """Native tool schemas for the model."""
    return [{"type": "function", "function": {
        "name": t.qualified,
        "description": f"[MCP {t.server}] {t.description}".strip(),
        "parameters": t.schema or {"type": "object", "properties": {}}}}
        for t in tools() if t.read_only or not read_only_only]


def prompt_section() -> str:
    """For text-tag models: which MCP tools exist and how to call them."""
    available = tools()
    if not available:
        return ""
    lines = ["## MCP инструменти (външни системи)",
             'Извикване: [MCP: сървър.инструмент | {"аргумент": "стойност"}] — JSON по схемата.']
    for t in available[:60]:
        props = ", ".join(list((t.schema.get("properties") or {}).keys())[:8])
        lines.append(f"- {t.server}.{t.name}({props}) — {t.description[:160]}")
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
        state = f"❌ {s.error}" if s.error else ("✓ върви" if s.proc else "спрян")
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
    _started = False
