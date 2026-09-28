"""Фактура, каквато идва от истинска фирма: дата ДД.ММ.ГГГГ г., номер с водещи
нули, суми с интервал за хилядите и „лв.“, доставчик в кавички, таблица с редове."""
from pathlib import Path

import pytest
from faktura.extract import extract
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

LINES = [
    "ФАКТУРА",
    "Оригинал",
    "Фактура № 0000004217",
    "Дата: 03.09.2026 г.",
    "Доставчик: „Стройинвест България“ ЕООД",
    "ЕИК: 204512378",
    "Получател: Иван Петров",
    "",
    "Наименование            Кол.   Ед. цена     Стойност",
    "Цимент 25 кг            400    9,50         3 800,00",
    "Арматура ф12            1200   7,20         8 640,00",
    "",
    "Данъчна основа: 12 440,00 лв.",
    "ДДС 20%: 2 488,00 лв.",
    "Сума за плащане: 14 928,00 лв.",
    "Словом: четиринадесет хиляди деветстотин двадесет и осем лева",
]


@pytest.fixture
def real_pdf(tmp_path: Path) -> Path:
    pdfmetrics.registerFont(TTFont("Arial", "C:/Windows/Fonts/arial.ttf"))
    path = tmp_path / "real.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Arial", 11)
    y = 800
    for line in LINES:
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return path


def test_real_bulgarian_invoice(real_pdf: Path) -> None:
    got = extract(str(real_pdf))
    assert got["nomer"] == "0000004217"
    assert got["data"] == "2026-09-03"
    assert got["dostavchik"] == "„Стройинвест България“ ЕООД"
    assert got["eik"] == "204512378"
    assert got["suma_bez_dds"] == 12440.0
    assert got["dds"] == 2488.0
    assert got["obshto"] == 14928.0
