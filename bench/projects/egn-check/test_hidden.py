import pytest
from egn import parse, validate

W = [2, 4, 8, 5, 10, 9, 7, 3, 6]

def make(y, m, d, serial):
    mm = m + (20 if y < 1900 else 40 if y >= 2000 else 0)
    base = f"{y % 100:02d}{mm:02d}{d:02d}{serial:03d}"
    c = sum(int(a) * w for a, w in zip(base, W)) % 11
    return base + str(0 if c == 10 else c)

@pytest.mark.parametrize("y,m,d,serial,g", [
    (1975, 1, 1, 1, "Ж"), (1975, 1, 1, 2, "М"), (1990, 12, 31, 444, "М"),
    (1899, 5, 17, 123, "Ж"), (2005, 3, 9, 580, "М"), (2000, 2, 29, 111, "Ж"),
])
def test_valid(y, m, d, serial, g):
    e = make(y, m, d, serial)
    assert validate(e) is True
    assert parse(e) == {"birth_date": f"{y}-{m:02d}-{d:02d}", "gender": g}

@pytest.mark.parametrize("e", ["", "123", "12345678901", "75010100a1", " 7501010010"])
def test_bad_format(e):
    assert validate(e) is False

def test_wrong_checksum():
    e = make(1980, 6, 15, 321)
    bad = e[:-1] + str((int(e[-1]) + 1) % 10)
    assert validate(bad) is False

@pytest.mark.parametrize("y,m,d", [(1975, 2, 30), (1900, 2, 29), (1990, 4, 31)])
def test_impossible_date(y, m, d):
    mm = m + (20 if y < 1900 else 40 if y >= 2000 else 0)
    base = f"{y % 100:02d}{mm:02d}{d:02d}000"
    c = sum(int(a) * w for a, w in zip(base, W)) % 11
    assert validate(base + str(0 if c == 10 else c)) is False

def test_month_out_of_range():
    base = "751301000"
    c = sum(int(a) * w for a, w in zip(base, W)) % 11
    assert validate(base + str(0 if c == 10 else c)) is False

def test_checksum_10_becomes_0():
    for s in range(1000):
        base = f"800615{s:03d}"
        if sum(int(a) * w for a, w in zip(base, W)) % 11 == 10:
            assert validate(base + "0") is True
            return
    pytest.skip("no case")

def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse("7501010011")
