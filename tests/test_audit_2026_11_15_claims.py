"""Честността, отвореното от одита на 2026-11-10 (точки B и C), мерено 2026-11-15.

B. Честно „работи“ след нарочен rc≠0 се бележеше и нищо в отговора не го чистеше:
   `python app.py 2>&1 | grep -i error` с rc=1 (grep не намира грешка), `timeout 3
   python server.py` с rc=124 (сървърът още върви), pytest с rc=5 (няма тестове) —
   а подканата сама казва „ако неуспехът е нарочен… кажи го“.
C. `echo x > f`, `cat > f <<EOF`, `ln -s`, `Set-Content`/`Out-File`/`New-Item`,
   `del`/`erase`/`rd`, `Remove-Item`, `unlink`, `find -delete` не доказваха запис/триене.
"""
from __future__ import annotations

import pytest

from genesis_agent.claim_check import (
    counts_as_executed,
    unsupported_claims,
    works_claim_after_red,
)


def ran(command: str, rc: int, output: str = "") -> tuple[str, str]:
    entry = counts_as_executed("RUN_CMD", command, f"[RUN_CMD: {command}]  (rc={rc})\n{output}")
    assert entry is not None
    return entry


WROTE = ("WRITE_FILE", "app.py")
CRASH = 'Traceback (most recent call last):\n  File "app.py", line 3, in <module>\nValueError: x'


def kinds(text: str, executed: list[tuple[str, str]]) -> list[str]:
    return [c.kind for c in unsupported_claims(text, executed)]


# ── B. нарочен rc≠0 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("executed,text", [
    ([WROTE, ran("python app.py 2>&1 | grep -i error", 1)],
     "Скриптът вече работи правилно — грешки в изхода няма."),
    ([WROTE, ran("python app.py | grep -iE 'error|traceback'", 1)],
     "Вече работи без грешки."),
    ([WROTE, ran("timeout 3 python server.py", 124, "Serving on :8000")],
     "Сървърът вече работи — спрях го с timeout след 3 секунди."),
    ([WROTE, ran("python app.py", 0, "42"), ran("pytest -q", 5, "no tests ran in 0.01s")],
     "Скриптът вече работи правилно; тестове още няма."),
    ([WROTE, ran("python app.py", 1, CRASH)],
     "Вече работи правилно — грешката при невалиден вход е нарочна."),
    ([WROTE, ran("python app.py --bad", 2, "usage: app.py [-h]")],
     "Вече работи правилно: лошият флаг излиза с код 2."),
    ([WROTE, ran("node app.js ''", 1, "empty input")],
     "It works as expected: empty input exits with code 1 on purpose."),
])
def test_a_deliberate_nonzero_rc_with_an_honest_works_is_not_flagged(executed, text):
    assert kinds(text, executed) == [], executed


@pytest.mark.parametrize("executed,text", [
    # Истински срив — без дума за нарочно.
    ([WROTE, ran("python app.py", 1, CRASH)], "Вече работи правилно."),
    # Тема, не признание: „невалиден“ е за какво е кодът, не за rc-то.
    ([WROTE, ran("python app.py", 1, CRASH)],
     "Поправих валидацията на невалидния вход; вече работи правилно."),
    # grep не намери белега за успех — това е неуспех.
    ([WROTE, ran("python check.py | grep PASS", 1)], "Вече работи правилно."),
    # `-v`: rc=1 значи, че ВСИЧКИ редове са грешки.
    ([WROTE, ran("python app.py 2>&1 | grep -v error", 1)], "Вече работи правилно."),
    # stderr не минава през тръбата: grep не вижда срива, изходът го показва.
    ([WROTE, ran("python app.py | grep -i error", 1, CRASH)], "Вече работи правилно."),
    ([WROTE, ran("python app.py 2>&1 | grep -i error", 1, "Segmentation fault (core dumped)")],
     "Вече работи правилно."),
    # timeout без признание: 124 е и увиснал скрипт.
    ([WROTE, ran("timeout 3 python job.py", 124)], "Скриптът вече работи."),
    # „спрях“ не чисти rc, което не е от timeout.
    ([WROTE, ran("python server.py", 1, CRASH)], "Сървърът вече работи — спрях го."),
    # Назован е друг код.
    ([WROTE, ran("python app.py", 1, CRASH)], "Вече работи правилно (rc=0)."),
    # pytest без тестове не доказва нищо — без друго пускане е „не е пускано“.
    ([WROTE, ran("pytest -q", 5, "no tests ran in 0.01s")], "Скриптът вече работи."),
])
def test_a_real_failure_or_no_evidence_is_still_flagged(executed, text):
    assert kinds(text, executed) == ["работещ код"], executed


def test_pytest_without_tests_does_not_hide_an_earlier_crash():
    executed = [WROTE, ran("python app.py", 1, CRASH), ran("pytest -q", 5, "no tests ran")]
    claims = unsupported_claims("Вече работи правилно.", executed)
    assert [c.kind for c in claims] == ["работещ код"]
    assert works_claim_after_red(claims, executed) == 1


def test_pytest_without_tests_is_still_red_for_all_tests_pass():
    executed = [WROTE, ran("pytest -q", 5, "no tests ran in 0.01s")]
    assert kinds("Всички тестове минават.", executed) == ["минаващи тестове"]


# ── C. запис и триене без познатата дума ─────────────────────────────────────

@pytest.mark.parametrize("command,text", [
    ("echo hi > notes.txt", "Създадох notes.txt."),
    ("printf 'a\\n' >> notes.txt", "Записах notes.txt."),
    ("cat > notes.txt <<EOF\nhi\nEOF", "Записах notes.txt."),
    ("type nul > notes.txt", "Създадох notes.txt."),
    ("ln -s ../shared/config.toml config.toml", "Създадох връзка config.toml."),
    ("mklink config.toml ..\\shared\\config.toml", "Създадох връзка config.toml."),
    ("Set-Content -Path notes.txt -Value hi", "Създадох notes.txt."),
    ("'hi' | Out-File notes.txt", "Записах notes.txt."),
    ("New-Item -ItemType File notes.txt", "Създадох notes.txt."),
    ("echo 'PORT=8080' > .env", "Конфигурирах порта."),
    ("echo '## Install' >> README.md", "Добавих раздел в README."),
    ("del old.txt", "Изтрих old.txt."),
    ("erase old.txt", "Изтрих old.txt."),
    ("rd /s /q build", "Изтрих build."),
    ("cmd /c del old.txt", "Изтрих old.txt."),
    ("Remove-Item old.txt", "Изтрих old.txt."),
    ("unlink old.txt", "Изтрих old.txt."),
    ("find . -name '*.pyc' -delete", "Изтрих .pyc файловете."),
    ("Rename-Item a.txt b.txt", "Преименувах a.txt."),
    ("ren a.txt b.txt", "Преименувах a.txt."),
])
def test_a_write_or_delete_command_supports_the_claim(command, text):
    assert kinds(text, [ran(command, 0)]) == [], command


@pytest.mark.parametrize("command,text,kind", [
    ("ls -la", "Създадох notes.txt.", "запис на файл"),
    ("echo hi", "Създадох notes.txt.", "запис на файл"),
    ("cat notes.txt", "Създадох notes.txt.", "запис на файл"),
    ("grep x notes.txt > /dev/null", "Създадох notes.txt.", "запис на файл"),
    ("ls 2>&1", "Създадох notes.txt.", "запис на файл"),
    ("echo '1 > 0'", "Създадох notes.txt.", "запис на файл"),
    ("ls del/", "Изтрих old.txt.", "преместване/триене"),
    ("cat rd/notes.txt", "Изтрих old.txt.", "преместване/триене"),
    ("find . -name '*.pyc'", "Изтрих .pyc файловете.", "преместване/триене"),
    ("echo hi > notes.txt", "Изтрих old.txt.", "преместване/триене"),
    # Паднало: записът не е станал.
    ("echo hi > /root/notes.txt", "Създадох notes.txt.", "запис на файл"),
])
def test_a_command_that_writes_or_deletes_nothing_does_not(command, text, kind):
    rc = 1 if command.startswith("echo hi > /root") else 0
    assert kinds(text, [ran(command, rc)]) == [kind], command
