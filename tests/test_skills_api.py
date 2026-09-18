"""genesis_agent.skills_api — reuse of existing skills as building blocks for
generated code. Zero coverage before this file despite being one of the two
modules the codebase's own ruff-ignore comment names as reliability-critical
(alongside sandbox/brain/executor/scheduler/budget).

What matters here: verified-only is the default and actually gates load()/
list_available(), a skill's own __main__ self-test block never runs on load
(only its top-level definitions), and the failure modes (unverified skill,
missing function, missing skill) raise the specific, documented exception —
not something generic a caller can't distinguish.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import skill_loader as sl
from genesis_agent import skills_api as api
from genesis_agent import skills_manager as sm

try:
    from genesis_agent import cryptography_utils as _cu
except ImportError:
    _cu = None


@pytest.fixture
def _isolated_skills(tmp_path, monkeypatch):
    """Same isolation as test_skill_loader.py: redirect both the writer
    (skills_manager) and the reader (skill_loader, which skills_api sits on
    top of) at a throwaway directory, and reset the module-level index cache
    so a stale cache from an earlier test can't leak in.

    Also redirects cryptography_utils's key storage (save_skill() signs on
    every save when the `cryptography` package is installed) — without this,
    a real dev machine's GENESIS_HOME points straight at the user's actual
    ~/.genesis, and a "test" here would generate/touch real key material.
    """
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(sm, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sm, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sl, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    if _cu is not None:
        key_dir = tmp_path / "keys"
        monkeypatch.setattr(_cu, "KEY_DIR", key_dir)
        monkeypatch.setattr(_cu, "PRIVATE_KEY_PATH", key_dir / "private_key.pem")
        monkeypatch.setattr(_cu, "PUBLIC_KEY_PATH", key_dir / "public_key.pem")
    return skills_dir


def _mark_unverified(skills_dir, slug: str) -> None:
    """Flips the stored verdict directly, the same way test_skill_loader.py
    tampers with `signature` — decouples "index says unverified" from
    "code happens to be broken", which is what skills_api actually branches
    on (it trusts the index, it does not re-run verify_skill)."""
    idx_path = skills_dir / sm.SKILLS_INDEX_NAME
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    for entry in idx["skills"]:
        if entry["name"] == slug:
            entry["verified"] = False
    idx_path.write_text(json.dumps(idx), encoding="utf-8")
    sl.reload_skills_index()


_ADD_ONE = "def add_one(x):\n    return x + 1\n"


def test_list_available_defaults_to_verified_only(_isolated_skills) -> None:
    sm.save_skill(slug="add_one", code=_ADD_ONE, goal="add 1 to a number")
    sm.save_skill(slug="untrusted", code=_ADD_ONE, goal="not to be trusted")
    _mark_unverified(_isolated_skills, "untrusted")

    assert api.list_available() == ["add_one"]
    assert sorted(api.list_available(verified_only=False)) == ["add_one", "untrusted"]


def test_load_runs_top_level_code_but_not_the_main_guard(_isolated_skills) -> None:
    """load() must execute the skill's definitions (so its functions are
    callable) but never the `if __name__ == "__main__":` self-test — that
    block exists to validate the skill at save time (verify_skill() runs the
    file as a real script, where __name__ IS "__main__"), not to run again
    on every reuse (load() execs it as "genesis_skill_<name>" instead)."""
    code = (
        "ran_main = False\n"
        + _ADD_ONE
        + "\nif __name__ == '__main__':\n"
        "    ran_main = True\n"
        "    assert add_one(1) == 2\n"
        "    print('OK')\n"
    )
    sm.save_skill(slug="add_one", code=code, goal="add 1 to a number")

    mod = api.load("add_one")

    assert mod.add_one(41) == 42
    assert mod.ran_main is False
    assert mod.__name__ == "genesis_skill_add_one"


def test_load_refuses_an_unverified_skill_by_default(_isolated_skills) -> None:
    sm.save_skill(slug="untrusted", code=_ADD_ONE, goal="not to be trusted")
    _mark_unverified(_isolated_skills, "untrusted")

    with pytest.raises(PermissionError, match="untrusted"):
        api.load("untrusted")


def test_load_allows_an_unverified_skill_when_explicitly_opted_in(_isolated_skills) -> None:
    sm.save_skill(slug="untrusted", code=_ADD_ONE, goal="not to be trusted")
    _mark_unverified(_isolated_skills, "untrusted")

    mod = api.load("untrusted", verified_only=False)
    assert mod.add_one(1) == 2


def test_load_missing_skill_raises_file_not_found(_isolated_skills) -> None:
    with pytest.raises(FileNotFoundError):
        api.load("does-not-exist")


class TestGetFunction:
    def test_returns_a_callable_that_behaves_like_the_skill_function(self, _isolated_skills) -> None:
        sm.save_skill(slug="add_one", code=_ADD_ONE, goal="add 1 to a number")
        fn = api.get_function("add_one", "add_one")
        assert fn(1) == 2

    def test_missing_function_raises_type_error_naming_both(self, _isolated_skills) -> None:
        sm.save_skill(slug="add_one", code=_ADD_ONE, goal="add 1 to a number")
        with pytest.raises(TypeError, match="add_one"):
            api.get_function("add_one", "no_such_function")

    def test_non_callable_attribute_raises_type_error(self, _isolated_skills) -> None:
        code = _ADD_ONE + "\nadd_one_result = 42\n"
        sm.save_skill(slug="add_one", code=code, goal="add 1 to a number")
        with pytest.raises(TypeError):
            api.get_function("add_one", "add_one_result")
