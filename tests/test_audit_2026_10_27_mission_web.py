"""Одит 2026-10-27 (пети кръг): мисиите (orchestrator TDD), уеб проверките
(web_check, page_check), приемните тестове, маскирането в notifier и
наблюдението (cloud/monitor). Всеки тест падаше срещу кода преди поправката
(възпроизведено със скриптовете на одита, audit5c/)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import genesis_agent.orchestrator as orch_mod
from cloud import monitor
from genesis_agent import acceptance, page_check
from genesis_agent.brain import Brain as _RealBrain
from genesis_agent.notifier import redact
from genesis_agent.web_check import check_html, is_site_page

# ── 1. orchestrator: sys.exit(0) в self-test-а прескачаше тестовете на Tester-а ──


class _FakeBrain:
    trim_round_history = staticmethod(_RealBrain.trim_round_history)
    replies: ClassVar[list[SimpleNamespace]] = []

    def __init__(self, **kw) -> None:
        self._replies = list(self.replies)

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "sys"

    def complete(self, messages, tools=None):
        return self._replies.pop(0)


def _reply(raw_text: str = "", code: str = "") -> SimpleNamespace:
    return SimpleNamespace(raw_text=raw_text, code=code, tool_calls=None)


_FIB_SELF_TEST = """import sys


def fib(n):
    {body}


if __name__ == "__main__":
    assert fib(1) == 1
    print("OK")
    sys.exit(0)
"""


def _run_tdd(monkeypatch, tmp_path, code: str) -> tuple[orch_mod.OrchestratedOutcome, list]:
    from genesis_agent.executor import run_python_subprocess as real_run
    monkeypatch.setattr(orch_mod.dna, "validate_goal_ethics", lambda goal: None)
    monkeypatch.setattr(orch_mod.dna, "assert_operator_if_strict", lambda operator_id: None)
    monkeypatch.setattr(orch_mod, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(orch_mod, "run_python_subprocess", real_run)
    monkeypatch.setattr(orch_mod, "verify_skill",
                        lambda code: SimpleNamespace(verified=True, method="self_test_passed", detail=""))
    saved: list = []
    monkeypatch.setattr(orch_mod, "save_skill", lambda **kw: saved.append(kw) or tmp_path / "x.md")
    monkeypatch.setattr(orch_mod, "_write_tests_first",
                        lambda brain, goal: "assert fib(10) == 55\nassert fib(7) == 13")
    _FakeBrain.replies = [_reply(raw_text="plan"), _reply(code=code), _reply(raw_text="reviewer")]
    monkeypatch.setattr(orch_mod, "Brain", _FakeBrain)
    return orch_mod.run_orchestrated("fibonacci", tdd=True, max_rounds=1), saved


def test_tdd_tests_run_even_when_the_self_test_exits_the_process(monkeypatch, tmp_path) -> None:
    """`fib(n) = n` с `sys.exit(0)` след собствения assert: Tester-тестовете бяха
    залепени СЛЕД кода, процесът излизаше с 0 преди тях и умението се записваше."""
    out, saved = _run_tdd(monkeypatch, tmp_path, _FIB_SELF_TEST.format(body="return n"))
    assert out.success is False
    assert saved == []


def test_a_correct_skill_with_a_self_test_that_exits_still_passes_tdd(monkeypatch, tmp_path) -> None:
    body = "a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a"
    out, saved = _run_tdd(monkeypatch, tmp_path, _FIB_SELF_TEST.format(body=body))
    assert out.success is True
    assert len(saved) == 1


# ── 2. web_check.is_site_page: „test“ в testimonials, „data“ в database ──

_STYLED = '<!doctype html><html><head><link rel="stylesheet" href="s.css"></head><body>x</body></html>'


@pytest.mark.parametrize("name", ["testimonials", "contest", "latest", "examples", "mockups",
                                  "datasets", "database", "page2"])
def test_a_site_page_whose_name_merely_contains_a_data_word_is_a_site_page(name) -> None:
    assert is_site_page(Path(f"{name}.html"), _STYLED)


@pytest.mark.parametrize("name", ["sample", "test_data", "fixture-1", "page_2", "mock"])
def test_a_data_page_by_whole_word_stays_data(name) -> None:
    assert not is_site_page(Path(f"{name}.html"), _STYLED)


# ── 3. acceptance: .venv на проекта без pytest ──


class _Reply:
    def __init__(self, raw: str) -> None:
        self.raw_text, self.code = raw, ""


_GOOD = "```python\nfrom calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n```"


def test_a_project_venv_without_pytest_is_not_a_failing_acceptance_run(tmp_path) -> None:
    """`python -m pytest` в .venv без pytest връщаше rc 1 „No module named pytest“
    и това стигаше до модела като „✗ падат“."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(ws / ".venv")],
                   check=True, capture_output=True, timeout=120)
    c = acceptance.AcceptanceCheck("Направи calc.py с add(a, b)", ws,
                                   complete=lambda m: _Reply(_GOOD), on=True)
    c.observe(f"[WRITE_FILE: {ws / 'calc.py'}] ✓ записани 30 символа")
    note, line = c.check()
    assert "✗" not in line and "No module named pytest" not in note
    assert c.passed is not False
    assert "✓" in line or "няма pytest" in line


# ── 4. notifier.redact ──


@pytest.mark.parametrize("text", [
    "scp -P 2222 secret.txt deploy:hunter2@127.0.0.1:/tmp/",
    "mysql -u root --password hunter2 db",
    "mysqldump -u root --password=hunter2 db",
    "mysql -u root -p hunter2 db",
    "curl -u admin:hunter2 https://host/api",
])
def test_passwords_in_flags_and_bare_user_pass_at_host_are_masked(text) -> None:
    out = redact(text)
    assert "hunter2" not in out, out
    assert "***" in out


def test_the_host_after_a_bare_user_pass_stays_visible() -> None:
    assert "127.0.0.1" in redact("scp secret.txt deploy:hunter2@127.0.0.1:/tmp/")


@pytest.mark.parametrize("text", [
    "намери най-късия path: Dijkstra върху графа",
    "regex pattern: ^\\d{4}$",
    "bypass: не; compass: север; passage: 3",
])
def test_ordinary_words_containing_pat_or_pass_are_not_masked(text) -> None:
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "export OPENAI_API_KEY=sk-proj-abcdefghijklmnop",
    "PGPASSWORD=hunter2 psql",
    "DSN: postgres://u:hunter2@h/db",
    "GH_PAT=hunter2 gh auth",
    "MYSQL_PWD=hunter2 mysql",
    "api_key: hunter2",
    "git push https://me:hunter2@github.com/x",
])
def test_real_secrets_are_still_masked(text) -> None:
    out = redact(text)
    assert "hunter2" not in out and "abcdefghijklmnop" not in out, out


# ── 5. cloud/monitor: пълен диск → save_state хвърля ──


def test_a_state_file_that_cannot_be_written_is_a_code_1_not_a_traceback(tmp_path, monkeypatch,
                                                                       capsys) -> None:
    """ENOSPC — точно състоянието, за което проверката на диска алармира — убиваше
    пускането с traceback след вече извикания sender."""
    findings = [monitor.Finding("disk", False, "свободни 0.1 GB")]
    monkeypatch.setattr(monitor, "run_checks", lambda *a, **k: findings)

    def save_fail(path, state):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(monitor, "save_state", save_fail)
    sent: list[str] = []
    s = monitor.Settings(min_free_gb=1, name="srv", webhook_url="https://hooks.example.com/t")
    rc = monitor.run_once(s, data=tmp_path, jobs=tmp_path, backup_dir=tmp_path, web=None,
                          state_path=tmp_path / "state.json", now=1000.0,
                          sender=lambda *a: sent.append(a[1]) or [], out=lambda *_: None)
    assert rc == 1
    assert len(sent) == 1
    err = capsys.readouterr().err
    assert "не се записа" in err and "Traceback" not in err


# ── 6. web_check: фиксираният курс 1 € = 1.95583 лв. не е цена ──


def test_the_fixed_euro_rate_is_not_reported_as_a_price_in_leva(tmp_path) -> None:
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    head = ('<!doctype html><html lang="bg"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width"><title>T</title>'
            '<meta name="description" content="d"><link rel="stylesheet" href="styles.css"></head><body>')
    page = tmp_path / "index.html"
    content = head + "<h1>x</h1><p>Стая: 50 €. Курс: 1 € = 1.95583 лв.</p></body></html>"
    page.write_text(content, encoding="utf-8")
    assert not [f for f in check_html(page, content) if "лева" in f]
    content = head + "<h1>x</h1><p>Стая: 120 лв. Курс: 1 € = 1,95583 лв.</p></body></html>"
    page.write_text(content, encoding="utf-8")
    lev = [f for f in check_html(page, content) if "лева" in f]
    assert len(lev) == 1 and "1 цена" in lev[0] and "120 лв" in lev[0]


# ── 7. page_check._clean_env: MYSQL_PWD и webhook адресите ──


def test_passwords_and_webhooks_do_not_reach_the_browser_runner(monkeypatch) -> None:
    for name in ("MYSQL_PWD", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK", "MONITOR_WEBHOOK_URL"):
        monkeypatch.setenv(name, "sekret")
    monkeypatch.setenv("HOME", "/home/x")
    env = page_check._clean_env()
    assert not {"MYSQL_PWD", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK", "MONITOR_WEBHOOK_URL"} & set(env)
    assert "PATH" in env and "HOME" in env
