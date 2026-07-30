"""The Skills Library — persist verified tools under skills/<slug>.md + skills.json index.

Writes the same format genesis_agent.skill_loader/trigger_engine read (YAML
frontmatter + fenced python block, indexed in skills/skills.json). Do not
reintroduce the old .py + metadata.json format here — skill_loader.py and
trigger_engine.py cannot see it, which is exactly the split-brain that
scripts/unify_skills_format.py had to clean up once already.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis_agent import dna
from genesis_agent.config import SKILLS_DIR

# `file_path` in skills.json is relative to this, not PROJECT_ROOT — see the
# comment on skill_loader.SKILLS_ROOT for why the two are not the same thing
# once Genesis is pip-installed rather than run from a checkout.
SKILLS_ROOT = SKILLS_DIR.parent

SKILLS_INDEX_NAME = "skills.json"

# Заключване за паралелни записи — за да не си повредят skills.json.
_SAVE_LOCK = threading.Lock()


def slugify(text: str, max_len: int = 48) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_") or "skill"
    return s[:max_len]


def _index_path() -> Path:
    return SKILLS_DIR / SKILLS_INDEX_NAME


def ensure_skills_layout() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    idx = _index_path()
    if not idx.exists():
        idx.write_text(
            json.dumps({"version": 1, "skills": []}, indent=2),
            encoding="utf-8",
        )


def _load_index() -> dict[str, Any]:
    ensure_skills_layout()
    return json.loads(_index_path().read_text(encoding="utf-8"))


def _save_index(data: dict[str, Any]) -> None:
    _index_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_skills() -> list[dict[str, Any]]:
    return list(_load_index().get("skills", []))


def _build_md(*, slug: str, description: str, triggers: list[str], code: str,
              last_updated: str, note: str) -> str:
    frontmatter = (
        "---\n"
        f"name: '{slug}'\n"
        "category: 'autonomous'\n"
        f"description: '{description}'\n"
        f"triggers: {json.dumps(triggers, ensure_ascii=False)}\n"
        "version: '1.0'\n"
        "author: 'Genesis'\n"
        f"last_updated: '{last_updated}'\n"
        "---\n"
    )
    body = (
        f"\n## Описание\n{description}\n\n"
        f"## Python Код\n```python\n{code.rstrip()}\n```\n\n"
        f"## Pitfalls\n- {note}\n"
    )
    return frontmatter + body


def save_skill(
    *,
    slug: str,
    code: str,
    goal: str,
    verification_stdout: str = "",
    extra: dict[str, Any] | None = None,
    require_verified: bool | None = None,
) -> Path:
    """
    Write skills/<slug>.md and update skills.json — the format skill_loader.py/
    trigger_engine.py actually read.

    Всяко умение се проверява реално (genesis_agent.verifier) и получава verified печат
    в индекса. Ако require_verified е True (или env GENESIS_REQUIRE_VERIFIED=1) и
    умението не мине проверката → НЕ се записва и се вдига GenesisDNAError.
    """
    import os

    from genesis_agent.verifier import verify_skill

    dna.validate_skill_payload(goal=goal, code=code)

    if require_verified is None:
        require_verified = os.environ.get("GENESIS_REQUIRE_VERIFIED") == "1"
    vres = verify_skill(code)
    if require_verified and not vres.verified:
        raise dna.GenesisDNAError(
            f"GENE-QUALITY: умението не мина верификация ({vres.method}: {vres.detail[:120]})"
        )

    ensure_skills_layout()
    base_slug = slugify(slug)
    now = datetime.now(timezone.utc).isoformat()
    note = (verification_stdout or "")[:2000] or "Автоматично генерирано от autonomous_loop."

    # Критична секция — под lock, за да са безопасни паралелните записи И за да
    # решим финалния slug atomically с колизионната проверка по-долу.
    with _SAVE_LOCK:
        idx = _load_index()
        skills: list[dict[str, Any]] = list(idx.get("skills", []))
        existing = next((s for s in skills if s.get("name") == base_slug), None)
        final_slug = base_slug
        if existing is not None and existing.get("description") != goal:
            # slugify() реже на 48 символа — различни цели с еднакъв дълъг общ
            # префикс (напр. шаблонните goal_engine теми) иначе биха се сблъскали
            # тук и втората тихо ще презапише .md файла и записа в индекса на
            # първата (реален случай, хванат при ръчно тестване 2026-07-26).
            # Дедупликираме само истински повторни записи на СЪЩАТА цел (напр.
            # deep_verifier re-verify) — различна цел получава disambiguated slug.
            suffix = hashlib.sha1(goal.encode("utf-8")).hexdigest()[:6]
            final_slug = f"{base_slug[:40]}_{suffix}"
            while any(s.get("name") == final_slug for s in skills):
                suffix = hashlib.sha1((goal + suffix).encode("utf-8")).hexdigest()[:6]
                final_slug = f"{base_slug[:40]}_{suffix}"

        md_path = SKILLS_DIR / f"{final_slug}.md"
        triggers = [final_slug.replace("_", " ")]
        md_path.write_text(
            _build_md(slug=final_slug, description=goal, triggers=triggers, code=code,
                       last_updated=now, note=note),
            encoding="utf-8",
        )

        rel = str(md_path.relative_to(SKILLS_ROOT)).replace("\\", "/")
        entry: dict[str, Any] = {
            "name": final_slug,
            "category": "autonomous",
            "file_path": rel,
            "description": goal,
            "triggers": triggers,
            "version": "1.0",
            "last_updated": now,
            "verified": vres.verified,
            "verification": vres.as_dict(),
        }
        if extra:
            entry["extra"] = extra

        replaced = False
        for i, s in enumerate(skills):
            if s.get("name") == final_slug:
                skills[i] = entry
                replaced = True
                break
        if not replaced:
            skills.append(entry)
        idx["skills"] = skills
        _save_index(idx)
    slug = final_slug

    # Автоматично индексиране за семантично търсене (извън lock-а — пише в
    # отделен embeddings.db, не в skills.json). Никога не чупи запазването.
    try:
        from genesis_agent.embeddings import index_skill
        index_skill(slug, f"{slug.replace('_', ' ')}. {goal}")
    except Exception:
        pass

    return md_path


def load_skill_code(skill_id: str) -> str:
    from genesis_agent.skill_loader import skill_view
    return skill_view(slugify(skill_id))["code"]
