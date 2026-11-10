"""Числата в README срещу кода (2026-11-10): README казваше „Fourteen verified
skills“, а в genesis_agent/skills/ има 37 (33 verified) — първото, което
купувачът проверява, беше грешно."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_skill_count_in_the_readme_is_the_real_one() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skills = json.loads((ROOT / "genesis_agent" / "skills" / "skills.json")
                        .read_text(encoding="utf-8"))["skills"]
    m = re.search(r"(\d+) dependency-free skills ship in `genesis_agent/skills/` "
                  r"\((\d+) verified, (\d+) waiting", readme)
    assert m, "README: изречението за библиотеката с умения е сменено — обнови теста"
    total, verified, waiting = map(int, m.groups())
    assert total == len(skills)
    assert verified == sum(1 for s in skills if s.get("verified"))
    assert waiting == total - verified


def test_the_claude_prices_in_the_readme_are_the_ones_charged() -> None:
    from genesis_agent.anthropic_spend import PRICES
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    opus, sonnet = PRICES["claude-opus-5-5"], PRICES["claude-sonnet-5-5"]
    assert f"Opus 5.5 ${opus[0]:g}/${opus[1]:g}" in readme
    assert f"Sonnet 5.5\n${sonnet[0]:g}/${sonnet[1]:g}" in readme.replace("Sonnet 5.5 $", "Sonnet 5.5\n$")
    # Одит 2026-11-10: Sonnet 5.5 чете от кеша за $0.10, не колкото Opus 5.5.
    assert f"четене от кеша ${opus[2]:.2f} и ${sonnet[2]:.2f}" in readme


def test_the_waitlist_page_does_not_promise_the_files_are_deleted() -> None:
    # 2026-11-10: „среда, която се изтрива след това“ — контейнерът да, но
    # папките на разговорите в /srv/jobs не се трият (cloud/README.md,
    # „Не се трие автоматично“), а изтриване от човека няма.
    page = (ROOT / "site" / "public" / "index.html").read_text(encoding="utf-8")
    assert "изтрива след това." not in page
    assert "Файловете от разговора остават на сървъра" in page
