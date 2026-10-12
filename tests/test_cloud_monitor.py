"""cloud/monitor.py — наблюдение и аларми само при смяна (2026-10-12).

Фалшиви са всички: Docker (tests/conftest.py), /healthz, SMTP, webhook и
часовникът (`now`). Нищо не стига до мрежата и до истинския /srv."""
from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from typing import Any, ClassVar

import pytest

from cloud import check, monitor
from cloud.web import server as web
from cloud.web.store import MICRO, Store

TOKEN = "123456:SECRET-bot-token-abc"
TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
HELPER_OK = ("inspect", "-f", "{{.State.Running}} {{.State.Restarting}}")
# Праговете на диска са 0 — за да не зависи от машината; webhook-ът е фалшивият sender.
QUIET = monitor.Settings(min_free_gb=0, name="test", webhook_url="https://hooks.example.com/t")


class Env:
    def __init__(self, tmp_path: Path, fake_docker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.data = tmp_path / "web"
        self.store = Store(self.data / "web.db")
        self.jobs = tmp_path / "jobs"
        self.jobs.mkdir()
        self.backup = tmp_path / "bk"
        self.now = time.time()
        (self.backup / time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now))).mkdir(parents=True)
        self.state = self.data / "monitor-state.json"
        self.docker = fake_docker
        fake_docker.answers[HELPER_OK] = (0, "true false", "")
        self.web_down = False

        def health(url: str, timeout: float = 10) -> None:
            if self.web_down:
                raise check.CheckFailed(f"уеб сървърът {url}: Connection refused")

        monkeypatch.setattr(check, "web_health", health)
        self.sent: list[tuple[str, str]] = []
        self.out: list[str] = []

    def sender(self, s: monitor.Settings, subject: str, body: str) -> list[str]:
        self.sent.append((subject, body))
        return []

    def run(self, s: monitor.Settings = QUIET, *, after: float = 0, **kw: Any) -> int:
        kw.setdefault("sender", self.sender)
        return monitor.run_once(s, data=self.data, jobs=self.jobs, backup_dir=self.backup,
                                web="http://127.0.0.1:8080", state_path=self.state,
                                now=self.now + after, out=self.out.append, **kw)

    def findings(self, s: monitor.Settings = QUIET, after: float = 0) -> dict[str, monitor.Finding]:
        found = monitor.run_checks(s, data=self.data, jobs=self.jobs, backup_dir=self.backup,
                                   web="http://127.0.0.1:8080", now=self.now + after)
        return {f.name: f for f in found}


@pytest.fixture
def env(tmp_path, fake_docker, monkeypatch) -> Env:
    return Env(tmp_path, fake_docker, monkeypatch)


def test_a_healthy_machine_is_quiet(env) -> None:
    assert env.run() == 0
    assert env.sent == []
    state = json.loads(env.state.read_text(encoding="utf-8"))["checks"]
    assert set(state) == set(monitor.LABELS) and all(v["ok"] for v in state.values()), state


def test_an_alert_only_on_change_and_one_when_it_is_fixed(env) -> None:
    """Аларма при наред → проблем, тишина докато трае, „наред“ при оправяне."""
    env.run()
    env.docker.answers[HELPER_OK] = (0, "true true", "")        # шлюзът се рестартира
    env.web_down = True
    env.run(after=300)
    (subject, body), = env.sent
    assert "ПРОБЛЕМ" in subject and "шлюзът/проксито" in subject and "уеб сървърът" in subject
    assert "genesis-gateway не е Up" in body and "Connection refused" in body
    env.run(after=600)
    env.run(after=900)
    assert len(env.sent) == 1                                   # не на всеки 5 минути
    env.docker.answers[HELPER_OK] = (0, "true false", "")
    env.run(after=1500)
    subject, body = env.sent[-1]
    assert "наред: шлюзът/проксито" in subject
    assert "отново наред след 20 мин" in body and "Още с проблем: уеб сървърът" in body
    env.web_down = False
    env.run(after=1800)
    assert len(env.sent) == 3 and "наред: уеб сървърът" in env.sent[-1][0]


def test_a_failed_delivery_is_retried_and_the_token_never_printed(env, capsys) -> None:
    s = monitor.Settings(min_free_gb=0, name="test", webhook_url=TELEGRAM,
                         webhook_format="telegram", telegram_chat_id="42")
    env.run(s)
    env.web_down = True
    calls: list[Any] = []

    def down(req: Any, timeout: float = 0) -> Any:
        calls.append(req)
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    code = env.run(s, after=300, sender=lambda *a: monitor.send(*a, urlopen=down))
    err = capsys.readouterr().err
    assert code == 1 and len(calls) == 1
    assert "алармата не стигна" in err and TOKEN not in err and "***" in err
    assert all(TOKEN not in line for line in env.out)
    # Състоянието не е записано: следващото пускане праща същата аларма.
    env.run(s, after=600)
    assert len(env.sent) == 1 and "ПРОБЛЕМ: уеб сървърът" in env.sent[0][0]


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        return None


@pytest.mark.parametrize("fmt, chat, expected", [
    ("text", "", {"text"}),                 # Slack, Mattermost
    ("discord", "", {"content"}),
    ("telegram", "42", {"chat_id", "text"}),
])
def test_webhook_formats(fmt, chat, expected) -> None:
    got: list[Any] = []

    def urlopen(req: Any, timeout: float = 0) -> FakeResponse:
        got.append(req)
        return FakeResponse(204 if fmt == "discord" else 200)

    s = monitor.Settings(webhook_url="https://hooks.example.com/x/y", webhook_format=fmt,
                         telegram_chat_id=chat)
    assert monitor.send(s, "[Genesis] ПРОБЛЕМ: диск", "✗ диск: 1 GB", urlopen=urlopen) == []
    (req,) = got
    payload = json.loads(req.data)
    assert req.get_method() == "POST" and set(payload) == expected
    assert "✗ диск" in (payload.get("text") or payload["content"])
    if chat:
        assert payload["chat_id"] == "42"


def test_a_webhook_that_answers_an_error_is_a_failed_delivery() -> None:
    s = monitor.Settings(webhook_url="https://hooks.example.com/x")
    errors = monitor.send(s, "t", "b", urlopen=lambda req, timeout=0: FakeResponse(500))
    assert errors and "HTTP 500" in errors[0]


class FakeSMTP:
    made: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None) -> None:
        self.host, self.port, self.context = host, port, context
        self.steps: list[str] = []
        self.messages: list[Any] = []
        FakeSMTP.made.append(self)

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        self.steps.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.steps.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.steps.append(f"login {user}")
        if password != "pw-secret-1":
            raise OSError(f"535 bad password {password}")

    def send_message(self, msg: Any) -> None:
        self.steps.append("send")
        self.messages.append(msg)


def _mail(**kw: Any) -> monitor.Settings:
    base = {"smtp_host": "smtp.example.com", "smtp_user": "alerts@example.com",
            "smtp_password": "pw-secret-1", "smtp_from": "alerts@example.com",
            "smtp_to": ("op@example.com", "two@example.com")}
    return monitor.Settings(**{**base, **kw})


def test_email_goes_over_starttls_with_login() -> None:
    FakeSMTP.made.clear()
    errors = monitor.send(_mail(), "[Genesis] ПРОБЛЕМ: диск", "✗ диск: 1 GB", smtp=FakeSMTP,
                          smtp_ssl=None)
    assert errors == []
    (conn,) = FakeSMTP.made
    assert (conn.host, conn.port) == ("smtp.example.com", 587)
    assert conn.steps == ["starttls", "login alerts@example.com", "send", "quit"]
    (msg,) = conn.messages
    assert msg["To"] == "op@example.com, two@example.com" and "ПРОБЛЕМ" in msg["Subject"]
    assert "✗ диск" in msg.get_content() and "pw-secret-1" not in msg.as_string()


def test_email_over_ssl_and_a_wrong_password_is_not_printed() -> None:
    FakeSMTP.made.clear()
    errors = monitor.send(_mail(smtp_tls="ssl", smtp_port=465), "t", "b", smtp=None,
                          smtp_ssl=FakeSMTP)
    assert errors == [] and FakeSMTP.made[0].context is not None
    assert "starttls" not in FakeSMTP.made[0].steps
    errors = monitor.send(_mail(smtp_password="other-secret"), "t", "b", smtp=FakeSMTP,
                          smtp_ssl=None)
    assert errors and "other-secret" not in errors[0] and "***" in errors[0]


def test_spend_failed_turns_and_lockouts_over_the_threshold(env) -> None:
    store = env.store
    ana = store.add_user("ana@example.com", "correct-horse-battery")
    bob = store.add_user("bob@example.com", "correct-horse-battery")
    s = monitor.Settings(min_free_gb=0, max_user_spend=100 * MICRO, max_total_spend=500 * MICRO,
                         max_failed_turns=2, max_lockouts=1)
    found = env.findings(s)
    assert found["spend"].ok and found["turns"].ok and found["lockouts"].ok
    store.add_entry(ana, "turn:1", "charge", -150 * MICRO)
    store.add_entry(bob, "turn:2", "charge", -50 * MICRO)
    store.add_entry(bob, "stripe:cs_1", "purchase", 900 * MICRO)  # покупка не е разход
    spend = env.findings(s)["spend"]
    assert not spend.ok and f"#{ana} 150.00" in spend.detail and f"#{bob} " not in spend.detail
    assert "ana@example.com" not in spend.detail                  # номер, не имейл
    # Час по-късно разходът е стар — пак наред.
    assert env.findings(s, after=3601)["spend"].ok
    # Общо над тавана, никой поотделно.
    many = monitor.Settings(min_free_gb=0, max_user_spend=1000 * MICRO, max_total_spend=100 * MICRO)
    assert not env.findings(many)["spend"].ok
    chat = store.new_chat(ana, "t")
    for _ in range(3):
        store.finish_turn(store.add_turn(chat, "x"), ok=False, error="e", tokens={}, seconds=1)
    turns = env.findings(s)["turns"]
    assert not turns.ok and turns.detail.startswith("3 за последния час")
    store.add_lockout("email")
    store.add_lockout("ip")
    lockouts = env.findings(s)["lockouts"]
    assert not lockouts.ok and "email 1, ip 1" in lockouts.detail


def test_the_server_records_each_lockout_once_per_window(tmp_path) -> None:
    """Броячът на входа е в паметта на сървъра — без реда в web.db
    наблюдението не вижда налучкване на пароли. Ред на ключ, не на опит."""
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", "correct-horse-battery")
    app = web.App(store, tmp_path / "jobs", runner=lambda *a, **k: None, recover=False)
    for _ in range(web.LOGIN_FAILURES):
        assert app.login("ana@example.com", "wrong-password-1", "203.0.113.5") is None
    for _ in range(20):
        with pytest.raises(web.LoginBlocked):
            app.login("ana@example.com", "wrong-password-1", "203.0.113.5")
    for _ in range(web.LOGIN_FAILURES + 3):
        try:
            app.signup("bad-code", "x@example.com", "correct-horse-battery", "198.51.100.7")
        except (ValueError, web.LoginBlocked):
            pass
    found = monitor.check_lockouts(tmp_path, monitor.Settings(max_lockouts=10), time.time())
    assert found == (True, "3 за последния час (email 1, ip 1, signup 1) (праг 10)")


def test_a_task_container_older_than_the_limit_is_stuck(env) -> None:
    created = env.now - 3600
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + ".123456789Z"
    env.docker.answers.update({
        ("ps",): (0, "abc123\ngone456\n", ""),
        ("inspect", "-f", "{{.Name}} {{.Created}}", "abc123"): (0, f"/genesis-task-x {stamp}", ""),
        ("inspect", "-f", "{{.Name}} {{.Created}}", "gone456"): (1, "", "No such object"),
    })
    s = monitor.Settings(min_free_gb=0, task_seconds=600)
    stuck = env.findings(s)["stuck"]
    assert not stuck.ok and "genesis-task-x 60 мин" in stuck.detail
    assert env.findings(monitor.Settings(min_free_gb=0, task_seconds=3600))["stuck"].ok


def test_a_broken_check_is_a_problem_and_the_rest_still_run(env) -> None:
    """Паднал Docker не скрива пълния диск: всяка проверка е отделна."""
    env.docker.answers[("ps",)] = (1, "", "Cannot connect to the Docker daemon")
    env.docker.answers[HELPER_OK] = (1, "", "Cannot connect to the Docker daemon")
    found = env.findings(monitor.Settings(min_free_gb=10 ** 9))
    assert not found["stuck"].ok and "Cannot connect" in found["stuck"].detail
    assert not found["helper"].ok and not found["disk"].ok and "праг" in found["disk"].detail
    assert found["turns"].ok and found["backup"].ok


def test_backup_age(env, tmp_path) -> None:
    s = monitor.Settings(min_free_gb=0, max_backup_hours=13)
    assert env.findings(s)["backup"].ok
    old = env.findings(s, after=14 * 3600)["backup"]
    assert not old.ok and "отпреди 14.0 ч" in old.detail
    env.backup = tmp_path / "empty"
    none = env.findings(s)["backup"]
    assert not none.ok and "genesis-backup.service" in none.detail


def test_a_missing_database_is_a_problem_not_a_crash(env, tmp_path) -> None:
    env.data = tmp_path / "nothing"
    found = env.findings()
    assert not found["turns"].ok and not found["spend"].ok and found["helper"].ok
    assert found["turns"].detail.startswith(str(env.data / "web.db"))


def test_a_broken_state_file_counts_as_all_ok(env) -> None:
    env.data.joinpath("monitor-state.json").write_text("{не е json", encoding="utf-8")
    env.web_down = True
    assert env.run() == 0 and len(env.sent) == 1
    assert json.loads(env.state.read_text(encoding="utf-8"))["checks"]["web"]["ok"] is False


def _main_args(env: Env, config: Path, *extra: str) -> list[str]:
    return ["--once", "--config", str(config), "--data", str(env.data), "--jobs", str(env.jobs),
            "--backup", str(env.backup), "--web", "", *extra]


def test_dry_run_shows_what_it_would_send_and_changes_nothing(env, tmp_path, capsys,
                                                              monkeypatch) -> None:
    config = tmp_path / "monitor.env"
    config.write_text(f"MONITOR_WEBHOOK_URL={TELEGRAM}\nMONITOR_WEBHOOK_FORMAT=telegram\n"
                      "MONITOR_TELEGRAM_CHAT_ID=42\nMONITOR_MIN_FREE_GB=0\nMONITOR_NAME=prod\n",
                      encoding="utf-8")

    def no_network(*a: Any, **k: Any) -> Any:
        raise AssertionError("сухото пускане не праща")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    env.docker.answers[HELPER_OK] = (1, "", "No such object")
    assert monitor.main(_main_args(env, config, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "би пратил по: webhook telegram към api.telegram.org" in out
    assert "[Genesis prod] ПРОБЛЕМ: шлюзът/проксито" in out and "up.sh" in out
    assert TOKEN not in out and not env.state.exists()


def test_without_a_config_alerts_go_to_the_journal(env, tmp_path, capsys) -> None:
    env.docker.answers[HELPER_OK] = (1, "", "No such object")
    assert monitor.main(_main_args(env, tmp_path / "none.env")) == 0
    got = capsys.readouterr()
    assert "няма" in got.err and "monitor.env.example" in got.err
    assert "само в журнала" in got.out and "ПРОБЛЕМ" in got.out
    assert json.loads(env.state.read_text(encoding="utf-8"))["checks"]["helper"]["ok"] is False


@pytest.mark.parametrize("line, words", [
    ("MONITOR_MAX_FAILED_TURNS=много", "MONITOR_MAX_FAILED_TURNS"),
    ("MONITOR_MIN_FREE_GB=nan", "MONITOR_MIN_FREE_GB"),
    ("MONITOR_MAX_USER_CREDITS_HOUR=-1", "MONITOR_MAX_USER_CREDITS_HOUR"),
    ("MONITOR_WEBHOOK_URL=ftp://x", "https"),
    ("MONITOR_WEBHOOK_URL=https://x\nMONITOR_WEBHOOK_FORMAT=telegram", "CHAT_ID"),
    ("MONITOR_SMTP_HOST=smtp.example.com", "MONITOR_SMTP_TO"),
    ("MONITOR_SMTP_TLS=maybe", "starttls"),
])
def test_a_bad_config_is_refused_with_the_key(env, tmp_path, capsys, line, words) -> None:
    config = tmp_path / "monitor.env"
    config.write_text(line + "\n", encoding="utf-8")
    assert monitor.main(_main_args(env, config)) == 2
    assert words in capsys.readouterr().err


def test_the_example_config_is_valid() -> None:
    """Всички редове в примера, разкоментирани, минават load_settings —
    иначе операторът копира пример, който спира деплоя."""
    from cloud.gateway.gateway import read_keys
    example = Path(__file__).resolve().parent.parent / "cloud" / "deploy" / "monitor.env.example"
    lines = [ln[1:] for ln in example.read_text(encoding="utf-8").splitlines()
             if ln.startswith("#MONITOR_")]
    keys: dict[str, str] = {}
    for ln in lines:
        k, v = ln.split("=", 1)
        if v:
            keys[k] = v
    keys["MONITOR_SMTP_PASSWORD"] = "x" * 8      # в примера е празна
    s = monitor.load_settings(keys)
    assert s.smtp_to == ("operator@example.com", "second@example.com")
    mail = "имейл до operator@example.com, second@example.com през smtp.example.com:587 (starttls)"
    assert s.channels() == [mail, "webhook text към hooks.slack.com"]
    defaults = monitor.Settings()
    assert (s.min_free_gb, s.max_failed_turns, s.max_user_spend, s.max_total_spend,
            s.max_lockouts, s.task_seconds, s.max_backup_hours) == (
        defaults.min_free_gb, defaults.max_failed_turns, defaults.max_user_spend,
        defaults.max_total_spend, defaults.max_lockouts, defaults.task_seconds,
        defaults.max_backup_hours)
    assert read_keys(example) == {}         # всичко в примера е коментар
