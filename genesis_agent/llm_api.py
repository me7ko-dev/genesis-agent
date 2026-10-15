"""genesis_agent.llm_api — `genesis api`: локален OpenAI-съвместим текстов вход.

За други програми на СЪЩАТА машина (напр. игра, чиито жители говорят през
Genesis): съобщения влизат, текст излиза. Нищо друго — без инструменти, без
агентен цикъл, без умения, sandbox, терминал или браузър. Моделите са
безплатната верига на Brain с леките (бързи) модели отпред.

    GET  /v1/health            → {"app":"genesis","api":1,"ok":true, …}
    POST /v1/chat/completions  ← {"messages":[{"role","content"}, …],
                                  "response_format":{"type":"json_object"}?}
                               → OpenAI `chat.completion` (без streaming)

`temperature` и `max_tokens` се приемат, но не се препращат — Brain праща
свои стойности; кратък отговор се иска в самия промпт.

Сигурност — всяка уеб страница в браузъра на оператора може да опита да
стигне 127.0.0.1, затова:
  * слуша само на 127.0.0.1;
  * Host трябва да е 127.0.0.1:<порт> или localhost:<порт> (DNS rebinding);
  * заявка с Origin минава само от http://127.0.0.1 или http://localhost
    (всеки порт); без Origin (локален процес) — минава;
  * тяло до 64 KB, до 64 съобщения, до 2 едновременни обръщения към
    моделите, краен срок на заявка;
  * логът е един ред на заявка — модел и време, никога съдържание.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

API_VERSION = 1
DEFAULT_PORT = 8770           # 8765 е `genesis serve`
MAX_BODY = 64 * 1024
MAX_MESSAGES = 64
MAX_PARALLEL = 2
REQUEST_TIMEOUT_S = 18.0      # клиентът на играта чака 20 s
MODEL_TIMEOUT_S = 8           # на модел; бавният се прескача към следващия
MAX_MODELS = 6                # най-много толкова модела от веригата на заявка
_ROLES = ("system", "user", "assistant")
_LOCAL_ORIGIN = re.compile(r"http://(127\.0\.0\.1|localhost)(:\d{1,5})?")
JSON_INSTRUCTION = ("Return exactly one valid JSON object and nothing else: "
                    "no markdown, no code fences, no text before or after it.")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ── модели ──────────────────────────────────────────────────────────────────

def _has_key(brain: Any, provider: str) -> bool:
    from genesis_agent.brain import _PROVIDERS
    env = _PROVIDERS.get(provider, ("", None))[1]
    return bool(env and str(brain.keys.get(env) or "").strip())


def _make_brain() -> Any:
    """Brain само за текст: стартовият модел, после леките, само доставчици с
    ключ, без локален ollama (бавен за 20 s) и без платени модели.

    Стартовият (groq gpt-oss-120b) е пръв, защото наживо 2026-10-06 говори
    по-чист български от леките 20B при същото време (0.5–1 s срещу 0.7–1.4 s)."""
    from genesis_agent.brain import Brain, _load_chain
    # quality="normal": иначе GENESIS_QUALITY=max от средата слага платените отпред.
    brain = Brain(light=True, use_local=False, quality="normal")
    start = _load_chain()[:1]
    first = {(c["provider"], c["model"]) for c in start}
    chain = start + [c for c in brain.chain if (c["provider"], c["model"]) not in first]
    brain.chain = [c for c in chain if _has_key(brain, c["provider"])][:MAX_MODELS]
    brain.current = brain.chain[0] if brain.chain else None
    brain.timeout = MODEL_TIMEOUT_S
    return brain


def health() -> dict:
    from genesis_agent import __version__
    info: dict[str, Any] = {"app": "genesis", "api": API_VERSION, "ok": True,
                            "version": __version__}
    try:
        chain = _make_brain().chain
    except Exception:  # счупен config.yaml — API-то пак отговаря
        chain = []
    info["ready"] = bool(chain)
    info["providers"] = sorted({c["provider"] for c in chain})
    info["models"] = [f"{c['provider']}/{c['model']}" for c in chain]
    return info


def extract_json_object(text: str) -> str | None:
    """Първият JSON обект в текста (и в ```json ограда или с проза около него)."""
    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(text) if ch == "{"][:50]
    for i in starts:
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return json.dumps(obj, ensure_ascii=False)
    return None


def with_json_instruction(messages: list[dict]) -> list[dict]:
    out = [dict(m) for m in messages]
    if out and out[0]["role"] == "system":
        out[0]["content"] = f"{out[0]['content']}\n\n{JSON_INSTRUCTION}"
    else:
        out.insert(0, {"role": "system", "content": JSON_INSTRUCTION})
    return out


def parse_request(body: bytes) -> tuple[list[dict], bool]:
    """(messages, json_mode) от тялото на заявката, или ApiError(400)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "Тялото не е валиден JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(400, "Тялото трябва да е JSON обект.")
    if data.get("stream"):
        raise ApiError(400, "stream не се поддържа — отговорът идва наведнъж.")
    raw = data.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "Липсва `messages` (непразен списък).")
    if len(raw) > MAX_MESSAGES:
        raise ApiError(400, f"Твърде много съобщения (най-много {MAX_MESSAGES}).")
    messages: list[dict] = []
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in _ROLES:
            raise ApiError(400, "Всяко съобщение иска role: system, user или assistant.")
        content = m.get("content")
        if isinstance(content, list):  # OpenAI части: [{"type":"text","text":…}]
            content = "".join(str(p.get("text") or "") for p in content
                              if isinstance(p, dict) and p.get("type") == "text")
        if not isinstance(content, str):
            raise ApiError(400, "content трябва да е текст.")
        messages.append({"role": m["role"], "content": content})
    fmt = data.get("response_format")
    json_mode = isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")
    return messages, json_mode


def generate(messages: list[dict], json_mode: bool) -> tuple[str, str, dict | None]:
    """(текст, provider/model, usage). Само текст: `tools` НИКОГА не се подава."""
    brain = _make_brain()
    if not brain.chain:
        raise ApiError(503, "Няма облачен модел с ключ — пусни `genesis setup`.")
    msgs = with_json_instruction(messages) if json_mode else messages
    avoid: tuple[str, str] | None = None
    for _ in range(2 if json_mode else 1):
        reply = brain.complete(msgs, tools=None, avoid=avoid)
        text = str(reply.raw_text or "").strip()
        if text.startswith("Error:"):
            raise ApiError(502, f"Моделите не отговориха: {text[6:].strip()[:200]}")
        cur = brain.current or {}
        used = f"{cur.get('provider', '?')}/{cur.get('model', '?')}"
        usage = reply.usage if isinstance(reply.usage, dict) else None
        if not json_mode:
            return text, used, usage
        found = extract_json_object(text)
        if found is not None:
            return found, used, usage
        avoid = (str(cur.get("provider")), str(cur.get("model")))  # друг модел
    raise ApiError(502, "Моделът не върна валиден JSON обект.")


def completion(text: str, model: str, usage: dict | None) -> dict:
    out: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}]}
    if usage:
        p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        out["usage"] = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
    return out


# ── HTTP ────────────────────────────────────────────────────────────────────

class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR пуска втори процес на същия порт без грешка.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port: int, *, request_timeout: float = REQUEST_TIMEOUT_S,
                 parallel: int = MAX_PARALLEL) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.request_timeout = request_timeout
        self.slots = threading.BoundedSemaphore(parallel)

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # клиентът се е отказал — не е грешка за терминала
        super().handle_error(request, client_address)

    def run_limited(self, fn: Callable[[], Any]) -> Any:
        """fn() в отделна нишка: до `parallel` наведнъж, с краен срок на заявката.
        Нишка след срока довършва сама и чак тогава освобождава мястото."""
        t0 = time.monotonic()
        if not self.slots.acquire(timeout=self.request_timeout):
            raise ApiError(503, "Genesis е зает с други заявки — опитай след малко.")
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = fn()
            except BaseException as e:
                box["err"] = e
            finally:
                self.slots.release()

        worker = threading.Thread(target=work, daemon=True, name="genesis-api-call")
        worker.start()
        worker.join(max(0.0, self.request_timeout - (time.monotonic() - t0)))
        if worker.is_alive():
            raise ApiError(504, "Моделите не отговориха навреме.")
        if "err" in box:
            raise box["err"]
        return box["ok"]


class _Handler(BaseHTTPRequestHandler):
    server_version = "genesis"
    sys_version = ""
    timeout = 10  # бавен/недовършен клиент не държи нишка завинаги

    def log_message(self, format: str, *args: Any) -> None:
        return  # свой ред в _handle, без съдържание

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _send(self, status: int, obj: dict | None, origin: str | None) -> None:
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        self.wfile.write(body)

    def _check_caller(self) -> str | None:
        """Allowed Origin (или None без Origin); иначе ApiError(403)."""
        port = cast(ApiServer, self.server).server_port
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise ApiError(403, "Host трябва да е 127.0.0.1 или localhost — отказано.")
        origin = self.headers.get("Origin")
        if origin is not None and not _LOCAL_ORIGIN.fullmatch(origin):
            raise ApiError(403, "Достъп само от http://127.0.0.1 или http://localhost.")
        return origin

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise ApiError(411, "Липсва Content-Length.") from None
        if length > MAX_BODY:
            raise ApiError(413, f"Тялото е над {MAX_BODY // 1024} KB.")
        return self.rfile.read(max(0, length))

    def _handle(self, method: str) -> None:
        srv = cast(ApiServer, self.server)
        t0 = time.monotonic()
        path = self.path.split("?", 1)[0].rstrip("/")
        status, model, origin = 500, "-", None
        try:
            origin = self._check_caller()
            if method == "OPTIONS":
                status = 204
                self._send(204, None, origin)
            elif path == "/v1/health" and method == "GET":
                status = 200
                self._send(200, health(), origin)
            elif path == "/v1/chat/completions" and method == "POST":
                messages, json_mode = parse_request(self._read_body())
                text, model, usage = srv.run_limited(lambda: generate(messages, json_mode))
                status = 200
                self._send(200, completion(text, model, usage), origin)
            elif path in ("/v1/health", "/v1/chat/completions"):
                raise ApiError(405, "Този метод не се поддържа тук.")
            else:
                raise ApiError(404, "Няма такъв адрес. Виж /v1/health и /v1/chat/completions.")
        except ApiError as e:
            status = e.status
            self._send(status, {"error": {"message": e.message}}, origin)
        except Exception as e:
            status = 500
            self._send(500, {"error": {"message": f"Вътрешна грешка: {type(e).__name__}"}}, origin)
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            print(f"[api] {time.strftime('%H:%M:%S')} {method} {path[:60]} → {status} "
                  f"{model} {ms} ms", flush=True)


# ── `genesis api` ───────────────────────────────────────────────────────────

API_USAGE = f"""Употреба: genesis api [--port N]

Локален OpenAI-съвместим вход САМО за текст (напр. за игра на същия компютър):
POST /v1/chat/completions и GET /v1/health на http://127.0.0.1:{DEFAULT_PORT}.
Без инструменти, файлове, терминал или браузър. Достъп само от тази машина.

  --port N    порт (по подразбиране {DEFAULT_PORT})"""


def serve(args: list[str]) -> int:
    """`genesis api [--port N]`."""
    port = DEFAULT_PORT
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            i += 1
            try:
                port = int(args[i])
            except ValueError:
                port = -1
            if not 0 < port < 65536:
                print(f"--port иска число от 1 до 65535, не {args[i]!r}")
                return 2
        elif a in ("-h", "--help"):
            print(API_USAGE)
            return 0
        else:
            print(f"Непозната опция: {a}\n\n{API_USAGE}")
            return 2
        i += 1

    try:
        httpd = ApiServer(port)
    except OSError as e:
        print(f"Не мога да слушам на 127.0.0.1:{port} ({e}). "
              f"Друг порт: genesis api --port {port + 1}")
        return 1

    info = health()
    base = f"http://127.0.0.1:{port}"
    print("Genesis API — само текст, само за тази машина")
    print(f"  Адрес:    {base}/v1/chat/completions")
    print(f"  Проверка: {base}/v1/health")
    print("  Без инструменти: заявките само превръщат съобщения в текст —")
    print("  не пускат команди, не пипат файлове, не отварят браузър.")
    if info["models"]:
        print(f"  Модели:   {' → '.join(info['models'])}")
    else:
        print("  ⚠ Няма облачен модел с ключ — пусни `genesis setup`.")
    print("Ctrl+C спира.", flush=True)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print("\nGenesis API е спрян.")
    return 0
