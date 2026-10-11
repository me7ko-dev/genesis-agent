"""Одит 2026-10-11, трети кръг: веригата за подправяне на доверието (HMAC)
end to end и маршрутите, по които вторият кръг я пропускаше. Всеки тест тук
пада на кода преди поправката."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys

import pytest

from genesis_agent import hooks, mcp_client, sandbox
from genesis_agent.sandbox import RiskLevel


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Изолиран ~/.genesis с готов ключ за подпис (Genesis вече е вървял)."""
    from genesis_agent import paths
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(paths, "GENESIS_HOME", h / ".genesis")
    hooks.trust_key()
    return h


# ── Цялата верига за подправяне на доверието в режим „deny“ ──────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell: ~, подшел, env -C")
def test_the_whole_hmac_forgery_chain_is_denied_and_the_hook_stays_inactive(
        home, tmp_path, monkeypatch) -> None:
    """Веригата: 1) прочети ключа за подписа, 2) запиши запис за доверие с
    подправен подпис, 3) подхвърли ~/.genesis/hooks.json. Втори кръг:
    четенето на ключа през подшел/`env -C`/glob и записът на trusted_hooks.json
    минаваха като SAFE и в режим „deny“, тъй че подхвърленият hook ставаше
    активен без `/hooks trust`. Всяка стъпка през всеки известен маршрут сега
    иска потвърждение, значи в „deny“ е блокирана — и hookът остава неактивен."""
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    genesis = home / ".genesis"

    # 1) Четене на ключа — през всеки маршрут, който гейтът познава.
    key_reads = [
        "cat ~/.genesis/hooks_trust.key",
        "cd ~/.genesis; cat hooks_trust.key",
        "cd ~/.genesis && cat hooks_trust.key",
        "pushd ~/.genesis; cat hooks_trust.key",
        "(cd ~/.genesis; cat hooks_trust.key)",
        "env -C ~/.genesis cat hooks_trust.key",
        "env --chdir=$HOME/.genesis cat hooks_trust.key",
        "sh -c 'cd ~/.genesis; cat hooks_trust.key'",
        "cd ~/.genesis; cat hooks_t*",
        "cat ~/.genesis/hooks_tr''ust.key",
    ]
    for cmd in key_reads:
        assert sandbox.run_shell(cmd, cwd=tmp_path).blocked, cmd

    # 2) Запис на подправен запис за доверие — през всеки маршрут.
    planted = genesis / "hooks.json"
    planted.write_text(json.dumps({"hooks": {"Stop": [{"command": "echo PWNED"}]}}),
                       encoding="utf-8")
    # Нападателят дори да е прочел ключа, сглобява валиден подпис:
    seal = hmac.new(hooks.trust_key(),
                    f"{hooks._key(planted)}\0{hooks._digest(planted)}".encode(),
                    hashlib.sha256).hexdigest()
    record = json.dumps({hooks._key(planted): seal})
    trust_writes = [
        f"printf '%s' '{record}' > ~/.genesis/trusted_hooks.json",
        "cd ~/.genesis && echo '{}' > trusted_hooks.json",
        "(cd ~/.genesis; echo '{}' > trusted_hooks.json)",
        "env -C ~/.genesis sh -c 'echo x > trusted_hooks.json'",
        "echo '{}' > ~/.genesis/trust''ed_hooks.json",
        "cp /tmp/x ~/.genesis/trusted_hooks.json",
    ]
    for cmd in trust_writes:
        assert sandbox.run_shell(cmd, cwd=tmp_path).blocked, cmd

    # 3) Нищо не е записало записа за доверие — hookът не се пуска.
    assert not (genesis / "trusted_hooks.json").exists()
    active, _ = hooks.configured(tmp_path)
    assert active == []
    assert not hooks.is_trusted(planted)


# ── Отделните маршрути искат потвърждение (а не „deny“), за interactive ──────

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
@pytest.mark.parametrize("command", [
    "(cd ~/.genesis; cat hooks_trust.key)",
    "env -C ~/.genesis cat hooks_trust.key",
    "env --chdir=$HOME/.genesis cat hooks_trust.key",
    "sh -c 'cd ~/.genesis; cat hooks_trust.key'",
    "echo '{}' > ~/.genesis/trusted_hooks.json",
    "echo '{}' > ~/.genesis/trust''ed_hooks.json",
    "cd ~/.genesis && echo '{}' > trusted_hooks.json",
    "(cd ~/.genesis; echo x > trusted_mcp.json)",
    "cp /tmp/x ~/.genesis/trusted_hooks.json",
])
def test_every_route_to_the_trust_key_or_records_asks(home, tmp_path, command) -> None:
    assert sandbox.assess_command(command, cwd=tmp_path).level == RiskLevel.CONFIRM, command


# ── Не бива да отслабва: четенето/споменаването в кода остава SAFE ───────────

@pytest.mark.parametrize("command", [
    "grep -rn trusted_hooks genesis_agent/",
    "grep -n hooks_trust.key genesis_agent/hooks.py",
    "cat ~/.genesis/settings.json",
    "cat .genesis/hooks.json",
    "ls .genesis/commands",
    "ls ~/.genesis",
])
def test_reading_or_mentioning_control_files_stays_safe(home, command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.SAFE, command


# ── MCP: относителен път с интервал пак се проверява ────────────────────────

@pytest.mark.parametrize("arguments", [
    {"path": "My Keys/.env"},
    {"path": "config dir/.npmrc"},
    {"path": "secret files\\id_rsa"},
])
def test_a_relative_path_with_a_space_still_asks(arguments) -> None:
    """Втори кръг: всеки интервал правеше низа „свободен текст“, освен ако
    започва от корен/`~`/буква на диск — относителният път с интервал
    (`My Keys/.env`) четеше тайната без въпрос. Пътят има разделител
    (`/` или `\\`); изречението-заявка отдолу няма."""
    assert mcp_client._secret_argument(arguments)


@pytest.mark.parametrize("arguments", [
    {"query": "how do I rotate the credentials in .env"},
    {"sql": "SELECT * FROM credentials WHERE id = 1"},
    {"title": "Leaked .npmrc token in CI logs"},
])
def test_free_text_without_a_separator_stays_free_text(arguments) -> None:
    assert mcp_client._secret_argument(arguments) is None


# ── environ-glob: обикновените glob-ове след променлива не са тайна ──────────

@pytest.mark.parametrize("command", ["ls $HOME/env*", "ls $VENV/envi*", "cat $d/en*"])
def test_a_plain_env_glob_after_a_variable_is_not_a_secret(command) -> None:
    """`fnmatch('environ', 'env*')` хващаше изброяването на виртуални среди."""
    assert sandbox.assess_command(command).level == RiskLevel.SAFE, command


@pytest.mark.parametrize("command", ["cat $p/environ", "cat ${dir}/environ*"])
def test_the_process_environ_after_a_variable_still_asks(command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.CONFIRM, command
