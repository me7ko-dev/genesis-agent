"""genesis_agent.accept_check — tests from the request alone, run against the code."""
from __future__ import annotations

from genesis_agent.accept_check import AcceptCheck, _project_root, signatures

TASK = ("Направи money.py с функция to_eur(text) -> float, която превръща цена като "
        "\"1 299,00 лв.\" или \"24,90 €\" в евро по фиксирания курс 1.95583, закръглено до "
        "2 знака. Интервалът за хилядите може да е и непрекъсваем. Напиши тестове и ги пусни.")
CODE = ("def to_eur(text):\n"
        "    n = float(text.split()[0].replace(',', '.')) if ' ' not in text[:-4] else 0.0\n"
        "    return round(n / 1.95583, 2) if 'лв' in text else n\n")


def _tester(body: str):
    return lambda messages: f"Ето:\n```python\nfrom money import to_eur\n\n{body}\n```\n"


def _written(tmp_path, name="money.py", code=CODE) -> AcceptCheck:
    (tmp_path / name).write_text(code, encoding="utf-8")
    check = AcceptCheck(TASK)
    check.observe(f"[WRITE_FILE: {tmp_path / name}] ✓ записани {len(code)} символа")
    return check


def test_due_only_once_for_code_and_a_real_request(tmp_path, monkeypatch) -> None:
    assert not AcceptCheck(TASK).due()                       # нищо не е записано
    short = AcceptCheck("скрипт, който печата часа")
    short.observe(f"[WRITE_FILE: {tmp_path / 'clock.py'}] ✓ записани 9 символа")
    assert not short.due()                                   # кратка заявка
    tests_only = AcceptCheck(TASK)
    tests_only.observe(f"[WRITE_FILE: {tmp_path / 'test_money.py'}] ✓ записани 9 символа")
    assert not tests_only.due()
    check = _written(tmp_path)
    assert check.due()
    monkeypatch.setenv("GENESIS_ACCEPT_CHECK", "0")
    assert not check.due()


def test_a_failure_goes_back_with_the_request_as_judge(tmp_path) -> None:
    check = _written(tmp_path)
    note, line = check.check(_tester(
        "def test_bgn():\n    assert to_eur('24,90 €') == 24.90\n\n"
        "def test_nbsp():\n    assert to_eur('1\\u00a0299,00 лв.') == 664.17\n"))
    assert line == "1 от 2 теста само по заявката паднаха"
    assert "test_nbsp" in note and "ТЕКСТА НА ЗАЯВКАТА" in note and "НЕ пипай" in note
    assert not check.due()                                   # веднъж на ход


def test_all_green_says_nothing_to_the_model(tmp_path) -> None:
    note, line = _written(tmp_path).check(_tester(
        "def test_eur():\n    assert to_eur('24,90 €') == 24.90\n"))
    assert note == "" and line == "✓ 1/1 теста само по заявката"


def test_broken_tests_or_no_tests_are_noise_not_findings(tmp_path) -> None:
    note, line = _written(tmp_path).check(_tester("def test_x(:\n    pass\n"))
    assert note == "" and "не тръгнаха" in line
    note, line = _written(tmp_path).check(lambda m: "Не мога.")
    assert note == "" and "не върна тестове" in line

    def boom(messages):
        raise RuntimeError("all models down")
    note, line = _written(tmp_path).check(boom)
    assert note == "" and "RuntimeError" in line


def test_the_tester_sees_signatures_not_code(tmp_path) -> None:
    pkg = tmp_path / "sklad"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .fifo import Warehouse\n", encoding="utf-8")
    (pkg / "fifo.py").write_text("class Warehouse:\n    def sell(self, sku, qty):\n"
                                 "        return SECRET_BODY\n", encoding="utf-8")
    (tmp_path / "test_sklad.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    files = [pkg / "__init__.py", pkg / "fifo.py", tmp_path / "test_sklad.py"]
    root = _project_root(files[:2])
    assert root == tmp_path                                   # над пакета, за да се внася
    sig = signatures(files, root)
    assert "# sklad/fifo.py" in sig and "def sell(self, sku, qty):" in sig
    assert "SECRET_BODY" not in sig and "test_sklad" not in sig
