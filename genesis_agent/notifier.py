#!/usr/bin/env python3
"""
genesis_agent/notifier.py — Multi-channel delivery за Genesis Agent.

Поддържа: Telegram.
Конфигурира се от .env:
    GENESIS_TELEGRAM_TOKEN, GENESIS_TELEGRAM_CHAT_ID

Употреба:
    from genesis_agent.notifier import send_message
    send_message("✅ Задача изпълнена: retry-decorator")
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import os
import re
import urllib.request
from collections.abc import Callable

from genesis_agent.paths import CONFIG_PATH as _CONFIG_YAML

log = logging.getLogger("genesis.notifier")

# ─── Разрешаване на конфигурация (env → .env файлове → config.yaml) ────────────
# Ядрото (autonomous loop и т.н.) не зарежда .env в os.environ, затова notifier-ът
# сам намира webhook-а от същите източници, които ползва терминалният агент.


def _from_env_files(key: str) -> str:
    """Стойността на ключ от .env файловете, или "" ако липсва.

    Делегира на paths.read_env_files вместо да преповтаря четенето (bug fix,
    2026-08-12). Локалното копие тук имаше два дефекта, които общата функция
    отдавна няма:

      • съпоставяше имената с `line.startswith(key)` — ПРЕФИКС, не точно
        съвпадение, така че `GENESIS_TELEGRAM_TOKEN` хващаше и
        `GENESIS_TELEGRAM_TOKEN_2=...`, ако то стои по-нагоре във файла.
        Тук цената е известията да заминат към ЧУЖД бот — не просто грешна
        конфигурация, а изпращане на съдържание не където трябва.
      • не махаше inline коментар, тоест
        `GENESIS_TELEGRAM_TOKEN=123:abc # моят бот` връщаше стойност със
        залепен коментар и заявката просто се проваляше.
    """
    from genesis_agent.paths import read_env_files
    return read_env_files(key) or ""


def _from_config_yaml(section: str, field: str) -> str:
    if not _CONFIG_YAML.exists():
        return ""
    try:
        import yaml
        data = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8")) or {}
        return (data.get(section, {}) or {}).get(field, "") or ""
    except Exception:
        log.debug("config.yaml не се прочете", exc_info=True)
        return ""


def resolve_setting(env_key: str, *, yaml_section: str = "", yaml_field: str = "") -> str:
    """Намира настройка от os.environ, после .env файловете, после config.yaml."""
    # strip (одит 2026-10-31): Windows `set GENESIS_TELEGRAM_TOKEN=123:abc `
    # пази интервала, `export X=$(cat token.txt)` от CRLF файл — \r. С тях
    # http.client хвърляше InvalidURL с целия токен в съобщението, известията
    # мълчаливо спираха, а следата отиваше в debug.log. .env стойностите
    # read_env_files ги чисти отдавна — средата не.
    val = os.environ.get(env_key, "").strip()
    if not val:
        val = _from_env_files(env_key)
    if not val and yaml_section and yaml_field:
        val = _from_config_yaml(yaml_section, yaml_field)
    return val


# ─── Telegram ────────────────────────────────────────────────────────────────

def _send_telegram(text: str, token: str, chat_id: str) -> bool:
    """Изпраща съобщение чрез Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (OSError, ValueError, http.client.HTTPException) as e:
        # Не само URLError (одит 2026-10-31): токен с интервал/контролен знак
        # дава http.client.InvalidURL — HTTPException, — която минаваше покрай
        # except-а, а notify() я пишеше в debug.log със следата и адреса
        # `/bot<токен>/sendMessage`. Съобщението тук носи адреса — токенът се
        # маха изрично, и по формата си (redact), и дословно.
        msg = redact(f"{type(e).__name__}: {e}")
        if token:
            msg = msg.replace(token, "***")
        print(f"[notifier] Telegram грешка: {msg}")
        return False


# ─── Публичен интерфейс ───────────────────────────────────────────────────────

# Известие за блокирана команда носеше самата команда — и токена в нея:
# `git push https://me:ghp_…@github.com`, `Authorization: Bearer sk-ant-…`
# заминаваха към Telegram (одит 2026-10-07). Тайните се маскират тук, на
# изхода, независимо кой вика notify().
# Познатите формати API ключове — споделени с `genesis export` (deliver.py),
# който по тях задържа файл с ключ, вписан в кода (одит 2026-10-11).
API_TOKEN_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{16,}|gh[ousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{16,}|gsk_[A-Za-z0-9]{16,}|nvapi-[A-Za-z0-9_-]{16,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})")
# Одит 2026-10-27: `deploy:hunter2@host` без `://` (scp), `--password x`,
# `-p x`, `-u user:pass` минаваха нередактирани; а думата-ключ се търсеше и
# вътре в думи — `path:`, `pattern:`, `bypass:`, `compass:`, `passage:` ставаха
# `path=***`. Сега ключът е цяла дума (`_`-токен; слепен отпред само с главни
# букви — PGPASSWORD — или с api/auth/access/db — apikey).
#
# Одит 2026-10-31 (шести кръг), всичко възпроизведено през _tool_run_cmd до
# записан Telegram payload:
#   • стойността беше `\S+` — от `DB_PASSWORD="correct horse battery staple"`
#     се маскираше само първата дума; сега стойност в кавички се взима цяла;
#   • `redis://:парола@host` (Redis/Celery нямат потребител) — изискваше се
#     поне един знак преди `:`;
#   • ключ в кавички — `{"password": "x"}`, `{'api_key': 'x'}`, AWS
#     `"SecretAccessKey": "x"` — кавичката между ключа и `:` чупеше общото
#     правило. Преди ключовата дума се иска `_`/`-` или camelCase граница, за
#     да остане `"monkey"`/`"compass"`;
#   • флагове: слепено `-phunter2` (mysql, sshpass, 7z — само след тях, иначе
#     `-print`/`-pthread`), `-P` на sqlcmd/zip (не глобално: `scp -P 2222`),
#     `redis-cli -a`, `--user u:p`/`-uu:p` (само с `:` — `pip install --user
#     requests` остава), `--api-key`/`--secret`/`--*-password`;
#   • токенът на САМИЯ Telegram бот (`<id>:AA…`, и в `/bot<id>:AA…/`) не се
#     познаваше, а debug.log пазеше InvalidURL с него. Върви първи: `token
#     <id>:AA…` иначе ставаше `token ***:AA…` — правилото за bearer спира на `:`;
#   • `BYPASS:`/`COMPASS:` в главни и `--- PASS: TestX` (go test) ставаха
#     `X=***`. Слепени отпред с главни букви са само силните думи (PGPASSWORD,
#     PGPassword, JWTSecret, VAULTtoken), а `--- PASS:` е изход, не тайна.
#
# Стойността е цялата shell дума: части в кавички и слепените за тях (след
# прегледа, 2026-11-05). С `"…"|'…'|\S+` маскирането спираше на първата
# затваряща кавичка — `shlex.quote("Xy7'q9!secret")` дава `'Xy7'"'"'q9!secret'`
# и от `DB_PASSWORD=`, `--password `, `mysql -p`, `sshpass -p` оставаше видимо
# `q9!secret`; също `PASSWORD="abc"def` → `***def`. Мерено на 2160 генерирани
# входа (27 ключа/флага × 10 стойности × 8 обкръжения): с `…|\S+` изтичаха
# 1248, от тях 448, които `\S+` на a4fd75c маскираше; сега 0. Стойността е
# последна във всяко правило, затова назад не се връща: 50 KB вход — 0.01–0.1 s,
# колкото и преди.
_VALUE = r"""(?:"[^"\n]*"|'[^'\n]*'|\S)+"""


def _tool_flag(tools: str, flag: str) -> re.Pattern[str]:
    """Флаг с парола, но само след изброените програми: `-p`/`-P` на друго
    място е `-print`, `-pthread`, `scp -P 2222`.

    До 300 знака между програмата и флага: без таван ред от 30 000 знака
    `mysql mysql …` (debug.log) отнемаше 1.2 s, с него — 0.03 s (2026-10-31)."""
    # Името — без значение на регистъра (сливането с #108, 2026-11-05):
    # `MySQL.exe -uroot -phunter2` на Windows минаваше; флагът остава точен
    # (`mysql -P 3306` е порт).
    return re.compile(rf"(?<![\w.-])((?i:{tools})(?i:\.exe)?\b[^\n|;&]{{0,300}}?\s{flag})"
                      rf"(?=[^\s-]){_VALUE}")


_RUN_RE = re.compile(r"[\w.+-]+")
_USERPASS_TAIL_RE = re.compile(r"[^\s@/:]+@(?=[\w.-])")


def _mask_user_pass_at(text: str) -> str:
    """`user:pass@host` без `://` → `***@host`.

    Същото като `(?<![\\w/@:])[\\w.+-]+:[^\\s@/:]+@(?=[\\w.-]+)`, но с един
    проход: изразът опитваше от всяка позиция след `.`/`-`/`+` и всеки път
    стигаше до края на поредицата — ред от 50 000 точки отнемаше 8 s, от
    тирета 7 s, а debug.log минава всеки запис през redact (2026-11-05; сега
    под 0.05 s). Съвпадение може да има само ако поредицата от `[\\w.+-]`
    свършва с `:`; началото е най-лявото, което lookbehind-ът пуска."""
    if ":" not in text or "@" not in text:
        return text
    out: list[str] = []
    last = 0
    for m in _RUN_RE.finditer(text):
        start, end = m.span()
        if start < last or text[end:end + 1] != ":":
            continue
        p = start
        if start and text[start - 1] in "/@:":
            p = next((i for i in range(start + 1, end) if text[i - 1] in ".+-"), -1)
            if p < 0:
                continue
        tail = _USERPASS_TAIL_RE.match(text, end + 1)
        if tail:
            out += [text[last:p], "***@"]
            last = tail.end()
    out.append(text[last:])
    return "".join(out)


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str] | Callable[[str], str]] = [
    (re.compile(r"\b((?:bot)?)\d{6,12}:AA[A-Za-z0-9_-]{30,}"), r"\1***"),
    (re.compile(r"(://)[^/\s:@]*:[^/\s@]+@"), r"\1***@"),
    _mask_user_pass_at,
    # 2026-11-09 (отвореното от 31.10, мерено с 29 реда): `"APIKEY"`/`"apikey"`
    # (слепено без граница — само api/auth/access/db, `"monkey"` остава),
    # `"passphrase"`, индекс вместо литерал — `d['password'] = 'x'`, — и
    # сравнение (`== 'x'`, `!= 'x'`: стойността пак е тайната) изтичаха.
    (re.compile(r"""(["'](?:[\w-]*[_-](?i:api|auth|access|db)?|[A-Za-z0-9]*[a-z0-9](?=[A-Z])"""
                r"|(?i:api|auth|access|db))?"
                r"(?:[Kk]ey|KEY|[Tt]oken|TOKEN|[Ss]ecret|SECRET|[Pp]ass(?:word|wd|phrase)?|PASS(?:WORD|WD|PHRASE)?"
                r"""|[Pp]wd|PWD|[Pp]at|PAT|[Dd]sn|DSN)\\?["']\]?\s*(?::|=>|!?={1,3})\s*)"""
                r"""(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|[^\s,}]+)"""),
     r'\1"***"'),
    # `mysql_config`/`mariadb_config` не са клиенти: `gcc $(mysql_config
    # --cflags) -pthread` ставаше `-p***` (прегледът, 2026-11-05).
    (_tool_flag(r"(?:mysql|mariadb)(?!_config\b)\w*|sshpass|7z[ar]?|(?:un)?rar", "-p"),
     r"\1***"),
    # `rar -hp<парола>` шифрова и имената; `ssh-keygen -N <фраза>` (2026-11-09).
    (_tool_flag(r"(?:un)?rar", "-hp"), r"\1***"),
    (_tool_flag(r"ssh-keygen", "-N ?"), r"\1***"),
    (_tool_flag(r"sqlcmd|osql|isql|bcp|zip|unzip|zipcloak", "-P ?"), r"\1***"),
    (_tool_flag(r"redis-cli", "-a ?"), r"\1***"),
    # `--user` е цял флаг: `chrome --user-data-dir=C:\Users\x` и
    # `--user-agent=a:b` ставаха `--user ***` (прегледът, 2026-11-05).
    (re.compile(rf"(?<!\S)(--(?:proxy-)?user(?![\w-])|-u)[ =]?(?=[\"']?[^\s\"':]*:){_VALUE}"),
     r"\1 ***"),
    (re.compile(r"(?i)(?<!\S)(--?pass(?:word|wd|phrase)?|--?pwd|(?-i:-p|-u)"
                r"|--(?!no-)(?:[a-z0-9]+-)*(?:password|passwd|passphrase|api-?key|secret(?:-access)?-key|private-key|secret))"
                rf"[ =]{_VALUE}"), r"\1 ***"),
    (re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    # Сливане с одита 2026-11-05 от main: голото `PASS: x`/`PAT: x` се маскира
    # (изтекла тайна е по-лошо от маскиран ред на тест); само `--- PASS:` на
    # `go test` остава. Слепено отпред — само пред силна дума: `TURKEY:`,
    # `MONKEY:` не са ключове.
    (re.compile(r"(?i)(?<!--- )\b((?:[A-Z0-9]+_)*"
                r"(?:(?-i:[A-Z0-9]+)(?=(?:PASSWORD|PASSWD|PASSPHRASE|TOKEN|SECRET)(?![A-Za-z]))|api|auth|access|db)?"
                r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|PWD|PASS|PAT|DSN)(?![A-Za-z])[A-Z0-9_]*)"
                rf"\s*[=:]\s*{_VALUE}"),
     r"\1=***"),
    (API_TOKEN_RE, "***"),
]


def redact(text: str) -> str:
    """Текстът без ключове, токени и пароли в адреси."""
    for rule in _SECRET_PATTERNS:
        if callable(rule):
            text = rule(text)
        else:
            rx, repl = rule
            text = rx.sub(repl, text)
    return text


# Викащите пишат Markdown (`**Genesis Sandbox**`, командата в `…`), а заявката
# е с parse_mode=HTML — всяко известие пристигаше с буквални ** и ` (одит
# 2026-10-31, записан payload). Превръща се СЛЕД html.escape, така че текстът
# не може да вмъкне свой таг. Кодът се отделя пръв и вътре в него нищо не се
# пипа: Telegram отхвърля `<code>` с вложен таг (400 — известието не тръгва).
# `***` от redact не е маркер за удебеляване — затова (?<!\*)…(?!\*).
_CODE_SPAN_RE = re.compile(r"(`[^`]+`)")
_BOLD_RE = re.compile(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)")


def _markdown_to_html(escaped: str) -> str:
    parts = _CODE_SPAN_RE.split(escaped)
    return "".join(f"<code>{p[1:-1]}</code>" if i % 2 else _BOLD_RE.sub(r"<b>\1</b>", p)
                   for i, p in enumerate(parts))


def send_message(
    text: str,
    *,
    channels: list[str] | None = None,
) -> dict[str, bool]:
    """
    Изпраща съобщение към всички конфигурирани канали.

    Args:
        text:     Текстът на съобщението.
        channels: Списък от канали ['telegram'].
                  По подразбиране — всички налични.

    Returns:
        dict с резултат за всеки канал: {'telegram': True}
    """
    results: dict[str, bool] = {}
    text = _markdown_to_html(html.escape(redact(text), quote=False))

    if channels is None:
        channels = ["telegram"]

    if "telegram" in channels:
        token = resolve_setting("GENESIS_TELEGRAM_TOKEN")
        chat_id = resolve_setting("GENESIS_TELEGRAM_CHAT_ID")
        if token and chat_id:
            results["telegram"] = _send_telegram(text, token, chat_id)
        else:
            results["telegram"] = False  # Не е конфигуриран

    return results


def notify(text: str) -> bool:
    """
    Удобен helper — праща до всички конфигурирани канали, безопасно за
    извикване отвсякъде (никога не хвърля). Връща True ако поне един канал
    е приел съобщението.
    """
    try:
        results = send_message(text)
        return any(results.values())
    except Exception:
        log.debug("известието не замина", exc_info=True)
        return False


if __name__ == "__main__":
    # Тест (нужни са env vars)
    r = send_message("🤖 Genesis notifier тест!")
    print(f"Резултати: {r}")
    if not any(r.values()):
        print("⚠️  Нито един канал не е конфигуриран.")
        print("   Задайте: GENESIS_TELEGRAM_TOKEN + GENESIS_TELEGRAM_CHAT_ID")
