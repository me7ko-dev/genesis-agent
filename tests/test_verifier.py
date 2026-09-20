"""genesis_agent.verifier — печатът, който решава кое умение влиза в
библиотеката. Нула покритие преди този файл, въпреки че `save_skill`,
`autonomous_loop` (тест-гейтът) и `ensemble` (рейтингът на кандидати) вземат
решенията си точно от него.

Тестовете пускат РЕАЛЕН sandbox подпроцес — това е смисълът на модула. Мок на
sandbox-а тук би проверявал само че `if` -ите са на място, а точно връзката
между класификацията и присъдата е онова, което се чупи.
"""
from __future__ import annotations

from genesis_agent.verifier import VerifyResult, verify_skill

CLEAN = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    assert add(2, 2) == 4\n"
    "    print('OK')\n"
)


class TestStaticRejections:
    def test_unparsable_code_is_syntax_err(self) -> None:
        res = verify_skill("def broken(:\n    pass\n")
        assert not res.verified
        assert res.method == "syntax_err"

    def test_a_placeholder_key_is_rejected_before_it_ever_runs(self) -> None:
        """Код с `YOUR_API_KEY` не може да работи при никого — приемането му
        пълни библиотеката с умения, които гърмят чак при употреба."""
        res = verify_skill("api_key = 'YOUR_API_KEY'\nprint('OK')\n")
        assert not res.verified
        assert res.method == "placeholder"


class TestSelfTestStamp:
    def test_a_real_assert_plus_printed_ok_is_the_only_full_pass(self) -> None:
        res = verify_skill(CLEAN)
        assert res.verified
        assert res.method == "self_test_passed"

    def test_a_tautological_assert_is_not_a_self_test(self) -> None:
        """`assert True` минава винаги и не проверява нищо. Слаб модел,
        инструктиран „винаги слагай self-test“, свършва точно тук."""
        res = verify_skill("assert True\nassert 1 == 1\nprint('OK')\n")
        assert res.verified
        assert res.method == "runs_clean", "тавтологията не бива да дава печат"

    def test_asserts_without_printed_ok_are_not_a_full_pass(self) -> None:
        res = verify_skill("x = 1 + 1\nassert x == 2\n")
        assert res.verified
        assert res.method == "runs_clean"

    def test_code_that_raises_is_a_runtime_error(self) -> None:
        res = verify_skill("raise ValueError('нарочно')\n")
        assert not res.verified
        assert res.method == "runtime_err"
        assert "ValueError" in res.detail


class TestUnprovenIsNotTheSameAsDangerous:
    """Двете излизаха с една и съща дума. Измерено преди поправката: умение,
    което вика `git rev-parse` през subprocess, получаваше същата присъда
    („blocked“, в документацията — „опасен“), както `os.system('rm -rf /')`.

    Разликата решава какво се казва на модела после. При смесването
    единственият начин да изпълни обратната връзка („няма self-test, добави
    assert-и“) беше да махне подпроцеса — тоест да обезсмисли умението — или
    да си измисли тест. А това са точно уменията, които вършат истинска
    работа: пускат тестове, викат git, четат изхода на инструмент.
    """

    GIT = (
        "import subprocess\n"
        "\n"
        "def current_branch() -> str:\n"
        "    out = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],\n"
        "                         capture_output=True, text=True)\n"
        "    return out.stdout.strip()\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    assert isinstance(current_branch(), str)\n"
        "    print('OK')\n"
    )

    def test_a_skill_that_shells_out_is_unproven_not_dangerous(self) -> None:
        res = verify_skill(self.GIT)
        assert not res.verified, "гейтът остава строг — непроверено не се приема"
        assert res.method == "needs_confirmation"
        assert "SANDBOX DENIED" in res.detail

    def test_a_catastrophic_skill_is_still_plainly_blocked(self) -> None:
        res = verify_skill("import os\nos.system('rm -rf /')\n")
        assert not res.verified
        assert res.method == "blocked"
        assert "SANDBOX BLOCKED" in res.detail

    def test_the_two_verdicts_never_collapse_into_one(self) -> None:
        shells_out = verify_skill(self.GIT)
        catastrophic = verify_skill("import os\nos.system('rm -rf /')\n")
        assert shells_out.method != catastrophic.method
        assert not shells_out.verified and not catastrophic.verified


class TestResultShape:
    def test_as_dict_carries_the_verdict_and_caps_the_detail(self) -> None:
        res = VerifyResult(False, "runtime_err", "щ" * 900)
        d = res.as_dict()
        assert d["verified"] is False
        assert d["method"] == "runtime_err"
        assert len(d["detail"]) == 500, "индексът не бива да поема цял traceback"


class TestWhatCountsAsASelfTest:
    """`self_test_passed` е присъдата, с която умение влиза в библиотеката като
    доказано и оттам се преизползва от бъдещи мисии през RAG. Две дупки я
    раздаваха на код, който не проверява нищо."""

    def test_a_main_block_that_only_prints_is_not_a_self_test(self) -> None:
        """Условието беше `"__main__" in code` — низ, не структура. Точно това
        пише слаб модел, инструктиран „винаги слагай self-test и печатай OK":
        блокът е налице, проверка няма."""
        res = verify_skill(
            'def add(a, b):\n    return a + b\n'
            'if __name__ == "__main__":\n    print("OK")\n'
        )
        assert res.method == "runs_clean"

    def test_the_word_main_in_a_comment_does_not_make_a_self_test(self) -> None:
        res = verify_skill(
            'def add(a, b):\n    return a + b\n'
            '# no __main__ block here\nprint("OK")\n'
        )
        assert res.method == "runs_clean"

    def test_a_real_assert_is_a_self_test(self) -> None:
        res = verify_skill(
            'def add(a, b):\n    return a + b\nassert add(2, 3) == 5\nprint("OK")\n'
        )
        assert res.method == "self_test_passed"

    def test_a_conditional_raise_is_a_self_test_too(self) -> None:
        """Легитимна алтернатива на assert — и причината `__main__` някога да
        се брои изобщо."""
        res = verify_skill(
            'def add(a, b):\n    return a + b\n'
            'if add(2, 3) != 5:\n    raise ValueError("грешно")\nprint("OK")\n'
        )
        assert res.method == "self_test_passed"

    def test_a_tautological_assert_is_still_not_a_self_test(self) -> None:
        res = verify_skill('def add(a, b):\n    return a + b\nassert True\nprint("OK")\n')
        assert res.method == "runs_clean"


class TestWhatCountsAsPrintingOK:
    """`"OK" in stdout` е проверка за ПОДНИЗ, а "OK" се съдържа в BROKEN,
    TOKEN, LOOKUP — и в самото "NOT OK"."""

    def test_a_skill_announcing_that_it_is_broken_does_not_pass(self) -> None:
        res = verify_skill('x = 1\nassert x == 1\nprint("BROKEN")\n')
        assert res.method == "runs_clean"

    def test_not_ok_does_not_pass(self) -> None:
        res = verify_skill('x = 1\nassert x == 1\nprint("NOT OK")\n')
        assert res.method == "runs_clean"

    def test_a_token_in_the_output_does_not_pass(self) -> None:
        res = verify_skill('x = 1\nassert x == 1\nprint("TOKEN refreshed")\n')
        assert res.method == "runs_clean"

    def test_a_bare_ok_line_passes(self) -> None:
        res = verify_skill('x = 1\nassert x == 1\nprint("OK")\n')
        assert res.method == "self_test_passed"

    def test_ok_followed_by_detail_passes(self) -> None:
        res = verify_skill('x = 1\nassert x == 1\nprint("OK: 3 проверки")\n')
        assert res.method == "self_test_passed"

    def test_ok_on_a_later_line_among_other_output_passes(self) -> None:
        res = verify_skill(
            'x = 1\nassert x == 1\nprint("работя...")\nprint("OK")\n'
        )
        assert res.method == "self_test_passed"
