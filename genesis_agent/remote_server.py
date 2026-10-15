"""genesis_agent.remote_server — `genesis serve`: Genesis от телефона.

Агентът остава на компютъра (там са файловете, shell-ът, ключовете);
телефонът (mobile/, Android и iOS) е само прозорец към него — както Claude
Code на телефона управлява сесия, която тече другаде. iOS и без това не
позволява на приложение да пуска процеси, тоест агент на самия телефон не
би могъл да прави нищо полезно.

Сигурност. Това е отдалечено изпълнение на команди на машината — затова:

  * Всяка заявка и всеки отговор са криптирани и автентикирани с
    ChaCha20-Poly1305 и 32-байтов ключ, който се ражда тук и стига до
    телефона САМО през QR кода на екрана (във фрагмента на адреса, `#k=`,
    който браузърите не пращат по мрежата). По мрежата не минава нито ключ,
    нито токен — само шифротекст. Който слуша в същия Wi-Fi, не може нито да
    чете разговора, нито да изпрати команда.
  * Повторено (записано и пуснато пак) съобщение се отказва: всяка заявка
    носи час и еднократен идентификатор.
  * Отговорът е вързан за заявката (AAD съдържа нейния идентификатор), тоест
    не може да бъде подменен с друг, стар отговор.
  * Опасните команди минават през същия sandbox и питат оператора — само
    че въпросът отива на телефона, а без отговор до 5 минути е „не".
  * `genesis serve --reset` сменя ключа: всички сдвоени телефони губят
    достъп.

Протокол v1 (mobile/src/protocol.ts е другата страна):

    GET  /api/hello  → {"app":"genesis","v":1,"key_id":"…","name":"…"}   явно
    POST /api/v1     ← {"v":1,"n":b64(nonce),"c":b64(шифротекст)}
                       AAD "genesis/1/req"; открит текст:
                       {"ts":ms,"rid":"…","op":"status|send|events|confirm|stop|clear", …}
                     → {"v":1,"n":…,"c":…}, AAD "genesis/1/res:" + rid

`events` носи и "state": {"plan": bool, "todos": [{content, status}]}; събитието
"progress" е напредъкът на под-агент. `send` с "/plan", "/undo" … — командите на
чата (phone_command), не текст за модела.

2026-10-11: `events` с "more": true — отговорът е стигнал тавана, телефонът пита
веднага пак от "last". `send` може да носи "mid" (идентификатор от телефона):
повторен опит със същия "mid" получава първия отговор, без втори ход. Грешка в
отворена заявка е {"ok": false, "error": "unknown_op"|"empty"|"bad_args"|…},
не 401.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROTOCOL = 1
DEFAULT_PORT = 8765
_REQ_AAD = b"genesis/1/req"
_RES_AAD = b"genesis/1/res:"
_MAX_SKEW_MS = 5 * 60 * 1000
_MAX_BODY = 256 * 1024
_MAX_WAIT_S = 25.0
_CONFIRM_TIMEOUT_S = 300.0
_EVENT_LOG = 1000
_TOOL_PREVIEW = 4000
# Таван на един отговор на `events` (одит 2026-10-11): студен старт след 1000
# изхода на инструменти теглеше 7.3 MiB, а open() на телефона ги декодираше
# 1.1 s (5 s без JIT, близо до Hermes) на JS нишката. Над тавана: при reset —
# най-новите; иначе — НАЙ-СТАРИТЕ след `after` и "more", телефонът дотегля
# (втори одит 2026-10-11: най-новите + reset губеха дупката и целия разговор
# на телефон, събуден след ~130 инструмента).
_REPLY_BYTES = 512 * 1024
# Колко последни `mid` на `send` помним (2026-10-11): повторен опит след
# изгубен отговор не пуска хода втори път.
_SENT_MIDS = 256
# Колко от тях идват с всеки отговор на `events` (`mids`): един телефон има
# най-много един неизпратен `mid`, а 16 покриват и няколко телефона наведнъж.
_ACK_MIDS = 16
_MID = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Видът на плика {"v":1,"n":…,"c":…} в суровите байтове (преглед 2026-10-11):
# адрес над спирачката получава 429 веднага, без json.loads и опит за
# отваряне — освен ако тялото не е точно плик (сдвоеният телефон зад същия
# NAT минава). Без тази проверка всяка спряна заявка от 240 KiB струваше на
# сървъра 10 ms вместо 1.5 ms преди спирачката да се премести след плика.
_ENVELOPE_SHAPE = re.compile(
    rb'\s*\{\s*"v"\s*:\s*\d{1,3}\s*,\s*"n"\s*:\s*"[A-Za-z0-9_=+/-]{1,64}"\s*,'
    rb'\s*"c"\s*:\s*"[A-Za-z0-9_=+/-]+"\s*\}\s*')
# Началото на същия плик — първите _HEAD_BYTES байта на дълго тяло от спрян
# адрес (вълна 5): `{"v":1,"n":"<до 64>","c":"` е под 100 байта и с интервали.
_ENVELOPE_HEAD = re.compile(
    rb'\s*\{\s*"v"\s*:\s*\d{1,3}\s*,\s*"n"\s*:\s*"[A-Za-z0-9_=+/-]{1,64}"\s*,'
    rb'\s*"c"\s*:\s*"[A-Za-z0-9_=+/-]*')
_HEAD_BYTES = 160
_BRAKED_READ_S = 2.0
# След 429 без прочетено тяло: колко най-много да се изхвърля остатъкът, за да
# стигне отговорът до клиента вместо RST (виж _linger_close).
_LINGER_S = 0.5


class ProtocolError(Exception):
    """Заявка, която не се приема — отговорът е явна грешка без подробности."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def utf8_bytes(text: str) -> bytes:
    """UTF-8 байтове и за текст с lone surrogates (име на файл в cp1251/cp866).

    Одит 2026-10-11: os.listdir връща непрочитаемите байтове като \\udcXX и
    .encode("utf-8") гърмеше — всеки отговор на `events` с такъв LIST_DIR
    затваряше връзката. Байтовете се връщат каквито са били и невалидното става
    U+FFFD; останали surrogates (не от файловата система) — „?“."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        try:
            return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace").encode("utf-8")
        except UnicodeEncodeError:
            return text.encode("utf-8", "replace")


def key_id(key: bytes) -> str:
    """Отпечатък на ключа — телефонът проверява, че говори с СВОЯ компютър."""
    return hashlib.sha256(key).hexdigest()[:16]


# ── ключ и настройки ────────────────────────────────────────────────────────

def _config_path() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return GENESIS_HOME / "remote.json"


def load_or_create_key(*, reset: bool = False) -> bytes:
    """Ключът за сдвояване. Живее в ~/.genesis/remote.json (само за собственика)."""
    path = _config_path()
    if not reset:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            key = _b64d(data["key"])
            if len(key) == 32:
                return key
        except (OSError, ValueError, KeyError, TypeError):
            pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"key": _b64e(key), "created": int(time.time())}, f)
    os.replace(tmp, path)
    return key


def lan_addresses() -> list[str]:
    """IPv4 адресите, на които телефонът в същата мрежа може да стигне компютъра.

    Първият е този, през който минава маршрутът навън (UDP „connect" не праща
    нищо — само пита таблицата с маршрути). После останалите от името на
    машината. Loopback и link-local отпадат: телефонът не може да ги ползва.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return [a for a in found if not a.startswith(("127.", "169.254.", "0."))]


def pairing_url(host: str, port: int, key: bytes, name: str, *, web: bool = False) -> str:
    """Какво носи QR кодът: `genesisremote://pair#u=<адрес>&k=<ключ>&n=<име>`.

    Схемата на приложението, не `http://` (2026-10-09): камерата на телефона
    отваряше `http://…/#k=…` в браузъра — уеб версията по обикновен HTTP в
    LAN, където който подмени страницата, вижда ключа. Сега кодът отваря само
    приложението. Ключът пак е във фрагмента (след `#`).

    `web=True` (`genesis serve --web`) — старият `http://…/#k=…` за уеб
    версията на iPhone без приложение: само по изричен избор и с предупреждение."""
    from urllib.parse import quote
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"   # IPv6 — без скоби приложението не четеше кода (одит 2026-10-09)
    if web:
        return f"http://{host}:{port}/#k={_b64e(key)}&n={quote(name)}"
    base = quote(f"http://{host}:{port}", safe="")
    return f"genesisremote://pair#u={base}&k={_b64e(key)}&n={quote(name)}"


# ── криптиране ─────────────────────────────────────────────────────────────

class Cipher:
    """ChaCha20-Poly1305 със случаен 96-битов nonce на всяко съобщение."""

    def __init__(self, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        if len(key) != 32:
            raise ValueError("ключът трябва да е 32 байта")
        self._aead = ChaCha20Poly1305(key)

    def seal(self, payload: dict, aad: bytes) -> dict:
        nonce = secrets.token_bytes(12)
        raw = utf8_bytes(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return {"v": PROTOCOL, "n": _b64e(nonce), "c": _b64e(self._aead.encrypt(nonce, raw, aad))}

    def open(self, envelope: Any, aad: bytes) -> dict:
        from cryptography.exceptions import InvalidTag
        if not isinstance(envelope, dict) or envelope.get("v") != PROTOCOL:
            raise ProtocolError("unsupported envelope")
        try:
            nonce = _b64d(str(envelope["n"]))
            data = _b64d(str(envelope["c"]))
            plain = self._aead.decrypt(nonce, data, aad)
            payload = json.loads(plain.decode("utf-8"))
        except (KeyError, ValueError, InvalidTag, UnicodeDecodeError) as e:
            raise ProtocolError("cannot open") from e
        if not isinstance(payload, dict):
            raise ProtocolError("payload is not an object")
        return payload


class ReplayGuard:
    """Отказва заявка извън ±5 минути или с вече виждан идентификатор."""

    def __init__(self, max_skew_ms: int = _MAX_SKEW_MS, *, path: Path | None = None) -> None:
        self._max_skew_ms = max_skew_ms
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        # Видяното се пази и на диска (одит 2026-10-07): само в паметта
        # рестарт на `genesis serve` го губеше и записано от мрежата съобщение
        # („rm -rf build && deploy“) се изпълняваше пак в рамките на 5-те минути.
        self._path = path
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                horizon = time.time() * 1000 - 2 * max_skew_ms
                self._seen = {str(r): float(t) for r, t in data.items()
                              if isinstance(t, (int, float)) and t >= horizon}
            except (OSError, ValueError, AttributeError):
                self._seen = {}

    def check(self, payload: dict, now_ms: float | None = None) -> str:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        rid, ts = payload.get("rid"), payload.get("ts")
        # Само латиница/цифри: rid влиза в AAD на отговора като ASCII — кирилица
        # гърмеше в seal() СЛЕД като операцията вече е изпълнена (одит 2026-10-09).
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid):
            raise ProtocolError("bad rid")
        if not isinstance(ts, (int, float)) or abs(now_ms - ts) > self._max_skew_ms:
            raise ProtocolError("stale request (check the phone's clock)")
        with self._lock:
            horizon = now_ms - 2 * self._max_skew_ms
            for old in [r for r, t in self._seen.items() if t < horizon]:
                del self._seen[old]
            if rid in self._seen:
                raise ProtocolError("replayed request")
            self._seen[rid] = now_ms
            if self._path is not None:
                try:
                    tmp = self._path.with_name(self._path.name + ".tmp")
                    tmp.write_text(json.dumps(self._seen), encoding="utf-8")
                    os.replace(tmp, self._path)
                except OSError:
                    pass  # без запис пазим поне в паметта — както преди
        return rid


# ── сесията: събития, ход на агента, потвърждения ──────────────────────────

@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    allow: bool = False


TurnRunner = Callable[[str, Any], None]


_CLIPPED_NOTE = "\n…[отрязано — целият текст е в терминала]"


def _fit_event(event: dict) -> dict:
    """Копие на събитие, орязано до _REPLY_BYTES: дългите текстове (над 1 KiB)
    делят тавана по равно, по 6 байта на знак (най-лошото в JSON — `\\u0001`),
    с място за кратките полета."""
    long = [k for k, v in event.items() if isinstance(v, str) and len(v) > 1024]
    share = (_REPLY_BYTES // 6 - 4096) // max(1, len(long)) - len(_CLIPPED_NOTE)
    out = dict(event)
    for k in long:
        if len(out[k]) > share:
            out[k] = out[k][:share] + _CLIPPED_NOTE
            out["clipped"] = True
    return out


class RemoteSession:
    """Един разговор, споделен от всички сдвоени телефони.

    Ходовете вървят един по един в отделна нишка; всичко, което агентът
    показва, става събитие с пореден номер. Телефонът пита „какво има след
    N" (дълго чакане до 25 s) — ако е бил офлайн, получава пропуснатото.
    """

    def __init__(self, runner: TurnRunner, *, on_event: Callable[[dict], None] | None = None,
                 state: Callable[[], dict] | None = None) -> None:
        self._runner = runner
        self._on_event = on_event
        # Какво телефонът показва постоянно, не като събитие: режим план и
        # списъкът със задачи. Идва с всеки отговор на `events` (2026-10-09).
        self._state = state
        self._events: deque[dict] = deque(maxlen=_EVENT_LOG)
        self._seq = 0
        self._cond = threading.Condition()
        self._busy = False
        self._stop = threading.Event()
        self._pending: dict[str, _Pending] = {}
        self.epoch = secrets.token_hex(4)

    # -- събития --
    def emit(self, kind: str, **data: Any) -> dict:
        with self._cond:
            self._seq += 1
            event = {"seq": self._seq, "ts": int(time.time() * 1000), "type": kind, **data}
            self._events.append(event)
            self._cond.notify_all()
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass
        return event

    def events_after(self, after: int, wait: float = 0.0) -> dict:
        deadline = time.monotonic() + max(0.0, min(wait, _MAX_WAIT_S))
        with self._cond:
            while self._seq <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            first = self._events[0]["seq"] if self._events else self._seq + 1
            # Пропуснатото вече не е в дневника → телефонът получава целия
            # наличен дневник и знае, че трябва да го покаже наново.
            # after <= 0 — телефон без история (студен старт): и той е reset,
            # иначе над тавана щеше да тегли целия дневник от НАЙ-СТАРИТЕ
            # страница по страница (преглед 2026-10-11: 450 инструмента без
            # завъртян дневник — 7 страници, 3.3 MB вместо 1 страница 0.5 MB).
            reset = after <= 0 or after + 1 < first or after > self._seq
            items = list(self._events) if reset else [e for e in self._events if e["seq"] > after]
            reply = {"events": items, "last": self._seq, "reset": reset,
                     "busy": self._busy, "epoch": self.epoch, "more": False}
        # Размерът се мери извън ключалката — emit() не чака сериализацията.
        # reset: телефонът подменя всичко → най-новите. Иначе той вече показва
        # всичко до `after` → най-старите след него, `last` = последното
        # изпратено и "more": дупка няма, а всеки отговор е до тавана (одит
        # 2026-10-11: 200 инструмента в заключен телефон — изчезваха 130
        # събития и собствените му съобщения).
        ordered = list(reversed(items)) if reset else items
        kept, size, head = 0, 0, 0
        for event in ordered:
            size += len(utf8_bytes(json.dumps(event, ensure_ascii=False)))
            if kept and size > _REPLY_BYTES:
                break
            kept += 1
            head = head or size
        if kept < len(items):
            if reset:
                reply["events"] = items[-kept:]
            else:
                reply["events"], reply["more"] = items[:kept], True
                reply["last"] = items[kept - 1]["seq"]
        if head > _REPLY_BYTES:
            # Едно събитие над тавана влиза винаги (иначе телефонът не мърда)
            # — значи таванът не важеше за него. Вълна 5, 2026-10-11:
            # отговор от 600 000 знака при студен старт — 1.2 MB в един
            # отговор. Сега дългите текстове се орязват до тавана.
            reply["events"] = [_fit_event(ordered[0])]
        if self._state is not None:
            try:
                reply["state"] = self._state()
            except Exception:
                pass   # състоянието е украса — без него разговорът върви
        return reply

    # -- ход --
    @property
    def busy(self) -> bool:
        return self._busy

    def send(self, text: str) -> bool:
        text = text.strip()
        if not text:
            raise ProtocolError("empty message")
        with self._cond:
            if self._busy:
                return False
            self._busy = True
            self._stop.clear()
        self.emit("user", text=text)
        threading.Thread(target=self._run, args=(text,), daemon=True, name="genesis-turn").start()
        return True

    def _run(self, text: str) -> None:
        self.emit("busy", busy=True)
        try:
            self._runner(text, RemoteTurnUI(self))
        except Exception as e:
            self.emit("error", text=f"{type(e).__name__}: {e}")
        finally:
            with self._cond:
                self._busy = False
            self.emit("busy", busy=False)

    def request_stop(self) -> None:
        self._stop.set()
        with self._cond:
            for pending in list(self._pending.values()):
                if not pending.event.is_set():
                    pending.allow = False
                    pending.event.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- потвърждения от sandbox-а --
    def confirm(self, operation: str, reasons: list[str], timeout: float = _CONFIRM_TIMEOUT_S) -> bool:
        # След „Стоп“ нищо не чака отговор: иначе всеки следващ въпрос държеше
        # хода до 5 минути и пак се отказваше (одит 2026-10-09).
        if self._stop.is_set():
            return False
        from genesis_agent import sandbox
        cid = secrets.token_hex(6)
        if sandbox.too_long_to_confirm(operation):
            self.emit("confirm_done", id=cid, allow=False,
                      note=f"операция от {len(operation)} знака — твърде дълга за одобрение")
            return False
        pending = self._pending[cid] = _Pending()
        # Цялата операция: отрязана на 1000 знака без знак, краят ѝ (`&& rm -r src`)
        # се одобряваше невидян (одит 2026-10-09).
        self.emit("confirm", id=cid, operation=operation, reasons=list(reasons)[:10])
        answered = pending.event.wait(timeout)
        self._pending.pop(cid, None)
        allow = answered and pending.allow and not self._stop.is_set()
        self.emit("confirm_done", id=cid, allow=allow,
                  note="" if answered else "няма отговор — отказано")
        return allow

    def answer(self, cid: str, allow: bool) -> bool:
        # Първият отговор печели: второ докосване („Откажи“ и веднага
        # „Изпълни“) не сменя вече даденото решение (одит 2026-10-07).
        with self._cond:
            pending = self._pending.get(cid)
            if pending is None or pending.event.is_set():
                return False
            pending.allow = bool(allow)
            pending.event.set()
        return True


class RemoteTurnUI:
    """TurnUI (genesis_terminal_agent) за телефона: всичко става събитие."""

    def __init__(self, session: RemoteSession) -> None:
        self._s = session

    def thinking(self, label: str, spinner: str = "dots"):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self._s.emit("thinking", label=label)
            yield
        return _cm()

    def assistant(self, text: str) -> None:
        if text.strip():
            self._s.emit("assistant", text=text)

    def tool(self, name: str, result: str) -> None:
        clipped = len(result) > _TOOL_PREVIEW
        self._s.emit("tool", name=name, result=result[:_TOOL_PREVIEW], clipped=clipped)

    def tool_start(self, name: str, detail: str) -> None:
        # Етикетът на хода, докато инструментът върви („RUN_CMD: pytest -q“).
        self._s.emit("thinking", label=f"{name}: {detail}" if detail else name)

    def asked(self, question: str) -> None:
        self._s.emit("asked", text=question)

    def spinning(self, note: str) -> None:
        self._s.emit("warn", text=note)

    def warn(self, text: str) -> None:
        self._s.emit("warn", text=text)

    def info(self, text: str) -> None:
        self._s.emit("info", text=text)

    def cancelled(self) -> bool:
        return self._s.stopped()


# ── командите от телефона ──────────────────────────────────────────────────

# Командите на терминала, които телефонът не може да изпълни (менюта и въпроси
# в конзолата, смяна на модела). Без този отказ стигаха до модела като текст и
# той отговаряше наслуки (2026-10-09).
_TERMINAL_ONLY = frozenset({"/model", "/models", "/agent", "/backup", "/status", "/history",
                            "/export", "/предай", "/update", "/ъпдейт", "/done", "/drop",
                            "/готово", "/maxcoding", "/макскод", "/local_model_max",
                            "/local_model_normal", "/локален_макс", "/локален_нормал"})

PHONE_HELP = """Команди от телефона:
/plan — режим план: само чете и планира; /plan пак — изпълнява плана
/plan <задача> — план за задачата
/undo — връща файловете от последния ход (пита първо)
/rewind — твоите съобщения с номера; /rewind <номер> — назад до него: реже разговора и връща файловете (пита първо)
/resume — по-ранните разговори в тази папка; /resume <номер> — зарежда го
/todos — списъкът със задачи на агента
/agents — под-агентите (.genesis/agents/*.md)
/compact — компресира историята
/context — с какво е пълен контекстът
/review [клон] — преглед на промените за грешки
/doctor — работи ли всичко на компютъра (git, Python, ключове, MCP…)
/memory, /init, /bg, /hooks, /mcp, /commands, /skills, /tasks
/clear — нов разговор
@файл — прикача файл от работната папка
Само в терминала: /model, /history, /backup, /export, /update и доверяването (/hooks trust, /mcp trust)."""


def phone_command(text: str, ui: Any, session: RemoteSession, *, messages: Any,
                  workspace: Path, compact: Callable[[Any], Any] | None = None,
                  context: Callable[[], dict] | None = None,
                  conversations: Any = None) -> tuple[Any, str | None]:
    """Съобщение от телефона, преди да стигне до модела.

    Връща (историята, текст за модела); None за текста = ходът свършва тук.
    Командите са същите като в терминала (chat_commands) — досега телефонът
    пращаше `/plan` и `/undo` на модела като обикновен текст (2026-10-09).
    """
    from genesis_agent import chat_commands
    stripped = text.strip()
    # Същото делене като chat_commands: `/hooks\ntrust` е /hooks trust и тук.
    head, rest = chat_commands._split(stripped)
    cmd = head.lower()
    if stripped.startswith("/"):
        if cmd == "/help":
            ui.info(PHONE_HELP)
            return messages, None
        if cmd in _TERMINAL_ONLY:
            ui.warn(f"{head} е само в терминала на компютъра. /help — какво може оттук.")
            return messages, None
        # Доверяването пуска програми на компютъра при всеки следващ ход, без
        # да пита. Решава се пред целия файл в терминала, не с едно докосване
        # на телефон, който може да е в чужди ръце.
        if cmd in ("/hooks", "/mcp") and rest.strip().lower() == "trust":
            ui.warn(f"{cmd} trust е само в терминала на компютъра — там се вижда целият файл.")
            return messages, None
        # Входът в MCP сървър отваря браузър на компютъра и чака там (2026-10-09).
        if cmd == "/mcp" and chat_commands._split(rest)[0].lower() in ("login", "logout"):
            ui.warn("/mcp login и /mcp logout са само в терминала на компютъра — браузърът е там.")
            return messages, None
        if cmd in ("/skills", "/умения"):
            from genesis_agent.skill_loader import format_skill_list
            ui.info(format_skill_list())
            return messages, None
        if cmd in ("/tasks", "/задачи", "/state"):
            from genesis_agent import workspace_memory as _wm
            ui.info(_wm.briefing(max_threads=20, max_decisions=10) or "Още нищо не е записано.")
            return messages, None

        said: list[str] = []
        answered_at = [0]

        def out(line: str) -> None:
            said.append(line)
            ui.info(line)

        def ask(question: str) -> str:
            # Въпросът на командата (/undo) става карта „Изпълни/Откажи“ на
            # телефона; какво точно ще стане е последното, което командата каза.
            reason = question.split("(", 1)[0].strip(" >") or question
            answered_at[0] = len(said)
            return "да" if session.confirm(said[-1] if said else question, [reason]) else "не"

        # Без `choose`: картата на телефона е да/не, не поле за номер — /resume и
        # /rewind показват списъка, а номерът идва като `/rewind 3` (2026-10-15).
        res = chat_commands.handle(stripped, messages=messages, workspace=workspace,
                                   out=out, ask=ask, compact=compact, context=context,
                                   conversations=conversations)
        if res is not None:
            if res.messages is not None:
                messages = res.messages
                if cmd in _REDRAWN:
                    _redraw(session, messages, said[answered_at[0]:])
            if not res.prompt:
                return messages, None
            stripped = res.prompt
    from genesis_agent import mentions
    try:
        stripped, notes = mentions.expand(stripped, workspace)
    except Exception as e:
        notes = [f"@: {e}"]
    for note in notes:
        ui.info(f"📎 {note}")
    return messages, stripped


# Командите, след които историята в приложението е друга (заредена или
# отрязана) — то я рисува наново, по същите събития като /history.
_REDRAWN = frozenset({"/resume", "/rewind"})


def _redraw(session: RemoteSession, messages: Any, result: list[str]) -> None:
    """"cleared" + репликите (desktop_commands.replay), после какво стана —
    бележките преди "cleared" изчезват с него. „Последният отговор“ не се
    повтаря: той вече е в нарисуваната история."""
    from genesis_agent import desktop_commands
    desktop_commands.replay(session.emit, messages)
    for line in result:
        if not line.startswith("Последният отговор:"):
            session.emit("info", text=line)


def show_agent_progress(session: RemoteSession) -> None:
    """Под-агентите (AGENT) казват какво правят — на телефона като събития.
    Без това там се виждаше само „мисли…“, докато под-агентът работи минути
    наред (2026-10-09)."""
    from genesis_agent import agents

    def say(text: str) -> None:
        session.emit("progress", text=text)
    agents.progress = say


def phone_state() -> dict:
    """Режимът план и задачите — за постоянния ред на телефона."""
    from genesis_agent import plan_mode, todos
    return {"plan": plan_mode.active(), "todos": todos.items()}


# ── HTTP ────────────────────────────────────────────────────────────────────

class RemoteServer:
    """Сглобява ключ, сесия и HTTP сървър. `serve_forever` блокира."""

    def __init__(self, key: bytes, session: RemoteSession, *, name: str,
                 status: Callable[[], dict] | None = None,
                 clear: Callable[[], None] | None = None,
                 commands: Callable[[str, dict], dict] | None = None,
                 web_root: Path | None = None, replay_path: Path | None = None) -> None:
        self.key = key
        self.cipher = Cipher(key)
        self.replay = ReplayGuard(path=replay_path)
        self.session = session
        self.name = name
        self.status = status or (dict)
        self.clear = clear
        self.commands = commands
        self.web_root = web_root
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        # Приетите `send` по идентификатора от телефона (`mid`), за повторен
        # опит след изгубен отговор (одит 2026-10-11: LAN прекъсне, телефонът
        # казва „няма връзка“, операторът натиска пак — ходът вървеше два пъти).
        self._sent: dict[str, dict] = {}
        self._send_lock = threading.Lock()

    # -- заявки --
    def hello(self) -> dict:
        from genesis_agent import __version__
        return {"app": "genesis", "v": PROTOCOL, "key_id": key_id(self.key),
                "name": self.name, "version": __version__,
                "features": ["commands"] if self.commands else []}

    def handle(self, envelope: Any) -> tuple[dict, str]:
        """(отговор, rid) за вече декодиран JSON плик. Хвърля ProtocolError
        САМО за плик, който не се отваря с ключа или е стар/повторен.

        Грешка в самата заявка (непозната операция, празно съобщение) идва в
        запечатания отговор като {"ok": false, "error": …} — одит 2026-10-11:
        беше 401, телефонът го показваше като „ключът вече не се приема,
        сдвои отново“ и го броеше за спирачката срещу налучкване."""
        payload = self.cipher.open(envelope, _REQ_AAD)
        rid = self.replay.check(payload)
        return self._dispatch(payload), rid

    def _dispatch(self, p: dict) -> dict:
        op = p.get("op")
        s = self.session
        if op == "status":
            return {"ok": True, **self.hello(), "busy": s.busy, **self.status()}
        if op == "send":
            mid = p.get("mid")
            if not (isinstance(mid, str) and _MID.fullmatch(mid)):
                return self._send(p)
            with self._send_lock:
                if mid in self._sent:
                    return self._sent[mid]
                reply = self._send(p)
                # Само приетото: „busy“ не е изпълнено — повторният опит
                # трябва да може да мине, щом ходът свърши.
                if reply.get("ok"):
                    self._sent[mid] = reply
                    while len(self._sent) > _SENT_MIDS:
                        del self._sent[next(iter(self._sent))]
                return reply
        if op == "events":
            try:
                after = int(p.get("after") or 0)
                wait = float(p.get("wait") or 0)
            except (TypeError, ValueError, OverflowError):   # "after": Infinity
                return {"ok": False, "error": "bad_args"}
            reply = {"ok": True, **s.events_after(after, wait)}
            # Последните приети `mid` (вълна 5, 2026-10-11): телефонът по тях
            # забравя повторения опит, и когато своето балонче не е в
            # страницата (reset от най-новите, „/clear“ без балонче) — иначе
            # нарочното повторение със стария `mid` се поглъщаше тук.
            with self._send_lock:
                reply["mids"] = list(self._sent)[-_ACK_MIDS:]
            return reply
        if op == "confirm":
            return {"ok": s.answer(str(p.get("id") or ""), bool(p.get("allow")))}
        if op == "stop":
            s.request_stop()
            return {"ok": True}
        if op == "clear":
            if s.busy:
                return {"ok": False, "error": "busy"}
            if self.clear is not None:
                self.clear()
            s.emit("cleared")
            return {"ok": True}
        if op == "command" and self.commands is not None:
            arg = p.get("arg")
            return self.commands(str(p.get("name") or ""), arg if isinstance(arg, dict) else {})
        # По-ново приложение с операция, която този `genesis serve` не знае.
        return {"ok": False, "error": "unknown_op"}

    def _send(self, p: dict) -> dict:
        text = str(p.get("text") or "")
        if len(text) > 20000:
            return {"ok": False, "error": "too_long"}
        # Python strip(), не JS trim(): "\x1c" или "\x85" минават trim() на
        # телефона и тук са празно (одит 2026-10-11).
        if not text.strip():
            return {"ok": False, "error": "empty"}
        if text.strip().lower() in ("/clear", "/нов"):
            return self._dispatch({"op": "clear"})
        return {"ok": True} if self.session.send(text) else {"ok": False, "error": "busy"}

    def seal(self, response: dict, rid: str) -> dict:
        return self.cipher.seal(response, _RES_AAD + rid.encode("ascii"))

    # -- защита от налучкване --
    def too_many_failures(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < 60]
            self._failures[ip] = recent
            return len(recent) >= 30

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.monotonic())

    def make_http(self, host: str, port: int) -> ThreadingHTTPServer:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "genesis"
            sys_version = ""
            # Без таймаут клиент, който не праща тялото, държеше нишка завинаги —
            # 200 такива = 200 нишки, без нито един „провал“ за спирачката (одит
            # 2026-10-09). Чакането на събития е след четенето — не го засяга.
            timeout = 30

            def log_message(self, fmt: str, *args: Any) -> None:  # тихо: дисплеят е събитията
                return

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # Уеб клиентът се сервира от същия адрес; CORS е за Expo в
                # режим на разработка (друг порт). Съдържанието е шифровано,
                # тоест чужд сайт не печели нищо от достъпа.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: dict) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def do_OPTIONS(self) -> None:
                self._send(204, b"")

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/api/hello":
                    self._json(200, server.hello())
                    return
                self._static(path)

            def _static(self, path: str) -> None:
                root = server.web_root
                if root is None or not (root / "index.html").is_file():
                    self._send(200, _LANDING.encode("utf-8"), "text/html; charset=utf-8")
                    return
                rel = path.lstrip("/") or "index.html"
                try:
                    target = (root / rel).resolve()
                    if root.resolve() not in target.parents or not target.is_file():
                        target = root / "index.html"  # едностранично приложение
                except (OSError, ValueError):
                    # NUL в пътя, твърде дълго име — без отговор и с traceback в
                    # терминала на оператора (одит 2026-10-09).
                    target = root / "index.html"
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)

            def _read_by(self, size: int, deadline: float) -> bytes:
                """До `size` байта до `deadline` (time.monotonic) общо, не на recv."""
                parts: list[bytes] = []
                got = 0
                try:
                    while got < size:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise TimeoutError
                        self.connection.settimeout(left)
                        chunk = self.rfile.read1(size - got)
                        if not chunk:
                            break
                        parts.append(chunk)
                        got += len(chunk)
                finally:
                    self.connection.settimeout(self.timeout)
                return b"".join(parts)

            def _braked_body(self, length: int) -> bytes | None:
                """Тялото от спрян адрес, ако започва като плик и идва цяло до
                _BRAKED_READ_S; иначе None — 429, без да се чете останалото.

                Вълна 5, 2026-10-11: над спирачката сървърът пак четеше цялото
                тяло — клиент, обявил 200 KiB и пратил 1 KiB, държеше нишка и
                едно от 16-те места на адреса 30 s на заявка (преди вълна 4:
                429 без четене). Сега първите _HEAD_BYTES трябва да са начало
                на плик. Ревю на вълна 5: таймаутът беше на recv, не общ —
                байт на 1.5 s не стигаше до 429 и след 12 s, а публичното
                начало на плик (~30 байта) и спиране държеше мястото с 30 s на
                recv; 16 такива връзки заключваха сдвоения телефон зад същия
                адрес. Сега целият прочит е до един общ срок; пликовете на
                телефона са няколко KiB."""
                deadline = time.monotonic() + _BRAKED_READ_S
                want = min(length, _HEAD_BYTES)
                try:
                    head = self._read_by(want, deadline)
                    whole = len(head) == length
                    shape = _ENVELOPE_SHAPE if whole else _ENVELOPE_HEAD
                    if len(head) != want or not shape.fullmatch(head):
                        return None
                    body = head + self._read_by(length - len(head), deadline)
                except OSError:   # TimeoutError е OSError
                    return None
                return body if len(body) == length else None

            def _linger_close(self, left: int) -> None:
                """Затваряне след 429 с непрочетено тяло: първо FIN, после до
                `left` байта и до _LINGER_S се изхвърлят. На Windows close() с
                непрочетени данни праща RST — клиентът, който още праща тялото,
                получаваше WinError 10053 вместо 429 (лаптопът, 2026-10-11).
                Срокът е общ и кратък, за да не върне вълна 5 (място за 30 s)."""
                self.close_connection = True
                try:
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_WR)
                    self._read_by(max(left, 0), time.monotonic() + _LINGER_S)
                except OSError:
                    pass

            def do_POST(self) -> None:
                ip = self.client_address[0]
                if self.path.split("?", 1)[0] != "/api/v1":
                    self._json(404, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 < length <= _MAX_BODY:
                    self._json(413, {"error": "size"})
                    return
                try:
                    if server.too_many_failures(ip):
                        braked_body = self._braked_body(length)
                        if braked_body is None:
                            server.record_failure(ip)
                            self.close_connection = True
                            self._json(429, {"error": "slow_down"})
                            self._linger_close(length)
                            return
                        body = braked_body
                    else:
                        body = self.rfile.read(length)
                except OSError:   # таймаут — броим го като провал за спирачката
                    server.record_failure(ip)
                    self.close_connection = True
                    return
                # Спирачката е СЛЕД опита да отворим плика (одит 2026-10-11):
                # преди беше първа и по адрес — страница в браузъра на самия
                # телефон (30 POST-а text/plain) или съсед зад същия NAT
                # заключваше сдвоения телефон. Заявка, която се отваря с ключа и
                # минава ReplayGuard, не се спира; провалите се броят както
                # преди и над 30/мин получават 429 без подробност. Над
                # спирачката само тяло с вида на плик стига до json.loads и
                # ключа (_ENVELOPE_SHAPE); всичко друго — 429 веднага.
                if server.too_many_failures(ip) and not _ENVELOPE_SHAPE.fullmatch(body):
                    server.record_failure(ip)
                    self._json(429, {"error": "slow_down"})
                    return
                try:
                    envelope = json.loads(body.decode("utf-8"))
                    response, rid = server.handle(envelope)
                # RecursionError: `[` × 200 000 без ключ — traceback в терминала
                # на оператора на всяка заявка и без провал за спирачката (одит
                # 2026-10-11).
                except (ProtocolError, ValueError, UnicodeDecodeError, RecursionError) as e:
                    braked = server.too_many_failures(ip)
                    server.record_failure(ip)
                    if braked:
                        self._json(429, {"error": "slow_down"})
                        return
                    # Подробността е само вида на отказа („cannot open",
                    # „stale request"): полезна на своя телефон, безполезна
                    # на непознат, който няма ключа.
                    self._json(401, {"error": "rejected", "detail": str(e)[:80]})
                    return
                self._json(200, server.seal(response, rid))

        httpd = _QuietServer((host, port), Handler)
        httpd.daemon_threads = True
        return httpd


class _QuietServer(ThreadingHTTPServer):
    """Телефон, който заспи или смени мрежата насред дългото чакане, затваря
    връзката — това е нормално, не грешка за печатане в терминала.

    Най-много _MAX_CONNECTIONS връзки наведнъж: над тях новата се затваря
    веднага, вместо да ражда още една нишка (одит 2026-10-09). И най-много
    _MAX_PER_IP от един адрес (одит 2026-10-11): 64 празни връзки от един
    съсед без ключ заемаха всички места за 30 s и сдвоеният телефон не
    можеше да влезе, а спирачката не виждаше провал."""

    _MAX_CONNECTIONS = 64
    _MAX_PER_IP = 16

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self._MAX_CONNECTIONS)
        self._per_ip: dict[str, int] = {}
        self._per_ip_lock = threading.Lock()

    @staticmethod
    def _ip(client_address: Any) -> str:
        try:
            return str(client_address[0])
        except (TypeError, IndexError):
            return ""

    def _release(self, client_address: Any) -> None:
        ip = self._ip(client_address)
        with self._per_ip_lock:
            left = self._per_ip.get(ip, 0) - 1
            if left > 0:
                self._per_ip[ip] = left
            else:
                self._per_ip.pop(ip, None)
        self._slots.release()

    def process_request(self, request: Any, client_address: Any) -> None:
        ip = self._ip(client_address)
        with self._per_ip_lock:
            full = self._per_ip.get(ip, 0) >= self._MAX_PER_IP
            if not full and self._slots.acquire(blocking=False):
                self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            else:
                full = True
        if full:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(client_address)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".ttf": "font/ttf", ".woff2": "font/woff2", ".map": "application/json",
}

_LANDING = """<!doctype html><html lang="bg"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Genesis</title>
<body style="font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1>Genesis е тук</h1>
<p>Отвори приложението <b>Genesis Remote</b> на телефона и сканирай QR кода от
терминала на компютъра. Уеб версията не е включена в тази инсталация.</p>
</body></html>"""


# ── `genesis serve` ─────────────────────────────────────────────────────────

# Какво приложението приема за адрес (BASE в mobile/src/lib/protocol.ts).
_HOST = re.compile(r"^(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[?[0-9A-Fa-f:.]+\]?)$")


def valid_host(host: str) -> bool:
    """Адрес, който телефонът ще прочете от кода. `PC_1.local` или `user@pc`
    правеха код, който приложението тихо отхвърляше (одит 2026-10-09)."""
    return bool(_HOST.match(host))


def _print_qr(console: Any, url: str) -> None:
    try:
        import qrcode  # type: ignore[import-untyped]
    except ImportError:
        console.print("[yellow](за QR код: pip install qrcode — или въведи адреса ръчно)[/]")
        return
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    # Два реда модули на един ред текст (▀ ▄ █): кодът е квадратен и се
    # чете от телефон и в малък прозорец. Цветовете са зададени изрично, за
    # да е тъмно-на-светло и в тъмна тема на терминала.
    lines = []
    for y in range(0, len(matrix), 2):
        row = []
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bottom = matrix[y + 1][x] if y + 1 < len(matrix) else False
            # Тъмният модул е черен знак на бял фон — обърнат код камерата
            # на iPhone не чете надеждно.
            row.append({(True, True): "█", (True, False): "▀",
                        (False, True): "▄", (False, False): " "}[(top, bottom)])
        lines.append("".join(row))
    console.print("\n".join(lines), style="black on white", highlight=False)


def _web_root() -> Path | None:
    from genesis_agent.paths import PACKAGE_DIR
    root = PACKAGE_DIR / "web"
    return root if (root / "index.html").is_file() else None


@dataclass
class ServeOptions:
    port: int = DEFAULT_PORT
    host: str = ""          # адресът в QR кода; празно — първият LAN адрес
    bind: str = "0.0.0.0"   # къде слуша сървърът
    reset: bool = False
    web: bool = False       # QR код за уеб версията (iPhone без приложението)


def parse_serve_args(args: list[str]) -> ServeOptions | int:
    """Опциите на `genesis serve` — или код за изход (--help, грешна опция)."""
    opts = ServeOptions()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            i += 1
            try:
                opts.port = int(args[i])
            except ValueError:
                print(f"--port иска число, не {args[i]!r}")
                return 2
        elif a == "--host" and i + 1 < len(args):
            i += 1
            opts.host = args[i]
        elif a == "--bind" and i + 1 < len(args):
            i += 1
            opts.bind = args[i]
        elif a == "--reset":
            opts.reset = True
        elif a == "--web":
            opts.web = True
        elif a in ("-h", "--help"):
            print(SERVE_USAGE)
            return 0
        else:
            print(f"Непозната опция: {a}\n\n{SERVE_USAGE}")
            return 2
        i += 1
    return opts


def serve(args: list[str]) -> int:
    """`genesis serve [--port N] [--host IP] [--bind IP] [--reset] [--web]`."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("`genesis serve` иска cryptography:  pip install \"genesis-agent[mobile]\"")
        return 2

    opts = parse_serve_args(args)
    if isinstance(opts, int):
        return opts
    port, host_override, bind, reset, web = opts.port, opts.host, opts.bind, opts.reset, opts.web
    # Слуша само на един адрес (127.0.0.1 за Genesis Desktop) → той е и в QR кода.
    if not host_override and bind not in ("", "0.0.0.0"):
        host_override = bind
    if host_override and not valid_host(host_override):
        print(f"--host {host_override!r}: телефонът не може да го прочете — IP адрес или име "
              "от латиница, цифри, точки и тирета.")
        return 2
    if web and _web_root() is None:
        # Уеб версията е само в Windows приложението; другаде кодът водеше до
        # страница „не е включена“ (одит 2026-10-09).
        print("--web: уеб версията не е в тази инсталация (идва с Windows приложението). "
              "Пусни без --web и сдвои с приложението Genesis Remote.")
        return 2

    key = load_or_create_key(reset=reset)
    from rich.panel import Panel
    from rich.text import Text

    import genesis_terminal_agent as gta

    console = gta.console
    system_prompt, _briefing = gta.build_system_prompt()
    state: dict[str, Any] = {"messages": deque([{"role": "system", "content": system_prompt}],
                                               maxlen=gta._HISTORY_MAXLEN)}

    def runner(text: str, ui: Any) -> None:
        messages, prompt = phone_command(text, ui, session, messages=state["messages"],
                                         workspace=Path(gta.WORKSPACE), compact=gta._force_compact,
                                         context=gta.context_info, conversations=gta._conversations())
        state["messages"] = messages
        if prompt is not None:
            try:
                state["messages"] = gta.run_turn(state["messages"], prompt, ui)
            except Exception as e:
                # Свършеното до грешката остава в разговора (одит 2026-10-11).
                state["messages"] = gta.history_after_error(e, state["messages"])
                raise

    def clear() -> None:
        # Както /clear в терминала: нов файл за историята (иначе следващият
        # разговор презаписваше файла на предишния — одит 2026-10-09) и
        # системният промпт наново (GENESIS.md, агентите, MCP).
        nonlocal system_prompt
        try:
            system_prompt, _ = gta.build_system_prompt()
        except Exception:
            pass
        gta._new_session()
        state["messages"] = deque([{"role": "system", "content": system_prompt}],
                                  maxlen=gta._HISTORY_MAXLEN)
        # Нов разговор от телефона/приложението = нова сесия и за брояча
        # (NEXT_STEPS В.8): иначе статус редът носеше Σ на стария разговор.
        gta.reset_usage()
        from genesis_agent import todos
        todos.clear()   # както /clear в терминала

    def status() -> dict:
        return {"model": f"{gta.current_provider}/{gta.current_model_id}",
                "workspace": str(gta.WORKSPACE)}

    # Текстът от телефона и от модела — като текст, не като Rich разметка: `[/]`
    # изтриваше реда, а `rm -r [bold]build[/bold] src` се показваше като
    # `rm -r build src` в записа на одобреното (одит 2026-10-09).
    def show(event: dict) -> None:
        kind = event["type"]
        if kind == "user":
            console.print(Text.assemble("\n", ("📱 ❯ ", "bold green"), str(event["text"])))
        elif kind == "assistant":
            gta.RICH_UI.assistant(event["text"])
        elif kind == "tool":
            gta.RICH_UI.tool(event["name"], event["result"])
        elif kind in ("warn", "asked", "error"):
            console.print(Text(f"⚠ {event['text']}", style="yellow"))
        elif kind in ("info", "progress"):
            console.print(Text(str(event["text"]), style="dim"))
        elif kind == "confirm":
            console.print(Text.assemble(("📱 Чака потвърждение от телефона: ", "bold yellow"),
                                        str(event["operation"])))
        elif kind == "confirm_done":
            console.print(Text(f"   → {'разрешено' if event['allow'] else 'отказано'} "
                               f"{event.get('note', '')}", style="dim"))

    session = RemoteSession(runner, on_event=show, state=phone_state)
    show_agent_progress(session)

    from genesis_agent import sandbox as _sandbox
    _sandbox.set_policy(_sandbox.SandboxPolicy(
        mode="interactive",
        confirm_fn=lambda op, verdict: session.confirm(op, list(verdict.reasons))))

    name = socket.gethostname()
    from genesis_agent import desktop_commands

    def set_messages(messages: Any) -> None:
        state["messages"] = messages

    ctx = desktop_commands.CommandContext(
        get_messages=lambda: state["messages"], set_messages=set_messages,
        busy=lambda: session.busy, emit=session.emit)
    server = RemoteServer(key, session, name=name, status=status, clear=clear,
                          commands=lambda n, a: desktop_commands.run(n, a, ctx),
                          web_root=_web_root(),
                          replay_path=_config_path().with_name("remote_seen.json"))
    try:
        httpd = server.make_http(bind, port)
    except OSError as e:
        print(f"Не мога да слушам на порт {port}: {e}. Друг порт: genesis serve --port 8766")
        return 1

    hosts = [host_override] if host_override else (lan_addresses() or ["127.0.0.1"])
    url = pairing_url(hosts[0], port, key, name, web=web)
    console.print(Panel(Text.assemble(
        (("Отвори кода с камерата на телефона — отваря уеб версията в браузъра:\n" if web else
          "Сканирай с телефона (приложението Genesis Remote или камерата — отваря приложението):\n"),
         "bold"),
        ("Работна папка: ", "dim"), (str(gta.WORKSPACE), ""),
        ("\nАдрес: ", "dim"), (f"http://{hosts[0]}:{port}", "cyan"),
        (("\nКлючът е само в QR кода. Нов ключ (отнема достъпа на всички телефони): "
          "genesis serve --reset"), "dim")),
        title="📱 Genesis за телефона", border_style="cyan"))
    if web:
        console.print(Text(
            "⚠ --web: кодът отваря уеб версията по обикновен HTTP. Който е в същата мрежа, може "
            "да подмени страницата и да види ключа — само вкъщи. С приложението: без --web.",
            style="yellow"))
    _print_qr(console, url)
    # Същото като текст — за поставяне в приложението, ако камерата не
    # хване кода. Терминалът е на машината на оператора, не в мрежата.
    console.print(f"[dim]Връзка:[/] {url}", highlight=False, soft_wrap=True)
    if len(hosts) > 1:
        console.print(f"[dim]Други адреси на тази машина: {', '.join(hosts[1:])} "
                      "(genesis serve --host <адрес>)[/]")
    if os.name == "nt":
        console.print("[dim]Windows пита за защитната стена при първото пускане — "
                      "разреши „Private networks“, иначе телефонът няма да стигне дотук.[/]")
    console.print("[dim]Ctrl+C спира.[/]")

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        session.request_stop()
        try:
            from genesis_agent import workspace_memory as _wm
            convo = [m for m in state["messages"] if m.get("role") in ("user", "assistant")]
            if len(convo) >= 2:
                _wm.auto_capture(list(convo))
        except Exception:
            pass
    console.print("\n[dim]Сървърът за телефона е спрян.[/]")
    return 0


SERVE_USAGE = """Употреба: genesis serve [--port N] [--host IP] [--bind IP] [--reset] [--web]

Пуска Genesis за телефона (приложението Genesis Remote за Android и iOS):
показва QR код, който сдвоява телефона с тази машина.

  --port N    порт (по подразбиране 8765)
  --host IP   адрес в QR кода — напр. Tailscale адрес за достъп извън дома
  --bind IP   на кой адрес да слуша (по подразбиране 0.0.0.0; 127.0.0.1 —
              само тази машина, както го пуска Genesis Desktop)
  --reset     нов ключ; всички сдвоени телефони губят достъп
  --web       QR код за уеб версията (iPhone без приложението) — по HTTP, само вкъщи"""
