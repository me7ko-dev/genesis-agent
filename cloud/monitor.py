"""Наблюдение и аларми на онлайн услугата — на 5 минути от genesis-monitor.timer.

    sudo python3 -m cloud.monitor --once --dry-run   # какво би пратил; нищо не праща и не пише
    sudo python3 -m cloud.monitor --once             # една проверка (така го пуска таймерът)

Какво гледа (всяко е „наред“ или „проблем“ с обяснение):
  web       /healthz на уеб сървъра (check.web_health);
  helper    шлюзът или проксито е Up и не се рестартира (check.helper);
  disk      свободно място за /srv/jobs и за web.db (check.free_gb);
  turns     провалени ходове за последния час над прага;
  spend     похарчени кредити за последния час — на човек и общо — над тавана;
  lockouts  заключени входове за последния час (таблица lockouts в web.db);
  stuck     контейнер на задача, по-стар от тавана на задачата + 2 мин;
  backup    последното копие от cloud/backup.py е по-старо от прага.

Аларма се праща само при смяна: наред → проблем и проблем → наред. Предишното
състояние е в monitor-state.json до web.db (без него всичко е „наред“). Каналът
— имейл по SMTP и/или webhook (JSON за Slack, Discord или Telegram бот) — и
праговете са в /srv/genesis/monitor.env (виж cloud/deploy/monitor.env.example).
Ако пращането не успее, състоянието не се записва — следващото пускане опитва
пак — и кодът е 1, за да се вижда в `systemctl --failed`. Паролата за SMTP и
адресът на webhook-а (в него е жетонът на Telegram бота) не се печатат: в
грешките се заменят с ***.

Защо (2026-10-12): check.py се пускаше само при деплой; паднал шлюз, пълен
диск, висящ контейнер, разход или налучкване на пароли се виждаха само ако
операторът погледне. Мерено в tests/test_cloud_monitor.py с фалшив SMTP,
webhook, Docker и часовник: аларма при наред → проблем, тишина при второ
пускане с проблем, „наред“ при оправяне, повтор при паднал канал.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import socket
import sqlite3
import ssl
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cloud import backup, check
from cloud.gateway.gateway import read_keys
from cloud.runner import launch
from cloud.web import billing
from cloud.web.store import MICRO

HOUR = 3600
# Контейнерът живее до тавана на задачата; над него + това е „висящ“
# (launch спира задачата на limits.seconds, docker rm отнема секунди).
STUCK_MARGIN = 120
LABELS = {"web": "уеб сървърът", "helper": "шлюзът/проксито", "disk": "диск",
          "turns": "провалени ходове", "spend": "разход", "lockouts": "заключени входове",
          "stuck": "висящи задачи", "backup": "резервно копие"}
WEBHOOK_FORMATS = ("text", "discord", "telegram")
SMTP_TLS = ("starttls", "ssl", "none")


@dataclass(frozen=True)
class Settings:
    min_free_gb: float = 5.0
    max_failed_turns: int = 10                 # за последния час
    max_user_spend: int = 200 * MICRO          # µc за последния час, на човек
    max_total_spend: int = 1000 * MICRO        # µc за последния час, общо
    max_lockouts: int = 20                     # за последния час
    task_seconds: int = launch.Limits.seconds  # като --seconds на уеб сървъра
    max_backup_hours: float = 13.0             # таймерът е на 6 ч — две пропуснати копия
    name: str = ""
    webhook_url: str = ""
    webhook_format: str = "text"
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_tls: str = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: tuple[str, ...] = field(default_factory=tuple)

    def secrets(self) -> list[str]:
        """Каквото не бива да стигне до журнала (и части от адреса на webhook-а)."""
        out = [self.smtp_password, self.webhook_url]
        if self.webhook_url:
            parts = urlsplit(self.webhook_url)
            out += [parts.path, parts.query]
        return [s for s in out if len(s) >= 4]

    def channels(self) -> list[str]:
        """Каналите без тайни — за журнала и --dry-run."""
        out = []
        if self.smtp_host:
            out.append(f"имейл до {', '.join(self.smtp_to)} през {self.smtp_host}:{self.smtp_port}"
                       f" ({self.smtp_tls})")
        if self.webhook_url:
            out.append(f"webhook {self.webhook_format} към {urlsplit(self.webhook_url).hostname}")
        return out


def _number(env: dict[str, str], key: str, cast: Callable[[str], Any], default: Any) -> Any:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{key}={raw!r} не е число") from None
    if not value >= 0:     # и NaN
        raise ValueError(f"{key}={raw!r} трябва да е ≥ 0")
    return value


def load_settings(env: dict[str, str]) -> Settings:
    """Settings от KEY=value (monitor.env). ValueError с името на грешния ключ."""
    s = Settings(
        min_free_gb=_number(env, "MONITOR_MIN_FREE_GB", float, Settings.min_free_gb),
        max_failed_turns=_number(env, "MONITOR_MAX_FAILED_TURNS", int, Settings.max_failed_turns),
        max_user_spend=_number(env, "MONITOR_MAX_USER_CREDITS_HOUR", billing.parse_credits,
                               Settings.max_user_spend),
        max_total_spend=_number(env, "MONITOR_MAX_TOTAL_CREDITS_HOUR", billing.parse_credits,
                                Settings.max_total_spend),
        max_lockouts=_number(env, "MONITOR_MAX_LOCKOUTS_HOUR", int, Settings.max_lockouts),
        task_seconds=_number(env, "MONITOR_TASK_SECONDS", int, Settings.task_seconds),
        max_backup_hours=_number(env, "MONITOR_MAX_BACKUP_HOURS", float, Settings.max_backup_hours),
        name=env.get("MONITOR_NAME", ""),
        webhook_url=env.get("MONITOR_WEBHOOK_URL", ""),
        webhook_format=env.get("MONITOR_WEBHOOK_FORMAT", "text"),
        telegram_chat_id=env.get("MONITOR_TELEGRAM_CHAT_ID", ""),
        smtp_host=env.get("MONITOR_SMTP_HOST", ""),
        smtp_port=_number(env, "MONITOR_SMTP_PORT", int, Settings.smtp_port),
        smtp_tls=env.get("MONITOR_SMTP_TLS", "starttls"),
        smtp_user=env.get("MONITOR_SMTP_USER", ""),
        smtp_password=env.get("MONITOR_SMTP_PASSWORD", ""),
        smtp_from=env.get("MONITOR_SMTP_FROM", ""),
        smtp_to=tuple(a.strip() for a in env.get("MONITOR_SMTP_TO", "").split(",") if a.strip()),
    )
    if s.webhook_url and urlsplit(s.webhook_url).scheme not in ("https", "http"):
        raise ValueError("MONITOR_WEBHOOK_URL трябва да е https://…")
    if s.webhook_format not in WEBHOOK_FORMATS:
        raise ValueError(f"MONITOR_WEBHOOK_FORMAT е някое от {', '.join(WEBHOOK_FORMATS)}")
    if s.webhook_format == "telegram" and not s.telegram_chat_id:
        raise ValueError("за Telegram е нужен MONITOR_TELEGRAM_CHAT_ID")
    if s.smtp_tls not in SMTP_TLS:
        raise ValueError(f"MONITOR_SMTP_TLS е някое от {', '.join(SMTP_TLS)}")
    if s.smtp_host and not (s.smtp_from and s.smtp_to):
        raise ValueError("за имейл са нужни MONITOR_SMTP_FROM и MONITOR_SMTP_TO")
    if s.smtp_user and not s.smtp_password:
        raise ValueError("MONITOR_SMTP_USER е без MONITOR_SMTP_PASSWORD")
    return s


# ── проверките ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str


def _query(data: Path, sql: str, args: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """Само четене на web.db — наблюдението не пише в базата на сървъра."""
    path = data / "web.db"
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()
    except sqlite3.Error as e:
        raise check.CheckFailed(f"{path}: {e}") from None


def _credits(micro: int) -> str:
    return billing.fmt_credits(micro)


def check_disk(paths: list[Path], s: Settings) -> tuple[bool, str]:
    free = {p: check.free_gb(p) for p in paths}
    low = [p for p, gb in free.items() if gb < s.min_free_gb]
    detail = ", ".join(f"{p} {gb:.1f} GB" for p, gb in free.items())
    return not low, f"свободни: {detail} (праг {s.min_free_gb:g} GB)"


def check_turns(data: Path, s: Settings, now: float) -> tuple[bool, str]:
    (n,), = _query(data, "SELECT COUNT(*) FROM turns WHERE status = 'failed' AND created >= ?",
                   (now - HOUR,))
    return n <= s.max_failed_turns, f"{n} за последния час (праг {s.max_failed_turns})"


def check_spend(data: Path, s: Settings, now: float) -> tuple[bool, str]:
    rows = _query(data, "SELECT user_id, -SUM(amount) AS spent FROM ledger WHERE kind = 'charge' "
                        "AND created >= ? GROUP BY user_id ORDER BY spent DESC, user_id",
                  (now - HOUR,))
    total = sum(int(spent) for _, spent in rows)
    over = [(uid, int(spent)) for uid, spent in rows if int(spent) > s.max_user_spend]
    detail = (f"за последния час {_credits(total)} кредита общо (таван {_credits(s.max_total_spend)})"
              f", таван на човек {_credits(s.max_user_spend)}")
    if over:
        # Номерът, не имейлът — алармата може да мине през Telegram/Slack;
        # имейлът е в `python3 -m cloud.web.server users`.
        detail += "; над него: " + ", ".join(f"#{uid} {_credits(v)}" for uid, v in over)
    return not over and total <= s.max_total_spend, detail


def check_lockouts(data: Path, s: Settings, now: float) -> tuple[bool, str]:
    rows = _query(data, "SELECT kind, COUNT(*) FROM lockouts WHERE created >= ? GROUP BY kind "
                        "ORDER BY kind", (now - HOUR,))
    n = sum(int(c) for _, c in rows)
    parts = ", ".join(f"{kind} {c}" for kind, c in rows)
    return n <= s.max_lockouts, (f"{n} за последния час{f' ({parts})' if parts else ''}"
                                 f" (праг {s.max_lockouts})")


def _docker_time(value: str) -> float:
    """'2026-10-12T10:00:00.123456789Z' (Docker, винаги UTC) → секунди."""
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


def check_stuck(s: Settings, now: float) -> tuple[bool, str]:
    r = launch._docker("ps", "-aq", "--filter", f"name=^{launch.TASK_PREFIX}")
    if r.returncode:
        raise check.CheckFailed(f"docker ps: {(r.stderr or '').strip()[-200:]}")
    limit = s.task_seconds + STUCK_MARGIN
    stuck = []
    ids = r.stdout.split()
    for cid in ids:
        r = launch._docker("inspect", "-f", "{{.Name}} {{.Created}}", cid)
        if r.returncode:     # махнат между ps и inspect — нормален край на задача
            continue
        name, created = r.stdout.split()
        age = now - _docker_time(created)
        if age > limit:
            stuck.append(f"{name.lstrip('/')} {age / 60:.0f} мин")
    detail = f"{len(ids)} контейнера на задачи, таван {limit // 60} мин"
    if stuck:
        detail += "; по-стари: " + ", ".join(stuck) + " (docker rm -f <име>)"
    return not stuck, detail


def check_backup(dest: Path, s: Settings, now: float) -> tuple[bool, str]:
    made = backup.latest(dest)
    if made is None:
        return False, f"няма копие в {dest} (systemctl start genesis-backup.service)"
    hours = (now - made) / HOUR
    return hours <= s.max_backup_hours, (f"последното е отпреди {hours:.1f} ч"
                                         f" (праг {s.max_backup_hours:g} ч)")


def _helper() -> tuple[bool, str]:
    mode, url = check.helper()
    return True, f"режим {mode}, {launch.helper_container(url)} е Up"


def _web(web: str) -> tuple[bool, str]:
    check.web_health(web)
    return True, f"{web}/healthz → 200"


def _safe(name: str, fn: Callable[[], tuple[bool, str]]) -> Finding:
    """Всяка проверка е отделна: паднала проверка е „проблем“ с причината,
    а не край на наблюдението (иначе паднал Docker скрива и пълния диск)."""
    try:
        ok, detail = fn()
    except check.CheckFailed as e:
        return Finding(name, False, str(e))
    except Exception as e:   # каквото и да е, то е проблемът
        return Finding(name, False, f"{type(e).__name__}: {e}"[:500])
    return Finding(name, ok, detail)


def run_checks(s: Settings, *, data: Path, jobs: Path, backup_dir: Path, web: str | None,
               now: float) -> list[Finding]:
    plan: list[tuple[str, Callable[[], tuple[bool, str]]]] = []
    if web:
        plan.append(("web", lambda: _web(web)))
    plan += [
        ("helper", _helper),
        ("disk", lambda: check_disk([jobs, data], s)),
        ("turns", lambda: check_turns(data, s, now)),
        ("spend", lambda: check_spend(data, s, now)),
        ("lockouts", lambda: check_lockouts(data, s, now)),
        ("stuck", lambda: check_stuck(s, now)),
        ("backup", lambda: check_backup(backup_dir, s, now)),
    ]
    return [_safe(name, fn) for name, fn in plan]


# ── състояние и съобщение ───────────────────────────────────────────────────

def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """{име: {"ok", "since", "detail"}} от предишното пускане; счупен файл = няма."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, dict):
        return {}
    return {k: v for k, v in checks.items() if isinstance(v, dict)}


def _was_ok(prev: dict[str, dict[str, Any]], name: str) -> bool:
    return prev.get(name, {}).get("ok") is not False


def changes(prev: dict[str, dict[str, Any]], findings: list[Finding]) -> list[Finding]:
    """Проверките, чието „наред/проблем“ е друго от предишното пускане."""
    return [f for f in findings if f.ok != _was_ok(prev, f.name)]


def new_state(prev: dict[str, dict[str, Any]], findings: list[Finding],
              now: float) -> dict[str, Any]:
    checks = {}
    for f in findings:
        since = prev.get(f.name, {}).get("since")
        if f.ok != _was_ok(prev, f.name) or not isinstance(since, (int, float)):
            since = now
        checks[f.name] = {"ok": f.ok, "since": since, "detail": f.detail}
    return {"checks": checks, "updated": now}


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    return f"{minutes} мин" if minutes < 120 else f"{minutes / 60:.1f} ч"


def message(s: Settings, changed: list[Finding], findings: list[Finding],
            prev: dict[str, dict[str, Any]], now: float) -> tuple[str, str]:
    """(тема, текст) за едно пускане: всички промени в едно съобщение."""
    host = s.name or socket.gethostname()
    bad = [f for f in changed if not f.ok]
    good = [f for f in changed if f.ok]
    if bad:
        subject = f"[Genesis {host}] ПРОБЛЕМ: " + ", ".join(LABELS[f.name] for f in bad)
    else:
        subject = f"[Genesis {host}] наред: " + ", ".join(LABELS[f.name] for f in good)
    lines = [f"✗ {LABELS[f.name]}: {f.detail}" for f in bad]
    for f in good:
        since = prev.get(f.name, {}).get("since")
        after = f" след {_duration(now - since)}" if isinstance(since, (int, float)) else ""
        lines.append(f"✓ {LABELS[f.name]}: отново наред{after} — {f.detail}")
    still = [LABELS[f.name] for f in findings if not f.ok and f not in changed]
    if still:
        lines.append("Още с проблем: " + ", ".join(still))
    when = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["", f"{when}, {host}. Всичко: sudo python3 -m cloud.monitor --once --dry-run"]
    return subject, "\n".join(lines)


# ── пращане ─────────────────────────────────────────────────────────────────

def _redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def send_mail(s: Settings, subject: str, body: str, *, smtp: Callable[..., Any] = smtplib.SMTP,
              smtp_ssl: Callable[..., Any] = smtplib.SMTP_SSL) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_from
    msg["To"] = ", ".join(s.smtp_to)
    msg["Date"] = formatdate()
    msg["Message-ID"] = make_msgid(domain=s.smtp_from.rpartition("@")[2] or None)
    msg.set_content(body)
    context = ssl.create_default_context()
    if s.smtp_tls == "ssl":
        conn = smtp_ssl(s.smtp_host, s.smtp_port, timeout=20, context=context)
    else:
        conn = smtp(s.smtp_host, s.smtp_port, timeout=20)
    with conn:
        if s.smtp_tls == "starttls":
            conn.starttls(context=context)
        if s.smtp_user:
            conn.login(s.smtp_user, s.smtp_password)
        conn.send_message(msg)


def webhook_payload(s: Settings, text: str) -> dict[str, Any]:
    if s.webhook_format == "discord":
        return {"content": text[:1900]}           # таванът на Discord е 2000 знака
    if s.webhook_format == "telegram":
        return {"chat_id": s.telegram_chat_id, "text": text[:4000]}   # 4096 в Telegram
    return {"text": text}                         # Slack, Mattermost, общ JSON


def send_webhook(s: Settings, subject: str, body: str, *,
                 urlopen: Callable[..., Any] = urllib.request.urlopen) -> None:
    data = json.dumps(webhook_payload(s, f"{subject}\n\n{body}"), ensure_ascii=False).encode()
    req = urllib.request.Request(s.webhook_url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": "genesis-monitor"})
    with urlopen(req, timeout=20) as resp:
        status = resp.status
    if not 200 <= status < 300:
        raise OSError(f"HTTP {status}")


def send(s: Settings, subject: str, body: str, *, smtp: Callable[..., Any] = smtplib.SMTP,
         smtp_ssl: Callable[..., Any] = smtplib.SMTP_SSL,
         urlopen: Callable[..., Any] = urllib.request.urlopen) -> list[str]:
    """Праща по всеки настроен канал. Връща грешките — без тайни в тях."""
    errors = []
    if s.smtp_host:
        try:
            send_mail(s, subject, body, smtp=smtp, smtp_ssl=smtp_ssl)
        except (OSError, ValueError) as e:     # smtplib.SMTPException е OSError
            errors.append(f"имейл: {type(e).__name__}: {_redact(str(e), s.secrets())}")
    if s.webhook_url:
        try:
            send_webhook(s, subject, body, urlopen=urlopen)
        except (OSError, ValueError) as e:     # HTTPError/URLError са OSError
            errors.append(f"webhook: {type(e).__name__}: {_redact(str(e), s.secrets())}")
    return errors


# ── едно пускане ────────────────────────────────────────────────────────────

def run_once(s: Settings, *, data: Path, jobs: Path, backup_dir: Path, web: str | None,
             state_path: Path, now: float | None = None, dry_run: bool = False,
             sender: Callable[[Settings, str, str], list[str]] = send,
             out: Callable[[str], None] = print) -> int:
    now = time.time() if now is None else now
    findings = run_checks(s, data=data, jobs=jobs, backup_dir=backup_dir, web=web, now=now)
    for f in findings:
        out(f"{'✓' if f.ok else '✗'} {LABELS[f.name]}: {f.detail}")
    prev = load_state(state_path)
    changed = changes(prev, findings)
    channels = s.channels()
    if not changed:
        out("няма промяна — нищо за пращане")
    elif dry_run:
        subject, body = message(s, changed, findings, prev, now)
        out(f"\nби пратил по: {'; '.join(channels) or 'няма канал — само в журнала'}\n"
            f"{subject}\n{body}")
    elif not channels:
        subject, body = message(s, changed, findings, prev, now)
        out(f"\nняма канал за аларми (monitor.env) — само в журнала:\n{subject}\n{body}")
    else:
        subject, body = message(s, changed, findings, prev, now)
        errors = sender(s, subject, body)
        if errors:
            # Състоянието остава старото: следващото пускане праща пак.
            for e in errors:
                print(f"ГРЕШКА: алармата не стигна: {e}", file=sys.stderr)
            return 1
        out(f"пратено по: {'; '.join(channels)} — {subject}")
    if not dry_run:
        save_state(state_path, new_state(prev, findings, now))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Наблюдение на Genesis онлайн и аларми при промяна.")
    p.add_argument("--once", action="store_true", help="една проверка и край (така го пуска таймерът)")
    p.add_argument("--dry-run", action="store_true",
                   help="показва проверките и какво би пратил; не праща и не пише състояние")
    p.add_argument("--interval", type=float, default=300, help="секунди между проверките без --once")
    p.add_argument("--config", type=Path, default=Path("/srv/genesis/monitor.env"))
    p.add_argument("--data", type=Path, default=Path("/srv/genesis/web"))
    p.add_argument("--jobs", type=Path, default=Path("/srv/jobs"))
    p.add_argument("--backup", type=Path, default=Path("/srv/backup/genesis"))
    p.add_argument("--web", default="http://127.0.0.1:8080", help="'' — без /healthz")
    p.add_argument("--state", type=Path, help="по подразбиране <data>/monitor-state.json")
    a = p.parse_args(argv)
    try:
        env = read_keys(a.config) if a.config.exists() else {}
        s = load_settings(env)
    except (OSError, ValueError) as e:
        print(f"ГРЕШКА в {a.config}: {e}", file=sys.stderr)
        return 2
    if not env:
        print(f"внимание: няма {a.config} — алармите са само в журнала "
              "(виж cloud/deploy/monitor.env.example)", file=sys.stderr)
    state = a.state or a.data / "monitor-state.json"
    while True:
        code = run_once(s, data=a.data, jobs=a.jobs, backup_dir=a.backup, web=a.web or None,
                        state_path=state, dry_run=a.dry_run)
        if a.once:
            return code
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
