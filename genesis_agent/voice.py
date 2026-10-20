"""genesis_agent.voice — глас и микрофон: говор → текст, текст → говор, запис.

Обратно на решението от 2026-09-23 (PR #2 махна GUI-то и гласа): от
2026-10-15 операторът иска Genesis с глас — в терминала (`/listen`,
`/voice on`) и от телефона/Desktop (`transcribe` и `speak` в
remote_server.py). Пише се наново; от стария genesis_jarvis.py са взети само
идеите: махане на markdown преди говорене, edge-tts с офлайн резерва.

Публичното API (стабилно — телефонът и Desktop стъпват на него):

    transcribe(audio, mime="audio/wav", language="bg") -> str
    synthesize(text, voice=None) -> (bytes, mime)
    speak(text) -> нишката, която говори (или None — няма какво да се каже)
    record(max_seconds=60, stop_event=None) -> WAV байтове, 16 kHz, моно
    speech_text(text) -> текстът за изговаряне (без markdown, с таван)

Всички грешки за оператора са VoiceError с текст на български — без ключове.

Говор → текст (по ред, `voice.stt.backends` в config.yaml или
GENESIS_STT_BACKENDS):
  groq    Whisper на Groq (/audio/transcriptions, OpenAI-съвместим) със
          същите GROQ_API_KEY[_2.._10], които ползва brain.py. Безплатен слой.
  openai  произволен OpenAI-съвместим адрес — САМО ако е изрично настроен
          (voice.stt.openai_base_url / GENESIS_STT_BASE_URL). OpenRouter не
          е тук: няма /audio/transcriptions (2026-10-15).
  local   faster-whisper, ако е инсталиран (`genesis-agent[voice-local]`):
          офлайн, след първото сваляне на модела.

Текст → говор (voice.tts.backends / GENESIS_TTS_BACKENDS):
  edge     edge-tts (онлайн, безплатно; bg-BG-KalinaNeural / BorislavNeural)
  espeak   espeak-ng (офлайн; роботизиран, но има български)
  pyttsx3  SAPI5/espeak през pyttsx3 — само ако има БЪЛГАРСКИ глас

Сигурност: аудиото не се пише на диск, освен във временна папка, която се
трие веднага (pyttsx3 и MCI на Windows искат файл); праща се само до
избрания доставчик на говор → текст; ключовете не се печатат и не се логват.
Разпознатият текст минава по пътя на написания (sandbox-ът е същият) —
терминалът и телефонът винаги го показват за потвърждение, преди да тръгне.
"""
from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import queue
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

log = logging.getLogger("genesis.voice")


def _hints(frozen: bool) -> tuple[str, str]:
    """Съвет „какво да инсталираш“ — в genesis.exe pip няма смисъл.

    2026-10-15: genesis.exe вече носи sounddevice, edge-tts и Pillow
    (packaging/genesis.spec); ако някой от тях липсва там, поправката е по-нов
    exe, а `pip install` в чужд Python не помага. faster-whisper нарочно не е
    в exe-то (стотици MB) — офлайн разпознаването иска pip инсталацията."""
    if frozen:
        return ("/update — по-нов genesis.exe носи гласа",
                ("офлайн разпознаването го няма в genesis.exe — инсталирай "
                 'с pip: pip install "genesis-agent[voice-local]"'))
    return 'pip install "genesis-agent[voice]"', 'pip install "genesis-agent[voice-local]"'


INSTALL_HINT, LOCAL_HINT = _hints(bool(getattr(sys, "frozen", False)))

SAMPLE_RATE = 16_000
# Таваните за един запис (и за `transcribe` от телефона): 2 минути и 10 MB.
# Groq приема до 25 MB на безплатния слой; 10 MB държи плика на телефона под
# 19 MiB (виж remote_server._MAX_VOICE_BODY).
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0
# Под това записът е случайно натискане, не реч (0.3 s = 9 600 байта PCM).
MIN_AUDIO_SECONDS = 0.3

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_KEY_ENVS = ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 11)]

# mime → разширение за multipart качването (Whisper разпознава формата по
# името). `audio/webm;codecs=opus` от браузъра → `audio/webm`.
_MIME_EXT = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "m4a",
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/opus": "ogg",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/flac": "flac", "audio/x-flac": "flac",
}


class VoiceError(RuntimeError):
    """Грешка за оператора — на български, без ключове и без traceback."""


class _Skip(Exception):
    """Този доставчик не е наличен (няма ключ, не е инсталиран) — следващият."""


# ── настройки ───────────────────────────────────────────────────────────────

@dataclass
class Settings:
    language: str = "bg"
    stt_backends: tuple[str, ...] = ("groq", "openai", "local")
    # large-v3 пръв: за български е по-точният (Groq го описва като „best
    # accuracy, multilingual“), turbo е резерва с отделен лимит. Не е мерено с
    # глас на оператора — виж NEXT_STEPS (лаптопът).
    groq_models: tuple[str, ...] = ("whisper-large-v3", "whisper-large-v3-turbo")
    openai_base_url: str = ""
    openai_key_env: str = "OPENAI_API_KEY"
    openai_model: str = "whisper-1"
    local_model: str = "small"
    # Подсказка за Whisper (думи, които да очаква) — празно по подразбиране.
    stt_prompt: str = ""
    stt_timeout_s: float = 60.0
    tts_backends: tuple[str, ...] = ("edge", "espeak", "pyttsx3")
    tts_voice: str = "bg-BG-KalinaNeural"
    max_sentences: int = 4
    max_chars: int = 600
    mic_max_seconds: float = 60.0
    # Тишина след реч, която спира записа сама; 0 = само с клавиш.
    silence_seconds: float = 3.0
    speak_answers: bool = False


def _names(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [v.strip().lower() for v in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip().lower() for v in value]
    else:
        return default
    return tuple(v for v in items if v) or default


def _num(value: Any, default: float) -> float:
    # `.inf`/`.nan` от YAML са float, но int() от тях е OverflowError/ValueError
    # и сриваха settings() → speak() и `speak` от телефона (2026-10-20).
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _section() -> dict:
    """`voice:` от config.yaml — празно, ако го няма или файлът е счупен."""
    import yaml

    from genesis_agent.paths import CONFIG_PATH
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        log.debug("voice: config.yaml не се прочете: %s", e)
        return {}
    section = data.get("voice") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def settings() -> Settings:
    """config.yaml `voice:` и отгоре променливите на средата."""
    sec = _section()
    stt, tts, mic = (v if isinstance(v := sec.get(k), dict) else {} for k in ("stt", "tts", "mic"))
    d = Settings()
    env = os.environ.get
    s = Settings(
        language=str(env("GENESIS_VOICE_LANGUAGE") or sec.get("language") or d.language).strip().lower(),
        stt_backends=_names(env("GENESIS_STT_BACKENDS") or stt.get("backends"), d.stt_backends),
        groq_models=_names(env("GENESIS_STT_GROQ_MODELS") or stt.get("groq_models"), d.groq_models),
        openai_base_url=str(env("GENESIS_STT_BASE_URL") or stt.get("openai_base_url") or "").strip(),
        openai_key_env=str(env("GENESIS_STT_KEY_ENV") or stt.get("openai_key_env") or d.openai_key_env),
        openai_model=str(env("GENESIS_STT_MODEL") or stt.get("openai_model") or d.openai_model),
        local_model=str(env("GENESIS_STT_LOCAL_MODEL") or stt.get("local_model") or d.local_model),
        stt_prompt=str(stt.get("prompt") or ""),
        stt_timeout_s=_num(stt.get("timeout_s"), d.stt_timeout_s),
        tts_backends=_names(env("GENESIS_TTS_BACKENDS") or tts.get("backends"), d.tts_backends),
        tts_voice=str(env("GENESIS_TTS_VOICE") or tts.get("voice") or d.tts_voice),
        max_sentences=int(_num(tts.get("max_sentences"), d.max_sentences)),
        max_chars=int(_num(tts.get("max_chars"), d.max_chars)),
        mic_max_seconds=_num(mic.get("max_seconds"), d.mic_max_seconds),
        silence_seconds=_num(mic.get("silence_seconds"), d.silence_seconds),
        speak_answers=str(env("GENESIS_VOICE_SPEAK") or sec.get("speak_answers") or "").strip().lower()
        in ("1", "true", "yes", "on", "да"),
    )
    s.mic_max_seconds = min(max(s.mic_max_seconds, 1.0), MAX_AUDIO_SECONDS)
    # timeout_s ≤ 0 → requests хвърля ValueError преди мрежата и от телефона
    # ставаше 401 „rejected“ + спирачката по IP (2026-10-20).
    s.stt_timeout_s = max(1.0, s.stt_timeout_s)
    s.max_sentences = max(1, s.max_sentences)
    s.max_chars = max(80, s.max_chars)
    return s


# ── аудио помощници ─────────────────────────────────────────────────────────

def normalize_mime(mime: str) -> str:
    """`audio/webm;codecs=opus` → `audio/webm`; непознат формат → VoiceError."""
    base = str(mime or "").split(";", 1)[0].strip().lower()
    if base not in _MIME_EXT:
        raise VoiceError(f"Непознат аудио формат: {base or '(празен)'} — приемам wav, m4a, webm, "
                         "ogg, mp3, flac.")
    return base


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _mdat_ranges(audio: bytes) -> list[tuple[int, int]]:
    """[начало, край) на `mdat` кутиите на горно ниво — там е звукът, а в него
    четирите байта „mvhd“ се срещат случайно (~0.2 % за 10 MB). Непознат
    строеж → празен списък (търсенето остава както преди)."""
    ranges: list[tuple[int, int]] = []
    pos, n = 0, len(audio)
    while pos + 8 <= n:
        size = int.from_bytes(audio[pos:pos + 4], "big")
        kind = audio[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > n:
                break
            size, header = int.from_bytes(audio[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = n - pos
        if size < header:
            break
        if kind == b"mdat":
            ranges.append((pos, pos + size))
        pos += size
    return ranges


def _mp4_seconds(audio: bytes) -> float | None:
    """Продължителността от `mvhd` на m4a/mp4 (телефонът записва така).
    Телефонът пише `moov` СЛЕД `mdat`, затова „mvhd“ вътре в звука се прескача
    (2026-10-20: случайно съвпадение даваше 1000 s и „твърде дълъг“ завинаги)."""
    skip = _mdat_ranges(audio)
    at = audio.find(b"mvhd")
    while at >= 0 and any(lo <= at < hi for lo, hi in skip):
        at = audio.find(b"mvhd", at + 4)
    if at < 0:
        return None
    try:
        version = audio[at + 4]
        if version == 1:
            timescale, duration = struct.unpack(">IQ", audio[at + 24:at + 36])
        else:
            timescale, duration = struct.unpack(">II", audio[at + 16:at + 24])
    except (IndexError, struct.error):
        return None
    return duration / timescale if timescale else None


def audio_seconds(audio: bytes, mime: str) -> float | None:
    """Колко секунди е записът, ако форматът го казва евтино; иначе None."""
    kind = _MIME_EXT.get(str(mime).split(";", 1)[0].strip().lower())
    if kind == "wav":
        try:
            with wave.open(io.BytesIO(audio), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / rate if rate else None
        # RuntimeError: блок с размер над файла (wave/chunk.seek) — ревю
        # 2026-10-15, от телефона това прекъсваше връзката с traceback.
        except (wave.Error, EOFError, RuntimeError, ValueError, struct.error):
            return None
    if kind == "m4a":
        return _mp4_seconds(audio)
    return None


def _scrub(text: str, secrets: list[str] | None = None) -> str:
    """Текст от доставчик/библиотека за пред оператора: без ключове, на един ред."""
    from genesis_agent.notifier import redact
    for value in secrets or []:
        if value:
            text = text.replace(value, "***")
    return redact(" ".join(str(text).split()))[:200]


# ── говор → текст ───────────────────────────────────────────────────────────

@dataclass
class Transcript:
    text: str
    provider: str
    model: str
    seconds: float


def _keys() -> dict[str, str]:
    from genesis_agent.brain import _load_keys
    return _load_keys()


def groq_keys() -> list[str]:
    keys = _keys()
    return [str(keys.get(n) or "").strip() for n in _GROQ_KEY_ENVS if str(keys.get(n) or "").strip()]


def _post_audio(url: str, key: str, *, model: str, audio: bytes, mime: str, language: str,
                prompt: str, verbose: bool, timeout: float) -> Any:
    """Една заявка към `/audio/transcriptions` (multipart, като OpenAI)."""
    import requests
    data = {"model": model, "temperature": "0",
            "response_format": "verbose_json" if verbose else "json"}
    if language and language != "auto":
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    files = {"file": (f"audio.{_MIME_EXT[mime]}", audio, mime)}
    return requests.post(url, headers=headers, data=data, files=files, timeout=timeout)


def _parse_reply(r: Any) -> tuple[str, float | None]:
    try:
        body = r.json()
    except ValueError:
        return str(getattr(r, "text", "") or "").strip(), None
    if not isinstance(body, dict):
        return "", None
    duration = body.get("duration")
    return (str(body.get("text") or "").strip(),
            float(duration) if isinstance(duration, (int, float)) else None)


def _stt_http(provider: str, url: str, keys: list[str], models: tuple[str, ...], audio: bytes,
              mime: str, language: str, cfg: Settings, *, verbose: bool) -> Transcript:
    """Ключ по ключ, модел по модел. 401/403/429 → следващ ключ; друга HTTP
    грешка → следващ модел; няма връзка → следващият доставчик."""
    import requests
    reasons: list[str] = []
    for model in models:
        for i, key in enumerate(keys or [""], 1):
            label = f"{provider}/{model}" + (f" (ключ №{i})" if len(keys) > 1 else "")
            try:
                r = _post_audio(url, key, model=model, audio=audio, mime=mime, language=language,
                                prompt=cfg.stt_prompt, verbose=verbose, timeout=cfg.stt_timeout_s)
            except (requests.RequestException, ValueError) as e:
                # ValueError: requests отказва timeout ≤ 0 или счупен адрес —
                # грешка на настройката, не на мрежата, но пак VoiceError.
                raise VoiceError(f"{provider}: няма връзка ({type(e).__name__})") from None
            if r.status_code == 200:
                text, seconds = _parse_reply(r)
                return Transcript(text, provider, model, seconds or 0.0)
            reasons.append(f"{label}: HTTP {r.status_code} {_scrub(r.text or '', keys)}".strip())
            if r.status_code not in (401, 403, 429):
                break
    raise VoiceError("; ".join(reasons) or f"{provider}: без отговор")


def _stt_groq(audio: bytes, mime: str, language: str, cfg: Settings) -> Transcript:
    keys = groq_keys()
    if not keys:
        raise _Skip("groq: няма GROQ_API_KEY")
    return _stt_http("groq", GROQ_URL, keys, cfg.groq_models, audio, mime, language, cfg,
                     verbose=True)


def _stt_openai(audio: bytes, mime: str, language: str, cfg: Settings) -> Transcript:
    base = cfg.openai_base_url.rstrip("/")
    if not base:
        raise _Skip("openai: не е настроен (voice.stt.openai_base_url)")
    key = str(_keys().get(cfg.openai_key_env) or "").strip()
    # `json`, не `verbose_json`: gpt-4o-*-transcribe приемат само json/text.
    return _stt_http("openai", f"{base}/audio/transcriptions", [key] if key else [],
                     (cfg.openai_model,), audio, mime, language, cfg, verbose=False)


_LOCAL_MODELS: dict[str, Any] = {}
_LOCAL_LOCK = threading.Lock()


def _stt_local(audio: bytes, mime: str, language: str, cfg: Settings) -> Transcript:
    if find_spec("faster_whisper") is None:
        raise _Skip(f"local: faster-whisper не е инсталиран ({LOCAL_HINT})")
    try:
        with _LOCAL_LOCK:
            model = _LOCAL_MODELS.get(cfg.local_model)
            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(cfg.local_model, device="auto", compute_type="int8")
                _LOCAL_MODELS[cfg.local_model] = model
            lang = None if language in ("", "auto") else language
            segments, info = model.transcribe(io.BytesIO(audio), language=lang, beam_size=5,
                                              vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:   # ctranslate2/av хвърлят свои типове — само съобщението стига до оператора
        log.debug("faster-whisper гръмна", exc_info=True)
        raise VoiceError(f"local/{cfg.local_model}: {type(e).__name__}: {_scrub(str(e))}") from None
    return Transcript(text, "local", f"faster-whisper-{cfg.local_model}",
                      float(getattr(info, "duration", 0.0) or 0.0))


STT_BACKENDS: dict[str, Callable[[bytes, str, str, Settings], Transcript]] = {
    "groq": _stt_groq, "openai": _stt_openai, "local": _stt_local}


def _record_budget(provider: str, model: str, seconds: float, kind: str) -> None:
    from genesis_agent import budget
    budget.record_usage(provider=provider, model=model, prompt_tokens=0, completion_tokens=0,
                        context=f"voice:{kind}", audio_seconds=round(seconds, 2))


def transcribe_detail(audio: bytes, mime: str = "audio/wav", language: str = "bg", *,
                      cfg: Settings | None = None) -> Transcript:
    """Като `transcribe`, но казва и кой доставчик/модел и колко секунди."""
    cfg = cfg or settings()
    mime = normalize_mime(mime)
    if not audio:
        raise VoiceError("Записът е празен.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(f"Записът е {len(audio) / 1048576:.1f} MB — таванът е "
                         f"{MAX_AUDIO_BYTES // 1048576} MB.")
    known = audio_seconds(audio, mime)
    if known is not None and known > MAX_AUDIO_SECONDS + 1:
        raise VoiceError(f"Записът е {known:.0f} s — таванът е {MAX_AUDIO_SECONDS:.0f} s.")
    language = (language or cfg.language or "bg").strip().lower()
    skipped: list[str] = []
    failed: list[str] = []
    for name in cfg.stt_backends:
        backend = STT_BACKENDS.get(name)
        if backend is None:
            skipped.append(f"{name}: непознат")
            continue
        try:
            result = backend(audio, mime, language, cfg)
        except _Skip as e:
            skipped.append(str(e))
            continue
        except VoiceError as e:
            failed.append(str(e))
            continue
        result.seconds = result.seconds or known or 0.0
        _record_budget(result.provider, result.model, result.seconds, "stt")
        return result
    if not failed:
        raise VoiceError("Няма с какво да превърна гласа в текст: " + "; ".join(skipped)
                         + ". Сложи GROQ_API_KEY (безплатен: https://console.groq.com/keys) в "
                         f"~/.genesis/.env или офлайн модел: {LOCAL_HINT}")
    raise VoiceError("Гласът не стана текст: " + "; ".join(failed + skipped))


def transcribe(audio: bytes, mime: str = "audio/wav", language: str = "bg") -> str:
    """Говор → текст. VoiceError с причината, ако нито един доставчик не стане."""
    return transcribe_detail(audio, mime, language).text


# ── текст за изговаряне ─────────────────────────────────────────────────────

REST_ON_SCREEN = "Останалото е на екрана."
CODE_ON_SCREEN = "Кодът е на екрана."

_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
                    "\U0000FE0F\U00002B00-\U00002BFF\U0001F000-\U0001F2FF]+")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+")
_INLINE_CODE = re.compile(r"`+([^`]*)`+")
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|~~)(\S(?:.*?\S)?)\1")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def speech_text(text: str, *, max_sentences: int | None = None, max_chars: int | None = None) -> str:
    """Отговорът така, както се казва на глас: без код, без markdown, кратко.

    Без това TTS-ът четеше „звезда звезда Готово“, адреси буква по буква и
    цели функции (идеята е от стария genesis_jarvis.py). Дълъг отговор: първите
    `max_sentences` изречения, до `max_chars` знака, и „Останалото е на екрана.“
    """
    cfg = settings() if max_sentences is None or max_chars is None else None
    n = max_sentences if max_sentences is not None else (cfg.max_sentences if cfg else 4)
    limit = max_chars if max_chars is not None else (cfg.max_chars if cfg else 600)
    had_code = bool(_FENCE.search(text or ""))
    prose = _FENCE.sub("\n", text or "")
    prose = _HTML.sub(" ", prose)
    prose = _IMAGE.sub(r"\1", prose)
    prose = _LINK.sub(r"\1", prose)
    prose = _URL.sub("линк", prose)
    prose = _INLINE_CODE.sub(r"\1", prose)
    prose = _EMOJI.sub("", prose)
    lines: list[str] = []
    for raw in prose.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"[-*_=|:\s]{3,}", line):   # ---, ***, |---|---|
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^(>\s*)+", "", line)
        line = re.sub(r"^([-*+•]|\d+[.)])\s+", "", line)
        if line.startswith("|"):
            line = ", ".join(c.strip() for c in line.strip("|").split("|") if c.strip())
        line = _EMPHASIS.sub(r"\2", line)
        line = line.replace("→", ",").replace("—", ",").replace("*", "").replace("#", "")
        line = " ".join(line.split())
        if line:
            lines.append(line if line[-1] in ".!?…:;," else line + ".")
    spoken = " ".join(lines).strip()
    sentences = [s for s in _SENTENCE.split(spoken) if s]
    cut = len(sentences) > n
    spoken = " ".join(sentences[:n])
    if len(spoken) > limit:
        cut = True
        spoken = spoken[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    if cut:
        return f"{spoken} {REST_ON_SCREEN}".strip()
    if had_code:
        return f"{spoken} {CODE_ON_SCREEN}".strip()
    return spoken


# ── текст → говор ───────────────────────────────────────────────────────────

def _run_async(make: Callable[[], Any], timeout: float) -> Any:
    """asyncio корутина от синхронен код — и от нишка, в която вече върти цикъл."""
    import asyncio

    def run() -> Any:
        return asyncio.run(asyncio.wait_for(make(), timeout))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = run()
        except BaseException as e:   # noqa: BLE001 — пренася се в извикващата нишка долу
            box["error"] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout + 5)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _tts_edge(text: str, voice: str, cfg: Settings) -> tuple[bytes, str]:
    if find_spec("edge_tts") is None:
        raise _Skip(f"edge: edge-tts не е инсталиран ({INSTALL_HINT})")
    import edge_tts

    async def collect() -> bytes:
        chunks: list[bytes] = []
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)
    try:
        audio = _run_async(collect, timeout=45.0)
    except Exception as e:   # aiohttp/edge_tts хвърлят свои типове (мрежа, NoAudioReceived)
        log.debug("edge-tts гръмна", exc_info=True)
        raise VoiceError(f"edge: {type(e).__name__}: {_scrub(str(e))} (иска интернет)") from None
    if not audio:
        raise VoiceError(f"edge: празен звук за гласа {voice}")
    return audio, "audio/mpeg"


def _espeak_voice(voice: str, language: str) -> str:
    """edge гласът `bg-BG-KalinaNeural` не е espeak глас — тогава езикът."""
    return language if not voice or "neural" in voice.lower() else voice


def _tts_espeak(text: str, voice: str, cfg: Settings) -> tuple[bytes, str]:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise _Skip("espeak: espeak-ng не е инсталиран")
    # Текстът по stdin (-b 1 = UTF-8), не в argv: кирилица в командния ред на
    # Windows минава през кодовата страница на конзолата.
    argv = [exe, "-v", _espeak_voice(voice, cfg.language), "-b", "1", "--stdin", "--stdout"]
    try:
        r = subprocess.run(argv, input=text.encode("utf-8"), capture_output=True, timeout=60,
                           check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VoiceError(f"espeak: {type(e).__name__}") from None
    if r.returncode != 0 or not r.stdout.startswith(b"RIFF"):
        raise VoiceError(f"espeak: код {r.returncode} {_scrub(r.stderr.decode('utf-8', 'replace'))}")
    return r.stdout, "audio/wav"


def bulgarian_voice_id(voices: list[Any], language: str = "bg") -> str | None:
    """Идентификаторът на глас за езика сред гласовете на pyttsx3, или None."""
    lang = language.lower()
    names = {"bg": "bulgar"}
    for v in voices or []:
        langs = []
        for item in getattr(v, "languages", None) or []:
            langs.append(item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item))
        blob = " ".join([str(getattr(v, "id", "")), str(getattr(v, "name", "")), *langs]).lower()
        tags = re.split(r"[^a-z]+", blob)
        if lang in tags or (names.get(lang) and names[lang] in blob):
            return str(getattr(v, "id", "")) or None
    return None


def _tts_pyttsx3(text: str, voice: str, cfg: Settings) -> tuple[bytes, str]:
    if find_spec("pyttsx3") is None:
        raise _Skip("pyttsx3: не е инсталиран")
    try:
        import pyttsx3
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
    except Exception as e:   # драйверите (SAPI5/espeak/nsss) хвърлят свои типове
        log.debug("pyttsx3.init гръмна", exc_info=True)
        raise VoiceError(f"pyttsx3: {type(e).__name__}: {_scrub(str(e))}") from None
    vid = bulgarian_voice_id(list(voices or []), cfg.language)
    if vid is None:
        hint = (" SAPI5 на Windows няма български глас по подразбиране (Microsoft Ivan е само "
                f"за новите приложения) — {INSTALL_HINT} дава edge-tts.") if sys.platform == "win32" else ""
        raise VoiceError(f"pyttsx3: няма глас за „{cfg.language}“.{hint}")
    with tempfile.TemporaryDirectory(prefix="genesis-tts-") as tmp:
        path = Path(tmp) / "speech.wav"
        try:
            engine.setProperty("voice", vid)
            engine.save_to_file(text, str(path))
            engine.runAndWait()
            audio = path.read_bytes()
        except Exception as e:   # pyttsx3 драйверите
            log.debug("pyttsx3.save_to_file гръмна", exc_info=True)
            raise VoiceError(f"pyttsx3: {type(e).__name__}: {_scrub(str(e))}") from None
    if not audio:
        raise VoiceError("pyttsx3: празен звук")
    return audio, "audio/aiff" if audio.startswith(b"FORM") else "audio/wav"


TTS_BACKENDS: dict[str, Callable[[str, str, Settings], tuple[bytes, str]]] = {
    "edge": _tts_edge, "espeak": _tts_espeak, "pyttsx3": _tts_pyttsx3}


def _tts_installed(name: str) -> bool:
    if name == "edge":
        return find_spec("edge_tts") is not None
    if name == "espeak":
        return bool(shutil.which("espeak-ng") or shutil.which("espeak"))
    if name == "pyttsx3":
        return find_spec("pyttsx3") is not None
    return False


def tts_ready(cfg: Settings | None = None) -> tuple[bool, str]:
    """Може ли изобщо да се говори тук — без мрежа и без звук: първият
    инсталиран доставчик и плейър. (False, защо) иначе."""
    cfg = cfg or settings()
    found = [n for n in cfg.tts_backends if _tts_installed(n)]
    if not found:
        return False, f"няма с какво да говоря — {INSTALL_HINT}"
    mime = "audio/mpeg" if found[0] == "edge" else "audio/wav"
    player = choose_player(mime)
    if player is None:
        return False, "няма с какво да пусна звука — инсталирай ffmpeg (ffplay) или mpg123"
    label = {"edge": f"edge-tts, {cfg.tts_voice}", "espeak": "espeak-ng", "pyttsx3": "pyttsx3"}[found[0]]
    return True, label


def synthesize(text: str, voice: str | None = None, *, cfg: Settings | None = None) -> tuple[bytes, str]:
    """Текст → (аудио байтове, mime). Текстът се казва както е — за отговор
    на модела първо speech_text()."""
    cfg = cfg or settings()
    text = (text or "").strip()
    if not text:
        raise VoiceError("Няма какво да кажа.")
    voice = (voice or cfg.tts_voice).strip()
    skipped: list[str] = []
    failed: list[str] = []
    for name in cfg.tts_backends:
        backend = TTS_BACKENDS.get(name)
        if backend is None:
            skipped.append(f"{name}: непознат")
            continue
        try:
            return backend(text, voice, cfg)
        except _Skip as e:
            skipped.append(str(e))
        except VoiceError as e:
            failed.append(str(e))
    if not failed:
        raise VoiceError("Няма с какво да говоря: " + "; ".join(skipped) + f". {INSTALL_HINT}")
    raise VoiceError("Не успях да говоря: " + "; ".join(failed + skipped))


# ── пускане на звука ────────────────────────────────────────────────────────

@dataclass
class Player:
    kind: str                     # "exe" (stdin) | "winsound" | "mci" | "file" (временен файл)
    argv: list[str] = field(default_factory=list)


def choose_player(mime: str, *, platform: str | None = None,
                  which: Callable[[str], str | None] | None = None) -> Player | None:
    """С какво да се пусне звукът. Чисто решение — изпълнението е в `_run_player`.

    Без тежки зависимости: wav на Windows — winsound от паметта; mp3 на
    Windows — ffplay, иначе MCI (winmm, вграден); Linux — ffplay, после
    paplay/aplay (wav) или mpg123 (mp3), всички със звука по stdin, не файл."""
    plat = platform or sys.platform
    which = which or shutil.which
    is_wav = mime in ("audio/wav", "audio/x-wav", "audio/wave")
    ffplay = which("ffplay")
    ff = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"] if ffplay else []
    if plat == "win32":
        if is_wav:
            return Player("winsound")
        return Player("exe", ff) if ff else Player("mci")
    if ff:
        return Player("exe", ff)
    if plat == "darwin" and which("afplay"):
        return Player("file", [str(which("afplay"))])
    if is_wav:
        for name, args in (("paplay", []), ("aplay", ["-q", "-"])):
            exe = which(name)
            if exe:
                return Player("exe", [exe, *args])
    if mime == "audio/mpeg" and which("mpg123"):
        return Player("exe", [str(which("mpg123")), "-q", "-"])
    return None


_CURRENT: dict[str, Any] = {}
_SPEAK_LOCK = threading.Lock()
# Поредният номер на говоренето: отговор, чийто звук пристигне (edge-tts е
# онлайн, секунди) след по-нов, не се пуска — иначе старият би прозвучал пръв.
_GENERATION = [0]
_MCI_ALIAS = "genesis_tts"


def _ext(mime: str) -> str:
    return {"audio/mpeg": "mp3", "audio/aiff": "aiff"}.get(mime, _MIME_EXT.get(mime, "wav"))


def _run_player(player: Player, audio: bytes, mime: str) -> None:
    """Истинското пускане (conftest го спира в тестовете)."""
    if player.kind == "exe":
        proc = subprocess.Popen(player.argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _CURRENT["proc"] = proc
        try:
            with contextlib.suppress(BrokenPipeError, OSError):
                proc.communicate(audio, timeout=600)
        finally:
            _CURRENT.pop("proc", None)
            if proc.poll() is None:
                proc.kill()
        return
    if player.kind == "winsound":
        if sys.platform == "win32":
            import winsound
            _CURRENT["winsound"] = True
            try:
                winsound.PlaySound(audio, winsound.SND_MEMORY)
            finally:
                _CURRENT.pop("winsound", None)
        return
    with tempfile.TemporaryDirectory(prefix="genesis-tts-") as tmp:
        path = os.path.join(tmp, f"speech.{_ext(mime)}")
        with open(path, "wb") as f:
            f.write(audio)
        if player.kind == "file":
            subprocess.run([*player.argv, path], check=False, timeout=600,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif player.kind == "mci" and sys.platform == "win32":
            _play_mci(path)


def _play_mci(path: str) -> None:
    """mp3 на Windows без ffplay: MCI от winmm.dll. Пътят е от tempfile (без
    кавички в него), командата не минава през shell."""
    if sys.platform != "win32":
        return
    import ctypes
    send = ctypes.WinDLL("winmm").mciSendStringW
    err = send(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}', None, 0, None)
    if err:
        raise VoiceError(f"Windows не пусна mp3 (MCI грешка {err}).")
    _CURRENT["mci"] = True
    try:
        send(f"play {_MCI_ALIAS} wait", None, 0, None)
    finally:
        send(f"close {_MCI_ALIAS}", None, 0, None)
        _CURRENT.pop("mci", None)


def play(audio: bytes, mime: str) -> None:
    """Пуска звука тук, на компютъра, и чака да свърши."""
    player = choose_player(mime)
    if player is None:
        raise VoiceError("Няма с какво да пусна звука — инсталирай ffmpeg (ffplay) или mpg123.")
    _run_player(player, audio, mime)


def stop_speaking() -> None:
    """Прекъсва говоренето (нов отговор, /voice off)."""
    _GENERATION[0] += 1
    proc = _CURRENT.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
    # Системният звук се пипа само ако НИЕ пускаме нещо (2026-10-15): преди
    # всяко /listen и всеки нов отговор викаше winsound.PlaySound(None, …) —
    # ненужно, а на Windows CI без звукова карта тестът висеше.
    if sys.platform == "win32":
        if _CURRENT.get("mci"):
            import ctypes
            ctypes.WinDLL("winmm").mciSendStringW(f"stop {_MCI_ALIAS}", None, 0, None)
        if _CURRENT.get("winsound"):
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)


def speak(text: str, *, block: bool = False, cfg: Settings | None = None,
          on_error: Callable[[str], None] | None = None) -> threading.Thread | None:
    """Казва отговора на глас (speech_text: без код и markdown, кратко).

    На заден план по подразбиране — чатът не чака; ново говорене спира
    старото. Грешка (няма TTS, няма плейър) отива в `on_error`, не хвърля."""
    cfg = cfg or settings()
    spoken = speech_text(text, max_sentences=cfg.max_sentences, max_chars=cfg.max_chars)
    if not spoken:
        return None

    stop_speaking_quietly()
    _GENERATION[0] += 1
    mine = _GENERATION[0]

    def work() -> None:
        try:
            audio, mime = synthesize(spoken, cfg=cfg)
            with _SPEAK_LOCK:
                if mine == _GENERATION[0]:
                    play(audio, mime)
        except VoiceError as e:
            if on_error is not None:
                on_error(str(e))
            else:
                log.warning("voice: %s", e)
        except OSError as e:
            if on_error is not None:
                on_error(f"Звукът не тръгна: {type(e).__name__}")
            log.debug("voice: плейърът гръмна", exc_info=True)

    t = threading.Thread(target=work, name="genesis-speak", daemon=True)
    t.start()
    if block:
        t.join()
    return t


def stop_speaking_quietly() -> None:
    try:
        stop_speaking()
    except (OSError, RuntimeError, AttributeError, ImportError):
        log.debug("voice: спирането на звука не стана", exc_info=True)


# ── микрофон ────────────────────────────────────────────────────────────────

class SilenceDetector:
    """Спира записа след `silence_seconds` тишина СЛЕД реч (не преди нея).

    Прагът е по шума в първите 0.3 s (×3, поне 400 от 32 767): лаптоп с
    вентилатор и тиха стая не са един и същ под. 0 секунди = изключено."""

    def __init__(self, silence_seconds: float, rate: int = SAMPLE_RATE) -> None:
        self.silence_seconds = silence_seconds
        self.rate = rate
        self.floor: list[float] = []
        self.heard = False
        self.quiet = 0.0

    @staticmethod
    def rms(chunk: bytes) -> float:
        samples = array("h")
        samples.frombytes(chunk[: len(chunk) // 2 * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0.0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    def feed(self, chunk: bytes) -> bool:
        """True = достатъчно тишина след реч, спри."""
        if self.silence_seconds <= 0 or not chunk:
            return False
        level = self.rms(chunk)
        seconds = len(chunk) / 2 / self.rate
        if len(self.floor) < 3:
            self.floor.append(level)
            return False
        threshold = max(400.0, 3 * min(self.floor))
        if level >= threshold:
            self.heard, self.quiet = True, 0.0
            return False
        if self.heard:
            self.quiet += seconds
        # 1e-6: десет парчета по 0.1 s са 0.999… s във float.
        return self.heard and self.quiet >= self.silence_seconds - 1e-6


def _capture_sounddevice(max_seconds: float, stop: threading.Event, detector: SilenceDetector) -> bytes:
    import sounddevice as sd  # OSError без PortAudio — хваща се в _capture
    chunks: queue.Queue[bytes] = queue.Queue()

    def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
        chunks.put(bytes(indata))

    pcm = bytearray()
    deadline = time.monotonic() + max_seconds
    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=SAMPLE_RATE // 10, callback=callback):
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            pcm += chunk
            if detector.feed(chunk):
                break
    while not chunks.empty():
        pcm += chunks.get_nowait()
    return bytes(pcm[: int(max_seconds * SAMPLE_RATE) * 2])


def _capture_arecord(max_seconds: float, stop: threading.Event, detector: SilenceDetector) -> bytes:
    """Linux без PortAudio (libportaudio2 иска sudo): ALSA `arecord`."""
    exe = shutil.which("arecord")
    if not exe:
        raise VoiceError("няма arecord")
    proc = subprocess.Popen([exe, "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    return _drain_recorder(proc, max_seconds, stop, detector)


def _drain_recorder(proc: subprocess.Popen, max_seconds: float, stop: threading.Event,
                    detector: SilenceDetector) -> bytes:
    """PCM от stdout на записващия процес до Стоп, тавана или тишина; процесът
    се убива винаги. read() без данни блокира завинаги: заседнал arecord (жив,
    но мълчи) държеше микрофона до края на сесията, Enter/Esc и таванът не
    стигаха до тук (2026-10-20) — select прави чакането прекъсваемо."""
    pcm = bytearray()
    deadline = time.monotonic() + max_seconds
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while not stop.is_set() and time.monotonic() < deadline:
            if sys.platform != "win32" and not select.select([fd], [], [], 0.1)[0]:
                continue
            chunk = os.read(fd, SAMPLE_RATE // 10 * 2)   # каквото има, не чака цяло парче
            if not chunk:
                break
            pcm += chunk
            if detector.feed(chunk):
                break
    finally:
        proc.kill()
        proc.wait(5)
    return bytes(pcm)


def _capture(max_seconds: float, stop: threading.Event, detector: SilenceDetector) -> bytes:
    """Суровият PCM от истинския микрофон (conftest го спира в тестовете)."""
    if find_spec("sounddevice") is not None:
        try:
            return _capture_sounddevice(max_seconds, stop, detector)
        except Exception as e:   # OSError без PortAudio, sd.PortAudioError без устройство
            log.debug("sounddevice без PortAudio/устройство", exc_info=True)
            why = f"микрофонът не тръгна: {_scrub(str(e))}"
    else:
        why = f"няма sounddevice ({INSTALL_HINT})"
    if sys.platform.startswith("linux") and shutil.which("arecord"):
        return _capture_arecord(max_seconds, stop, detector)
    extra = " На Linux sounddevice иска и libportaudio2 (sudo apt install libportaudio2)." \
        if sys.platform.startswith("linux") else ""
    raise VoiceError(f"Не мога да записвам — {why}.{extra}")


def record(max_seconds: float = 60, stop_event: threading.Event | None = None, *,
           silence_seconds: float | None = None) -> bytes:
    """Записва от микрофона до `stop_event`, `max_seconds` или тишина след реч.
    Връща WAV (16 kHz, моно, 16 бита) в паметта — нищо на диска."""
    stop = stop_event or threading.Event()
    seconds = min(max(float(max_seconds), 0.5), MAX_AUDIO_SECONDS)
    quiet = settings().silence_seconds if silence_seconds is None else silence_seconds
    pcm = _capture(seconds, stop, SilenceDetector(quiet))
    return pcm_to_wav(pcm)


def mic_status() -> tuple[str, str]:
    """(ok|fail|skip, подробност) за /doctor — без запис, без мрежа."""
    if find_spec("sounddevice") is None:
        if sys.platform.startswith("linux") and shutil.which("arecord"):
            return "ok", "arecord (ALSA) — sounddevice не е инсталиран"
        return "skip", "sounddevice не е инсталиран"
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
    except Exception as e:   # sd.PortAudioError, OSError без PortAudio, ValueError без устройство
        log.debug("sounddevice.query_devices гръмна", exc_info=True)
        if sys.platform.startswith("linux") and shutil.which("arecord"):
            return "ok", "arecord (ALSA) — sounddevice не намира PortAudio/устройство"
        return "fail", f"няма входно устройство ({type(e).__name__}: {_scrub(str(e))})"
    name = dev.get("name") if isinstance(dev, dict) else None
    return ("ok", f"„{name}“") if name else ("fail", "няма входно устройство")


# ── терминалът: /listen ─────────────────────────────────────────────────────

CONFIRM_PROMPT = "[Enter = изпрати, e = редактирай, Esc = откажи]"


def _posix_key(timeout: float | None) -> str | None:
    """Един клавиш без Enter (cbreak); None — нищо до `timeout`."""
    if sys.platform == "win32":   # termios/tty ги няма; mypy на Windows ги отказва
        return None
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        raw = os.read(fd, 1)
        if raw == b"\x1b":
            # Стрелка/функционален клавиш е ESC + още байтове — не е „откажи“.
            if select.select([fd], [], [], 0.05)[0]:
                os.read(fd, 16)
                return "other"
            return "esc"
        # Кирилско „е“ е два байта в UTF-8: дочитат се според първия.
        lead = raw[0] if raw else 0
        more = 1 if 0xC0 <= lead < 0xE0 else 2 if 0xE0 <= lead < 0xF0 else 3 if lead >= 0xF0 else 0
        if more:
            raw += os.read(fd, more)
        return raw.decode("utf-8", "replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _windows_key(timeout: float | None) -> str | None:
    if sys.platform != "win32":
        return None
    import msvcrt
    end = None if timeout is None else time.monotonic() + timeout
    while not msvcrt.kbhit():
        if end is not None and time.monotonic() >= end:
            return None
        time.sleep(0.03)
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):   # стрелка/F-клавиш — втори знак
        msvcrt.getwch()
        return "other"
    return "esc" if ch == "\x1b" else ch


def read_key(timeout: float | None = None) -> str | None:
    """`enter`, `esc`, `space`, `ctrl-c`, `other` или самият знак; None след `timeout`."""
    if not sys.stdin.isatty():
        if timeout is not None:   # без терминал няма клавиш „край“ — записът спира сам
            time.sleep(timeout)
            return None
        line = input()
        return "enter" if not line.strip() else line.strip()[:1].lower()
    ch = _windows_key(timeout) if sys.platform == "win32" else _posix_key(timeout)
    if ch is None:
        return None
    return {"\r": "enter", "\n": "enter", " ": "space", "\x03": "ctrl-c"}.get(ch, ch)


def edit_line(text: str, ask: Callable[[str], str] = input) -> str:
    """Поправка на разпознатото: на Linux/macOS readline слага текста в реда;
    без readline (Windows) — нов текст, празно = без промяна."""
    if sys.platform != "win32" and sys.stdin.isatty():
        try:
            import readline
        except ImportError:
            pass
        else:
            readline.set_startup_hook(lambda: readline.insert_text(text))
            try:
                return ask("✎ ").strip()
            finally:
                readline.set_startup_hook()
    new = ask("✎ поправеният текст (Enter = остави както е): ").strip()
    return new or text


def listen_flow(*, out: Callable[[str], None], key: Callable[[float | None], str | None] = read_key,
                edit: Callable[[str], str] = edit_line, cfg: Settings | None = None,
                capture: Callable[..., bytes] | None = None,
                stt: Callable[[bytes, str, str], str] | None = None) -> str | None:
    """`/listen`: запис → текст → потвърждение. Връща текста за изпращане или None.

    Никога не праща сам: зле чута команда може да пусне инструменти, затова
    операторът вижда „Чух: …“ и решава — Enter, поправка или отказ."""
    cfg = cfg or settings()
    capture = capture or record
    stt = stt or transcribe
    out(f"🎙 Слушам… Enter/Space = край, Esc = откажи (до {cfg.mic_max_seconds:.0f} s"
        + (", спира и след тишина" if cfg.silence_seconds > 0 else "") + ")")
    stop = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["audio"] = capture(cfg.mic_max_seconds, stop, silence_seconds=cfg.silence_seconds)
        except VoiceError as e:
            box["error"] = str(e)
        finally:
            box["done"] = True

    t = threading.Thread(target=work, name="genesis-mic", daemon=True)
    t.start()
    # Ctrl+C тук е „откажи“, не изход от чата (главният цикъл спира на
    # KeyboardInterrupt) — и записът не остава да тече до тавана.
    cancelled = False
    try:
        while t.is_alive():
            k = key(0.1)
            if k in ("enter", "space"):
                break
            if k in ("esc", "ctrl-c"):
                cancelled = True
                break
    except KeyboardInterrupt:
        cancelled = True
    stop.set()
    t.join(5)
    if cancelled:
        out("Отказано.")
        return None
    if "error" in box:
        out(f"⚠ {box['error']}")
        return None
    audio = box.get("audio") or b""
    seconds = audio_seconds(audio, "audio/wav") or 0.0
    if seconds < MIN_AUDIO_SECONDS:
        out("Нищо не чух — записът е твърде кратък.")
        return None
    out(f"Превръщам {seconds:.1f} s в текст…")
    try:
        text = stt(audio, "audio/wav", cfg.language).strip()
    except VoiceError as e:
        out(f"⚠ {e}")
        return None
    except KeyboardInterrupt:
        out("Отказано.")
        return None
    if not text:
        out("Нищо не разпознах.")
        return None
    try:
        while True:
            out(f"Чух: {text}  {CONFIRM_PROMPT}")
            k = key(None)
            if k == "enter":
                return text
            if k in ("esc", "ctrl-c"):
                break
            if k is not None and k.lower() in ("e", "е"):   # латинско и кирилско „е“
                edited = edit(text).strip()
                if edited:
                    return edited
                break
    except (KeyboardInterrupt, EOFError):
        pass
    out("Отказано.")
    return None


# ── /doctor ─────────────────────────────────────────────────────────────────

def doctor_lines() -> list[tuple[str, str, str, str]]:
    """(име, ok|fail|skip, подробност, поправка) — бързо и без мрежа."""
    cfg = settings()
    rows: list[tuple[str, str, str, str]] = []
    stt: list[str] = []
    for name in cfg.stt_backends:
        if name == "groq":
            n = len(groq_keys())
            stt.append(f"groq ({n} ключ{'а' if n != 1 else ''})" if n else "groq: няма ключ")
        elif name == "openai":
            stt.append(f"openai: {cfg.openai_base_url}" if cfg.openai_base_url else "openai: не е настроен")
        elif name == "local":
            stt.append("local: faster-whisper" if find_spec("faster_whisper") else "local: няма faster-whisper")
    stt_ok = (("groq" in cfg.stt_backends and bool(groq_keys()))
              or ("openai" in cfg.stt_backends and bool(cfg.openai_base_url))
              or ("local" in cfg.stt_backends and find_spec("faster_whisper") is not None))
    rows.append(("Глас → текст", "ok" if stt_ok else "skip", "; ".join(stt) or "няма доставчици",
                 f"GROQ_API_KEY в ~/.genesis/.env или {LOCAL_HINT}"))
    tts: list[str] = []
    for name in cfg.tts_backends:
        if name == "edge":
            tts.append(f"edge-tts ({cfg.tts_voice}, онлайн)" if find_spec("edge_tts") else "edge-tts: не е инсталиран")
        elif name == "espeak":
            tts.append("espeak-ng" if shutil.which("espeak-ng") or shutil.which("espeak") else "espeak-ng: няма го")
        elif name == "pyttsx3":
            tts.append("pyttsx3 (без проверка за български)" if find_spec("pyttsx3") else "pyttsx3: не е инсталиран")
    tts_ok = any(not t.endswith(("не е инсталиран", "няма го")) for t in tts)
    rows.append(("Текст → глас", "ok" if tts_ok else "skip", "; ".join(tts) or "няма доставчици", INSTALL_HINT))
    status, detail = mic_status()
    rows.append(("Микрофон", status, detail, INSTALL_HINT if status == "skip" else
                 "провери входното устройство в настройките на звука"))
    player = choose_player("audio/mpeg")
    if player is None:
        how = "няма плейър"
    elif player.argv:
        how = Path(player.argv[0]).name
    else:
        how = "Windows MCI (winmm)"
    rows.append(("Звук (mp3)", "ok" if player else "skip", how,
                 "инсталирай ffmpeg (ffplay) или mpg123"))
    return rows
