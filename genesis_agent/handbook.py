"""Наръчникът на оператора — кратки бележки „кое къде стои и как се прави“.

Защо (2026-11-17): локалната резерва (9B модел на 4 GB видеокарта) реши 13/21
от трудните задачи, при 19/21 за облачния мозък. Малкият модел не може да
измисли къде е проектът, с коя команда се качва и какво значи познатата
грешка — но може да го ПРОЧЕТЕ, ако му се подаде точната страница. Това е
същата идея като `skill_loader.domain_context` (проверено знание наготово),
само че за нещата на самия оператор.

Бележките живеят в `~/.genesis/handbook/*.md` — при оператора, никога в
репото: вътре има пътища, имена на проекти и адреси. Форматът:

    ---
    title: Качване в Cloudflare
    triggers:
    - качване в cloudflare
    - wrangler deploy
    ---
    (текстът, който моделът вижда)

Прозорецът на локалния модел е 8192 токена, затова подаваме най-много две
бележки и общо `MAX_CHARS` знака; дългата бележка се реже, не се изпуска.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from genesis_agent.skill_loader import _keywords, _light_keywords

log = logging.getLogger("genesis.handbook")

# ~750 токена: с системния промпт и заявката остава място и в 8K прозорец.
MAX_CHARS = 3000
MAX_NOTES = 2
HEADER = "## Бележка от наръчника: "

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_CACHE: dict[str, dict[str, Any]] | None = None


def handbook_dir() -> Path:
    override = os.environ.get("GENESIS_HANDBOOK_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    from genesis_agent import paths
    return paths.GENESIS_HOME / "handbook"


def reload() -> dict[str, dict[str, Any]]:
    """Чете бележките наново от диска. Счупена бележка се пропуска с ред в
    дневника — една грешка в YAML не бива да скрие останалите."""
    global _CACHE
    notes: dict[str, dict[str, Any]] = {}
    folder = handbook_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
                head = _FRONTMATTER.match(text)
                if not head:
                    continue
                meta = yaml.safe_load(head.group(1)) or {}
                triggers = meta.get("triggers") or []
                if isinstance(triggers, str):
                    triggers = [triggers]
                triggers = [str(t) for t in triggers if str(t).strip()]
                body = text[head.end():].strip()
                if not triggers or not body:
                    continue
                notes[path.stem] = {"name": path.stem, "title": str(meta.get("title") or path.stem),
                                    "triggers": triggers, "body": body}
            except Exception:
                log.debug("бележката %s не се прочете", path.name, exc_info=True)
    _CACHE = notes
    return notes


def _notes() -> dict[str, dict[str, Any]]:
    return reload() if _CACHE is None else _CACHE


def _ranked(query: str) -> list[dict[str, Any]]:
    """Бележките, на които ЦЯЛ тригер се среща в заявката, най-точните първи.

    Цял тригер, не обща дума: при уменията съвпадение по единични думи
    подаваше правилата за IBAN на задача за работни дни (2026-09-25). Мярката
    е дължината на най-дългия съвпаднал тригер — „тест на домакин“ бие „тест“.
    """
    light = _light_keywords(query)
    if not light:
        return []
    scored: list[tuple[int, int, str, dict[str, Any]]] = []
    for note in _notes().values():
        best = 0
        for trigger in note["triggers"]:
            words = _light_keywords(trigger)
            if words and words <= light:
                best = max(best, len(words))
        if best:
            overlap = len(_keywords(query) & {w for t in note["triggers"] for w in _keywords(t)})
            scored.append((best, overlap, note["name"], note))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return [note for *_, note in scored]


def search(query: str, top_n: int = 5) -> list[dict[str, str]]:
    """За инструмента: кои бележки отговарят на заявката (име и заглавие)."""
    return [{"name": n["name"], "title": n["title"]} for n in _ranked(query)[:top_n]]


def read(name: str) -> str:
    """Текстът на бележка по име; непознато име (и път навън) дава празен низ."""
    note = _notes().get(name)
    return note["body"] if note else ""


def handbook_context(query: str) -> str:
    """Бележките по темата на заявката, готови за лепене под нея. Празен низ =
    нищо не се добавя, нула токена."""
    return fresh_notes(query, [])[0]


def fresh_notes(query: str, earlier: list[str]) -> tuple[str, list[str]]:
    """Като handbook_context, но без бележките, които вече са в разговора
    (`earlier` — предишните съобщения на оператора). Иначе всяко продължение по
    същата тема лепи същата бележка пак — при уменията това беше 4 копия след
    4 реплики (одит 2026-10-07). Връща текста и имената за реда на екрана."""
    try:
        hits = [n for n in _ranked(query)
                if not any(f"{HEADER}{n['name']}\n" in text for text in earlier)][:MAX_NOTES]
    except Exception:
        log.debug("търсенето в наръчника се провали", exc_info=True)
        return "", []
    parts: list[str] = []
    names: list[str] = []
    left = MAX_CHARS
    for note in hits:
        if left < 200:
            break
        names.append(note["name"])
        body = note["body"]
        if len(body) > left:
            body = body[:left].rsplit("\n", 1)[0] + "\n[…бележката е съкратена]"
        parts.append(f"{HEADER}{note['name']}\n"
                     "Това е записано от оператора за неговите проекти и машина — довери му се "
                     "преди собствените си предположения.\n" + body)
        left -= len(body)
    return "\n\n".join(parts), names
