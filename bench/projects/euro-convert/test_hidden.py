import pytest
from money import official_currency, to_eur


def test_rate():
    assert to_eur(1.95583) == 1.0
    assert to_eur(100) == 51.13
    assert to_eur(0) == 0.0

@pytest.mark.parametrize("d,cur", [("2025-12-31", "BGN"), ("2026-01-01", "EUR"), ("2026-09-25", "EUR"), ("2020-06-01", "BGN")])
def test_currency(d, cur):
    assert official_currency(d) == cur
