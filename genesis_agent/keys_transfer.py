"""genesis_agent.keys_transfer — ключовете от компютъра към Genesis на телефона.

    genesis keys test [--from ФАЙЛ]     проверява всеки ключ с истинска заявка
    genesis keys qr [--from ФАЙЛ]       QR код с ключовете, сканира се с телефона
    genesis keys import <връзка|ФАЙЛ>   записва ключове в ~/.genesis/.env тук

QR кодът носи връзката genesisremote://keys?gq=…&ol=… (кратки имена, SHORT). Приложението Genesis
(меню → „Ключове от компютъра“) я сканира, показва кои ключове идват и с
едно докосване ги записва в Genesis на телефона — op `import_keys` по
криптирания канал (remote_server.py) → `save()` тук.

Без `--from` ключовете са тези, с които Genesis работи на компютъра
(~/.genesis/.env и променливите на средата). С `--from` — от текстов файл:
редове `ИМЕ=стойност`, или просто ключове, познати по началото си
(`gsk_` е Groq, `AIza` е Gemini…) или по доставчика, написан на същия ред.

Кодът съдържа ключовете открито — както QR кодът на `genesis serve` носи
ключа за достъп до целия компютър: показва се на собствения екран и
страницата с него се трие веднага след сканирането.
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

APP_LINK = "genesisremote://keys"

# Кои имена се пренасят: само ключовете на доставчиците на модели (brain._PROVIDERS), с резервните
# `_2`…`_10`, които brain ротира, и известията в Telegram. Не „всичко, което
# свършва на _TOKEN“: в средата на компютъра живеят и чужди токени (на
# редактора, на сайта), които нямат работа на телефона.
PROVIDER_KEYS = {
    "OLLAMA_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
    "NVIDIA_API_KEY", "GEMINI_API_KEY", "HF_TOKEN", "SAMBANOVA_API_KEY",
    "TOGETHER_API_KEY", "COHERE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
}
_NUMBERED = re.compile(r"^([A-Z][A-Z0-9_]+?)(?:_([2-9]|10))?$")
_EXTRA_NAMES = {"GENESIS_TELEGRAM_TOKEN", "GENESIS_TELEGRAM_CHAT_ID"}
_VALUE = re.compile(r"^[\x21-\x7e]{8,1024}$")   # видими ASCII знаци, без интервали

# Ключ без име във файла се познава по началото си.
_PREFIXES: list[tuple[str, str]] = [
    ("sk-or-", "OPENROUTER_API_KEY"),
    ("sk-ant-", "ANTHROPIC_API_KEY"),
    ("sk-proj-", "OPENAI_API_KEY"),
    ("gsk_", "GROQ_API_KEY"),
    ("AIza", "GEMINI_API_KEY"),
    ("nvapi-", "NVIDIA_API_KEY"),
    ("hf_", "HF_TOKEN"),
    ("csk-", "CEREBRAS_API_KEY"),
    ("tgp_", "TOGETHER_API_KEY"),
]
# …или по доставчика, написан на същия ред („groq: …“, „Ollama ключ …“).
_PROVIDER_WORDS: list[tuple[str, str]] = [
    ("openrouter", "OPENROUTER_API_KEY"),
    ("ollama", "OLLAMA_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
    ("google", "GEMINI_API_KEY"),
    ("nvidia", "NVIDIA_API_KEY"),
    ("cerebras", "CEREBRAS_API_KEY"),
    ("sambanova", "SAMBANOVA_API_KEY"),
    ("together", "TOGETHER_API_KEY"),
    ("cohere", "COHERE_API_KEY"),
    ("huggingface", "HF_TOKEN"),
    ("hugging face", "HF_TOKEN"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
]
_TOKEN = re.compile(r"[A-Za-z0-9_\-\.]{20,}")


def is_key_name(name: str) -> bool:
    m = _NUMBERED.match(name)
    return name in _EXTRA_NAMES or bool(m and m.group(1) in PROVIDER_KEYS)


def is_key_value(value: str) -> bool:
    return bool(_VALUE.match(value))


def clean(keys: dict) -> dict[str, str]:
    """Само ключове с познато име и стойност, която става за ред в .env."""
    out: dict[str, str] = {}
    for name, value in (keys or {}).items():
        if isinstance(name, str) and isinstance(value, str):
            value = value.strip()
            if is_key_name(name) and is_key_value(value):
                out[name] = value
    return out


def mask(value: str) -> str:
    return f"{value[:4]}…{value[-3:]}" if len(value) > 12 else "…"


def _unquote(value: str) -> str:
    from genesis_agent.paths import _strip_inline_comment
    return _strip_inline_comment(value.strip()).strip().strip('"').strip("'")


def from_env() -> dict[str, str]:
    """Ключовете, с които Genesis работи на тази машина: .env файловете, после
    средата (тя печели, както в paths.get_secret)."""
    from genesis_agent.paths import ENV_FILES
    found: dict[str, str] = {}
    for env_file in ENV_FILES:
        try:
            text = Path(env_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            found.setdefault(name.strip(), _unquote(value))
    for name, value in os.environ.items():
        if value:
            found[name] = value
    return clean(found)


def parse_text(text: str) -> tuple[dict[str, str], list[str]]:
    """Ключове от свободен текст → (ключове, непознати низове — маскирани)."""
    found: dict[str, str] = {}
    unknown: list[str] = []
    for line in text.splitlines():
        s = line.strip().removeprefix("export ").strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*[=:]\s*(\S+)\s*$", s)
        if m and is_key_name(m.group(1).upper()):
            found.setdefault(m.group(1).upper(), _unquote(m.group(2)))
            continue
        lower = s.lower()
        for token in _TOKEN.findall(s):
            token = token.strip(".:")
            name = next((n for p, n in _PREFIXES if token.startswith(p)), None)
            if name is None:
                name = next((n for w, n in _PROVIDER_WORDS if w in lower), None)
            if name is None or not is_key_value(token):
                unknown.append(mask(token))
                continue
            # Втори ключ за същия доставчик → ИМЕ_2, ИМЕ_3… (brain._numbered_keys
            # ги ротира), точно както са записани в .env на компютъра.
            if token in found.values():
                continue
            n = 1
            while (f"{name}_{n}" if n > 1 else name) in found:
                n += 1
            if n <= 10:
                found[f"{name}_{n}" if n > 1 else name] = token
    return clean(found), unknown


def read_file(path: str | Path) -> tuple[dict[str, str], list[str]]:
    raw = Path(path).expanduser().read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            return parse_text(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_text(raw.decode("utf-8", errors="replace"))


# ── връзката в QR кода ──────────────────────────────────────────────────────

# Кратки имена в QR кода: всеки знак там прави кода по-гъст и по-труден за
# сканиране от екран (15 ключа с пълни имена в JSON и base64 — версия 32,
# 145 модула; така — около 97). Същата таблица е в mobile/src/lib/keys.ts.
SHORT = {
    "OLLAMA_API_KEY": "ol", "GROQ_API_KEY": "gq", "OPENROUTER_API_KEY": "or",
    "CEREBRAS_API_KEY": "cb", "NVIDIA_API_KEY": "nv", "GEMINI_API_KEY": "gm",
    "HF_TOKEN": "hf", "SAMBANOVA_API_KEY": "sn", "TOGETHER_API_KEY": "tg",
    "COHERE_API_KEY": "co", "OPENAI_API_KEY": "oa", "DEEPSEEK_API_KEY": "ds",
    "ANTHROPIC_API_KEY": "an", "GENESIS_TELEGRAM_TOKEN": "tt", "GENESIS_TELEGRAM_CHAT_ID": "tc",
}
_LONG = {v: k for k, v in SHORT.items()}


def _short(name: str) -> str:
    m = _NUMBERED.match(name)
    if name in SHORT:
        return SHORT[name]
    return SHORT[m.group(1)] + m.group(2) if m and m.group(1) in SHORT and m.group(2) else name


def _long(code: str) -> str:
    m = re.match(r"^([a-z]{2})(\d{0,2})$", code)
    if not m or m.group(1) not in _LONG:
        return ""
    return _LONG[m.group(1)] + (f"_{m.group(2)}" if m.group(2) else "")


def encode_link(keys: dict[str, str]) -> str:
    """genesisremote://keys?gq=…&gq2=…&ol=… — всеки ключ е отделен параметър."""
    return f"{APP_LINK}?" + urlencode({_short(n): v for n, v in clean(keys).items()})


def decode_link(link: str) -> dict[str, str]:
    """Обратното на encode_link (и на mobile/src/lib/keys.ts)."""
    parsed = urlparse(link.strip())
    if f"{parsed.scheme}://{parsed.netloc}" != APP_LINK:
        return {}
    return clean({_long(code): values[0] for code, values in parse_qs(parsed.query).items()})


# ── запис ───────────────────────────────────────────────────────────────────

def save(keys: dict[str, str]) -> list[str]:
    """Слива ключовете в ~/.genesis/.env (с резервно копие) и ги прави видими
    за работещия агент веднага — Brain чете файла при всяко извикване."""
    from genesis_agent.paths import ENV_FILE, ensure_genesis_home
    from genesis_agent.setup_wizard import _prune_backups, _write_private

    keys = clean(keys)
    if not keys:
        return []
    ensure_genesis_home()
    old = ENV_FILE.read_text(encoding="utf-8", errors="replace") if ENV_FILE.exists() else ""
    if old:
        backup = ENV_FILE.with_name(f".env.backup-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        _write_private(backup, old)
        _prune_backups(ENV_FILE.parent)
    lines, seen = [], set()
    for line in old.splitlines():
        name = line.strip().removeprefix("export ").partition("=")[0].strip()
        if name in keys:
            if name not in seen:
                lines.append(f"{name}={keys[name]}")
                seen.add(name)
            continue
        lines.append(line)
    new = [n for n in keys if n not in seen]
    if new:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# Пренесени от компютъра, {datetime.date.today().isoformat()}")
        lines += [f"{n}={keys[n]}" for n in new]
    _write_private(ENV_FILE, "\n".join(lines) + "\n")

    # Работещият процес: средата печели пред файла (paths.get_secret, brain),
    # а терминалният агент пази ключовете в KEYS от стартирането си.
    for name, value in keys.items():
        os.environ[name] = value
    gta = sys.modules.get("genesis_terminal_agent")
    if gta is not None and isinstance(getattr(gta, "KEYS", None), dict):
        for name, value in keys.items():
            if name in gta.KEYS:
                gta.KEYS[name] = value
    return list(keys)


# ── проверка ────────────────────────────────────────────────────────────────

def _listed_model(base_url: str, key: str) -> str | None:
    """Първият модел от /models на доставчика — за доставчик, който не е във
    веригата (напр. изваден Cerebras): иначе ключът му не би имал с какво да се пробва."""
    import requests
    try:
        r = requests.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        data = r.json().get("data") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None
    ids = [m.get("id") for m in data or [] if isinstance(m, dict) and m.get("id")]
    return ids[0] if ids else None


def _probe_target(name: str, key: str) -> tuple[str, str] | None:
    """(base_url, модел) за проба на ключа — моделът е първият от веригата на
    агента за този доставчик, т.е. точно заявката, която Genesis ще прави."""
    base_name = re.sub(r"_\d+$", "", name)
    from genesis_agent.setup_wizard import PAID_PROVIDERS, PROVIDERS
    try:
        from genesis_agent.brain import _PROVIDERS, _load_chain
        chain = _load_chain()
    except Exception:
        _PROVIDERS, chain = {}, []
    for provider, (base_url, key_env) in _PROVIDERS.items():
        if key_env == base_name and not base_url.startswith("dynamic://"):
            model = next((c["model"] for c in chain if c.get("provider") == provider), None)
            if model is None:
                model = next((m for v, _, _, _, m in PROVIDERS + PAID_PROVIDERS if v == base_name), None)
            if model is None and not base_url.startswith("native://"):
                model = _listed_model(base_url, key)
            if model:
                return base_url, model
    return None


def test(keys: dict[str, str]) -> list[tuple[str, bool | None, str]]:
    """[(име, работи ли, защо)] — None за ключ, който не е за модел."""
    from genesis_agent.setup_wizard import _test_anthropic_key, _test_key
    results: list[tuple[str, bool | None, str]] = []
    for name, value in clean(keys).items():
        target = _probe_target(name, value)
        if target is None:
            results.append((name, None, "няма с какво да го пробвам (доставчикът не върна модели)"))
            continue
        base_url, model = target
        if base_url.startswith("native://"):
            ok, why = _test_anthropic_key(value, model)
        else:
            ok, why = _test_key(base_url, value, model)
        results.append((name, ok, f"{why} ({model})"))
    return results


# ── QR страницата ──────────────────────────────────────────────────────────

def qr_page(link: str, names: list[str]) -> str:
    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=4)
    qr.add_data(link)
    qr.make(fit=True)
    svg = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).to_string(encoding="unicode")
    items = "".join(f"<li>{n}</li>" for n in names)
    return f"""<!doctype html><html lang="bg"><head><meta charset="utf-8">
<title>Genesis — ключове за телефона</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#0f0e14;color:#ecebf2;margin:0;display:flex;
      gap:40px;align-items:center;justify-content:center;min-height:100vh;flex-wrap:wrap}}
 .qr{{background:#fff;padding:18px;border-radius:16px;width:min(80vh,560px)}}
 .qr svg{{width:100%;height:auto;display:block}}
 .txt{{max-width:420px;line-height:1.5}} h1{{font-size:26px}} li{{font-family:monospace}}
 .warn{{color:#fbbf24;font-size:14px}}
</style></head><body>
<div class="qr">{svg}</div>
<div class="txt"><h1>Ключове за Genesis на телефона</h1>
<ol><li style="font-family:inherit">Отвори Genesis на телефона → меню ⋯ → <b>Ключове от компютъра</b></li>
<li style="font-family:inherit">Насочи камерата към кода</li>
<li style="font-family:inherit">Натисни <b>Запиши</b></li></ol>
<p>В кода ({len(names)}):</p><ul>{items}</ul>
<p class="warn">Кодът съдържа самите ключове. Затвори страницата след сканирането —
файлът ѝ се трие, щом натиснеш Enter в терминала.</p></div>
</body></html>"""


# ── CLI ─────────────────────────────────────────────────────────────────────

USAGE = """Употреба:
  genesis keys test [--from ФАЙЛ]     проверява ключовете (истинска заявка към всеки доставчик)
  genesis keys qr [--from ФАЙЛ] [--all]
                                      QR код за телефона (приложението Genesis → Ключове от
                                      компютъра); само работещите ключове, с --all — всички
  genesis keys import <връзка|ФАЙЛ>   записва ключове в ~/.genesis/.env на тази машина

Без --from: ключовете, с които Genesis работи тук (~/.genesis/.env)."""


def _source(args: list[str]) -> tuple[dict[str, str], list[str]] | None:
    args = [a for a in args if a != "--all"]
    if args[:1] == ["--from"]:
        if len(args) < 2:
            print("--from иска път до файл.")
            return None
        try:
            return read_file(args[1])
        except OSError as e:
            print(f"Не мога да прочета {args[1]}: {e}")
            return None
    if args:
        print(f"Непозната опция: {args[0]}\n\n{USAGE}")
        return None
    return from_env(), []


def _report(keys: dict[str, str], unknown: list[str]) -> None:
    for name, value in keys.items():
        print(f"  {name:<28} {mask(value)}")
    for token in unknown:
        print(f"  (непознат низ, пропуснат)    {token}")


def main(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if args else 2
    cmd, rest = args[0], args[1:]

    if cmd == "import":
        if not rest:
            print(USAGE)
            return 2
        src = rest[0]
        keys = decode_link(src) if src.startswith(APP_LINK) else read_file(src)[0]
        saved = save(keys)
        print(f"Записани в ~/.genesis/.env: {', '.join(saved)}" if saved else "Няма ключове за запис.")
        return 0 if saved else 1

    source = _source(rest)
    if source is None:
        return 2
    keys, unknown = source
    if not keys:
        print("Не намерих ключове." + (" Непознати низове: " + ", ".join(unknown) if unknown else ""))
        return 1

    if cmd == "test":
        print(f"Проверявам {len(keys)} ключа (по една малка заявка)…\n")
        results = test(keys)
        for name, ok, why in results:
            mark = "—" if ok is None else ("✅" if ok else "❌")
            print(f"  {mark} {name:<28} {why}")
        for token in unknown:
            print(f"  ? (непознат низ)              {token}")
        return 0 if any(ok for _, ok, _ in results) else 1

    if cmd == "qr":
        if "--all" not in rest:
            # Счупен ключ на телефона е само шум във веригата — и прави кода по-гъст.
            print(f"Проверявам {len(keys)} ключа, в кода влизат само работещите…")
            dead = {name: why for name, ok, why in test(keys) if ok is False}
            for name, why in dead.items():
                print(f"  ❌ {name}: {why} — не влиза")
            keys = {n: v for n, v in keys.items() if n not in dead}
            if not keys:
                print("Нито един работещ ключ.")
                return 1
        print(f"\nВ кода влизат {len(keys)} ключа:")
        _report(keys, unknown)
        import tempfile
        import webbrowser
        fd, path = tempfile.mkstemp(prefix="genesis-keys-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(qr_page(encode_link(keys), list(keys)))
        try:
            webbrowser.open(Path(path).as_uri())
            print("\nQR кодът е отворен в браузъра. Сканирай го от приложението Genesis "
                  "(меню ⋯ → Ключове от компютъра).")
            try:
                input("Enter, когато си готов — страницата с ключовете се трие. ")
            except (EOFError, KeyboardInterrupt):
                pass
        finally:
            Path(path).unlink(missing_ok=True)
        return 0

    print(f"Непозната команда: {cmd}\n\n{USAGE}")
    return 2
