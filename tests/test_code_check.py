"""genesis_agent.code_check — Python written in a turn → one nudge to try it."""
from genesis_agent.code_check import RunCheck

WRITE = "[WRITE_FILE: C:\\work\\solution.py] ✓ записани 405 символа"
EDIT = "[EDIT_FILE: C:\\work\\solution.py] ✓ заменени 1 място"
RUN = "[RUN_CMD: python -c \"from solution import f; print(f(1))\"]  (rc=0)\n1"


def test_written_and_never_run_is_told_to_run_it_once():
    c = RunCheck()
    c.observe(WRITE)
    assert c.due()
    note = c.note()
    assert "solution.py" in note and "НЕ пуснат" in note and "отхвърлен" in note
    assert not c.due() and c.note() == ""


def test_run_only_with_the_examples_is_told_to_try_the_rules():
    c = RunCheck()
    c.observe(WRITE)
    c.observe(RUN)
    note = c.note()
    assert "НЕ пуснат" not in note and "различен от примерите" in note


def test_an_edit_after_the_run_counts_as_not_run():
    c = RunCheck()
    c.observe(WRITE)
    c.observe(RUN)
    c.observe(EDIT)
    assert "НЕ пуснат" in c.note()


def test_a_command_that_does_not_run_the_code_does_not_count():
    c = RunCheck()
    c.observe(WRITE)
    c.observe("[RUN_CMD: dir]  (rc=0)\n solution.py")
    c.observe("[RUN_CMD: type solution.py]  (rc=0)\ndef f(): return python_version")
    assert "НЕ пуснат" in c.note()


def test_every_way_of_running_python_counts():
    for cmd in ("C:/Python314/python.exe solution.py", "python - << 'PY'\nprint(1)\nPY",
                "py -3 solution.py", "cd C:/work && pytest -q", "python3 -m pytest"):
        c = RunCheck()
        c.observe(WRITE)
        c.observe(f"[RUN_CMD: {cmd}]  (rc=0)\nok")
        assert "НЕ пуснат" not in c.note(), cmd


def test_no_python_written_no_nudge():
    c = RunCheck()
    c.observe("[WRITE_FILE: C:\\work\\README.md] ✓ записани 10 символа")
    c.observe("[WRITE_FILE: C:\\work\\x.py] ✗ пътят е извън работната папка")
    c.observe(RUN)
    assert not c.due() and c.note() == ""
