"""Инварианти на genesis_agent.executor — през него минава ВСЯКО изпълнение на
код, произведен от модел.

tests/test_executor.py вече покрива DNA гейта, отчитането на успех/провал и
това, че съкращаването пази опашката. Тук са две неща, които той не проверява:

  1. ГРАНИЦАТА ДЪРЖИ ЛИ НАИСТИНА. test_sandbox.py проверява, че `_build_env`
     връща речник без тайните — това е тест на функцията. Тук кодът РЕАЛНО се
     изпълнява и сам се опитва да прочете ключа, тоест се проверява цялата
     верига. Разликата има значение: обещанието в docstring-а на executor е
     "no API-key leak", а не "функцията връща правилен речник".
  2. Обратната връзка към модела при произволен вход — тя е единственото,
     върху което самокорекцията стъпва, и празна или подвеждаща обратна
     връзка проваля рунда тихо.

Изпълненията са истински subprocess-и, затова всяко покрива отделно свойство,
вместо да се повтарят за обем.
"""
from __future__ import annotations

import random
import string

import pytest

from genesis_agent import executor
from genesis_agent.executor import ExecResult, format_failure_for_brain

SEED = 20260920


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "SANDBOX_DIR", tmp_path / "sbx")
    monkeypatch.delenv("GENESIS_RED_ZONE_TOKEN", raising=False)
    monkeypatch.delenv("GENESIS_RED_ZONE_SECRET", raising=False)


class TestSecretsDoNotReachGeneratedCode:
    """Обещанието е в docstring-а на run_python_subprocess: минимална среда,
    без изтичане на ключове. Проверява се чрез РЕАЛНО изпълнение, което се
    опитва да ги прочете — не чрез инспекция на речника."""

    def test_an_api_key_in_the_parent_env_is_invisible_to_the_child(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("TOTALLY_SECRET_API_KEY", "sk-must-not-leak-12345")
        res = executor.run_python_subprocess(
            "import os\n"
            "print('SEEN:' + os.environ.get('TOTALLY_SECRET_API_KEY', 'ABSENT'))\n"
        )
        assert res.ok, f"кодът трябва РЕАЛНО да се е изпълнил: {res.stderr[:300]}"
        combined = res.stdout + res.stderr
        assert "sk-must-not-leak-12345" not in combined, "ключът изтече в детето"
        # Без този ред тестът минава и когато нищо не се е изпълнило — тогава
        # "ключът не изтече" не значи нищо.
        assert "SEEN:ABSENT" in res.stdout, res.stdout

    def test_the_whole_parent_environment_is_not_inherited(self, monkeypatch) -> None:
        """Не само познатите имена: ако средата се наследява цяла, всеки бъдещ
        ключ изтича, без никой да добавя име в списък."""
        monkeypatch.setenv("GENESIS_TEST_CANARY", "canary-value-98765")
        res = executor.run_python_subprocess(
            "import os\nprint('|'.join(sorted(os.environ)))\nprint('N=', len(os.environ))\n"
        )
        assert res.ok, f"кодът трябва РЕАЛНО да се е изпълнил: {res.stderr[:300]}"
        assert "GENESIS_TEST_CANARY" not in res.stdout
        assert "canary-value-98765" not in res.stdout
        # Средата е шепа променливи, не наследеният списък на родителя —
        # това е разликата между whitelist и "махнахме познатите имена".
        assert "N= " in res.stdout
        count = int(res.stdout.split("N= ")[1].split()[0])
        assert count < 15, f"детето вижда {count} променливи — прилича на наследена среда"


class TestExecutionNeverRaises:
    def test_syntactically_broken_code_is_reported_not_raised(self) -> None:
        res = executor.run_python_subprocess("def f(:\n    pass\n")
        assert isinstance(res, ExecResult)
        assert not res.ok
        assert res.stderr.strip(), "провалът трябва да носи причина за модела"

    def test_code_that_exits_nonzero_is_reported(self) -> None:
        res = executor.run_python_subprocess("import sys\nsys.exit(3)\n")
        assert not res.ok

    def test_empty_code_does_not_raise(self) -> None:
        assert isinstance(executor.run_python_subprocess(""), ExecResult)

    def test_inprocess_path_catches_exceptions(self) -> None:
        res = executor.run_python_inprocess("raise ValueError('взрив')")
        assert not res.ok
        assert "взрив" in res.stderr

    def test_inprocess_captures_stdout(self) -> None:
        res = executor.run_python_inprocess("print('здрасти')")
        assert res.ok
        assert "здрасти" in res.stdout


class TestFeedbackToTheModel:
    """format_failure_for_brain е единственото, върху което самокорекцията
    стъпва. Празна или подвеждаща обратна връзка проваля рунда тихо."""

    def test_it_is_never_empty_even_with_nothing_captured(self) -> None:
        out = format_failure_for_brain(ExecResult(False, "", "", None))
        assert out.strip(), "празна обратна връзка не дава на модела какво да поправи"

    def test_the_tail_survives_because_the_exception_lives_there(self) -> None:
        stderr = "шум\n" * 5000 + "ValueError: ИСТИНСКАТА ПРИЧИНА"
        out = format_failure_for_brain(ExecResult(False, "", stderr, 1))
        assert "ИСТИНСКАТА ПРИЧИНА" in out
        assert "truncated" in out

    def test_the_return_code_is_included_when_known(self) -> None:
        out = format_failure_for_brain(ExecResult(False, "", "нещо", 42))
        assert "42" in out

    def test_no_random_result_makes_it_raise(self) -> None:
        rnd = random.Random(SEED)
        alphabet = string.printable + "щъьюя"
        for _ in range(500):
            res = ExecResult(
                ok=rnd.choice((True, False)),
                stdout="".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 200))),
                stderr="".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 200))),
                returncode=rnd.choice((None, 0, 1, -9, 255)),
            )
            try:
                out = format_failure_for_brain(res)
            except Exception as e:  # pragma: no cover
                raise AssertionError(f"хвърли за {res!r} (seed={SEED}): {e}") from e
            assert out.strip(), f"празен изход за {res!r} (seed={SEED})"

    def test_each_block_is_bounded_so_one_round_cannot_flood_the_context(self) -> None:
        """Необрязан traceback можеше да запуши контекста на слаб локален модел
        точно когато ескалацията е на път да се задейства."""
        huge = "x" * 200_000
        out = format_failure_for_brain(ExecResult(False, huge, huge, 1))
        assert len(out) < 2 * executor._MAX_FEEDBACK_CHARS + 500, len(out)
