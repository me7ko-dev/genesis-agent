"""Тестовият пакет не пише в истинското състояние на оператора.

Не е хигиена, а поправка на измерен проблем: преди изолацията в conftest,
`pytest tests/test_autonomous_loop.py tests/test_ensemble.py` записваше 16
епизода в настоящата `episodes.db`, с цели като „a goal that always fails".
Тази база захранва блока „скорошна активност" в системния промпт, генерирането
на цели и дестилираните уроци — тоест пакетът обучаваше агента върху
собствените си фикстури. `goal_engine`-овият тест описва последствието:
най-честият кандидат за ново умение е бил точно тази фикстура, 85 пъти.

Файлът съществува, за да падне, ако изолацията бъде махната — иначе загубата
ѝ е безшумна и се открива чак когато промптът се напълни с боклук.
"""
from __future__ import annotations

import importlib

import pytest
from conftest import _PERSISTED_STATE

from genesis_agent import config

# Внася се ПРИ СЪБИРАНЕ, преди фикстурите на теста (ревю 2026-10-11): иначе
# test_signing_keys_are_never_the_operators, пуснат сам, внасяше модула вече
# с подменения GENESIS_HOME и минаваше и без пренасочването в conftest —
# падаше само ако друг тест го е внесъл пръв. Сега пътят е вързан за
# истинския дом при всеки ред на тестовете, както в пълния пакет.
try:
    import genesis_agent.cryptography_utils as _signing
except ImportError:  # без `cryptography` (extra `signing`) няма и ключ
    _signing = None  # type: ignore[assignment]


def _real_data_dir_parts() -> set[str]:
    return {str(config.DATA_DIR)}


@pytest.mark.parametrize("module_name,attr,_filename", _PERSISTED_STATE)
def test_no_module_points_at_the_real_data_dir(module_name, attr, _filename) -> None:
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — модул, който не се внася в тази среда, се прескача
        pytest.skip(f"{module_name} не се внася в тази среда")
    current = getattr(module, attr, None)
    if current is None:
        pytest.skip(f"{module_name}.{attr} липсва")
    assert not str(current).startswith(str(config.DATA_DIR)), (
        f"{module_name}.{attr} сочи към истинското състояние: {current}")


def test_recording_an_episode_lands_in_the_tmp_database() -> None:
    """Проверката е за реален запис, не само за стойността на константа:
    пътят може да е подменен, а модулът да държи отворена стара връзка."""
    from genesis_agent import episodic_memory as em

    em._init_db()
    before = len(em._fetch_all_episodes())
    em.record_episode(goal="проба за изолация", outcome="success",
                      skill_path="test", tags=["mission", "success"])
    after = em._fetch_all_episodes()
    assert len(after) == before + 1
    assert after[-1]["goal"] == "проба за изолация"
    assert str(em.DB_PATH).startswith(str(config.DATA_DIR)) is False


def test_each_test_gets_its_own_database() -> None:
    """Ако файлът се споделяше между тестовете, епизодът от предния тест
    щеше да е тук — и редът на тестовете щеше да носи скрити зависимости."""
    from genesis_agent import episodic_memory as em

    em._init_db()
    goals = [e["goal"] for e in em._fetch_all_episodes()]
    assert "проба за изолация" not in goals, goals


def test_signing_keys_are_never_the_operators(tmp_path) -> None:
    """e2e 2026-10-11: пълният пакет създаде ~/.genesis/private_key.pem.
    cryptography_utils връзва пътя при внасяне, а GENESIS_KEY_DIR, зададен
    в теста по-късно, не го мести — подписваше се с ключа на оператора."""
    from pathlib import Path
    if _signing is None:
        pytest.skip("genesis_agent.cryptography_utils не се внася в тази среда")
    cu = _signing
    for path in (cu.KEY_DIR, cu.PRIVATE_KEY_PATH, cu.PUBLIC_KEY_PATH):
        assert tmp_path in Path(path).parents, path
    cu.sign_code("x = 1")
    assert Path(cu.PRIVATE_KEY_PATH).exists()
