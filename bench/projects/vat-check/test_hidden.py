"""Скрит приемен тест: български ДДС номер (чл. 94, ал. 2 ЗДДС) — BG + 9-цифрен
ЕИК (с резервните тегла 3..10) или BG + 10-цифрено ЕГН (валидна дата, тегла
2,4,8,5,10,9,7,3,6, остатък 10 → 0). Форматът в VIES е BG + 9 или 10 цифри,
така че 13-цифрен ЕИК на клон не е ДДС номер."""
import pytest
from vat import validate

W = [2, 4, 8, 5, 10, 9, 7, 3, 6]


def c9(d8):
    r = sum(int(a) * w for a, w in zip(d8, range(1, 9))) % 11
    if r == 10:
        r = sum(int(a) * w for a, w in zip(d8, range(3, 11))) % 11
        if r == 10:
            r = 0
    return str(r)


def first_r(d8, w):
    return sum(int(a) * x for a, x in zip(d8, w)) % 11


def find_eik(pred):
    for n in range(10000000, 99999999, 7919):
        d8 = str(n)
        if pred(d8):
            return d8 + c9(d8)


def egn_base(y, m, d, serial):
    mm = m + (20 if y < 1900 else 40 if y >= 2000 else 0)
    return f"{y % 100:02d}{mm:02d}{d:02d}{serial:03d}"


def egn(y, m, d, serial):
    base = egn_base(y, m, d, serial)
    c = sum(int(a) * w for a, w in zip(base, W)) % 11
    return base + str(0 if c == 10 else c)


def test_company_plain():
    e = find_eik(lambda d: first_r(d, range(1, 9)) != 10)
    assert validate("BG" + e) is True
    assert validate("BG" + e[:-1] + str((int(e[-1]) + 1) % 10)) is False


def test_company_second_weights():
    e = find_eik(lambda d: first_r(d, range(1, 9)) == 10 and first_r(d, range(3, 11)) != 10)
    assert validate("BG" + e) is True


def test_company_double_ten_gives_zero():
    e = find_eik(lambda d: first_r(d, range(1, 9)) == 10 and first_r(d, range(3, 11)) == 10)
    assert e[-1] == "0" and validate("BG" + e) is True


@pytest.mark.parametrize("y,m,d,serial", [(1975, 1, 1, 1), (1990, 12, 31, 444), (2005, 3, 9, 580), (1899, 5, 17, 123)])
def test_person(y, m, d, serial):
    v = "BG" + egn(y, m, d, serial)
    assert validate(v) is True
    assert validate(v[:-1] + str((int(v[-1]) + 1) % 10)) is False


def test_person_checksum_10_becomes_0():
    for s in range(1000):
        base = f"800615{s:03d}"
        if sum(int(a) * w for a, w in zip(base, W)) % 11 == 10:
            assert validate("BG" + base + "0") is True
            return
    pytest.skip("no case")


def test_person_impossible_date():
    base = "751301000"  # месец 13
    c = sum(int(a) * w for a, w in zip(base, W)) % 11
    assert validate("BG" + base + str(0 if c == 10 else c)) is False


def test_branch_13_digits_is_not_vat():
    e = find_eik(lambda d: first_r(d, range(1, 9)) != 10)
    tail = e[-1] + "000"
    r = sum(int(a) * w for a, w in zip(tail, (2, 7, 3, 5))) % 11
    assert validate("BG" + e + "000" + str(r if r != 10 else 0)) is False


@pytest.mark.parametrize("s", ["", "BG", "BG12345678", "BG12345678a", "RO100000001", "100000001", "BG100000001X"])
def test_bad_format(s):
    assert validate(s) is False


def test_bare_eik_without_prefix_rejected():
    assert validate(find_eik(lambda d: first_r(d, range(1, 9)) != 10)) is False
