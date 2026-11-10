"""Одит, седми кръг (2026-11-10) — портата (sandbox.py). Само оценка на командите,
нищо разрушително не се пуска. Всеки тест пада без поправката си."""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from genesis_agent import sandbox

SAFE, CONFIRM, BLOCKED = (sandbox.RiskLevel.SAFE, sandbox.RiskLevel.CONFIRM,
                          sandbox.RiskLevel.BLOCKED)


# ── обикновени думи не са команди ────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "grep -rn shutdown src/",
    "git commit -m 'Handle shutdown signal'",
    "cat docs/shutdown.md",
    "pytest -k 'reboot or halt'",
    "uvicorn app:app --timeout-graceful-shutdown 5",
    "vagrant halt",
    "adb reboot",
    "git checkout feature/shutdown-hook",
    "./reboot.sh",
    "ruff format C:\\Users\\ivan\\proj\\app.py",
    "ruff format c:/proj",
    "dotnet format C:/src/App.sln",
    "grep -rn 'mkfs' docs/",
])
def test_a_word_in_the_arguments_is_not_the_command(command: str) -> None:
    """Бяха BLOCKED — без въпрос, и в режим allow."""
    assert sandbox.assess_command(command).level is not BLOCKED, command


@pytest.mark.parametrize("command", [
    "shutdown -h now", "sudo reboot", "make && poweroff", "/sbin/halt", "timeout 5 reboot",
    "env X=1 shutdown now", "systemctl reboot", "bash -c 'reboot'", 'cmd /c "shutdown /s /t 0"',
    "shutdown.exe /s", "$(reboot)", '"shutdown" now', "echo x | sudo reboot",
    "mkfs.ext4 /dev/sdb1", "sudo mkfs -t ext4 /dev/sdb1",
    "format C: /y", "format /q D:", "format.com D: /q", "cmd /c format D:",
])
def test_the_real_commands_are_still_blocked(command: str) -> None:
    assert sandbox.assess_command(command).level is BLOCKED, command


@pytest.mark.parametrize("code", [
    "from concurrent.futures import ThreadPoolExecutor\nex = ThreadPoolExecutor(2)\nex.shutdown()\n",
    "import logging\nlogging.shutdown()\n",
    "srv.server_close()  # no shutdown needed\n",
    "print('disk format a: fat32')\n",
    "shutdown = True\n",
    "def halt(x):\n    pass\n",
    "log.info('shutdown requested')\n",
])
def test_python_that_only_mentions_the_word_runs(code: str) -> None:
    assert sandbox.assess_code(code).level is not BLOCKED, code


@pytest.mark.parametrize("code", [
    "import os\nos.system('shutdown -h now')\n",
    "subprocess.run(['reboot'])\n",
    'os.system("sudo poweroff")\n',
    "subprocess.run(['mkfs.ext4', dev])\n",
    "os.system('format D: /q')\n",
])
def test_python_that_runs_the_command_is_still_blocked(code: str) -> None:
    assert sandbox.assess_code(code).level is BLOCKED, code


# ── cmd.exe: ключ без интервал, ^ пред знак ──────────────────────────────────

@pytest.mark.parametrize("command", [
    "rd/s/q C:\\Users\\ivan",
    "cmd /c rd/s/q C:\\Users\\ivan",
    "del/f/s/q C:\\Users\\ivan\\*",
    "r^d /s /q C:\\Users\\ivan",
    "rd /s /q C:\\Us^ers\\ivan",
])
def test_cmd_spellings_of_a_home_wipe_are_blocked(command: str) -> None:
    assert sandbox.assess_command(command).level is BLOCKED, command


def test_cmd_spelling_does_not_touch_ordinary_paths() -> None:
    for command in ("grep '^del/s' notes.txt", "ls del/f", "cat rd/s/readme"):
        assert sandbox.assess_command(command).level is SAFE, command


# ── скрипт на входа на шел ───────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / "proj").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_a_script_fed_to_a_shell_is_judged_like_the_command(home: Path) -> None:
    wipe = f"find {home.as_posix()} -delete"
    assert sandbox.assess_command(wipe).level is BLOCKED
    for command in (f"bash <<EOF\n{wipe}\nEOF", f"echo '{wipe}' | sh", f"bash <<< '{wipe}'",
                    f"cat <<EOF | sudo bash\n{wipe}\nEOF"):
        assert sandbox.assess_command(command).level is BLOCKED, command
    for command in ("bash -s <<EOF\ngit reset --hard\nEOF", "sh <<EOF\nshred -u a.txt\nEOF"):
        assert sandbox.assess_command(command).level is CONFIRM, command


def test_data_on_stdin_is_not_a_script(home: Path) -> None:
    wipe = f"find {home.as_posix()} -delete"
    for command in ("bash <<EOF\necho hi\nls\nEOF", f"cat > notes.md <<EOF\n{wipe}\nEOF",
                    "echo 'hello' | sh", "python - <<'EOF'\nprint(1)\nEOF",
                    f"cat <<EOF | tee x\n{wipe}\nEOF"):
        assert sandbox.assess_command(command).level is SAFE, command


def test_a_secret_read_through_a_fed_script_asks(home: Path) -> None:
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
    for command in ("sh <<EOF\ncat ~/.s?h/id_r?a\nEOF", "echo 'cat ~/.s?h/i?_rsa' | sh"):
        assert sandbox.assess_command(command, cwd=home / "proj").level is CONFIRM, command


# ── hooks.json, подхвърлен без името му в аргументите ────────────────────────

def test_planting_a_hook_without_naming_the_file_asks(home: Path) -> None:
    ws = home / "proj"
    (ws / "e").mkdir()
    (ws / "e" / "hooks.json").write_text("{}", encoding="utf-8")
    with tarfile.open(ws / "e.tar", "w") as tar:
        tar.add(ws / "e" / "hooks.json", arcname="hooks.json")
    long_json = '{"hooks":{"PreToolUse":[{"command":"true # sees the whole environment"}]}}'
    for command in (
        f"cd ~/.genesis && printf '%s' '{long_json}' > hooks.json",
        f"cd ~/.genesis && cp {ws.as_posix()}/e/hooks.json .",
        "tar xf e.tar -C ~/.genesis",
        "unzip -o -q e.zip -d ~/.genesis",
        "cp -rT e ~/.genesis",
        "ln e/hooks.json ~/.genesis/",
        "cp e/hooks.json ~/.genes?s/",
        "G=~/.gene; cp e/hooks.json ${G}sis/",
    ):
        verdict = sandbox.assess_command(command, cwd=ws)
        assert verdict.level is CONFIRM and any("управляващ" in r for r in verdict.reasons), command


def test_ordinary_work_around_the_genesis_folder_does_not_ask(home: Path) -> None:
    ws = home / "proj"
    (ws / "notes.txt").write_text("x", encoding="utf-8")
    for command in ("ls ~/.genesis", "cd ~/.genesis && ls", "du -sh ~/.genesis",
                    "grep -rn model ~/.genesis", "mkdir -p ~/.genesis",
                    "cp notes.txt ~/.genesis/", "cd /tmp && echo x > hooks.json",
                    "X=1; echo $X"):
        assert sandbox.assess_command(command, cwd=ws).level is SAFE, command
