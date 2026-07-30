#!/usr/bin/env python3
"""
genesis_agent.cli — the `genesis` command.

    genesis                 start the terminal chat (default)
    genesis setup           configure API keys
    genesis mission "..."   run one autonomous mission and print the result
    genesis fix PATH "..."  fix a bug in an existing project (checkpoint + tests + diff)
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


def _project_root():
    """The directory the top-level modules import from — checkout or install."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def _chat() -> int:
    # Run as a module, not from a file path: an installed copy has the module
    # importable but no .py sitting next to the package in every layout.
    sys.path.insert(0, str(_project_root()))
    import runpy
    runpy.run_module("genesis_terminal_agent", run_name="__main__", alter_sys=True)
    return 0


_FIX_USAGE = """Употреба:
  genesis fix <път-до-проекта> "описание на бъга" [опции]
  genesis fix --revert <път-до-проекта>

Опции:
  --max              ползвай платените модели (иска ANTHROPIC_API_KEY / OPENAI_API_KEY /
                     DEEPSEEK_API_KEY; без ключ работи безплатната верига)
  --test "команда"   как се пускат тестовете (по подразбиране се разпознава сам)
  --rounds N         таван на рундовете (по подразбиране 8)
  --no-diff          не печатай диффа накрая

Преди първата промяна се прави снимка на проекта. `--revert` я връща обратно.
"""


def _fix(args: list[str]) -> int:
    """`genesis fix` — поправка на бъг в съществуващ проект."""
    if not args or args[0] in ("-h", "--help"):
        print(_FIX_USAGE)
        return 0 if args else 2

    from genesis_agent.repo_agent import format_outcome, repair, restore_checkpoint

    if args[0] == "--revert":
        if len(args) < 2:
            print("Кой проект да върна? `genesis fix --revert <път>`")
            return 2
        print(restore_checkpoint(args[1]))
        return 0

    project, rest = args[0], args[1:]
    task_parts: list[str] = []
    test_command: str | None = None
    max_rounds, quality, show_diff = 8, None, True
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--max":
            quality = "max"
        elif a == "--no-diff":
            show_diff = False
        elif a == "--test" and i + 1 < len(rest):
            i += 1
            test_command = rest[i]
        elif a == "--rounds" and i + 1 < len(rest):
            i += 1
            try:
                max_rounds = max(1, int(rest[i]))
            except ValueError:
                print(f"--rounds иска число, не {rest[i]!r}")
                return 2
        else:
            task_parts.append(a)
        i += 1

    task = " ".join(task_parts).strip()
    if not task:
        print("Липсва описание на бъга.\n")
        print(_FIX_USAGE)
        return 2

    out = repair(project, task, test_command=test_command,
                 max_rounds=max_rounds, quality=quality)
    print("\n" + format_outcome(out, show_diff=show_diff))
    return 0 if out.success else 1


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
        discord_bot.main()
        return 0

    if cmd == "skills":
        from genesis_agent.skill_loader import load_skills_index
        index = load_skills_index()
        # The index records `verified: true`, not a `status` string.
        verified = sum(1 for s in index.values() if s.get("verified"))
        print(f"{len(index)} умения, {verified} verified")
        return 0

    if cmd == "fix":
        return _fix(argv[1:])

    if cmd in ("gui", "voice"):
        import runpy

        from genesis_agent.paths import PACKAGE_DIR
        sys.path.insert(0, str(_project_root()))
        script = PACKAGE_DIR / "gui" / ("genesis_gui.py" if cmd == "gui" else "genesis_jarvis.py")
        if not script.exists():
            print(f"Липсва {script}.\n"
                  f"Този фронтенд се пуска от копие на repo-то:\n"
                  f"  git clone https://github.com/me7ko-dev/genesis-agent")
            return 1
        runpy.run_path(str(script), run_name="__main__")
        return 0

    if cmd == "chat":
        return _chat()

    print(f"Непозната команда: {cmd}\n")
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
