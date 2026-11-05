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
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

log = logging.getLogger("genesis.llm_api")

API_VERSION = 1
DEFAULT_PORT = 8770           # 8765 е `genesis serve`
MAX_BODY = 64 * 1024
MAX_MESSAGES = 64
MAX_PARALLEL = 2
REQUEST_TIMEOUT_S = 18.0      # клиентът на играта чака 20 s
MODEL_TIMEOUT_S = 8           # на модел; бавният се прескача към следващия
MAX_MODELS = 6                # най-много толкова модела от веригата на заявка
# Ново обръщение към модел тръгва само ако до срока остава поне толкова:
# стартовият модел отговаря за 0.5–1.4 s (наживо 2026-10-06), с по-малко
# отговорът почти сигурно се изхвърля, а мястото и квотата се хабят. Одит
# 2026-10-31: заявка, взела мястото с 0.5 s оставащи, пускаше цяло обръщение и
# пак връщаше 504; при натоварване следващите получаваха 503.
MIN_CALL_S = 1.0
_ROLES = ("system", "user", "assistant")
_LOCAL_ORIGIN = re.compile(r"http://(127\.0\.0\.1|localhost)(:\d{1,5})?")
# `[`, след която идва обект или масив — масив от стойности (може и счупен).
_ARRAY_OF_VALUES = re.compile(r"\[\s*[\[{]")
# Имена на заглавки (token по RFC 9110), разделени със запетаи — за CORS.
_HEADER_NAME = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_HEADER_LIST = re.compile(rf"{_HEADER_NAME}(?:[ \t]*,[ \t]*{_HEADER_NAME})*")
_ALLOW_HEADERS = ("Content-Type", "Authorization")
# Отказ с непрочетено тяло: колко най-много да се изхвърля остатъкът, за да
# стигне отговорът вместо RST — като remote_server._linger_close.
_LINGER_S = 0.5
_DRAIN_CHUNK = 64 * 1024
JSON_INSTRUCTION = ("Return exactly one valid JSON object and nothing else: "
                    "no markdown, no code fences, no text before or after it.")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ── модели ──────────────────────────────────────────────────────────────────

# Никога през API-то, дори със „ключ“: claude_cli е абонаментът на оператора —
# по правилата в genesis_agent/claude_cli.py само за личната му работа, не вход
# за друга програма (лекият Brain го слага накрая, щом е избран, 2026-10-12);
# ollama_local е бавен за 20-секундния срок на играта.
_NEVER_PROVIDERS = frozenset({"claude_cli", "ollama_local"})


def _has_key(brain: Any, provider: str) -> bool:
    from genesis_agent.brain import _PROVIDERS
    if provider in _NEVER_PROVIDERS:
        return False
    env = _PROVIDERS.get(provider, ("", None))[1]
    return bool(env and str(brain.keys.get(env) or "").strip())


def _make_brain() -> Any:
    """Brain само за текст: стартовият модел, после леките, само доставчици с
    ключ, без локален ollama (бавен за 20 s) и без платени модели.

    Стартовият (groq gpt-oss-120b) е пръв, защото наживо 2026-10-06 говори
    по-чист български от леките 20B при същото време (0.5–1 s срещу 0.7–1.4 s)."""
    from genesis_agent.brain import Brain, _load_chain
    # quality="normal": лекият Brain и без това не чете GENESIS_QUALITY
    # (2026-10-11), но изричното тук пази платените вън и ако това се смени.
    # Разходът се записва в budget_log от самия Brain.complete.
    brain = Brain(light=True, use_local=False, quality="normal")
    start = _load_chain()[:1]
    first = {(c["provider"], c["model"]) for c in start}
    chain = start + [c for c in brain.chain if (c["provider"], c["model"]) not in first]
    brain.chain = [c for c in chain if _has_key(brain, c["provider"])][:MAX_MODELS]
    brain.current = brain.chain[0] if brain.chain else None
    brain.timeout = MODEL_TIMEOUT_S
    # timeout на requests е между два пакета, а общият срок в Brain е ×4: модел,
    # който капе празни редове на <8 s, държеше веригата 32 s и всяка заявка
    # ставаше 504 на 18 s (одит 2026-10-31). 8 s тук са 8 s от начало до край.
    brain.total_deadline = MODEL_TIMEOUT_S
    return brain


def health() -> dict:
    from genesis_agent import __version__
    info: dict[str, Any] = {"app": "genesis", "api": API_VERSION, "ok": True,
                            "version": __version__}
    try:
        chain = _make_brain().chain
    except Exception:  # счупен config.yaml — API-то пак отговаря
        log.debug("веригата не се зареди за /v1/health", exc_info=True)
        chain = []
    info["ready"] = bool(chain)
    info["providers"] = sorted({c["provider"] for c in chain})
    info["models"] = [f"{c['provider']}/{c['model']}" for c in chain]
    return info


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} не е JSON (RFC 8259)")


def extract_json_object(text: str) -> str | None:
    """Първият JSON обект НА НАЙ-ГОРНО НИВО в текста (и в ```json ограда или с
    проза около него) — само по RFC 8259; иначе None.

    Одит 2026-10-31: raw_decode се пробваше на всяка `{` и при счупен външен
    обект (запетая накрая, единични кавички, отрязан отговор) връщаше вложения
    `{"ниво": 3}` с 200 — и повторният опит на друг модел не тръгваше. Масив
    отгоре даваше само първия си елемент. Затова един проход брои дълбочината
    на {} и [] (низовете в тях — с кавичките и екраните с `\\`) и пробва само
    `{` на дълбочина 0. Кавичките в прозата отвън не се броят: там често са
    несдвоени. NaN/Infinity (и 1e400 → inf) JSON.parse в играта отхвърля, затова
    такъв кандидат отпада, вместо да излезе като „валиден“ отговор.

    Ревю 2026-11-05: несдвоена `[` в прозата („виж [1): {…}“, „<think>[мисля
    </think>{…}“) отваряше дълбочина, която не се затваря, и криеше обекта
    (преди 2026-10-31 той се връщаше). `[` на дълбочина 0 е масив само ако се чете цял
    (прескача се) или ако след нея идва `{`/`[` (масив от обекти — и счупен, и
    отрязан пак крие елементите си); иначе е скоба в прозата. Несдвоена `{` в
    прозата пак крие обекта: не се различава от `{reply: "Hi", mood: {…}}`, а
    там вложеният фрагмент е грешен отговор — повторният опит е по-евтин.
    RecursionError: обект, вложен на 50 000 нива, излизаше като 500."""
    decoder = json.JSONDecoder(parse_constant=_no_constant)
    depth, objects, arrays, skip_to = 0, 0, 0, 0
    in_str = escaped = False
    for i, ch in enumerate(text):
        if i < skip_to:
            continue
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = depth > 0
        elif ch == "{" and depth == 0:
            objects += 1
            if objects > 50:
                return None
            try:
                obj, _ = decoder.raw_decode(text, i)
                return json.dumps(obj, ensure_ascii=False, allow_nan=False)
            except (ValueError, RecursionError):
                depth = 1  # счупен: броенето по-долу го прескача целия
        elif (ch == "[" and depth == 0 and arrays < 50
              and not _ARRAY_OF_VALUES.match(text, i)):
            # Най-много 50 опита: `[1,[1,[1,…` иначе е O(n²). След тях `[`
            # пак отваря дълбочина — както преди, на сигурно.
            arrays += 1
            try:
                _, skip_to = decoder.raw_decode(text, i)  # цял масив — прескача се
            except (ValueError, RecursionError):
                pass  # скоба в прозата: дълбочината не се променя
        elif ch in "{[":
            depth += 1
        elif ch in "}]" and depth:
            depth -= 1
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
        # utf-8-sig: C# StreamWriter(…, Encoding.UTF8) и файл от PowerShell 5.1
        # (-InFile, curl --data-binary @файл) започват с BOM — беше 400 „не е
        # валиден JSON“ (одит 2026-10-31; същият BOM в .env — brain._load_keys).
        data = json.loads(body.decode("utf-8-sig"))
    # RecursionError: `[` × 60 000 (под 64 KB) не е ValueError — беше 500
    # „Вътрешна грешка“ за грешка на клиента (одит 2026-11-04).
    except (UnicodeDecodeError, ValueError, RecursionError):
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
            # Само текстови части (одит 2026-11-04): другите се изпускаха тихо —
            # картинка сама стигаше до модела като `content: ""` и клиентът
            # получаваше 200 с отговор за нещо, което моделът не е видял.
            other = next((p for p in content
                          if not (isinstance(p, dict) and p.get("type") == "text")), None)
            if other is not None:
                kind = other.get("type") if isinstance(other, dict) else type(other).__name__
                raise ApiError(400, f"Само текст: частта {str(kind)[:40]!r} не се поддържа.")
            content = "".join(str(p.get("text") or "") for p in content)
        if not isinstance(content, str):
            raise ApiError(400, "content трябва да е текст.")
        messages.append({"role": m["role"], "content": content})
    fmt = data.get("response_format")
    json_mode = isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")
    return messages, json_mode


def _late(start_by: float | None) -> bool:
    return start_by is not None and time.monotonic() >= start_by


def generate(messages: list[dict], json_mode: bool,
             start_by: float | None = None) -> tuple[str, str, dict | None]:
    """(текст, provider/model, usage). Само текст: `tools` НИКОГА не се подава.

    `start_by` (time.monotonic()): след него не тръгва ново обръщение към модел
    — нито следващ модел от веригата, нито повторният опит за JSON (одит
    2026-10-31: тръгваха след срока, изхвърляха се и държаха мястото)."""
    brain = _make_brain()
    if not brain.chain:
        raise ApiError(503, "Няма облачен модел с ключ — пусни `genesis setup`.")
    brain.stop_at = start_by
    msgs = with_json_instruction(messages) if json_mode else messages
    avoid: tuple[str, str] | None = None
    for attempt in range(2 if json_mode else 1):
        if attempt and _late(start_by):
            break  # няма време за друг модел — 502 по-долу, веднага
        reply = brain.complete(msgs, tools=None, avoid=avoid)
        text = str(reply.raw_text or "").strip()
        if text.startswith("Error:"):
            # След start_by — 504 и при истински грешки (напр. само 502): веригата
            # е спряна от срока, не е изчерпана; клиентът вижда същото като при
            # 504 от run_limited (ревю 2026-11-05).
            if _late(start_by):
                raise ApiError(504, "Моделите не отговориха навреме.")
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


def _tokens(value: Any) -> int | None:
    """Брой токени от usage на доставчика, или None за поле, което не е брой.

    Brain подава usage както е дошъл: "n/a", {…}, [1], NaN, 1e999 в него
    гърмяха в int() и отговорът на модела се губеше зад 500 „Вътрешна грешка“
    (одит 2026-10-31) — боклук е полето, не отговорът."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def completion(text: str, model: str, usage: dict | None) -> dict:
    out: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}]}
    if usage:
        p, c = _tokens(usage.get("prompt_tokens")), _tokens(usage.get("completion_tokens"))
        if p is not None or c is not None:
            p, c = p or 0, c or 0
            out["usage"] = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
    return out


# ── HTTP ────────────────────────────────────────────────────────────────────

class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR пуска втори процес на същия порт без грешка.
    allow_reuse_address = os.name != "nt"
    # Опашката по подразбиране на socketserver е 5: измерено 2026-10-15 — от 20
    # връзки, отворени наведнъж, минаваха 6, а другите висяха до таймаута си
    # (на cloud/web същото даде ConnectionResetError в CI на 3.13, 2026-10-11).
    # Играта пита за няколко жители едновременно — затова същата поправка.
    request_queue_size = 128

    def __init__(self, port: int, *, request_timeout: float = REQUEST_TIMEOUT_S,
                 parallel: int = MAX_PARALLEL) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.request_timeout = request_timeout
        # Половината срок при малък request_timeout (тестовете), иначе MIN_CALL_S.
        self.min_call_s = min(MIN_CALL_S, request_timeout / 2)
        self.slots = threading.BoundedSemaphore(parallel)

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # клиентът се е отказал — не е грешка за терминала
        super().handle_error(request, client_address)

    def run_limited(self, fn: Callable[[float], Any]) -> Any:
        """fn(start_by) в отделна нишка: до `parallel` наведнъж, с краен срок на
        заявката. Нишка след срока довършва сама и чак тогава освобождава
        мястото. `start_by` е последният миг, в който има смисъл да тръгне
        обръщение към модел (срокът минус min_call_s): място, освободено след
        него, не пуска нищо — 503 веднага (одит 2026-10-31)."""
        t0 = time.monotonic()
        start_by = t0 + self.request_timeout - self.min_call_s
        if not self.slots.acquire(timeout=max(0.0, start_by - t0)):
            raise ApiError(503, "Genesis е зает с други заявки — опитай след малко.")
        if time.monotonic() >= start_by:
            self.slots.release()
            raise ApiError(503, "Genesis е зает с други заявки — опитай след малко.")
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = fn(start_by)
            except BaseException as e:  # noqa: BLE001 — пренася се в нишката на заявката
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
    _body_read = False  # прочетено ли е тялото (виж _close_after_unread_body)

    def log_message(self, format: str, *args: Any) -> None:
        return  # свой ред в _handle, без съдържание

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _send(self, status: int, obj: dict | None, origin: str | None) -> None:
        # "replace": отговор на модела с половин двойка (`"\ud83d"` в JSON-а на
        # доставчика — отрязано емоджи) даваше UnicodeEncodeError → 500 и
        # платеният отговор се губеше (одит 2026-11-04); сега тя е „?“.
        body = (b"" if obj is None
                else json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace"))
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
            self.send_header("Access-Control-Allow-Headers", self._allow_headers())
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        self.wfile.write(body)

    def _allow_headers(self) -> str:
        """Content-Type, Authorization + заглавките, поискани в preflight-а.

        Официалният openai JS SDK (dangerouslyAllowBrowser) праща X-Stainless-*;
        заглавка извън Access-Control-Allow-Headers проваля preflight-а и
        браузърът не праща POST-а (одит 2026-10-31). Origin вече е само
        localhost/127.0.0.1, затова отразяването не отваря нищо ново; отразява
        се само списък от имена на заглавки (token), иначе — само двете."""
        allow = list(_ALLOW_HEADERS)
        asked = (self.headers.get("Access-Control-Request-Headers") or "").strip()
        if len(asked) <= 2048 and _HEADER_LIST.fullmatch(asked):
            have = {h.lower() for h in allow}
            for name in (n.strip() for n in asked.split(",")):
                if name.lower() not in have:
                    have.add(name.lower())
                    allow.append(name)
        return ", ".join(allow)

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
        self._body_read = True
        return self.rfile.read(max(0, length))

    def _close_after_unread_body(self) -> None:
        """Отказ, изпратен преди тялото да е прочетено (413, 403, 404, 405):
        първо FIN, после до _LINGER_S остатъкът се изхвърля на парчета.

        Затворен сокет с непрочетени данни праща RST и клиентът, който още
        праща тялото, вижда BrokenPipe/ConnectionReset вместо 413 (одит
        2026-10-31: 4 MB → 20 от 20 без отговор на Linux; на Windows — при
        всеки размер). Същото като remote_server._linger_close (2026-10-11)."""
        if self._body_read:
            return
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = 0
        # И chunked тяло (одит 2026-11-04) — без Content-Length.
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        if left <= 0 and not chunked:
            return
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + _LINGER_S
            while chunked or left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                got = len(self.rfile.read1(_DRAIN_CHUNK if chunked else min(left, _DRAIN_CHUNK)))
                if not got:
                    break
                left -= got
        except OSError:
            pass

    def _handle(self, method: str) -> None:
        srv = cast(ApiServer, self.server)
        t0 = time.monotonic()
        path = self.path.split("?", 1)[0].rstrip("/")
        status, model, origin = 500, "-", None
        self._body_read = False
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
                text, model, usage = srv.run_limited(
                    lambda start_by: generate(messages, json_mode, start_by))
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
            log.debug("вътрешна грешка в genesis api", exc_info=True)
            status = 500
            self._send(500, {"error": {"message": f"Вътрешна грешка: {type(e).__name__}"}}, origin)
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            # И след успешен отговор, който не е чел тялото (GET/OPTIONS с тяло).
            self._close_after_unread_body()
            # Едно write с \n-а (одит 2026-11-04): print пише реда и \n отделно —
            # при 40 едновременни заявки редовете се слепваха („…0 ms[api] …“).
            # None: процес без конзола (pythonw) — print мълчи, и тук.
            out = sys.stdout
            if out is not None:
                out.write(f"[api] {time.strftime('%H:%M:%S')} {method} {path[:60]} → {status} "
                          f"{model} {ms} ms\n")
                out.flush()


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
