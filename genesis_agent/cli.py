#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.cli — the `genesis` command.

    genesis                 start the terminal chat (default)
    genesis setup           configure API keys
    genesis mission "..."   run one autonomous mission and print the result
    genesis gui             GTK chat window
    genesis voice           voice frontend
    genesis discord         Discord bot
    genesis skills          library status
    genesis --version
"""
from __future__ import annotations

import sys

__version__ = "0.1.0"

USAGE = __doc__.split("    genesis", 1)[0].strip() + "\n\n" + "\n".join(
    line for line in (__doc__ or "").splitlines() if line.startswith("    genesis")
)


def _chat() -> int:
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import runpy
    runpy.run_path(str(root / "genesis_terminal_agent.py"), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "chat"

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if cmd in ("-V", "--version", "version"):
        print(f"genesis-agent {__version__}")
        return 0

    if cmd == "setup":
        from genesis_agent.setup_wizard import run
        return run()

    if cmd == "mission":
        goal = " ".join(argv[1:]).strip()
        if not goal:
            print('Употреба: genesis mission "напиши функция, която ..."')
            return 2
        from genesis_agent.autonomous_loop import run_autonomous_loop
        out = run_autonomous_loop(goal)
        print(f"\n{'✅ успех' if out.success else '❌ провал'} — {out.rounds} рунда")
        if getattr(out, "skill_path", ""):
            print(f"умение: {out.skill_path}")
        return 0 if out.success else 1

    if cmd == "discord":
        from genesis_agent import discord_bot
        return discord_bot.main() or 0

    if cmd == "skills":
        from genesis_agent.skill_loader import load_skills_index
        index = load_skills_index()
        verified = sum(1 for s in index.values() if "verified" in str(s.get("status", "")))
        print(f"{len(index)} умения, {verified} verified")
        return 0

    if cmd in ("gui", "voice"):
        from pathlib import Path
        import runpy
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root))
        script = "genesis_gui.py" if cmd == "gui" else "genesis_jarvis.py"
        runpy.run_path(str(root / "gui" / script), run_name="__main__")
        return 0

    if cmd == "chat":
        return _chat()

    print(f"Непозната команда: {cmd}\n")
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
