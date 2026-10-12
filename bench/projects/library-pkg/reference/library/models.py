import re
from dataclasses import dataclass
from datetime import date

LOAN_DAYS = 14
MAX_LOANS = 3


def normalize_isbn(isbn: str) -> str:
    digits = re.sub(r"[\s-]", "", isbn or "")
    if not re.fullmatch(r"\d{13}", digits):
        raise ValueError(f"Невалиден ISBN-13: {isbn!r}")
    check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits[:12])) % 10) % 10
    if check != int(digits[12]):
        raise ValueError(f"Грешна контролна цифра в ISBN: {isbn!r}")
    return digits


@dataclass
class Book:
    isbn: str
    title: str
    author: str
    copies: int


@dataclass
class Reader:
    id: int
    name: str


@dataclass
class Loan:
    reader_id: int
    isbn: str
    taken: date
