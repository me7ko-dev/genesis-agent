"""A tiny MCP server over Streamable HTTP for the tests: JSON or SSE replies,
a session id from initialize, an optional bearer token, and a ping from the
server in the middle of an SSE reply."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
]


def serve(mode: str = "json", token: str = ""):
    state = {"session": "sess-42", "pings_answered": 0, "deleted": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, body=b"", ctype="application/json", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            state["deleted"] = True
            self._reply(200)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length))
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self._reply(401, b'{"error":"unauthorized"}')
            method, rid = msg.get("method"), msg.get("id")
            if method != "initialize" and self.headers.get("Mcp-Session-Id") != state["session"]:
                if "result" in msg or method is None:  # отговор на нашия ping
                    pass
                else:
                    return self._reply(404, b"no session")
            if "result" in msg and msg.get("id") == "srv-ping":
                state["pings_answered"] += 1
                return self._reply(202)
            if rid is None:
                return self._reply(202)
            if method == "initialize":
                result = {"protocolVersion": msg["params"]["protocolVersion"],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-http"}}
                return self._reply(200, json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode(),
                                   extra={"Mcp-Session-Id": state["session"]})
            if method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                args = msg["params"].get("arguments") or {}
                result = {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]}
            else:
                return self._reply(200, json.dumps({"jsonrpc": "2.0", "id": rid,
                                                    "error": {"code": -32601, "message": "no"}}).encode())
            reply = {"jsonrpc": "2.0", "id": rid, "result": result}
            if mode == "sse":
                events = [{"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                          {"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"}, reply]
                body = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                return self._reply(200, body, ctype="text/event-stream")
            return self._reply(200, json.dumps(reply).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/mcp", httpd, state
