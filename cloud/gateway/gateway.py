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
    # Част от prompt_tokens, прочетена от кеша на доставчика (Ollama Cloud я
    # връща в prompt_tokens_details.cached_tokens). Таванът на задачата не я
    # вади — тя е вътре в total; стои отделно, защото платените API-та я
    # таксуват по-евтино и сметката (етап 3) трябва да я знае.
    cached_tokens: int = 0

    def add(self, e: dict[str, Any]) -> None:
        self.prompt_tokens += int(e.get("prompt_tokens") or 0)
        self.completion_tokens += int(e.get("completion_tokens") or 0)
        self.total_tokens += int(e.get("total_tokens") or 0)
        self.cached_tokens += int(e.get("cached_tokens") or 0)
        self.calls += 1

    def as_dict(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens, "cached_tokens": self.cached_tokens,
                "calls": self.calls}


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
                self.jobs.setdefault(str(e.get("job")), Usage()).add(e)
                n += 1
        return n

    # Запазено за заявки, които още са при доставчика. Без това таванът се
    # проверяваше преди заявката, а се таксуваше след нея: 30 паралелни заявки
    # по 1 000 токена при бюджет 1 000 минаваха 24 (2026-10-07).
    reserved: dict[str, int] = field(default_factory=dict)

    def used(self, job: str) -> int:
        with self.lock:
            return self.jobs.get(job, Usage()).total_tokens

    def reserve(self, job: str, budget: int, amount: int) -> bool:
        """Запазва `amount` токена, ако разходът + запазеното още е под тавана."""
        with self.lock:
            spent = self.jobs.get(job, Usage()).total_tokens + self.reserved.get(job, 0)
            if spent >= budget:
                return False
            self.reserved[job] = self.reserved.get(job, 0) + amount
            return True

    def left(self, job: str, budget: int) -> int:
        """Колко още може да се похарчи (без вече запазеното от други заявки)."""
        with self.lock:
            return budget - self.jobs.get(job, Usage()).total_tokens - self.reserved.get(job, 0)

    def release(self, job: str, amount: int) -> None:
        with self.lock:
            left = self.reserved.get(job, 0) - amount
            if left > 0:
                self.reserved[job] = left
            else:
                self.reserved.pop(job, None)

    def add(self, job: str, provider: str, model: str, usage: dict[str, Any]) -> None:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        t = int(usage.get("total_tokens") or 0) or p + c
        details = usage.get("prompt_tokens_details")
        cached = int((details.get("cached_tokens") if isinstance(details, dict) else 0) or 0)
        entry: dict[str, Any] = {
            "job": job, "provider": provider, "model": model, "prompt_tokens": p,
            "completion_tokens": c, "total_tokens": t, "cached_tokens": cached,
            # Оценка без `usage` от доставчика (2026-10-11): сметката не знае
            # колко е вход и колко изход, затова таксува целия сбор по най-
            # скъпата тарифа — флагът трябва да стигне до файла.
            **({"estimated": True} if usage.get("estimated") else {})}
        with self.lock:
            self.jobs.setdefault(job, Usage()).add(entry)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({**entry, "at": time.time()}) + "\n")


def usage_size(path: Path) -> int:
    """Докъде е файлът с разхода сега — оттук нататък чете usage_for."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def usage_for(path: Path, job: str, start: int = 0) -> dict[str, int]:
    """Сборът за задача от JSONL файла — това чете сървърът за сметката.

    `start` е usage_size() преди задачата да тръгне: по-рано нейни редове няма.
    Деплой одит 2026-10-11: файлът расте без край и при 1M реда целият прочит
    струваше 3.3 s на всеки ход; от `start` — 0.13 ms (200 MB, измерено). Ако файлът е по-къс от `start` (сменен при
    месечното архивиране), се чете отначало."""
    u = Usage()
    try:
        with path.open("rb") as f:
            if start <= f.seek(0, os.SEEK_END):
                f.seek(start)
            else:
                f.seek(0)
            for raw in f:
                try:
                    e = json.loads(raw.decode("utf-8"))
                except ValueError:  # вкл. UnicodeDecodeError и средата на ред
                    continue
                if not isinstance(e, dict):
                    continue
                if e.get("job") != job:
                    continue
                u.add(e)
    except OSError:
        pass
    return u.as_dict()


def _count(v: Any) -> int:
    """Брой токени от реда, или 0 — никога отрицателен, никога float."""
    return v if type(v) is int and v > 0 else 0


def usage_lines_by_job(path: Path, jobs: set[str],
                       start: int = 0) -> dict[str, list[dict[str, Any]]]:
    """Разходът на всяка от `jobs` по (доставчик, модел, оценка) — за сметката.

    Сборът на usage_for не стига за цена: моделите струват различно, а
    кешираният вход — по-малко (2026-10-11). Файлът се чете веднъж за всички
    задачи (reconcile при старт иска много наведнъж). `start` — като при
    usage_for: най-малкият usage_size() отпреди задачите (2026-10-11)."""
    groups: dict[str, dict[tuple[str, str, bool], dict[str, Any]]] = {j: {} for j in jobs}
    try:
        with path.open("rb") as f:
            if start <= f.seek(0, os.SEEK_END):
                f.seek(start)
            else:
                f.seek(0)
            for raw in f:
                try:
                    e = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):  # вкл. UnicodeDecodeError
                    continue
                if not isinstance(e, dict) or e.get("job") not in groups:
                    continue
                est = bool(e.get("estimated"))
                key = (str(e.get("provider", "")), str(e.get("model", "")), est)
                g = groups[str(e["job"])].setdefault(key, {
                    "provider": key[0], "model": key[1], "prompt_tokens": 0,
                    "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0,
                    "calls": 0, **({"estimated": True} if est else {})})
                for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
                    g[k] += _count(e.get(k))
                g["calls"] += 1
    except OSError:
        pass
    return {j: list(g.values()) for j, g in groups.items()}


def usage_lines(path: Path, job: str, start: int = 0) -> list[dict[str, Any]]:
    return usage_lines_by_job(path, {job}, start)[job]


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
            request = json.loads(body)
            model = str(request.get("model", ""))
        except (ValueError, AttributeError, RecursionError):   # "[" × 200 000 (2026-10-11)
            return 400, _err("невалиден JSON")
        # Поточен отговор не е JSON — `usage` се губеше и задачата харчеше без
        # таван и без сметка (2026-10-07). Genesis не ползва stream.
        if request.get("stream"):
            return 400, _err("stream не се поддържа през шлюза")
        # Таванът на отговора е за един избор, а доставчикът таксува n × max_tokens:
        # n=128 при бюджет 20 000 записа 2 555 876 токена, а балансът падна от
        # 26 на -605 кредита за един ход (одит 2026-10-11). Genesis иска един.
        for key in ("n", "best_of"):
            if key in request and not (request[key] is None or
                                       (type(request[key]) is int and request[key] == 1)):
                return 400, _err(f"{key} различно от 1 не се поддържа през шлюза")
        # Жетонът е проверен при заглавията, а тялото може да е идвало бавно
        # (ревю 2026-10-11). Сметката на хода става окончателна след изтичането
        # + UPSTREAM_TIMEOUT — заявка, тръгнала по-късно, би останала без такса.
        if grant.expires < time.time():
            return 401, _err("невалиден или изтекъл жетон")
        # Отговорът се ограничава до оставащото: проверяваше се само дали вече е
        # похарчено — заявка от 1.9 MB с max_tokens 64k минаваше и струваше 1.8×
        # бюджета (одит 2026-10-09). Промпт, по-голям от остатъка, не минава.
        left = self.ledger.left(grant.job, grant.budget)
        prompt_est = len(body) // 3
        if prompt_est >= left:
            return 429, _err(f"бюджетът на задачата ({grant.budget} токена) е изчерпан")
        body = _cap_completion(request, body, left - prompt_est)
        estimate = _estimate(request, body)
        if not self.ledger.reserve(grant.job, grant.budget, estimate):
            return 429, _err(f"бюджетът на задачата ({grant.budget} токена) е изчерпан")
        try:
            return self._forward(provider, base, keys, body, model, grant, estimate)
        finally:
            self.ledger.release(grant.job, estimate)

    def _forward(self, provider: str, base: str, keys: list[str], body: bytes, model: str,
                 grant: Grant, estimate: int) -> tuple[int, bytes]:
        status, data = 502, _err("няма отговор")
        # Един общ срок за всички ключове (ревю 2026-10-11): с N ключа и 429
        # след 299 s разходът се записваше до N × 300 s след изтичането на
        # жетона — след окончателната сметка на хода (billing.LATE_MARGIN).
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        for key in keys:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            req = urllib.request.Request(
                f"{base}/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            try:
                with self.opener(req, timeout=left) as r:
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
            if not isinstance(usage, dict) or not any(
                    usage.get(k) for k in ("total_tokens", "prompt_tokens", "completion_tokens")):
                # Отговор без usage не е безплатен отговор: таксува се оценката.
                usage = {"total_tokens": estimate, "estimated": True}
            self.ledger.add(grant.job, provider, model, usage)
        return status, data


def _cap_completion(request: dict, body: bytes, allowed: int) -> bytes:
    """Всеки таван на отговора (`max_tokens`, `max_completion_tokens`) не над
    `allowed`; без таван — изрично `max_tokens`.

    Одит 2026-10-11: липсващ таван се приемаше за 4 096, но тялото оставаше
    непипнато и доставчикът слагаше своя (32 789 от бюджет 6 000); с двата
    ключа се режеше само `max_completion_tokens`, а `max_tokens=200000` минаваше."""
    allowed = max(1, allowed)
    keys = [k for k in ("max_tokens", "max_completion_tokens") if k in request] or ["max_tokens"]
    changed = False
    for key in keys:
        try:
            current = int(request.get(key) or 4096)
        except (TypeError, ValueError, OverflowError):
            current = 4096
        capped = max(1, min(current, allowed))
        if request.get(key) != capped or type(request.get(key)) is not int:
            request[key] = capped
            changed = True
    return json.dumps(request).encode() if changed else body


def _estimate(request: dict, body: bytes) -> int:
    """Горна оценка на заявката: промптът (~3 байта на токен) + по-големият от
    таваните на отговора (иначе 4 096)."""
    caps = []
    for key in ("max_tokens", "max_completion_tokens"):
        try:
            caps.append(int(request.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            pass
    cap = max(caps, default=0) or 4096
    return len(body) // 3 + max(1, min(cap, 64_000))


def _err(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "genesis-gateway"
    sys_version = ""
    # Без таймаут заявка без тяло държеше нишка завинаги: една задача можеше да
    # задържи стотици и да спре обръщенията на всички (одит 2026-10-09).
    timeout = 60
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
