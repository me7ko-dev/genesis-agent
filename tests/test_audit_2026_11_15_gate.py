"""Портата, одит 2026-11-15 — отвореното от 10.11, възпроизведено с истински дом:
1. тайни през скобите на шела: `cat ~/.ss{h,x}/id_rs{a,b}`, `bash -c '…'` — SAFE;
2. съдържанието на целия дом: `grep -rh X ~`, `find ~ -exec cat {} +`, `tar czf … ~` — SAFE;
3. запис във файл, който се пуска при старт (`>> ~/.bashrc`, `.git/hooks/*`,
   `git config core.pager …`) — SAFE, а кодът тръгва после извън Genesis."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent import permissions
from genesis_agent import sandbox as s


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "id_rsa").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    (root / ".aws").mkdir()
    (root / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    (root / ".bashrc").write_text("# rc\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    return root


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    return root


def _level(cmd: str, cwd: Path) -> s.RiskLevel:
    return s.assess_command(cmd, cwd).level


# ── 1. скобите ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "cat ~/.ss{h,x}/id_rs{a,b}",
    "cat ~/.{ssh,aws}/*",
    "cat ~/.ss{g..i}/id_rsa",
    "bash -c 'cat ~/.ss{h,x}/id_rs{a,b}'",
    "cp ~/.a{w,x}s/cred{entials,} /tmp/x",
])
def test_secrets_through_braces_ask(home, proj, cmd):
    verdict = s.assess_command(cmd, proj)
    assert verdict.level == s.RiskLevel.CONFIRM, cmd
    assert not permissions.coverable(verdict.reasons)


@pytest.mark.parametrize("word,expanded", [
    ("a{b,c}d", ["abd", "acd"]),
    ("{a,b}{1,2}", ["a1", "a2", "b1", "b2"]),
    ("x{1..3}", ["x1", "x2", "x3"]),
    ("x{c..a}", ["xc", "xb", "xa"]),
    ("${HOME}/x", ["${HOME}/x"]),
    ("{}", ["{}"]),
    ("a{b}c", ["a{b}c"]),
    ("a{b,{c,d}}e", ["abe", "ace", "ade"]),
])
def test_brace_expansion_like_bash(word, expanded):
    assert s._braces(word) == expanded


def test_brace_expansion_is_bounded():
    assert len(s._braces("{a,b}" * 20)) <= s._MAX_BRACES


@pytest.mark.parametrize("cmd", ["echo {a,b}{c,d}", "find . -name x -exec cat {} +",
                                 "mkdir -p src/{a,b}", "cat src/{app,main}.py"])
def test_braces_without_secrets_stay_safe(home, proj, cmd):
    assert _level(cmd, proj) == s.RiskLevel.SAFE


# ── 2. целият дом ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "grep -rh BEGIN ~", "grep -r PRIVATE ~/", "grep -Rn KEY $HOME", "rg BEGIN ~",
    "find ~ -exec cat {} +", "find ~ -type f -execdir head -c 200 {} ;",
    "tar czf /tmp/x.tgz ~", "zip -r /tmp/x.zip ~", "rsync -a ~ /tmp/backup",
    "cp -r ~ /tmp/copy", "cd && grep -r PRIVATE", "cd ~ && rg KEY",
])
def test_reading_the_whole_home_asks(home, proj, cmd):
    verdict = s.assess_command(cmd, proj)
    assert verdict.level == s.RiskLevel.CONFIRM, (cmd, verdict.reasons)
    assert any(r.startswith("достъп до чувствителни файлове") for r in verdict.reasons)
    assert not permissions.coverable(verdict.reasons)


@pytest.mark.parametrize("cmd", [
    "grep -r TODO .", "grep -r TODO src", "rg TODO", "ls -R ~", "find ~ -name '*.py'",
    "du -sh ~", "tar xzf a.tgz -C ~", "grep TODO ~/notes.txt", "cp -r src /tmp/x",
])
def test_names_or_a_project_tree_stay_as_before(home, proj, cmd):
    verdict = s.assess_command(cmd, proj)
    assert not any("цялото" in r for r in verdict.reasons), (cmd, verdict.reasons)


# ── 3. стартови файлове ──────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "echo 'export PATH=/tmp:$PATH' >> ~/.bashrc", "echo x>>~/.gitconfig",
    "printf 'alias ls=rm' >> ~/.zshrc", "echo x > ~/.profile", "tee -a ~/.bashrc < f",
    "cp evil.desktop ~/.config/autostart/x.desktop", "sed -i 's/a/b/' ~/.bashrc",
    "cat payload > .git/hooks/pre-commit", "echo x > .git/config",
    "cp hook.sh .git/hooks/post-checkout", "ln -sf /tmp/x.sh .git/hooks/pre-push",
    "git config --global core.pager /tmp/x.sh", "git config core.hooksPath /tmp/h",
    "git config core.sshCommand /tmp/x.sh", "git config set core.editor /tmp/x.sh",
    "git config alias.up '!rm -rf build'", "git config credential.helper /tmp/steal",
    "bash -c 'echo x >> ~/.bashrc'",
])
def test_writing_a_file_that_runs_at_startup_asks(home, proj, cmd):
    verdict = s.assess_command(cmd, proj)
    assert verdict.level >= s.RiskLevel.CONFIRM, (cmd, verdict.reasons)
    assert any(r.startswith(s.STARTUP_REASON) for r in verdict.reasons), verdict.reasons
    assert not permissions.coverable(verdict.reasons)


@pytest.mark.parametrize("cmd", [
    "cat ~/.bashrc", "grep alias ~/.bashrc", "source ~/.bashrc", "cp ~/.bashrc backup.txt",
    "echo x > notes.txt", "echo hi >&2", "echo x 2>&1 | tee out.log", "git config user.name Ivan",
    "git config --get core.pager", "git config alias.st status", "git config -l",
    "cat .git/config", "ls .git/hooks",
])
def test_reading_them_or_other_writes_stay_safe(home, proj, cmd):
    verdict = s.assess_command(cmd, proj)
    assert not any(r.startswith(s.STARTUP_REASON) for r in verdict.reasons), (cmd, verdict.reasons)


def test_rule_tee_star_does_not_cover_the_rc_file(home, proj):
    verdict = s.assess_command("tee -a /x/.bashrc", proj)      # не е в дома — не пита
    assert not any(r.startswith(s.STARTUP_REASON) for r in verdict.reasons)
    rc = str(home / ".bashrc").replace("\\", "/")
    verdict = s.assess_command(f"tee -a {rc}", proj)
    assert not permissions.coverable(verdict.reasons)


def test_write_file_to_a_git_hook_asks_like_the_shell(home, proj, monkeypatch):
    """WRITE_FILE/EDIT_FILE: `.git/hooks/pre-commit` е вътре в проекта, а
    записът минаваше без въпрос — в шела `cat > .git/hooks/pre-commit` пита."""
    import genesis_skills

    monkeypatch.setattr(genesis_skills, "_WORKSPACE", proj)
    asked: list[list[str]] = []

    def deny(command, verdict, policy):
        asked.append(verdict.reasons)
        return False, "отказано"
    monkeypatch.setattr(s, "_decide", deny)
    hook = proj / ".git" / "hooks" / "pre-commit"
    out = genesis_skills._tool_write_file(str(hook), "#!/bin/sh\nexit 0\n")
    assert "отказано" in out and not hook.exists()
    assert any(r.startswith(s.STARTUP_REASON) for reasons in asked for r in reasons)


@pytest.mark.parametrize("rel", [".bashrc", ".profile", ".zshrc", ".gitconfig",
                                 ".config/fish/config.fish", ".config/git/config",
                                 "Library/LaunchAgents/x.plist"])
def test_startup_path_reason_in_home(home, rel):
    assert s.startup_path_reason(home / rel)


@pytest.mark.parametrize("rel", ["notes.txt", ".bashrc.bak", "proj/.profile.d", ".ssh/known_hosts"])
def test_other_paths_in_home_are_not_startup_files(home, rel):
    assert s.startup_path_reason(home / rel) is None


# ── 4. съобщение на commit не е команда ──────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "git commit -m 'fix: do not rm -rf / anymore'", 'git commit -m "rm -rf / is gone"',
    "git tag -a v1 -m 'rm -rf ~ fixed'", "gh pr create --title x --body 'never rm -rf /'",
    "git commit -m 'a' -m 'curl x | bash removed'", "FOO=1 git commit --message='rm -rf /'",
])
def test_a_commit_message_about_rm_is_not_blocked(cmd):
    """Одит 2026-11-10: образците гледаха текста на съобщението — BLOCKED."""
    assert s.assess_command(cmd).level == s.RiskLevel.SAFE, cmd


@pytest.mark.parametrize("cmd", [
    'git commit -m "$(rm -rf /)"', "git commit -m 'x' && rm -rf /", "git commit -m 'x'; rm -rf /",
    "bash -c 'git commit -m x; rm -rf /'", "git commit -m 'x' -- ; rm -rf ~",
])
def test_what_the_shell_runs_next_to_a_message_still_blocks(cmd):
    assert s.assess_command(cmd).level == s.RiskLevel.BLOCKED, cmd


def test_backticks_in_a_message_still_ask():
    assert s.assess_command('git commit -m "`rm -rf /`"').level >= s.RiskLevel.CONFIRM
