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


# ── преглед на PR #122 (2026-11-15): след подкомандата стои цел, не команда ──

@pytest.mark.parametrize("rule", [
    "asdf env nodejs *", "npm explore lodash *", "npm explore lodash -- *",
    "rustup run stable *", "docker run ubuntu *", "docker exec web *",
    "podman run alpine *", "direnv exec src *", "git -c core.pager=less *",
    "nix shell nixpkgs.hello *", "nix develop *", "mise exec node@20 *",
    "mise x node@20 -- *",
    "asdf env nodejs bash *",          # инструментът след целта — обвивка
])
def test_rule_that_stops_at_the_target_is_refused(rule):
    assert p.normalize_rule(rule)[0] is None, rule


@pytest.mark.parametrize("rule,ok,bad", [
    ("asdf env nodejs npm *", "asdf env nodejs npm install", "asdf env nodejs rm -rf x"),
    ("npm explore lodash -- ls *", "npm explore lodash -- ls -la",
     "npm explore lodash -- rm -rf x"),
    ("rustup run stable cargo *", "rustup run stable cargo build", "rustup run stable rm -rf x"),
    ("docker run ubuntu ls *", "docker run ubuntu ls /", "docker run ubuntu rm -rf /x"),
    ("direnv exec src make *", "direnv exec src make test", "direnv exec src rm -rf x"),
    ("git -c color.ui=always log *", "git -c color.ui=always log --oneline",
     "git -c color.ui=always -c core.pager=/tmp/x.sh log"),
    ("nix shell nixpkgs.ripgrep --command rg *", "nix shell nixpkgs.ripgrep --command rg foo",
     "nix shell nixpkgs.ripgrep --command rm -rf x"),
    ("mise exec node@20 -- npm *", "mise exec node@20 -- npm test",
     "mise exec node@20 -- rm -rf x"),
    ("asdf env nodejs npm *", "asdf env nodejs npm install", "asdf env nodejs npm exec rm -rf x"),
])
def test_tool_after_the_target_is_checked_as_its_own_rule(rule, ok, bad):
    assert p.normalize_rule(rule)[0] == rule
    assert p.matches(ok, rule)
    assert not p.matches(bad, rule)


@pytest.mark.parametrize("rule,cmd", [
    ("asdf *", "asdf env nodejs rm -rf x"),
    ("rustup *", "rustup run stable rm -rf x"),
    ("direnv *", "direnv exec src rm -rf x"),
    ("nix *", "nix shell nixpkgs.hello --command rm -rf x"),
    ("mise *", "mise exec node@20 -- rm -rf x"),
])
def test_broad_rule_does_not_cover_the_command_after_a_target(rule, cmd):
    assert p.normalize_rule(rule)[0] == rule
    assert not p.matches(cmd, rule)


# ── git пуска команда през флаг на подкомандата (отворено от нощния проход) ──

@pytest.mark.parametrize("cmd", [
    "git bisect run rm -rf x", "git rebase -x rm main", "git rebase --exec rm main",
    "git rebase -i --exec=rm main", "git difftool -x rm", "git difftool --extcmd=rm HEAD",
    "git mergetool --extcmd rm", "git -c color.ui=always rebase -x rm main",
])
def test_git_star_does_not_cover_a_command_git_runs(cmd):
    assert not p.matches(cmd, "git *")


@pytest.mark.parametrize("rule", ["git bisect run *", "git rebase -x *", "git difftool --extcmd *"])
def test_git_run_rules_without_the_tool_refused(rule):
    assert p.normalize_rule(rule)[0] is None


def test_git_rebase_x_rule_covers_only_its_tool():
    rule = "git rebase -x make *"
    assert p.normalize_rule(rule)[0] == rule
    assert p.matches("git rebase -x make main", rule)
    assert not p.matches("git rebase -x make -x rm main", rule)   # второто -x пуска rm


@pytest.mark.parametrize("cmd", ["git cherry-pick -x abc123", "git bisect start",
                                 "git rebase -i main", "git difftool HEAD"])
def test_git_star_still_covers_the_plain_subcommands(cmd):
    assert p.matches(cmd, "git *")
