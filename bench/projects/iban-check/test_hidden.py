"""Скрит приемен тест: български IBAN по Наредба № 13 на БНБ (22 знака,
BG + 2 контролни цифри + BBAN: 4 букви от BIC, 4 цифри БАЕ, 2 цифри вид
сметка, 8 цифри ИЛИ букви; mod 97 == 1; на хартия — групи по 4 с интервал)."""
import pytest
from iban import validate


def num(s):
    return int("".join(str(int(c, 36)) for c in s))


def make(bban):
    cd = 98 - num(bban + "BG00") % 97
    return f"BG{cd:02d}{bban}"


def test_bnb_examples():
    assert validate("BG80BNBG96611020345678") is True
    assert validate("BG33AAAA12311012345678") is True  # примерът от Наредба № 13


def test_paper_format_with_spaces():
    assert validate("BG80 BNBG 9661 1020 3456 78") is True


def test_letters_allowed_in_last_eight():
    iban = make("UNCR70001512345ABC")
    assert validate(iban) is True
    assert validate(make("FINV91501017AB12CD")) is True


@pytest.mark.parametrize("bban", ["BNBG96611020345678", "UBBS80021087654321", "STSA93000012345678"])
def test_generated_valid(bban):
    assert validate(make(bban)) is True


def test_wrong_check_digits():
    assert validate("BG81BNBG96611020345678") is False
    assert validate("BG80BNBG96611020345687") is False  # разменени цифри


def test_foreign_iban_rejected():
    assert validate("DE89370400440532013000") is False  # валиден германски


@pytest.mark.parametrize("bban", ["1234" + "96611020345678", "BNBG" + "9A611020345678", "BNBG9661X0203456AB"])
def test_structure_even_with_valid_checksum(bban):
    assert validate(make(bban)) is False


@pytest.mark.parametrize("s", ["", "BG80BNBG9661102034567", "BG80BNBG966110203456789", "BG80BNBG96611020-45678"])
def test_bad_format(s):
    assert validate(s) is False
