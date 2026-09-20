"""Стрес/инвариантни тестове на частите, при които тиха грешка е скъпа.

Генераторите са детерминистични (фиксиран seed, stdlib `random`) вместо
hypothesis: проектът държи зависимостите минимални нарочно, а възпроизводим
seed дава същата полза без нова зависимост в CI. Всеки цикъл принтира seed-а
в assert съобщението, за да може провалът да се възпроизведе точно.

Какво пазят тези тестове, по ред на цената при провал:

  1. sandbox класификацията — мутация на опасна команда, която се промъкне,
     е унищожени данни. Тук провалът е необратим, затова е първи.
  2. budget — tool_call_id, изгубен при свиване, чупи цялата заявка към
     доставчика (OpenAI wire формат изисква всеки tool резултат да сочи към
     своето извикване). Тихо е: отговорът просто идва грешен.
  3. claim_check — вика се на всеки финален отговор; изключение там би
     съборило цикъла на самия финал, след като цялата работа е свършена.
"""
from __future__ import annotations

import random
import string

from genesis_agent import budget, claim_check, sandbox

SEED = 20260919


# ─────────────────────────────────────────────────────────────────────────────
# 1. Sandbox: опасното си остава опасно, както и да е написано
# ─────────────────────────────────────────────────────────────────────────────

class TestDangerousCommandsStayBlocked:
    """`rm -rf /` не е един низ, а семейство. Мутациите отдолу са тези, които
    реално се срещат в генериран код — допълнителни интервали, кавички,
    подразбиращи се пътища. Пропусната мутация тук означава изтрита машина."""

    _MUTATIONS = (
        "rm -rf /",
        "rm  -rf  /",
        "rm -rf  /",
        "rm -fr /",
        "rm -r -f /",
        "rm --recursive --force /",
        "rm -rf '/'",
        'rm -rf "/"',
        "rm -rf /*",
        "sudo rm -rf /",
        "rm -rf / --no-preserve-root",
        "\trm -rf /",
        "rm -rf /  ",
    )

    def test_every_mutation_of_rm_rf_root_is_refused(self) -> None:
        for cmd in self._MUTATIONS:
            verdict = sandbox.assess_command(cmd)
            allowed, _reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert not allowed, f"ПРОПУСНАТА опасна команда: {cmd!r}"

    def test_allow_mode_cannot_unlock_them(self) -> None:
        """`mode="allow"` е за CONFIRM-ниво операции. Катастрофалното е
        безусловно — иначе "allow" би значело "изтрий ми диска"."""
        for cmd in self._MUTATIONS:
            for mode in ("allow", "deny", "interactive"):
                allowed, _ = sandbox._decide(
                    cmd, sandbox.assess_command(cmd), sandbox.SandboxPolicy(mode=mode))
                assert not allowed, f"{cmd!r} мина при mode={mode}"

    def test_harmless_commands_are_not_swept_up(self) -> None:
        """Обратната грешка: гейт, който отказва всичко, е гейт, който
        операторът изключва."""
        # `echo rm -rf /` НЕ е в списъка нарочно: класификаторът го блокира,
        # и това е защитимо — той не парсва shell, а такъв ред е един `| sh`
        # от това да е истинско. Консервативното решение тук е правилното.
        for cmd in ("ls -la", "git status", "python3 -c 'print(1)'",
                    "pytest -q", "cat README.md", "rm -rf ./build",
                    "grep -rn foo ."):
            verdict = sandbox.assess_command(cmd)
            allowed, reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert allowed, f"безобидна команда беше отказана: {cmd!r} ({reason})"

    def test_classification_never_raises_on_random_input(self) -> None:
        rnd = random.Random(SEED)
        alphabet = string.printable + "рм -рф /"
        for _ in range(3000):
            cmd = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
            try:
                sandbox.assess_command(cmd)
            except Exception as e:  # pragma: no cover
                raise AssertionError(
                    f"assess_command хвърли за {cmd!r} (seed={SEED}): {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# 2. Budget: свиването пази структурата, каквато и да е историята
# ─────────────────────────────────────────────────────────────────────────────

def _random_history(rnd: random.Random, n: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "SYSTEM " * rnd.randint(1, 50)}]
    for i in range(n):
        kind = rnd.choice(("tool", "assistant", "user", "text_result"))
        body = "".join(rnd.choice(string.printable)
                       for _ in range(rnd.randint(0, 4000)))
        if kind == "tool":
            msgs.append({"role": "tool", "tool_call_id": f"call_{i}",
                         "name": rnd.choice(("RUN_CMD", "READ_FILE", "GLOB")),
                         "content": body})
        elif kind == "text_result":
            msgs.append({"role": "system", "content": "[Резултат]:\n" + body})
        else:
            msgs.append({"role": kind, "content": body})
    return msgs


class TestBudgetInvariants:
    def test_tool_call_ids_and_roles_always_survive(self) -> None:
        """Изгубен tool_call_id прави цялата заявка невалидна за доставчика,
        и се проявява като грешен отговор, не като явна грешка."""
        rnd = random.Random(SEED)
        for _ in range(300):
            msgs = _random_history(rnd, rnd.randint(0, 12))
            out = budget.budget_history(msgs, fresh=rnd.randint(0, 3),
                                        stale_limit=rnd.choice((0, 200, 2000)))
            assert len(out) == len(msgs), f"брой съобщения се промени (seed={SEED})"
            for before, after in zip(msgs, out):
                assert after["role"] == before["role"]
                assert after.get("tool_call_id") == before.get("tool_call_id")
                assert after.get("name") == before.get("name")

    def test_the_input_is_never_mutated(self) -> None:
        rnd = random.Random(SEED + 1)
        for _ in range(200):
            msgs = _random_history(rnd, rnd.randint(0, 10))
            snapshot = [dict(m) for m in msgs]
            budget.budget_history(msgs, fresh=1, stale_limit=500)
            assert msgs == snapshot, f"историята на извикващия беше пипната (seed={SEED})"

    def test_applying_it_twice_changes_nothing_further(self) -> None:
        """Brain.complete() може да бъде извикан пак със същата история
        (ретрай, друг доставчик). Второ прилагане трябва да е no-op, иначе
        съдържанието се рони при всеки опит."""
        rnd = random.Random(SEED + 2)
        for _ in range(200):
            msgs = _random_history(rnd, rnd.randint(2, 10))
            once = budget.budget_history(msgs, fresh=1, stale_limit=800)
            twice = budget.budget_history(once, fresh=1, stale_limit=800)
            assert [m["content"] for m in twice] == [m["content"] for m in once], (
                f"второто прилагане промени съдържанието (seed={SEED})")

    def test_clip_never_exceeds_its_budget(self) -> None:
        rnd = random.Random(SEED + 3)
        for _ in range(2000):
            limit = rnd.randint(1, 3000)
            text = "".join(rnd.choice(string.printable)
                          for _ in range(rnd.randint(0, 9000)))
            out = budget.clip_for_context(text, limit=limit)
            # Единственият инвариант, който наистина трябва да държи: свиването
            # никога не прави низа по-дълъг. Първата версия на този тест
            # изискваше out < text ВИНАГИ, щом text > limit, и точно така хвана
            # реален дефект — при текст малко над тавана бележката за отрязване
            # (~90 символа) правеше резултата ПО-ДЪЛЪГ от оригинала. Сега кодът
            # в такъв случай връща оригинала, което е правилното поведение.
            assert len(out) <= len(text), f"свиването удължи низа (seed={SEED})"
            if len(text) <= limit:
                assert out == text, "нищо под тавана не бива да се пипа"
            elif out != text:
                assert len(out) <= limit + 200, f"надхвърли тавана (seed={SEED})"
                assert len(out) < len(text)

    def test_clip_keeps_the_head_and_the_tail(self) -> None:
        """Краят носи изхода, който решава нещо (traceback, verdict). Ако се
        изгуби, моделът гадае — точно затова не е обикновено text[:limit]."""
        rnd = random.Random(SEED + 4)
        for _ in range(500):
            head, tail = "HEAD-MARK", "TAIL-MARK"
            middle = "m" * rnd.randint(2000, 8000)
            out = budget.clip_for_context(head + middle + tail, limit=500)
            assert out.startswith(head)
            assert out.endswith(tail)

    def test_clip_never_raises(self) -> None:
        rnd = random.Random(SEED + 5)
        for _ in range(1000):
            text = "".join(rnd.choice(string.printable + "…\n\t")
                           for _ in range(rnd.randint(0, 500)))
            for limit in (-5, 0, 1, 2, 3, 17, 500):
                budget.clip_for_context(text, limit=limit)


# ─────────────────────────────────────────────────────────────────────────────
# 3. claim_check: вика се на финала — не бива да пада точно там
# ─────────────────────────────────────────────────────────────────────────────

class TestClaimCheckRobustness:
    def test_no_input_can_make_it_raise(self) -> None:
        rnd = random.Random(SEED + 6)
        verbs = ["инсталирах", "преместих", "created", "I've installed", "готово е"]
        for _ in range(2000):
            text = " ".join(rnd.choice(verbs + list(string.printable))
                            for _ in range(rnd.randint(0, 40)))
            executed = [(rnd.choice(("RUN_CMD", "", "WRITE_FILE", "???")),
                         "".join(rnd.choice(string.printable)
                                 for _ in range(rnd.randint(0, 30))))
                        for _ in range(rnd.randint(0, 5))]
            try:
                found = claim_check.unsupported_claims(text, executed)
                claim_check.nudge_text(found)
            except Exception as e:  # pragma: no cover
                raise AssertionError(
                    f"хвърли за text={text[:80]!r} executed={executed!r} "
                    f"(seed={SEED}): {e}") from e

    def test_a_matching_command_always_clears_its_claim(self) -> None:
        """Инвариант срещу фалшива тревога: щом подходящата команда е текла,
        твърдението не бива да се оспорва — независимо какъв шум има наоколо."""
        rnd = random.Random(SEED + 7)
        pairs = (
            ("Инсталирах пакета.", "pip install requests"),
            ("Преместих файловете.", "mv a b"),
            ("Пуснах тестовете.", "pytest -q"),
        )
        for _ in range(300):
            claim, cmd = rnd.choice(pairs)
            noise = "".join(rnd.choice(string.ascii_letters + " ")
                            for _ in range(rnd.randint(0, 200)))
            found = claim_check.unsupported_claims(
                noise + " " + claim + " " + noise, [("RUN_CMD", cmd)])
            assert not found, f"фалшива тревога за {claim!r} + {cmd!r} (seed={SEED})"

    def test_empty_and_degenerate_inputs(self) -> None:
        assert claim_check.unsupported_claims("", []) == []
        assert claim_check.unsupported_claims("   ", [("RUN_CMD", "")]) == []
        assert claim_check.unsupported_claims("няма твърдения тук", []) == []


class TestStructuralRmDetection:
    """Регексите изброяват форми; формите са неограничено много. Тези случаи
    минаваха покрай тях, докато проверката не стана структурна (токени,
    флагове, пътища) вместо изброяване на изрази.

    Най-острият е първият: `--no-preserve-root` е флагът, който GNU rm
    ИЗИСКВА, за да изтрие `/` наистина — тоест единствената форма, която
    реално работи, минаваше като SAFE и се изпълняваше без да пита никого.
    """

    _MISSED_BEFORE = (
        "rm --no-preserve-root -rf /",
        "rm -rf --no-preserve-root /",
        "rm -rf ~/",
        "rm -rf $HOME/",
        "rm -rf ${HOME}",
        "rm -rf /home/user/",
        "rm -rf /home/ivan",
        "rm -rf /Users/ivan",
        "doas rm -rf /",
        "echo hi; rm -rf /",
        "rm -R -f ~",
        "rm -rf /etc",
    )

    def test_each_of_them_is_blocked_in_every_mode(self) -> None:
        for cmd in self._MISSED_BEFORE:
            verdict = sandbox.assess_command(cmd)
            for mode in ("allow", "deny", "interactive"):
                allowed, _ = sandbox._decide(
                    cmd, verdict, sandbox.SandboxPolicy(mode=mode))
                assert not allowed, f"ПРОПУСНАТА: {cmd!r} при mode={mode}"

    def test_deleting_a_project_inside_home_is_still_allowed(self) -> None:
        """Обратната грешка е също толкова лоша: гейт, който отказва
        `rm -rf ~/projects/old`, е гейт, който операторът изключва."""
        for cmd in ("rm -rf ~/projects/old", "rm -rf /home/user/projects",
                    "rm -rf /home/user/.cache", "rm -rf ./build",
                    "rm -rf /tmp/mydir", "rm --recursive ./node_modules"):
            verdict = sandbox.assess_command(cmd)
            allowed, reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert allowed, f"фалшива тревога за {cmd!r}: {reason}"


class TestTargetsDecidedAtRuntime:
    """Втората половина на същия проблем. Структурната проверка гледа целта,
    но целта невинаги е в текста: `$(echo /)` се решава от шела, а
    `echo / | xargs rm -rf` изобщо няма цел на командния ред — тя идва по
    тръбата. И двете минаваха като SAFE, тоест се изпълняваха автоматично
    дори в `deny` режим, защото нито един регекс не виждаше `rm` с корен.
    """

    _MISSED_BEFORE = (
        # стойността се решава при изпълнение
        "rm --no-preserve-root -rf $(echo /)",
        'rm -rf "$(echo /)"',
        "rm -rf `echo /`",
        "rm -rf $TARGET",
        "rm -rf $HOME/*",
        "rm -rf ~user",
        "rm -rf /h*",
        # целите идват по тръбата
        "echo / | xargs rm -rf",
        "echo / | xargs -0 rm -rf",
        "echo / | xargs -n 1 rm -rf",
        "echo / | xargs -I {} rm -rf {}",
        "echo / | xargs -i rm -rf {}",
        "echo $HOME | xargs rm -rf",
        "find / | xargs sudo rm -rf",
        "printf /home/user | xargs rm -rf",
        # обвивки и продължение на реда
        "env FOO=1 sudo rm --recursive --force /",
        "timeout 5 rm -rf /",
        "sudo -u root rm -rf /",
        "sudo -n rm -rf /",
        "nice -n 10 rm -rf /",
        "env -u PATH rm -rf /",
        "rm -rf \\\n  /",
    )

    def test_each_of_them_is_blocked_in_every_mode(self) -> None:
        for cmd in self._MISSED_BEFORE:
            verdict = sandbox.assess_command(cmd)
            for mode in ("allow", "deny", "interactive"):
                allowed, _ = sandbox._decide(
                    cmd, verdict, sandbox.SandboxPolicy(mode=mode))
                assert not allowed, f"ПРОПУСНАТА: {cmd!r} при mode={mode}"

    def test_ordinary_cleanup_through_xargs_still_runs(self) -> None:
        """Обратната грешка е по-скъпа тук, отколкото изглежда: `find ... |
        xargs rm -rf` е начинът, по който се чисти репо. Гейт, който отказва и
        него, спира нормалната работа — и операторът го изключва изцяло."""
        for cmd in (
            "find . -name '__pycache__' | xargs rm -rf",
            "find . -name '*.pyc' -print0 | xargs -0 rm -rf",
            "find ./build -type d | xargs -I {} rm -rf {}",
            "git ls-files --others | xargs rm -rf",
            "ls /tmp | xargs rm -rf",
        ):
            verdict = sandbox.assess_command(cmd)
            allowed, reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert allowed, f"фалшива тревога за {cmd!r}: {reason}"

    def test_a_command_word_is_never_read_as_the_rm_behind_a_wrapper(self) -> None:
        """Прескачането на флаговете на обвивката спира на първата истинска
        дума-команда. Иначе `sudo grep -r rm -rf /etc` — търсене на текста
        „rm“ — се четеше като rm върху /etc и се отказваше."""
        for cmd in ("sudo grep -r rm -rf /etc", "grep -rn rm /etc/hosts",
                    "sudo -u root rm -rf ./build"):
            verdict = sandbox.assess_command(cmd)
            allowed, reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert allowed, f"фалшива тревога за {cmd!r}: {reason}"

    def test_a_literal_name_after_the_expansion_is_what_limits_the_damage(self) -> None:
        """Каквото и да е `$X`, `$X/artifacts` трие нещо на име artifacts.
        Затова се гледа опашката СЛЕД разгъването, а не самото разгъване —
        иначе всяко `rm -rf $BUILD_DIR/out` става невъзможно."""
        for cmd in ("rm -rf $BUILD_DIR/artifacts", "rm -rf ${TMPDIR}/cache",
                    "rm -rf $(pwd)/build", "rm -rf $HOME/projects/old"):
            verdict = sandbox.assess_command(cmd)
            allowed, reason = sandbox._decide(
                cmd, verdict, sandbox.SandboxPolicy(mode="allow"))
            assert allowed, f"фалшива тревога за {cmd!r}: {reason}"

    def test_the_pipeline_check_reads_only_literals(self) -> None:
        """Съзнателна граница, записана като тест, за да не се чете като
        пропуск: целта на `... | xargs rm -rf` по дефиниция се изчислява при
        изпълнение. Литерален критичен корен по-рано в тръбата се хваща;
        изчислен — не. Ако някой ден това се промени, тестът ще падне и ще
        се вземе съзнателно решение, вместо да се открие след загуба."""
        computed = "ls $SOMEWHERE | xargs rm -rf"
        verdict = sandbox.assess_command(computed)
        assert verdict.level != sandbox.RiskLevel.BLOCKED
        assert verdict.level == sandbox.RiskLevel.CONFIRM, (
            "недоказуемото минава, но не безшумно — остава за потвърждение")
