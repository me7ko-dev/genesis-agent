#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бърз статус на библиотеката с умения (за десктоп shortcut-а)."""
import json
from collections import Counter
from pathlib import Path

SKILLS_JSON = Path(__file__).resolve().parent.parent / "genesis_agent" / "skills" / "skills.json"


def main() -> None:
    data = json.loads(SKILLS_JSON.read_text(encoding="utf-8"))
    skills = data["skills"]
    methods = Counter(s.get("verification", {}).get("method", "?") for s in skills)
    verified = sum(1 for s in skills if s.get("verified"))

    print("=" * 40)
    print("        GENESIS — СТАТУС НА УМЕНИЯТА")
    print("=" * 40)
    print(f"  Общо умения:            {len(skills)}")
    print(f"  ✅ Verified (работят):   {verified}")
    print(f"  ⬜ Непроверени:          {len(skills) - verified}")
    print("\n  Разбивка по статус:")
    labels = {
        "self_test_passed": "✅ със self-test",
        "runs_clean": "✅ чисто пускане",
        "needs_deps": "📦 искат пакети",
        "blocked": "🛡️ опасни (блокирани)",
        "runtime_err": "❌ грешка",
        "deep_verified": "✅ deep-verified",
        "deep_failed": "❌ deep-failed",
    }
    for method, count in methods.most_common():
        print(f"    {labels.get(method, method):24}: {count}")
    print("\n" + "=" * 40)


if __name__ == "__main__":
    main()
