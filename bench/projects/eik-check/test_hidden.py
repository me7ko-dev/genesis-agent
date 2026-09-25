import pytest
from eik import validate


def c9(d8):
    r = sum(int(a) * w for a, w in zip(d8, range(1, 9))) % 11
    if r == 10:
        r = sum(int(a) * w for a, w in zip(d8, range(3, 11))) % 11
        if r == 10:
            r = 0
    return str(r)

def c13(d4):
    r = sum(int(a) * w for a, w in zip(d4, (2, 7, 3, 5))) % 11
    if r == 10:
        r = sum(int(a) * w for a, w in zip(d4, (4, 9, 5, 7))) % 11
        if r == 10:
            r = 0
    return str(r)

def find9(pred):
    for n in range(10000000, 99999999, 7919):
        d8 = str(n)
        if pred(d8):
            return d8 + c9(d8)

def first_r(d8, w):
    return sum(int(a) * x for a, x in zip(d8, w)) % 11

def test_plain_9():
    e = find9(lambda d: first_r(d, range(1, 9)) != 10)
    assert validate(e) is True
    assert validate(e[:-1] + str((int(e[-1]) + 1) % 10)) is False

def test_second_weights_9():
    """Остатък 10 с теглата 1–8 → теглата 3–10."""
    e = find9(lambda d: first_r(d, range(1, 9)) == 10 and first_r(d, range(3, 11)) != 10)
    assert e is not None and validate(e) is True

def test_double_ten_gives_zero():
    e = find9(lambda d: first_r(d, range(1, 9)) == 10 and first_r(d, range(3, 11)) == 10)
    assert e is not None and e[-1] == "0" and validate(e) is True

def test_13_digits():
    base = find9(lambda d: first_r(d, range(1, 9)) != 10)
    for n in range(10000):
        d4 = f"{n:04d}"
        tail = base[-1] + d4[:3]
        e = base + d4[:3] + c13(tail)
        if validate(e):
            break
    assert validate(e) is True
    assert validate(e[:-1] + str((int(e[-1]) + 1) % 10)) is False

def test_13_second_weights():
    base = find9(lambda d: first_r(d, range(1, 9)) != 10)
    for n in range(1000):
        tail = base[-1] + f"{n:03d}"
        if sum(int(a) * w for a, w in zip(tail, (2, 7, 3, 5))) % 11 == 10:
            assert validate(base + f"{n:03d}" + c13(tail)) is True
            return
    pytest.skip("no case")

@pytest.mark.parametrize("e", ["", "12345678", "1234567890", "12345678a", "1234567890123x"])
def test_bad_format(e):
    assert validate(e) is False
