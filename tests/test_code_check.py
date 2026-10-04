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


def test_a_mismatch_is_checked_against_the_task_before_the_code_is_touched():
    """bench_fcc 2026-09-30: c333 — собствен грешен assert, верен код, моделът
    „поправи“ кода по assert-а; c125 — обратното. И двете подкани казват:
    очакваното първо срещу условието, кодът се пипа само по условието."""
    for observed in ([WRITE], [WRITE, RUN]):
        c = RunCheck()
        for r in observed:
            c.observe(r)
        note = c.note()
        assert "срещу условието" in note and "само ако условието" in note, note


def test_assumptions_go_to_the_readme_and_the_answer():
    """NEXT_STEPS Б.6: „ОБЩО със сумата“ — число или речник? Допускането се
    записва, не се крие. В същата подкана — без ново обръщение."""
    for observed in ([WRITE], [WRITE, RUN]):
        c = RunCheck()
        for r in observed:
            c.observe(r)
        note = c.note()
        assert "допускане" in note and "README.md" in note and "отговора" in note, note


SKLAD = ("python -m sklad --db склад.json report (печата всеки продукт и ред ОБЩО със "
         "стойността); GET /thanks → „Благодарим за запитването“")


def test_a_word_the_task_names_literally_is_checked_in_the_code(tmp_path):
    """bench sklad-package 2026-10-02: „ред ОБЩО“, а отчетът печата TOTAL 4/5 пъти."""
    report = tmp_path / "report.py"
    report.write_text('print("TOTAL", total)\nprint("Благодарим за запитването")\n',
                      encoding="utf-8")
    c = RunCheck(SKLAD)
    c.observe(f"[WRITE_FILE: {report}] ✓ записани 40 символа")
    note = c.note()
    assert "„ОБЩО“" in note and "Благодарим" not in note


def test_the_literal_words_present_in_the_code_add_nothing(tmp_path):
    report = tmp_path / "report.py"
    report.write_text('print("ОБЩО", total)\nTHANKS = "Благодарим за запитването"\n',
                      encoding="utf-8")
    c = RunCheck(SKLAD)
    c.observe(f"[WRITE_FILE: {report}] ✓ записани 40 символа")
    assert "буквално" not in c.note()


def test_only_named_or_shown_words_count_as_literal():
    from genesis_agent.code_check import literals
    assert literals(SKLAD) == ["ОБЩО", "Благодарим за запитването"]
    # bench cli-config / faktura-excel / eik-check: ударение, параметър, съкращение.
    assert literals("закръглена НАГОРЕ; `python -m faktura ПАПКА изход.xlsx`; ЕИК (БУЛСТАТ)") == []
    assert literals('ключ "ОБЩО" със сумата; накрая ред ОБЩО') == ["ОБЩО"]


def test_a_lowercase_column_name_is_literal_too():
    """bench cli-config 2026-10-04 #2: „още една колона продажна“, а изходът е sale_price
    (4/5 скрити теста). „командния ред сменя“ и „колона със сумата“ не са имена."""
    from genesis_agent.code_check import literals
    cli = ("Изходът е същият CSV с още една колона продажна, с десетична запетая. "
           "--markup от командния ред сменя общата надценка")
    assert literals(cli) == ["продажна"]
    assert literals("колони дата;продукт; добави колона със сумата и колона която е празна") == []


def test_the_terminal_turn_gives_the_check_its_task(tmp_path, monkeypatch):
    """bench_projects минава през терминала (cli → run_turn) — там заявката трябва да стигне."""
    from collections import deque

    import genesis_terminal_agent as gta
    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setattr("genesis_agent.skill_loader.domain_context", lambda q: "")
    monkeypatch.delenv("GENESIS_ACCEPTANCE", raising=False)
    report = tmp_path / "report.py"
    report.write_text('print("TOTAL", 12)\n', encoding="utf-8")
    call = {"id": "1", "type": "function", "function": {"name": "WRITE_FILE", "arguments": "{}"}}
    replies = [("", [call]), ("Готово.", [])]
    monkeypatch.setattr(gta, "ask_genesis", lambda *a, **k: replies.pop(0) if replies else ("Край.", []))
    monkeypatch.setattr(gta.genesis_skills, "dispatch_tool_call",
                        lambda name, args: f"[WRITE_FILE: {report}] ✓ записани 19 символа")
    monkeypatch.setattr(gta.genesis_skills, "parse_and_execute_tools", lambda text: [])
    messages = gta.run_turn(deque([{"role": "system", "content": "s"}], maxlen=50),
                            "report.py печата всеки продукт и ред ОБЩО", gta.TurnUI())
    assert any("„ОБЩО“" in str(m["content"]) for m in messages)


def test_a_test_file_with_the_word_does_not_count(tmp_path):
    (tmp_path / "tests").mkdir()
    report, test = tmp_path / "report.py", tmp_path / "tests" / "test_report.py"
    report.write_text('print("TOTAL", total)\n', encoding="utf-8")
    test.write_text('assert "ОБЩО" in out\n', encoding="utf-8")
    c = RunCheck(SKLAD)
    for f in (report, test):
        c.observe(f"[WRITE_FILE: {f}] ✓ записани 20 символа")
    assert "„ОБЩО“" in c.note()
