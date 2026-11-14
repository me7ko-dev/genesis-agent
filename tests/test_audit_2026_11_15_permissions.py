"""Одит 2026-11-15: обвивки, които пускат всяка команда, не се покриват от
широко правило (`pipenv *`, `bundle *`, `mise *`, `parallel *`, `npm *`)."""
from __future__ import annotations

import pytest

from genesis_agent import permissions as p


@pytest.mark.parametrize("rule", ["parallel *", "nix-shell *", "parallel -j4 *"])
def test_launcher_rules_refused(rule):
    assert p.normalize_rule(rule)[0] is None


@pytest.mark.parametrize("rule", ["pipenv run *", "bundle exec *", "mise exec *",
                                  "mise x *", "docker run *", "git submodule foreach *"])
def test_runner_rules_without_tool_refused(rule):
    assert p.normalize_rule(rule)[0] is None


@pytest.mark.parametrize("rule,cmd", [
    ("pipenv *", "pipenv run rm -rf build"),
    ("bundle *", "bundle exec rm -rf build"),
    ("mise *", "mise exec -- rm -rf build"),
    ("mise *", "mise x -- rm"),
    ("npm *", "npm explore pkg -- rm -rf build"),
    ("npm *", "npm exec rm -rf build"),
    ("pnpm *", "pnpm dlx rm -rf x"),
    ("pnpm *", "pnpm exec rm -rf x"),
    ("yarn *", "yarn dlx rm x"),
    ("docker *", "docker run --rm alpine rm -rf /x"),
    ("docker *", "docker exec c rm -rf /x"),
    ("direnv *", "direnv exec . rm -rf x"),
    ("flatpak *", "flatpak run org.x.App"),
    ("git *", "git submodule foreach rm -rf x"),
    ("git *", "git -c core.pager=rm log"),
])
def test_broad_rule_does_not_cover_runner(rule, cmd):
    assert p.normalize_rule(rule)[0] == rule
    assert not p.matches(cmd, rule)


@pytest.mark.parametrize("rule,cmd", [
    ("pipenv *", "pipenv install requests"),
    ("pipenv install *", "pipenv install requests"),
    ("bundle *", "bundle install"),
    ("bundle install", "bundle install"),
    ("npm *", "npm install left-pad"),
    ("git *", "git status"),
    ("docker *", "docker build ."),
])
def test_narrow_use_still_covered(rule, cmd):
    assert p.normalize_rule(rule)[0] == rule
    assert p.matches(cmd, rule)
