"""genesis_agent.parallel_forge — документиран entrypoint (`python3 -m
genesis_agent.parallel_forge`), без нито един тест досега.

Филтърът за вече свършена работа е мястото, където грешка струва пряко от
квотата: пропусне ли дубликат, една и съща цел се кове отново при всяко
пускане, паралелно, на няколко доставчика — и то без никакво съобщение, че
се повтаря.
"""
from __future__ import annotations

import pytest

from genesis_agent import parallel_forge as pf


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Никакъв реален доставчик и никакво известие — тестваме подбора на цели."""
    monkeypatch.setattr(pf, "_available_providers_cycle", lambda: ["huggingface"])
    monkeypatch.setattr("genesis_agent.notifier.notify", lambda *a, **kw: None)


def _index(monkeypatch, entries: list[tuple[str, str]]) -> None:
    """Индексът се ключира по ИМЕ на умението, а имената идват от
    `slugify(goal)` през save_skill — включително хеш суфикса, който
    колизионната защита добавя. Всеки тест изписва ключовете точно така,
    защото иначе проверката по slug би „минала“ по погрешна причина."""
    monkeypatch.setattr(pf, "load_skills_index", lambda: {
        name: {"name": name, "description": description}
        for name, description in entries
    })


def _record_calls(monkeypatch) -> list[str]:
    attempted: list[str] = []

    def _fake_one(goal: str, provider: str) -> pf.ForgeResult:
        attempted.append(goal)
        return pf.ForgeResult(goal, provider, True, 1, "skills/x.md", 0.1)

    monkeypatch.setattr(pf, "_one", _fake_one)
    return attempted


class TestAlreadyDoneGoalsAreSkipped:
    def test_an_exact_match_is_not_forged_again(self, monkeypatch) -> None:
        _index(monkeypatch, [("implement_an_lru_cache", "Implement an LRU cache")])
        attempted = _record_calls(monkeypatch)
        pf.forge(["Implement an LRU cache", "Write a CSV parser"],
                 notify_result=False)
        assert attempted == ["Write a CSV parser"]

    def test_a_cyrillic_goal_already_done_is_also_skipped(self, monkeypatch) -> None:
        """Проверката минаваше през `slugify(g)` като ключ в индекса. За
        изцяло кирилска цел slugify дава фолбека "skill", а save_skill пази
        такива умения с хеш суфикс — тоест ключът никога не съвпадаше и целта
        се коваше наново всеки път."""
        # Две кирилски умения: първото зае базовия slug "skill", второто
        # получи хеш суфикс — точно това прави save_skill.
        _index(monkeypatch, [
            ("skill", "Мигрирай базата към новата схема"),
            ("skill_4f1a2b", "Изчисти временните файлове по график"),
        ])
        attempted = _record_calls(monkeypatch)
        pf.forge(["Изчисти временните файлове по график",
                  "Направи отчет за деня"], notify_result=False)
        assert attempted == ["Направи отчет за деня"]

    def test_a_different_goal_with_the_same_long_prefix_is_still_forged(
        self, monkeypatch
    ) -> None:
        """Обратната грешка, хваната на живо 2026-07-26: slugify реже на 48
        символа, така че две различни цели с общ дълъг префикс изглеждаха като
        една и втората се прескачаше тихо."""
        done = ("Implement a production-grade utility that converts an integer "
                "to its roman numeral")
        other = ("Implement a production-grade utility that converts a string "
                 "to snake_case")
        from genesis_agent.skills_manager import slugify
        assert slugify(done) == slugify(other), "тестът разчита на общия slug"
        _index(monkeypatch, [(slugify(done), done)])
        attempted = _record_calls(monkeypatch)
        pf.forge([done, other], notify_result=False)
        assert attempted == [other]

    def test_an_empty_library_forges_everything(self, monkeypatch) -> None:
        _index(monkeypatch, [])
        attempted = _record_calls(monkeypatch)
        pf.forge(["а", "б"], notify_result=False)
        assert attempted == ["а", "б"]

    def test_nothing_left_to_do_is_not_an_error(self, monkeypatch) -> None:
        _index(monkeypatch, [("skill", "вече готово")])
        attempted = _record_calls(monkeypatch)
        results = pf.forge(["вече готово"], notify_result=False)
        assert attempted == []
        assert results == []


class TestWorkersAndProviders:
    def test_workers_default_to_the_providers_that_have_a_key(self, monkeypatch) -> None:
        monkeypatch.setattr(pf, "_available_providers_cycle",
                            lambda: ["huggingface", "openrouter"])
        _index(monkeypatch, [])
        used: list[str] = []

        def _fake_one(goal: str, provider: str) -> pf.ForgeResult:
            used.append(provider)
            return pf.ForgeResult(goal, provider, True, 1, "", 0.1)

        monkeypatch.setattr(pf, "_one", _fake_one)
        pf.forge(["а", "б", "в", "г"], notify_result=False)
        assert set(used) == {"huggingface", "openrouter"}, used

    def test_a_failing_goal_does_not_take_the_batch_down(self, monkeypatch) -> None:
        _index(monkeypatch, [])

        def _fake_one(goal: str, provider: str) -> pf.ForgeResult:
            if goal == "лошата":
                raise RuntimeError("доставчикът падна")
            return pf.ForgeResult(goal, provider, True, 1, "", 0.1)

        monkeypatch.setattr(pf, "_one", _fake_one)
        with pytest.raises(RuntimeError):
            pf.forge(["лошата"], notify_result=False)

    def test_one_goal_failing_inside_one_is_reported_not_raised(self, monkeypatch) -> None:
        """`_one` лови собствените си изключения — това е разликата между
        „една цел се провали“ и „партидата умря“."""
        _index(monkeypatch, [])
        monkeypatch.setattr(pf, "run_orchestrated",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("х")))
        result = pf._one("цел", "huggingface")
        assert result.success is False
        assert result.skill_path.startswith("err:")
