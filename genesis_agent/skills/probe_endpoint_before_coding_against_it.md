---
name: probe_endpoint_before_coding_against_it
category: autonomous
description: Probe an endpoint before writing code against it — a 400 or 401 proves
  the path exists, a 404 proves the address is wrong, and no valid key is needed to
  tell those apart.
triggers:
- probe endpoint before coding against it
- Probe an endpoint before writing code against it — a 400 or 401 proves the path
  exists, a 404 proves the address is wron
- провери адреса преди да пишеш код срещу него
- ударѝ endpoint-а и виж какво връща
- съществува ли този адрес изобщо
version: '1.0'
author: Genesis
last_updated: '2026-09-20T15:23:22.415077+00:00'
---

## Описание
Probe an endpoint before writing code against it — a 400 or 401 proves the path exists, a 404 proves the address is wrong, and no valid key is needed to tell those apart.

## Python Код
```python
"""Удари адреса и виж какво връща, вместо да пишеш URL по памет.

Защо съществува: моделът помни адреси и сигнатури приблизително, а приблизителен
адрес не гърми при писане — гърми посред мисия, с 404, час по-късно. Реален
случай от този проект: трите Google адреса (generativelanguage, регионалният
Vertex и `global`, който е ОТДЕЛЕН хост, а не празен префикс) бяха сглобени
правилно само защото бяха ударени на живо преди да се напише кодът.

Ключовото наблюдение, върху което стъпва всичко тук: за да разбереш дали един
път съществува, НЕ ти трябва валиден ключ. Отговорът сам го казва:

    404 / 405   пътят не съществува в тази форма  → адресът ти е грешен
    401 / 403   съществува, но иска автентикация  → адресът ти е верен
    400         съществува, заявката е грешна     → адресът ти е верен
    2xx         работи

Тоест една заявка с нарочно невалиден ключ отделя „сгрешил съм адреса" от
„сгрешил съм ключа" — двете, които иначе изглеждат еднакво.

Границите са истински:

  • Прокси или WAF пред услугата може да върне 401/403 за всичко, включително
    за несъществуващ път. Затова към `Probe` върви и тялото на отговора: то
    почти винаги казва кой е отговорил („Expected OAuth 2 access token" идва от
    Vertex, не от прокси).
  • 404 понякога значи „съществува, но не за теб" (частен ресурс). За базов
    адрес на API това е рядкост; за конкретен ресурс — не е.
  • Това проверява ПЪТЯ, не договора на тялото. Че адресът приема POST, не
    значи, че полетата ти са верните.

За код, който извикваш (а не адрес), същото важи за сигнатурите: питай
инсталирания пакет какво приема, вместо да си спомняш — `signature_of`.
"""
from __future__ import annotations

import inspect
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

_TIMEOUT = 15
_BODY_CHARS = 300

# Какво значи всеки статус за ВЪПРОСА „съществува ли този път".
_MEANING: dict[int, tuple[bool | None, str]] = {
    200: (True, "работи"),
    201: (True, "работи"),
    400: (True, "съществува — заявката е грешна (не адресът)"),
    401: (True, "съществува — иска автентикация"),
    403: (True, "съществува — отказан достъп"),
    404: (False, "НЕ съществува в тази форма"),
    405: (True, "съществува, но не приема този метод"),
    429: (True, "съществува — изчерпана квота"),
}


@dataclass
class Probe:
    """Какво каза услугата за един адрес."""
    url: str
    status: int | None = None
    exists: bool | None = None
    meaning: str = ""
    body: str = ""
    error: str = ""

    def __str__(self) -> str:
        if self.error:
            return f"❌ {self.url}\n   не се стигна дотам: {self.error}"
        mark = {True: "✅", False: "⚠️ ", None: "❓"}[self.exists]
        out = f"{mark} {self.status} {self.meaning}\n   {self.url}"
        if self.body:
            out += f"\n   каза: {self.body}"
        return out


def _shorten(text: str) -> str:
    """Едно изречение от тялото — достатъчно да се види КОЙ е отговорил."""
    text = " ".join(text.split())
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text[:_BODY_CHARS]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:_BODY_CHARS]
        if error:
            return str(error)[:_BODY_CHARS]
    return text[:_BODY_CHARS]


def _unencodable_header(headers: dict[str, str]) -> str | None:
    """Първият header, който HTTP не може да пренесе. HTTP заглавията са
    latin-1; кирилица в тях не е „грешен адрес", а незаминала заявка."""
    for name, value in headers.items():
        try:
            f"{name}: {value}".encode("latin-1")
        except UnicodeEncodeError:
            return name
    return None


def classify(status: int) -> tuple[bool | None, str]:
    """Съществува ли пътят, според този статус. None = не може да се съди."""
    if status in _MEANING:
        return _MEANING[status]
    if 500 <= status < 600:
        return None, "услугата се счупи — опитай пак, това не съди адреса"
    if 200 <= status < 300:
        return True, "работи"
    return None, f"неочакван статус {status}"


def probe(url: str, *, method: str = "POST", headers: dict[str, str] | None = None,
          body: bytes | None = None, timeout: int = _TIMEOUT) -> Probe:
    """Една заявка, само за да се види какво отговаря този адрес.

    Нарочно НЕ хвърля: целта е да се вземе присъда, а не да се прекъсне
    работата. Ключ не е нужен — виж какво отделя невалидният ключ в докстринга.
    """
    bad = _unencodable_header(headers or {})
    if bad:
        # Открито при реален пуск: кирилица в стойност на header гърми в
        # urllib с UnicodeEncodeError, а отвън прилича на „адресът е грешен".
        return Probe(url=url, error=f"header {bad!r} има стойност, която HTTP "
                                    "не носи (само latin-1) — заявката не тръгна")
    request = urllib.request.Request(url, method=method, data=body,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read(4096).decode("utf-8", "replace") if e.fp else ""
    except Exception as e:  # noqa: BLE001 — мрежа: всяка грешка е „не се стигна"
        return Probe(url=url, error=f"{type(e).__name__}: {e}")
    exists, meaning = classify(status)
    return Probe(url=url, status=status, exists=exists, meaning=meaning,
                 body=_shorten(raw))


def probe_all(urls: list[str], **kwargs) -> list[Probe]:
    return [probe(u, **kwargs) for u in urls]


def signature_of(dotted: str) -> str:
    """Истинската сигнатура на инсталираното, не спомнената.

    `signature_of("json.dumps")` → `(obj, *, skipkeys=False, ...)`. Това е
    същият въпрос като горния, но за код: пита се източникът, а не паметта.
    """
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        return f"{dotted}: няма модул в името — ползвай `модул.име`"
    try:
        import importlib
        obj = getattr(importlib.import_module(module_path), attr)
    except (ImportError, AttributeError) as e:
        return f"{dotted}: няма такова нещо ({type(e).__name__})"
    try:
        return f"{dotted}{inspect.signature(obj)}"
    except (TypeError, ValueError):
        return f"{dotted}: вградено, без обявена сигнатура"


def report(probes: list[Probe]) -> str:
    """Обобщение, което НЕ смесва „няма го" с „не се стигна дотам".

    Първата версия броеше и двете като „не съществува" — и когато всички
    заявки паднаха преди да тръгнат, написа „0 от 5 адреса съществуват",
    тоест обвини адресите за собствения си провал. Точно обратното на това,
    за което служи този модул.
    """
    lines = [str(p) for p in probes]
    good = [p for p in probes if p.exists is True]
    missing = [p for p in probes if p.exists is False]
    unreached = [p for p in probes if p.error]
    lines.append("")
    lines.append(f"{len(good)} от {len(probes)} адреса съществуват "
                 f"({len(missing)} ги няма). Пиши код само срещу съществуващите.")
    if unreached:
        lines.append(f"⚠️  {len(unreached)} изобщо не бяха достигнати — това НЕ е "
                     "присъда за адреса, поправи първо заявката/мрежата.")
    return "\n".join(lines)


if __name__ == "__main__":
    # Самопроверката НЕ ходи по мрежата: чистите решения се проверяват директно,
    # а мрежовият път — срещу адрес, който гарантирано не се резолвва.
    assert classify(404) == (False, "НЕ съществува в тази форма")
    assert classify(401)[0] is True, "401 доказва, че пътят е там"
    assert classify(400)[0] is True, "400 е за заявката, не за адреса"
    assert classify(503)[0] is None, "счупена услуга не съди адреса"
    assert classify(218)[0] is True
    assert classify(599)[0] is None

    assert _shorten('{"error": {"message": "Expected OAuth 2 access token"}}') \
        == "Expected OAuth 2 access token", "съобщението на услугата е важното"
    assert _shorten('{"error": "плосък низ"}') == "плосък низ"
    assert _shorten("не е json  <html>") == "не е json <html>"
    assert _shorten("x" * 1000) == "x" * _BODY_CHARS

    dead = probe("https://адрес-който-няма.невалиден-домейн-42/път", timeout=3)
    assert dead.error, "недостижим адрес дава error, не изключение"
    assert dead.exists is None
    assert "не се стигна дотам" in str(dead)

    assert signature_of("json.dumps").startswith("json.dumps(obj")
    assert "няма такова нещо" in signature_of("json.няма_такава_функция")
    assert "няма модул" in signature_of("самотноиме")

    cyrillic = probe("https://example.invalid/", headers={"Authorization": "Bearer ключ"})
    assert "не тръгна" in cyrillic.error, "кирилица в header е спрян ПРЕДИ заявката"
    assert _unencodable_header({"X": "ascii-only"}) is None

    text = report([Probe(url="u", status=401, exists=True, meaning="съществува")])
    assert "1 от 1" in text

    # Недостигнат адрес не бива да се брои като „не съществува" — точно това
    # объркване направи първата версия при реален пуск.
    mixed = report([Probe(url="a", error="мрежа"), Probe(url="b", error="мрежа")])
    assert "0 от 2" in mixed and "(0 ги няма)" in mixed
    assert "не бяха достигнати" in mixed

    print("OK")
```

## Pitfalls
- Автоматично генерирано от autonomous_loop.
