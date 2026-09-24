"""Шлюзът за моделите: ключовете остават тук, задачата получава само жетон.

Преди: API ключовете влизаха в контейнера (`--env-file`). Кодът на клиента
можеше да ги прочете и да вика моделите направо, а токените за сметката ги
отчиташе самият контейнер (`done` събитието) — тоест чужд код казваше колко
да му платим.

Сега контейнерът е без никакъв изход навън. Единственото, до което стига, е
този процес (`GENESIS_MODEL_GATEWAY=http://genesis-gateway:8090`). Вместо ключ
носи жетон `job.expires.budget.подпис` (HMAC-SHA256 с общата тайна на сървъра).
Шлюзът:
  - приема само `POST /<доставчик>/chat/completions` с валиден, неизтекъл жетон;
  - маха жетона, слага истинския ключ (ротира `<KEY>`, `<KEY>_2`.. при 429/401);
  - брои токените от `usage` в отговора — по тях се таксува, не по контейнера;
  - при изчерпан бюджет на задачата връща 429 (веригата спира, задачата пада).

    GATEWAY_SECRET=... python -m cloud.gateway.gateway --keys /keys.env --usage /data/usage.jsonl
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("genesis.gateway")

# Същите адреси и имена на ключове като genesis_agent/brain.py::_PROVIDERS
# (само OpenAI-съвместимите, отдалечени). Тест пази двете таблици еднакви.
UPSTREAMS: dict[str, tuple[str, str]] = {
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "cohere": ("https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "sambanova": ("https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "ollama_cloud": ("https://ollama.com/v1", "OLLAMA_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
}
MAX_BODY = 2 * 1024 * 1024
UPSTREAM_TIMEOUT = 300
_ROTATE_ON = {401, 403, 429}


# ── жетони ───────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(secret: str, job: str, *, budget: int, ttl: int, now: float | None = None) -> str:
    """Жетон за една задача. `job` е [a-zA-Z0-9_-], бюджетът е в токени."""
    if not job or not all(c.isalnum() or c in "-_" for c in job):
        raise ValueError("невалидно име на задача")
    expires = int((now if now is not None else time.time()) + ttl)
    body = f"{job}.{expires}.{int(budget)}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


@dataclass(frozen=True)
class Grant:
    job: str
    expires: int
    budget: int


def check_token(secret: str, token: str, now: float | None = None) -> Grant | None:
    parts = token.split(".")
    if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    body = ".".join(parts[:3])
    want = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want, parts[3]):
        return None
    grant = Grant(parts[0], int(parts[1]), int(parts[2]))
    if grant.expires < (now if now is not None else time.time()):
        return None
    return grant


# ── разход ───────────────────────────────────────────────────────────────────

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens, "calls": self.calls}


@dataclass
class Ledger:
    """Токените на задача: в паметта (за тавана) и в JSONL (за сметката)."""
    path: Path | None = None
    jobs: dict[str, Usage] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def load(self) -> int:
        """След рестарт: разходът от файла, за да не се нулират таваните."""
        if self.path is None or not self.path.exists():
            return 0
        n = 0
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                u = self.jobs.setdefault(str(e.get("job")), Usage())
                u.prompt_tokens += int(e.get("prompt_tokens") or 0)
                u.completion_tokens += int(e.get("completion_tokens") or 0)
                u.total_tokens += int(e.get("total_tokens") or 0)
                u.calls += 1
                n += 1
        return n

    def used(self, job: str) -> int:
        with self.lock:
            return self.jobs.get(job, Usage()).total_tokens

    def add(self, job: str, provider: str, model: str, usage: dict[str, Any]) -> None:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        t = int(usage.get("total_tokens") or 0) or p + c
        with self.lock:
            u = self.jobs.setdefault(job, Usage())
            u.prompt_tokens += p
            u.completion_tokens += c
            u.total_tokens += t
            u.calls += 1
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"job": job, "provider": provider, "model": model,
                                        "prompt_tokens": p, "completion_tokens": c,
                                        "total_tokens": t, "at": time.time()}) + "\n")


def usage_for(path: Path, job: str) -> dict[str, int]:
    """Сборът за задача от JSONL файла — това чете сървърът за сметката."""
    u = Usage()
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("job") != job:
                    continue
                u.prompt_tokens += int(e.get("prompt_tokens") or 0)
                u.completion_tokens += int(e.get("completion_tokens") or 0)
                u.total_tokens += int(e.get("total_tokens") or 0)
                u.calls += 1
    except OSError:
        pass
    return u.as_dict()


def read_keys(path: Path) -> dict[str, str]:
    """KEY=value редове (като .env). Коментари и празни редове се пропускат."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


def keys_for(keys: dict[str, str], env: str) -> list[str]:
    return [keys[n] for n in [env] + [f"{env}_{i}" for i in range(2, 11)] if n in keys]


def served_providers(keys: dict[str, str]) -> list[str]:
    return [p for p, (_, env) in UPSTREAMS.items() if keys_for(keys, env)]


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Gateway:
    def __init__(self, secret: str, keys: dict[str, str], ledger: Ledger,
                 opener: Any = urllib.request.urlopen) -> None:
        if len(secret) < 32:
            raise ValueError("GATEWAY_SECRET трябва да е поне 32 знака")
        self.secret = secret
        self.keys = keys
        self.ledger = ledger
        self.opener = opener

    def forward(self, provider: str, body: bytes, grant: Grant) -> tuple[int, bytes]:
        base, env = UPSTREAMS[provider]
        keys = keys_for(self.keys, env)
        if not keys:
            return 404, _err(f"няма ключ за {provider}")
        try:
            model = str(json.loads(body).get("model", ""))
        except (ValueError, AttributeError):
            return 400, _err("невалиден JSON")
        status, data = 502, _err("няма отговор")
        for key in keys:
            req = urllib.request.Request(
                f"{base}/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            try:
                with self.opener(req, timeout=UPSTREAM_TIMEOUT) as r:
                    status, data = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, data = e.code, e.read()
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, data = 502, _err(f"{provider}: {type(e).__name__}")
            if status not in _ROTATE_ON:
                break
        if status == 200:
            try:
                usage = json.loads(data).get("usage") or {}
            except (ValueError, AttributeError):
                usage = {}
            self.ledger.add(grant.job, provider, model, usage)
        return status, data


def _err(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "genesis-gateway"
    sys_version = ""
    gw: Gateway

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, b'{"ok": true}')
        else:
            self._send(404, _err("само POST /<доставчик>/chat/completions"))

    def do_POST(self) -> None:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 3 or parts[1:] != ["chat", "completions"] or parts[0] not in UPSTREAMS:
            self._send(404, _err("само POST /<доставчик>/chat/completions"))
            return
        auth = self.headers.get("Authorization", "")
        grant = check_token(self.gw.secret, auth.removeprefix("Bearer ").strip())
        if grant is None:
            self._send(401, _err("невалиден или изтекъл жетон"))
            return
        if self.gw.ledger.used(grant.job) >= grant.budget:
            self._send(429, _err(f"бюджетът на задачата ({grant.budget} токена) е изчерпан"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._send(413, _err("тялото липсва или е твърде голямо"))
            return
        status, data = self.gw.forward(parts[0], self.rfile.read(length), grant)
        self._send(status, data)


def make_server(gw: Gateway, host: str = "0.0.0.0", port: int = 8090) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"gw": gw})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Шлюз за моделите: ключовете остават тук.")
    p.add_argument("--keys", type=Path, required=True, help="KEY=value; вкл. GATEWAY_SECRET")
    p.add_argument("--usage", type=Path, required=True, help="JSONL с разхода на задача")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    keys = read_keys(a.keys)
    secret = os.environ.get("GATEWAY_SECRET") or keys.pop("GATEWAY_SECRET", "")
    ledger = Ledger(a.usage)
    ledger.load()
    gw = Gateway(secret, keys, ledger)
    server = make_server(gw, a.host, a.port)
    log.info("шлюз на :%d, доставчици: %s", server.server_address[1],
             ", ".join(served_providers(keys)) or "няма")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
