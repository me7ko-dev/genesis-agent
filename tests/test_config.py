"""genesis_agent.config — внася се от всичко, и се изчислява при внасяне.

Затова грешка тук не е локална: преди поправката една ЗАДАДЕНА, но ПРАЗНА
променлива убиваше целия агент, преди каквото и да е, на всеки вход:

    $ GENESIS_TOOL_ROUNDS= genesis
    ValueError: invalid literal for int() with base 10: ''

„Зададена, но празна" не е измислен случай — `docker run -e GENESIS_TOOL_ROUNDS`
без стойност прави точно това, както и ред в shell профила, от който е махната
стойността. Нула покритие преди този файл.
"""
from __future__ import annotations

import importlib

import pytest

from genesis_agent import config


class TestIntEnv:
    def test_a_missing_variable_gives_the_default(self, monkeypatch) -> None:
        monkeypatch.delenv("GENESIS_TEST_INT", raising=False)
        assert config._int_env("GENESIS_TEST_INT", 7) == 7

    def test_a_set_but_empty_variable_gives_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_TEST_INT", "")
        assert config._int_env("GENESIS_TEST_INT", 7) == 7

    def test_whitespace_only_is_treated_as_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_TEST_INT", "   ")
        assert config._int_env("GENESIS_TEST_INT", 7) == 7

    def test_a_real_value_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_TEST_INT", "42")
        assert config._int_env("GENESIS_TEST_INT", 7) == 42

    def test_surrounding_whitespace_is_tolerated(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_TEST_INT", " 42 ")
        assert config._int_env("GENESIS_TEST_INT", 7) == 42

    def test_a_non_numeric_value_warns_and_falls_back(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("GENESIS_TEST_INT", "осем")
        with caplog.at_level("WARNING"):
            assert config._int_env("GENESIS_TEST_INT", 7) == 7
        assert "GENESIS_TEST_INT" in caplog.text, "тихото връщане към default крие грешката"

    def test_a_value_below_the_minimum_falls_back(self, monkeypatch) -> None:
        """`GENESIS_TOOL_ROUNDS=0` означава цикъл, който не прави нищо —
        по-лошо от подразбиращата се стойност, защото изглежда като работа."""
        monkeypatch.setenv("GENESIS_TEST_INT", "0")
        assert config._int_env("GENESIS_TEST_INT", 25, minimum=1) == 25

    def test_zero_is_allowed_where_it_means_something(self, monkeypatch) -> None:
        """За таваните на символи 0 значи „без таван" — валидна настройка,
        не грешка."""
        monkeypatch.setenv("GENESIS_TEST_INT", "0")
        assert config._int_env("GENESIS_TEST_INT", 40000) == 0


class TestTheModuleStillImportsWithABrokenEnvironment:
    @pytest.fixture
    def _reloaded(self):
        """Връща модула в чисто състояние след теста — стойностите се
        изчисляват при внасяне, така че презареждането е част от теста."""
        yield
        importlib.reload(config)

    @pytest.mark.parametrize("value", ["", "   ", "много", "-1", "3.5"])
    def test_a_broken_tool_round_cap_does_not_kill_the_import(
        self, monkeypatch, _reloaded, value
    ) -> None:
        monkeypatch.setenv("GENESIS_TOOL_ROUNDS", value)
        reloaded = importlib.reload(config)
        assert reloaded.TOOL_ROUND_CAP == 25

    def test_a_valid_override_still_applies(self, monkeypatch, _reloaded) -> None:
        monkeypatch.setenv("GENESIS_TOOL_ROUNDS", "12")
        assert importlib.reload(config).TOOL_ROUND_CAP == 12

    def test_every_numeric_setting_survives_an_empty_value(
        self, monkeypatch, _reloaded
    ) -> None:
        """Един по един не е достатъчно: достатъчна е ЕДНА непокрита
        променлива, за да не се внесе модулът."""
        for name in ("GENESIS_MAX_RETRIES", "GENESIS_EXEC_TIMEOUT",
                     "GENESIS_TOOL_RESULT_MAX_CHARS",
                     "GENESIS_STALE_TOOL_RESULT_MAX_CHARS",
                     "GENESIS_FRESH_TOOL_RESULTS", "GENESIS_TOOL_ROUNDS",
                     "GENESIS_STORAGE_THRESHOLD_GB"):
            monkeypatch.setenv(name, "")
        reloaded = importlib.reload(config)
        assert reloaded.MAX_LLM_RETRIES == 8
        assert reloaded.EXEC_TIMEOUT_SEC == 120
        assert reloaded.TOOL_RESULT_MAX_CHARS == 40000
        assert reloaded.STALE_TOOL_RESULT_MAX_CHARS == 2000
        assert reloaded.FRESH_TOOL_RESULTS == 2
        assert reloaded.TOOL_ROUND_CAP == 25
        assert reloaded.STORAGE_THRESHOLD_BYTES == 100 * (1024**3)
