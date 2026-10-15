"""
genesis_agent.vision — картинки в разговора (2026-10-15).

Операторът обърна решението от PR #2 (2026-09-23, „без зрение“): Genesis вижда
картинки. Не като стария `vision.py` (локален moondream описваше екрана с
думи) — картинката отива при самия модел като част от съобщението, а Brain
избира модел, който я вижда.

Форматът в историята е OpenAI-съвместимият (`content` като списък от части):

    {"role": "user", "content": [
        {"type": "text", "text": "какво не е наред тук?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"},
         "genesis": {"name": "shot.png", "mime": "image/png", "bytes": 48213,
                     "sha256": "…", "width": 1280, "height": 720}},
    ]}

Полето `genesis` е само за Genesis (име за бележките, хеш за историята на
диска) — `for_provider` го маха, преди заявката да тръгне. Публичният API за
телефона и Desktop (стабилен — следващият агент го ползва):

    image_part(data, mime=None, name="") -> dict      # ImageError при лошо
    load_image(path, *, workspace=None) -> dict        # същите пътища като READ_FILE
    user_message(text, images) -> dict

Всичко останало (преводът за доставчика, бележките вместо картинка, историята
на диска, изпускането на стари картинки) е вътрешно за brain/budget/чата.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("genesis.vision")

# Сурова картинка (преди смаляването): по-голямо е снимка от 48 Mpx камера или
# не е картинка — 20 MB са и тавана на OpenAI за една картинка.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Без Pillow не се смалява: над това се отказва, защото Anthropic приема до
# 5 MB на картинка, а 20 MB base64 на всеки рунд са и бавни, и скъпи.
MAX_SEND_BYTES = 5 * 1024 * 1024
# Дългата страна след смаляването — над нея Claude и GPT така или иначе
# смаляват сами (Anthropic: 1568 px), тоест повече пиксели са само разход.
MAX_SIDE = 1568
# Пиксели, над които не се декодира (бомба: 1 KB PNG с 50 000 × 50 000).
MAX_PIXELS = 50_000_000
# Оценка за бюджета: картинка ≈ 1 500 токена (Claude: (w·h)/750 при 1568 px
# дълга страна ≈ 1 600; GPT-4o „high“ при 1024² ≈ 765–1 100). budget.py брои
# знаци по ~4 на токен.
IMAGE_TOKENS = 1500
IMAGE_CHARS = IMAGE_TOKENS * 4
# Картинки от по-стари ходове на оператора се изпускат от заявката (бележка на
# мястото им): всяка струва ~1.5K токена на ВСЕКИ рунд, а моделът вече е
# отговорил за нея. Историята в паметта и на диска остава пълна.
KEEP_IMAGE_TURNS = 3
# И отгоре: не повече от толкова картинки в една заявка (най-новите).
MAX_IMAGES_PER_REQUEST = 8

_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# Съобщението, което VIEW_IMAGE и проверката в браузър добавят — не е реплика
# на оператора (request_window, /rewind и рутерът го различават по това).
ATTACHMENT_PREFIX = "[прикачено от Genesis]"
NO_VISION_NOTE = ("нито един модел във веригата не вижда картинки; "
                  "добави vision модел в config.yaml (models.vision_models)")


class ImageError(ValueError):
    """Картинката не става за модела — съобщението е на български, за оператора."""


# ── картинка → част от съобщение ────────────────────────────────────────────

def sniff_mime(data: bytes) -> str | None:
    """Истинският вид по първите байтове (разширението може да лъже)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _pillow() -> Any:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    return Image, ImageOps


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _normalize(data: bytes, mime: str, label: str) -> tuple[bytes, str, int, int]:
    """(байтове, mime, ширина, височина) — смалени до MAX_SIDE, ако има Pillow."""
    pil = _pillow()
    if pil is None:
        if len(data) > MAX_SEND_BYTES:
            raise ImageError(
                f"{label}: {len(data) / 1048576:.1f} MB — над {MAX_SEND_BYTES // 1048576} MB "
                "без смаляване. Инсталирай Pillow (pip install pillow), за да се смалява "
                "сама, или я смали преди да я пратиш.")
        w, h = _png_size(data) or (0, 0)
        return data, mime, w, h
    Image, ImageOps = pil
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise ImageError(f"{label}: {w}×{h} px — твърде голяма за декодиране")
        img.load()
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        raise ImageError(f"{label}: не се отваря като картинка ({e})") from e
    # Снимка от телефон: ориентацията е в EXIF, иначе моделът я вижда легнала.
    # Завъртяна или с GPS → прекодира се и при малък размер (ревю 2026-10-15:
    # байтовете тръгваха както са — легнала снимка и координатите ѝ към
    # доставчика). Новият файл е без EXIF.
    try:
        exif = img.getexif()
        tagged = exif.get(0x0112, 1) != 1 or 0x8825 in exif
    except (OSError, ValueError, SyntaxError, TypeError):
        tagged = False
    try:
        img = ImageOps.exif_transpose(img)
    except (OSError, ValueError, SyntaxError):
        pass
    if getattr(img, "is_animated", False):
        img.seek(0)   # първият кадър — моделите не четат анимация
    w, h = img.size
    resized = max(w, h) > MAX_SIDE
    if resized:
        scale = MAX_SIDE / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        w, h = img.size
    if not resized and not tagged and mime != "image/gif" and len(data) <= MAX_SEND_BYTES:
        return data, mime, w, h            # като е: байтовете са същите → кешът също
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P") and mime in ("image/png", "image/gif", "image/webp"):
        img.save(out, format="PNG", optimize=True)
        new_mime = "image/png"
    else:
        img.convert("RGB").save(out, format="JPEG", quality=85)
        new_mime = "image/jpeg"
    if out.tell() > MAX_SEND_BYTES:      # PNG снимка с много детайл
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=80)
        new_mime = "image/jpeg"
    return out.getvalue(), new_mime, w, h


def image_part(data: bytes, mime: str | None = None, name: str = "") -> dict:
    """Картинка → OpenAI-съвместима част `image_url` с data: адрес.

    `mime` е подсказка: решават първите байтове (PNG/JPEG/WebP/GIF), иначе
    ImageError. С Pillow — смалена до MAX_SIDE px по дългата страна; без него —
    както е, ако е до MAX_SEND_BYTES."""
    label = name or "картинката"
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageError(f"{label}: празен файл")
    data = bytes(data)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"{label}: {len(data) / 1048576:.1f} MB — най-много "
                         f"{MAX_IMAGE_BYTES // 1048576} MB")
    real = sniff_mime(data)
    if real is None:
        hint = f" (казано: {mime})" if mime else ""
        raise ImageError(f"{label}: не е PNG, JPEG, WebP или GIF{hint}")
    data, real, w, h = _normalize(data, real, label)
    meta: dict[str, Any] = {"name": name, "mime": real, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()}
    if w and h:
        meta["width"], meta["height"] = w, h
    url = f"data:{real};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}, "genesis": meta}


def image_from_base64(text: str, mime: str | None = None, name: str = "") -> dict:
    """Както image_part, но от base64 низ (телефонът, Desktop). Приема и data: адрес."""
    raw = str(text or "")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ImageError(f"{name or 'картинката'}: невалиден base64") from e
    return image_part(data, mime, name)


def load_image(path: str | os.PathLike[str], *, workspace: Path | None = None) -> dict:
    """Картинка от файл — през същите пътища като READ_FILE: относителният път е
    спрямо работната папка, ключове/тайни минават през sandbox решението (в
    терминала пита, без терминал — отказ), само обикновен файл."""
    from genesis_agent import sandbox
    raw = os.fspath(path).strip().strip('"')
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (workspace or Path.cwd()) / p
    try:
        real = Path(os.path.realpath(p))
    except (OSError, ValueError) as e:
        raise ImageError(f"{raw}: невалиден път ({e})") from e
    for reason in (sandbox.sensitive_path_reason(str(p)), sandbox.sensitive_path_reason(str(real)),
                   sandbox.control_path_reason(str(p))):
        if reason:
            verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [reason])
            allowed, why = sandbox._decide(f"VIEW_IMAGE {p}", verdict, sandbox.get_policy())
            if not allowed:
                raise ImageError(f"{raw}: {why}")
            break
    try:
        st = os.stat(real)
    except FileNotFoundError as e:
        raise ImageError(f"{raw}: файлът не съществува") from e
    except OSError as e:
        raise ImageError(f"{raw}: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        raise ImageError(f"{raw}: не е обикновен файл")
    if st.st_size > MAX_IMAGE_BYTES:
        raise ImageError(f"{raw}: {st.st_size / 1048576:.1f} MB — най-много "
                         f"{MAX_IMAGE_BYTES // 1048576} MB")
    try:
        with open(real, "rb") as fh:
            data = fh.read(MAX_IMAGE_BYTES + 1)
    except OSError as e:
        raise ImageError(f"{raw}: {e}") from e
    return image_part(data, None, p.name)


def user_message(text: str, images: list[dict]) -> dict:
    """Съобщение от оператора с картинки. Без картинки — обикновен низ, както
    винаги (байт по байт същата история за всички останали)."""
    parts = [p for p in images or [] if is_image_part(p)]
    if not parts:
        return {"role": "user", "content": text}
    content: list[dict] = [{"type": "text", "text": text}] if text else []
    return {"role": "user", "content": content + parts}


# ── четене на съдържание ─────────────────────────────────────────────────────

def is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") == "image_url"


def meta_of(part: dict) -> dict:
    """Полето `genesis` на частта (име, вид, размер, хеш) или {}."""
    meta = part.get("genesis")
    return meta if isinstance(meta, dict) else {}


def image_name(part: dict) -> str:
    return str(meta_of(part).get("name") or "картинка")


def image_ref(part: dict) -> dict:
    """Какво телефонът/Desktop научават за картинка в събитие: име, вид, размер
    (никога байтовете — те биха пътували във всеки отговор на `events`)."""
    meta = meta_of(part)
    return {k: meta[k] for k in ("name", "mime", "bytes", "width", "height") if k in meta}


def images_in(content: Any) -> list[dict]:
    return [p for p in content if is_image_part(p)] if isinstance(content, list) else []


def has_images(messages: Any) -> bool:
    return any(isinstance(m, dict) and images_in(m.get("content")) for m in messages or [])


def text_of(content: Any) -> str:
    """Текстът на съобщение — за всичко, което чете историята като низ (резюме,
    памет, списъци). Картинката е кратка бележка, никога base64."""
    if not isinstance(content, list):
        return str(content or "")
    out: list[str] = []
    for p in content:
        if is_image_part(p):
            out.append(f"[картинка: {image_name(p)}]")
        elif isinstance(p, dict):
            out.append(str(p.get("text") or ""))
        else:
            out.append(str(p))
    return "\n".join(s for s in out if s)


def content_chars(content: Any) -> int:
    """Размерът за бюджета: текстът плюс ~IMAGE_CHARS за картинка (не base64-а)."""
    if not isinstance(content, list):
        return len(str(content or ""))
    return sum(IMAGE_CHARS if is_image_part(p)
               else len(str(p.get("text") or "")) if isinstance(p, dict) else len(str(p))
               for p in content)


def is_attachment(message: Any) -> bool:
    """Съобщение с картинки от инструмент/проверка, не от оператора."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    return text_of(message.get("content")).startswith(ATTACHMENT_PREFIX)


# ── кой модел вижда ──────────────────────────────────────────────────────────

# Подразбиращото се по име (2026-10-15, по публичните описания на моделите):
# изричното в config.yaml (`vision: true/false` на записа или
# models.vision_models) винаги е над него.
_VISION_NAME = re.compile(
    r"vision|[-_/.]vl(?:\b|[-_:])|\bvl[-_]|llama-?4|gemini|claude|gpt-4o|gpt-4\.1|gpt-5"
    r"|pixtral|gemma-?[34]|llava|moondream|minicpm-v|internvl|qwen[\d.]*-?vl",
    re.IGNORECASE)
# `claude -p` приема картинки в stream-json входа (формата на Claude Code SDK),
# но тук не е проверено с истински абонамент — затова само по изричен избор:
# `vision_models: ["claude_cli/sonnet"]` (2026-10-15).
_OPT_IN_ONLY = {"claude_cli"}

_cfg_cache: tuple[float, dict] | None = None


def _models_config() -> dict:
    global _cfg_cache
    from genesis_agent.paths import CONFIG_PATH
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cfg_cache is None or _cfg_cache[0] != mtime:
        import yaml
        try:
            cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, yaml.YAMLError):
            cfg = {}
        models = cfg.get("models") if isinstance(cfg, dict) else None
        _cfg_cache = (mtime, models if isinstance(models, dict) else {})
    return _cfg_cache[1]


def can_see(provider: str, model: str) -> bool:
    """Вижда ли (provider, model) картинки."""
    models = _models_config()
    names = {str(n).strip().lower() for n in models.get("vision_models") or []}
    pm = f"{provider}/{model}".lower()
    if pm in names or (provider not in _OPT_IN_ONLY and str(model).lower() in names):
        return True
    for key in ("fallback_models", "coding_models", "light_models", "premium_models"):
        for entry in models.get(key) or []:
            if (isinstance(entry, dict) and entry.get("provider") == provider
                    and entry.get("model") == model and "vision" in entry):
                return bool(entry.get("vision"))
    if provider in _OPT_IN_ONLY:
        return False
    return bool(_VISION_NAME.search(str(model or "")))


_sees_cache: tuple[float, tuple, bool] | None = None


def chain_sees(extra: tuple[str, str] | None = None) -> bool:
    """Има ли във веригата (с ключ) модел, който вижда — за VIEW_IMAGE и за
    снимките от проверката в браузър: без такъв модел картинката е само разход.
    Кешира се за 10 s: чатът пита на всеки рунд."""
    global _sees_cache
    now = time.monotonic()
    key = (extra, os.environ.get("GENESIS_QUALITY", ""))
    if _sees_cache is not None and _sees_cache[1] == key and now - _sees_cache[0] < 10:
        return _sees_cache[2]
    result = (extra is not None and can_see(*extra)) or any(
        can_see(c["provider"], c["model"]) for c in configured_chain())
    _sees_cache = (now, key, result)
    return result


def configured_chain() -> list[dict]:
    """Моделите от веригите, за които има ключ (или проект за Vertex)."""
    from genesis_agent import brain
    keys = brain._load_keys()
    chain = brain._load_chain() + brain._load_coding_chain()
    if os.environ.get("GENESIS_QUALITY", "").strip().lower() == "max":
        chain = brain._load_premium_chain() + chain
    out = []
    for c in chain:
        key_env = brain._PROVIDERS.get(c["provider"], ("", None))[1]
        if c["provider"] == "vertex" or key_env is None or str(keys.get(key_env) or "").strip():
            out.append(c)
    return out


# ── заявката към доставчика ──────────────────────────────────────────────────

def _note(part: dict, why: str) -> dict:
    return {"type": "text", "text": f"[картинка: {image_name(part)} — {why}]"}


def _collapse(parts: list) -> Any:
    """Само текст остана → обикновен низ (доставчиците без части го искат така)."""
    if all(isinstance(p, dict) and p.get("type") == "text" for p in parts):
        return "\n".join(str(p.get("text") or "") for p in parts)
    return parts


def without_images(messages: list[dict], why: str = NO_VISION_NOTE) -> list[dict]:
    """Картинките → текстова бележка, за модел, който не вижда (иначе 400)."""
    if not has_images(messages):
        return messages
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and images_in(content):
            m = {**m, "content": _collapse([_note(p, why) if is_image_part(p) else p
                                            for p in content])}
        out.append(m)
    return out


def for_provider(messages: list[dict], sees: bool, why: str = NO_VISION_NOTE) -> list[dict]:
    """Историята за конкретния модел: вижда → чисти OpenAI части (без полето
    `genesis`, което строг доставчик отказва); не вижда → бележки. Без картинки
    — същият списък (байтовете за кеша не се пипат)."""
    if not has_images(messages):
        return messages
    if not sees:
        return without_images(messages, why)
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and images_in(content):
            m = {**m, "content": [{"type": "image_url", "image_url": {"url": p["image_url"]["url"]}}
                                  if is_image_part(p) else p for p in content]}
        out.append(m)
    return out


def _url_of(part: dict) -> str:
    """Адресът на частта; "" при `image_url`, който не е речник (ръчно писан
    или чужд разговор — ревю 2026-10-15: AttributeError сваляше /resume)."""
    box = part.get("image_url")
    return str(box.get("url") or "") if isinstance(box, dict) else ""


def split_data_url(part: dict) -> tuple[str, str]:
    """(mime, base64) от `data:` адреса на частта."""
    url = _url_of(part)
    head, _, data = url.partition(",")
    mime = head[5:].split(";", 1)[0] if head.startswith("data:") else ""
    return mime or "image/png", data


def anthropic_block(part: dict) -> dict:
    """Частта във формата на Anthropic Messages API (и на stream-json на `claude`)."""
    mime, data = split_data_url(part)
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


def gemini_part(part: dict) -> dict:
    """Частта във формата на родния Gemini API (generateContent). Brain вика
    Gemini и Vertex през OpenAI-съвместимия им адрес, който приема `image_url`
    с data: адрес, тоест там това не е нужно — за клиент на родния API."""
    mime, data = split_data_url(part)
    return {"inline_data": {"mime_type": mime, "data": data}}


def elide_old_images(messages: list[dict], keep_turns: int = KEEP_IMAGE_TURNS,
                     max_images: int = MAX_IMAGES_PER_REQUEST) -> list[dict]:
    """Картинките от по-стари от `keep_turns` хода на оператора (и над
    `max_images` най-нови) → бележка. Само в изпратеното копие."""
    if not has_images(messages):
        return messages
    msgs = list(messages)
    turns = 0
    seen = 0
    out: list[dict] = []
    for m in reversed(msgs):
        content = m.get("content") if isinstance(m, dict) else None
        old = turns >= keep_turns
        if isinstance(content, list) and images_in(content):
            parts = []
            for p in content:
                if is_image_part(p):
                    seen += 1
                    if old or seen > max_images:
                        p = _note(p, "от по-ранен ход, махната от контекста; "
                                     "VIEW_IMAGE пак, ако ти трябва")
                parts.append(p)
            m = {**m, "content": _collapse(parts)}
        if isinstance(m, dict) and m.get("role") == "user" and not is_attachment(m):
            turns += 1
        out.append(m)
    return list(reversed(out))


# ── историята на диска ───────────────────────────────────────────────────────

# В записания разговор картинката е препратка, не base64: файлът на сесията
# се пише наново след всеки ход, а 4 снимки по 1 MB щяха да са 5 MB JSON на
# ход. Байтовете са веднъж, по хеш, в <history>/images/ (2026-10-15).
REF_SCHEME = "genesis-image:"
_STORE_MAX_FILES = 500
_REF_NAME = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|webp|gif)")


def _ref_name(meta: dict) -> str | None:
    sha = str(meta.get("sha256") or "")
    ext = _EXT.get(str(meta.get("mime") or ""))
    return f"{sha}.{ext}" if re.fullmatch(r"[0-9a-f]{64}", sha) and ext else None


def for_disk(messages: Any, store: Path) -> list:
    """Копие за JSON файла: data: адресите → `genesis-image:<sha>.<ext>`, а
    байтовете — в `store`. Без картинки — същият списък."""
    msgs = list(messages)
    if not has_images(msgs):
        return msgs
    out = []
    for m in msgs:
        content = m.get("content")
        if isinstance(content, list) and images_in(content):
            parts = []
            for p in content:
                if is_image_part(p):
                    p = _stored(p, store)
                parts.append(p)
            m = {**m, "content": parts}
        out.append(m)
    _prune(store)
    return out


def _stored(part: dict, store: Path) -> dict:
    meta = dict(meta_of(part))
    url = _url_of(part)
    if url.startswith(REF_SCHEME):
        return part
    if not url.startswith("data:"):
        return _note(part, "не е записана")
    mime, data = split_data_url(part)
    try:
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError):
        return _note(part, "не е записана")
    meta.setdefault("mime", mime)
    meta.setdefault("sha256", hashlib.sha256(raw).hexdigest())
    name = _ref_name(meta)
    if name is None:
        return _note(part, "не е записана")
    target = store / name
    try:
        store.mkdir(parents=True, exist_ok=True)
        if target.exists():
            os.utime(target)          # за _prune: ползвана наскоро
        else:
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, target)
    except OSError as e:
        log.debug("vision: картинката не се записа (%s)", e)
        return _note(part, "не е записана")
    return {"type": "image_url", "image_url": {"url": REF_SCHEME + name}, "genesis": meta}


def _prune(store: Path) -> None:
    try:
        files = sorted(store.glob("*.*"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:max(0, len(files) - _STORE_MAX_FILES)]:
        try:
            old.unlink()
        except OSError:
            continue


def from_disk(messages: list[dict], store: Path | None) -> list[dict]:
    """Обратното на for_disk при /resume: препратката → картинката; изтрита ли
    е (или няма `store`) — бележка: разговорът се зарежда и без нея."""
    if not has_images(messages):
        return list(messages)
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and images_in(content):
            parts = []
            for p in content:
                if not is_image_part(p):
                    parts.append(p)
                    continue
                url = _url_of(p)
                # Само `<sha256>.<ext>` от images/ (ревю 2026-10-15): друго име
                # не се отваря — `CON`/`COM1` на Windows е устройство, FIFO виси.
                ref = url[len(REF_SCHEME):] if url.startswith(REF_SCHEME) else None
                if ref is not None and store is not None and _REF_NAME.fullmatch(ref):
                    p = _loaded(p, store / ref)
                elif ref is not None:
                    p = _note(p, "файлът ѝ вече го няма в историята" if store is not None
                              else "не е заредена от историята")
                elif not url.startswith("data:"):
                    p = _note(p, "не е заредена от историята")
                parts.append(p)
            m = {**m, "content": _collapse(parts)}
        out.append(m)
    return out


def _loaded(part: dict, path: Path) -> dict:
    meta = dict(meta_of(part))
    try:
        raw = path.read_bytes()
    except OSError:
        return _note(part, "файлът ѝ вече го няма в историята")
    mime = sniff_mime(raw)
    if mime is None or hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        return _note(part, "файлът ѝ в историята е повреден")
    url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}, "genesis": meta}


# ── VIEW_IMAGE: картинка за следващата заявка ────────────────────────────────

_pending: list[dict] = []
_pending_lock = threading.Lock()


def queue_image(part: dict) -> None:
    with _pending_lock:
        _pending.append(part)


def take_pending() -> list[dict]:
    with _pending_lock:
        items = list(_pending)
        _pending.clear()
    return items


def attachment_message(parts: list[dict], what: str) -> dict:
    """Съобщението, с което картинката от инструмент стига до модела: tool
    резултат при повечето доставчици не носи картинка — затова отделно
    user съобщение веднага след резултатите."""
    return user_message(f"{ATTACHMENT_PREFIX} {what}", parts)


def describe(part: dict) -> str:
    meta = meta_of(part)
    size = f"{meta['width']}×{meta['height']} px, " if meta.get("width") else ""
    return f"{size}{int(meta.get('bytes') or 0) // 1024} KB"


def view_image(path: str, workspace: Path) -> str:
    """Инструментът VIEW_IMAGE: картинката отива в следващата заявка."""
    try:
        part = load_image(path, workspace=workspace)
    except ImageError as e:
        return f"[VIEW_IMAGE] {e}"
    queue_image(part)
    return (f"[VIEW_IMAGE: {path}] ✓ {image_name(part)} ({describe(part)}) е прикачена към "
            "следващото съобщение като картинка — погледни я там.")


TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "VIEW_IMAGE",
        "description": "Look at an image file (png/jpg/webp/gif), e.g. a screenshot: it is "
                       "attached to the next message.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
