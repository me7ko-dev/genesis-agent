#!/usr/bin/env python3
"""
genesis_agent.cli — the `genesis` command.

    genesis                 start the terminal chat (default)
    genesis setup           configure API keys
    genesis mission "..."   run one autonomous mission and print the result
    genesis fix PATH "..."  fix a bug in an existing project (checkpoint + tests + diff)
    genesis skills          library status
    genesis models          the model chain; `--refresh` re-scans free models
    genesis update          is there a newer commit on the installed branch
    genesis budget [N]      token usage today + last N days (default 7)
    genesis --version
"""
from __future__ import annotations

import sys

# Едно място за номера — беше на три (pyproject 0.1.0, __init__ 0.2.0, тук
# 0.1.0), а `--version` печаташе третото. Версия, която не съвпада със себе
# си, е по-лоша от липсваща: тя изглежда като отговор.
from genesis_agent import __version__

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
  --maxcoding        най-силните БЕЗПЛАТНИ модели за код (по-бавно, повече квота,
                     нула разход) — същото като /maxcoding в чата
  --max              платените модели отпред (иска ANTHROPIC_API_KEY / OPENAI_API_KEY /
                     DEEPSEEK_API_KEY); без такъв ключ пада на --maxcoding
  --test "команда"   как се пускат тестовете (по подразбиране се разпознава сам)
  --rounds N         таван на рундовете (по подразбиране 8)
  --no-diff          не печатай диффа накрая

Преди първата промяна се прави снимка на проекта. `--revert` я връща обратно.
"""


def _models(args: list[str]) -> int:
    """`genesis models [--refresh]` — какво реално ще бъде извикано и в какъв ред."""
    from genesis_agent import free_models

    if "--refresh" in args:
        count, message = free_models.refresh()
        print(message)
        if not count:
            return 1

    from genesis_agent.agent_core import MIN_SIZE_B
    from genesis_agent.brain import _load_chain
    chain = _load_chain()

    # Броят се РЕАЛНИТЕ членове на веригата, не се вади дължината на кеша:
    # `_load_chain` дедуплицира срещу config.yaml и филтрира по познати
    # доставчици, така че изваждането подценяваше ръчните записи с всеки
    # застъпен модел — а при силно застъпване стигаше до отрицателно число,
    # което `max(..., 0)` показваше като „≈0 ръчно проверени" до верига,
    # която е почти изцяло ръчна.
    discovered = {(m.get("provider"), m.get("model")) for m in free_models.cached()}
    auto = sum(1 for c in chain if (c["provider"], c["model"]) in discovered)
    print(f"\nВерига: {len(chain)} модела "
          f"({len(chain) - auto} ръчно проверени + {auto} автоматично открити)\n")

    unsized = 0
    for i, c in enumerate(chain, 1):
        size = f"{c['size_b']:g}B" if c["size_b"] else "?"
        tools = "tools" if c["supports_tools"] else "text-tags"
        # Модел без размер в името се изхвърля от всеки контекст, който
        # филтрира по размер (чат: ≥32B). Без този знак `genesis models`
        # изброява модели, които работещият агент никога не вика.
        skipped = "" if c["size_b"] >= MIN_SIZE_B else "  ← не и в чат"
        if not c["size_b"]:
            unsized += 1
        print(f"  {i:>2}. {c['provider']:<13} {c['model']:<52} {size:>6}  {tools}{skipped}")

    if unsized:
        print(f"\n{unsized} модела не обявяват размер в името си и затова не влизат "
              f"в контекстите с праг (чат иска ≥{MIN_SIZE_B:g}B). Мисиите и "
              "по-ниските прагове ги ползват.")

    age = free_models.cache_age_days()
    if age is None:
        print("\nБезплатните модели не са сканирани още — пусни `genesis models --refresh`.")
    elif age > 7:
        print(f"\nСписъкът с безплатни е отпреди {age:.0f} дни — той се мени всеки месец; "
              "`genesis models --refresh` го обновява.")
    return 0


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
        elif a in ("--maxcoding", "--coding"):
            quality = "coding"
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
    from genesis_agent.paths import ensure_utf8_streams
    ensure_utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "chat"

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if cmd in ("-V", "--version", "version"):
        from genesis_agent.version_info import describe
        print(describe(__version__))
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

    if cmd == "skills":
        from genesis_agent.skill_loader import format_skill_list
        print(format_skill_list())
        return 0

    if cmd == "update":
        # Само ПИТА. Обновяването не се пуска оттук нарочно: pipx подменя
        # точно този изпълним файл, а на Windows работещ .exe не може да бъде
        # заменен — командата щеше да се проваля най-често там, където е
        # най-нужна. Затова печатаме реда, който се пуска в чист терминал.
        from genesis_agent.version_info import update_report
        print(update_report(version=__version__))
        return 0

    if cmd == "budget":
        from genesis_agent import budget
        days = 7
        if len(argv) > 1:
            try:
                days = max(1, int(argv[1]))
            except ValueError:
                print(f"`genesis budget` иска число дни, не {argv[1]!r}")
                return 2
        print(budget.format_report(budget.today_totals(), title="Днес"))
        print()
        print(budget.format_report(budget.range_totals(days), title=f"Последните {days} дни"))
        return 0

    if cmd == "models":
        return _models(argv[1:])

    if cmd == "fix":
        return _fix(argv[1:])

    if cmd in ("gui", "voice"):
        # Махнати на 2026-09-23 — Genesis е само терминален. Изрично съобщение,
        # защото „Непозната команда" би звучало като счупена инсталация.
        print(f"`genesis {cmd}` е махнат — Genesis вече е само терминален. Пусни `genesis`.")
        return 2

    if cmd == "chat":
        return _chat()

    print(f"Непозната команда: {cmd}\n")
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
