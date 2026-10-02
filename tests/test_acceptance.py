"""genesis_agent.acceptance — tests written from the request alone (NEXT_STEPS Б.4)."""
from genesis_agent import acceptance as acc

MODULE = "def vat(net):\n    return round(net * 0.2, 2)\n"
TESTS = ("```python\nfrom money import vat\n\n\ndef test_vat_20_percent():\n    assert vat(100) == 20\n\n\n"
         "def test_total_with_vat():\n    from money import total\n    assert total(100) == 120\n```")


def _check(tmp_path, monkeypatch, reply=TESTS, module=MODULE):
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    (tmp_path / "money.py").write_text(module, encoding="utf-8")
    calls = []

    def fake(messages):
        calls.append(messages)
        return reply

    c = acc.AcceptanceCheck("Направи money.py с vat(net) и total(net) — сумата с 20% ДДС.", complete=fake)
    c.observe(f"[WRITE_FILE: {tmp_path / 'money.py'}] ✓ записани 40 символа")
    return c, calls


def test_failures_go_back_once_with_the_task_first_rule(tmp_path, monkeypatch):
    c, calls = _check(tmp_path, monkeypatch)
    assert c.due()
    note = c.check()
    assert "1/2" in note and "test_total_with_vat" in note and "условието" in note
    assert not c.due() and c.check() == ""
    sent = calls[0][-1]["content"]
    assert "total(net)" in sent and "round(net" not in sent  # заявката, никога кодът


def test_all_green_says_nothing(tmp_path, monkeypatch):
    c, _ = _check(tmp_path, monkeypatch, module=MODULE + "\n\ndef total(net):\n    return net + vat(net)\n")
    assert c.check() == ""


def test_off_by_default(tmp_path, monkeypatch):
    c, _ = _check(tmp_path, monkeypatch)
    monkeypatch.delenv("GENESIS_ACCEPTANCE")
    assert not c.due()


def test_no_names_in_the_request_or_a_failed_call_costs_nothing_more(tmp_path, monkeypatch):
    for reply in ("NONE", "Error: all models failed", "no code here"):
        c, _ = _check(tmp_path, monkeypatch, reply=reply)
        assert c.check() == "" and not c.due()


def test_nothing_written_nothing_due(monkeypatch):
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    c = acc.AcceptanceCheck("здрасти", complete=lambda m: TESTS)
    c.observe("[READ_FILE: a.py]\nx = 1")
    assert not c.due()


def test_the_project_root_is_above_tests(tmp_path):
    assert acc.project_root(tmp_path / "tests" / "test_money.py") == tmp_path
    assert acc.project_root(tmp_path / "money.py") == tmp_path


def test_model_written_tests_never_see_the_api_keys(tmp_path, monkeypatch):
    """Тестовете ги пише моделът — средата е тази на RUN_CMD, не на Genesis."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    reply = ("```python\nimport os\nfrom money import vat\n\n\ndef test_no_key():\n"
             "    assert vat(100) == 20\n    assert 'OPENROUTER_API_KEY' not in os.environ\n```")
    c, _ = _check(tmp_path, monkeypatch, reply=reply)
    assert c.check() == ""
