"""Правните страници (2026-11-13): условия, поверителност, връщане на пари, контакт.

Шаблоните са в репото (публично) с празни места `{{ПРОДАВАЧ}}` и т.н.;
истинските данни на продавача са във файл на сървъра (`--legal`, JSON), не в
git — иначе `deploy.sh` (git pull --ff-only) спираше на местни промени, а
личните данни щяха да са в публичното репо. Без файла страниците се
показват с празните места: бета с покани, не продажба. `golive_check.py`
отказва live, докато има празно място или страницата е още „чернова“.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

DIR = Path(__file__).with_name("static") / "legal"
PAGES = ("terms", "privacy", "refunds", "contact")
LANGS = ("bg", "en")
# Попълват се в --legal; ключът е името на празното място.
FIELDS = {
    "ПРОДАВАЧ": "име на продавача (физическо лице или фирма)",
    "ИДЕНТИФИКАТОР": "ЕИК/БУЛСТАТ, или „няма“ за физическо лице без регистрация",
    "АДРЕС": "адрес за кореспонденция",
    "ИМЕЙЛ": "имейл за връзка",
    "ХОСТИНГ": "доставчикът на сървъра и държавата (напр. Hetzner Online GmbH, Германия)",
    "ДОСТАВЧИЦИ_НА_МОДЕЛИ": "фирмите на моделите от GATEWAY_MODELS и държавата им "
                            "(напр. Groq, Inc., САЩ)",
    "СРОК_РАЗГОВОРИ": "колко се пазят разговорите и файловете (напр. 90 дни след последния ход)",
    "В_СИЛА_ОТ": "от коя дата важат страниците (напр. 2026-12-01)",
}
REVIEWED = "ПРЕГЛЕДАНО"          # "да" в --legal → без лентата „чернова“
_SLOT = re.compile(r"\{\{([^{}\s]{1,40})\}\}")
_DRAFT = re.compile(r"<!--draft-->.*?<!--/draft-->", re.DOTALL)


def template(page: str, lang: str) -> str:
    if page not in PAGES or lang not in LANGS:
        raise KeyError(f"{page}.{lang}")
    return (DIR / f"{page}.{lang}.html").read_text(encoding="utf-8")


def load_values(path: Path) -> dict[str, str]:
    """JSON обект „име на празно място → стойност“. Невалиден → ValueError."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ValueError(f"{path}: не се чете ({e.strerror})") from None
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: невалиден JSON") from None
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"{path}: очаква се обект с текстови стойности")
    unknown = set(data) - set(FIELDS) - {REVIEWED}
    if unknown:
        raise ValueError(f"{path}: непознати полета: {', '.join(sorted(unknown))}")
    return {k: v.strip() for k, v in data.items()}


def missing(values: dict[str, str]) -> list[str]:
    """Празните места, които още не са попълнени."""
    return [k for k in FIELDS if not values.get(k)]


def reviewed(values: dict[str, str]) -> bool:
    return values.get(REVIEWED, "").lower() in ("да", "yes")


def render(page: str, lang: str, values: dict[str, str]) -> str:
    """Шаблонът с попълнените места (екранирани — стойностите са текст, не HTML).

    Непопълненото остава `{{ИМЕ}}` — видимо, а не празно: празно място на
    страница за плащане изглежда като готов текст."""
    text = template(page, lang)
    if reviewed(values):
        text = _DRAFT.sub("", text)

    def fill(m: re.Match[str]) -> str:
        v = values.get(m.group(1), "")
        return html.escape(v) if v else m.group(0)

    return _SLOT.sub(fill, text)


def slots(text: str) -> set[str]:
    return set(_SLOT.findall(text))
