"""Драйверът на USE_SKILL тече с отделна домашна папка (2026-10-09):
`Path.home() / ".ssh"` и `expanduser("~/.aws")` стигаха до тайните на
оператора без нито един буквален път, който sandbox-ът да види."""
from __future__ import annotations

import json
import os
import site
from pathlib import Path

import pytest

from genesis_agent import paths
from genesis_agent import skill_loader as sl
from genesis_agent import skills_manager as sm


@pytest.fixture
def skills(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    for mod in (sm, sl):
        monkeypatch.setattr(mod, "SKILLS_DIR", skills_dir)
        monkeypatch.setattr(mod, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    sm.save_skill(slug="where_is_home", code="import os\nfrom pathlib import Path\n",
                  goal="where is home")
    return skills_dir


_DRIVER = ("import json, os, site\n"
           "print(json.dumps({'home': str(Path.home()), 'tilde': os.path.expanduser('~'),\n"
           "                  'userbase': os.environ.get('PYTHONUSERBASE', ''),\n"
           "                  'mpl': os.environ.get('MPLCONFIGDIR', '')}))")


def _run(driver: str) -> dict:
    out = sl.use_skill("where_is_home", driver)
    line = next((ln for ln in out.splitlines() if ln.startswith("{")), None)
    assert line, out
    return json.loads(line)


def test_a_skill_driver_does_not_see_the_operators_home(skills) -> None:
    got = _run(_DRIVER)
    own = sl.skill_home().resolve()
    assert Path(got["home"]).resolve() == own
    assert Path(got["tilde"]).resolve() == own
    assert Path(got["home"]).resolve() != Path.home().resolve()


def test_the_skill_home_is_not_next_to_the_secrets(skills) -> None:
    # Одит 2026-10-09: в ~/.genesis/skill_home `Path.home().parent` беше точно
    # .env, ключовете и mcp_tokens.json, а `.parents[1]` — истинският HOME.
    own = sl.skill_home().resolve()
    genesis = Path(paths.GENESIS_HOME).resolve()
    assert genesis not in own.parents and own != genesis
    assert Path.home().resolve() not in own.parents or os.name == "nt"


def test_caches_live_in_the_skill_home_and_user_packages_still_import(skills) -> None:
    got = _run(_DRIVER)
    own = sl.skill_home().resolve()
    assert Path(got["mpl"]).resolve().is_relative_to(own)
    # `pip install --user` пакетите — от истинския user site, не от новия HOME.
    assert got["userbase"] == site.getuserbase()


def test_run_skill_gets_the_same_home(skills) -> None:
    sm.save_skill(slug="home_now", code="from pathlib import Path\nprint(Path.home())\n",
                  goal="home now")
    out = sl.run_skill("home_now")
    assert Path(out).resolve() == sl.skill_home().resolve()


@pytest.mark.skipif(os.name == "nt", reason="symlink и uid — POSIX")
def test_a_planted_skill_home_is_not_used(skills, tmp_path) -> None:
    # Споделен /tmp: друг е създал папката като връзка към своя — не я ползваме.
    root = Path(sl.SKILL_HOME_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    os.symlink(elsewhere, root / f"genesis-skill-home-{os.getuid()}")
    assert elsewhere.resolve() != sl.skill_home().resolve()


def test_git_identity_and_pip_config_reach_the_skill(skills, monkeypatch) -> None:
    monkeypatch.setattr(sl, "_operator_env", {"GIT_AUTHOR_EMAIL": "op@example.com",
                                              "PIP_CONFIG_FILE": "/etc/pip.conf"})
    env = sl.skill_env()
    assert env["GIT_AUTHOR_EMAIL"] == "op@example.com" and env["PIP_CONFIG_FILE"] == "/etc/pip.conf"
