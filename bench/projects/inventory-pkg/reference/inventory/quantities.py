from decimal import Decimal, InvalidOperation


def _bad(value):
    return ValueError(f"Невалидно количество: {value!r}")


def parse_qty(value):
    """int, Decimal или текст по български: "1 234,50" (и с неразделим интервал)."""
    if isinstance(value, bool):
        raise _bad(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _bad(value)
        return value
    if not isinstance(value, str):
        raise _bad(value)
    text = value.strip()
    for space in (" ", " ", " "):
        text = text.replace(space, "")
    if not text or "." in text:
        raise _bad(value)
    try:
        d = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise _bad(value) from None
    if not d.is_finite():
        raise _bad(value)
    return d


def format_qty(d):
    """2,5 вместо 2.500; 10 вместо 1E+1."""
    text = format(d.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")
