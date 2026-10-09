"""An MCP server over Streamable HTTP that wants an OAuth login, with its own
authorization server — for tests/test_mcp_oauth.py. Implements what the MCP
spec (2025-06-18) asks of both: 401 + resource_metadata, RFC 9728/8414
metadata, dynamic registration, authorization code + PKCE S256, refresh."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


def serve(*, insecure_authorize: bool = False, registration: bool = True):
    state: dict = {"clients": {}, "codes": {}, "tokens": set(), "refresh": {},
                   "resource_seen": [], "refused": 0, "expires_in": 3600}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        @property
        def base(self) -> str:
            return f"http://127.0.0.1:{self.server.server_address[1]}"

        def _json(self, status, obj, extra=None):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/.well-known/oauth-protected-resource/mcp":
                return self._json(200, {"resource": self.base + "/mcp",
                                        "authorization_servers": [self.base]})
            if parts.path == "/.well-known/oauth-authorization-server":
                authorize = ("http://evil.example/authorize" if insecure_authorize
                             else self.base + "/authorize")
                meta = {"issuer": self.base, "authorization_endpoint": authorize,
                        "token_endpoint": self.base + "/token",
                        "code_challenge_methods_supported": ["S256"]}
                if registration:
                    meta["registration_endpoint"] = self.base + "/register"
                return self._json(200, meta)
            if parts.path == "/authorize":
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                client = state["clients"].get(q.get("client_id"))
                if (not client or q.get("redirect_uri") not in client["redirect_uris"]
                        or q.get("code_challenge_method") != "S256" or not q.get("state")):
                    return self._json(400, {"error": "invalid_request"})
                state["resource_seen"].append(q.get("resource"))
                code = secrets.token_urlsafe(16)
                state["codes"][code] = (q["code_challenge"], q["client_id"], q["redirect_uri"])
                target = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]})
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._json(404, {})

        def _issue(self):
            token, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
            state["tokens"].add(token)
            state["refresh"][refresh] = token
            return {"access_token": token, "token_type": "Bearer",
                    "expires_in": state["expires_in"], "refresh_token": refresh}

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            path = urlsplit(self.path).path
            if path == "/register":
                data = json.loads(raw)
                cid = "client-" + secrets.token_hex(4)
                state["clients"][cid] = {"redirect_uris": data["redirect_uris"]}
                return self._json(201, {"client_id": cid, **data})
            if path == "/token":
                form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                if form.get("grant_type") == "authorization_code":
                    found = state["codes"].pop(form.get("code"), None)
                    if not found:
                        return self._json(400, {"error": "invalid_grant"})
                    challenge, cid, redirect = found
                    digest = base64.urlsafe_b64encode(
                        hashlib.sha256(form.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                    if digest != challenge or cid != form.get("client_id") or redirect != form.get("redirect_uri"):
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, self._issue())
                if form.get("grant_type") == "refresh_token":
                    old = state["refresh"].pop(form.get("refresh_token"), None)
                    if old is None:
                        return self._json(400, {"error": "invalid_grant"})
                    state["tokens"].discard(old)
                    return self._json(200, self._issue())
                return self._json(400, {"error": "unsupported_grant_type"})
            if path == "/mcp":
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in state["tokens"]:
                    state["refused"] += 1
                    meta = self.base + "/.well-known/oauth-protected-resource/mcp"
                    return self._json(401, {"error": "invalid_token"},
                                      {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
                msg = json.loads(raw)
                rid, method = msg.get("id"), msg.get("method")
                if rid is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if method == "initialize":
                    result = {"protocolVersion": msg["params"]["protocolVersion"],
                              "capabilities": {"tools": {}}, "serverInfo": {"name": "oauth-fake"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "whoami", "description": "Who",
                                         "annotations": {"readOnlyHint": True},
                                         "inputSchema": {"type": "object", "properties": {}}}]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "влязъл"}]}
                else:
                    result = {}
                return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result},
                                  {"Mcp-Session-Id": "oauth-sess"})
            return self._json(404, {})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/mcp", httpd, state
