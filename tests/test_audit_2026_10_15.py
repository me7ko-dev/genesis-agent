"""Одит 2026-10-15 (сигурност): веригата за подправяне на доверието на hooks.

Истинската стена срещу авто-пускане на подхвърлени hooks/MCP е подписът по
съдържание (всяка промяна след доверие → недоверен) — той вече стои на main и
е покрит тук с тест (`test_content_seal_defeats_rewrite`). Останалите дупки са
несиметрия: по-слабият път (WRITE_FILE/EDIT_FILE и shell за `.mcp.json` в
корена и за символна връзка с голо име) оценяваше записа в управляващ файл като
SAFE, докато shell гейтът по `_GENESIS_CONTROL` го иска CONFIRM. Всеки тест тук
падаше преди поправката."""
from __future__ import annotations

import os
import sys

import pytest

import genesis_skills
from genesis_agent import hooks, sandbox
from genesis_agent.sandbox import RiskLevel


@pytest.fixture
def deny(monkeypatch):
    """Режим „deny": CONFIRM се отказва автоматично, както в автономен ход."""
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Проект с .git корен, зададен като workspace на инструментите."""
    ws = tmp_path / "proj"
    (ws / ".git").mkdir(parents=True)
    (ws / ".genesis").mkdir()
    genesis_skills.set_workspace(ws)
    return ws


# ── Веригата, която вече е затворена: подпис по съдържание ──────────────────

def test_content_seal_defeats_rewrite(project) -> None:
    """Пренаписване на доверен проектен hooks.json (през WRITE_FILE) го прави
    недоверен — командите му не се пускат без ново `/hooks trust`."""
    ph = project / ".genesis" / "hooks.json"
    ph.write_text('{"hooks":{"Stop":[{"command":"echo good"}]}}', encoding="utf-8")
    hooks.trust(ph)
    assert len(hooks.configured(project)[0]) == 1
    ph.write_text('{"hooks":{"Stop":[{"command":"evil"}]}}', encoding="utf-8")
    assert hooks.configured(project)[0] == []


# ── G1: shell запис в проектния .mcp.json (в корена, не под .genesis) ────────

@pytest.mark.parametrize("command", [
    "printf '%s' '{}' > .mcp.json",
    "cp evil.json .mcp.json",
    "mv evil.json .mcp.json",
    "echo '{}' | tee .mcp.json",
    "sed -i 's/a/b/' .mcp.json",
])
def test_shell_write_to_project_mcp_asks(command, project, deny) -> None:
    assert sandbox.assess_command(command, cwd=project).level == RiskLevel.CONFIRM


# ── G2: shell запис през символна връзка с голо име към управляващ файл ──────

def test_shell_write_through_bare_name_symlink_asks(project) -> None:
    """`printf … > link.json`, където link.json вече сочи към управляващ файл,
    минаваше SAFE: _control_file_reached отрязваше голите имена без разделител,
    без да разгъне символната връзка (одит 2026-10-15)."""
    control = project / ".genesis" / "hooks.json"
    control.write_text("{}", encoding="utf-8")
    link = project / "link.json"
    os.symlink(control, link)
    verdict = sandbox.assess_command("printf '%s' '{}' > link.json", cwd=project)
    assert verdict.level == RiskLevel.CONFIRM


# ── G3: WRITE_FILE/EDIT_FILE в управляващ файл ВЪТРЕ в workspace-а ───────────

@pytest.mark.parametrize("rel", [
    ".genesis/hooks.json",
    ".mcp.json",
    ".genesis/commands/review.md",
    ".genesis/agents/helper.md",
])
def test_write_file_to_control_file_is_denied(rel, project, deny) -> None:
    """Проектните управляващи файлове са ВЪТРЕ в workspace-а, затова проверката
    „извън workspace" не ги пазеше, а WRITE_FILE гейтът не гледаше
    _GENESIS_CONTROL — записът минаваше в режим „deny"."""
    (project / rel).parent.mkdir(parents=True, exist_ok=True)
    res = genesis_skills._tool_write_file(rel, "evil content")
    assert "SANDBOX DENIED" in res
    assert not (project / rel).exists()


def test_edit_file_to_trusted_hooks_is_denied(project, deny) -> None:
    """EDIT_FILE в доверен проектен hooks.json: подписът пак би го направил
    недоверен, но записът изобщо не бива да минава без потвърждение."""
    ph = project / ".genesis" / "hooks.json"
    ph.write_text('{"hooks":{"Stop":[{"command":"echo good"}]}}', encoding="utf-8")
    hooks.trust(ph)
    genesis_skills._SEEN_PATHS.add(ph.resolve())
    res = genesis_skills._tool_edit_file(".genesis/hooks.json", "echo good", "evil")
    assert "SANDBOX DENIED" in res
    assert "evil" not in ph.read_text(encoding="utf-8")


def test_write_file_through_symlink_to_control_is_denied(project, deny) -> None:
    """Символна връзка с невинно име вътре в workspace-а, сочеща към проектен
    управляващ файл: записът през нея стига до файла, затова се отказва."""
    control = project / ".genesis" / "hooks.json"
    control.write_text("{}", encoding="utf-8")
    link = project / "notes.json"
    os.symlink(control, link)
    genesis_skills._SEEN_PATHS.add(link.resolve())
    res = genesis_skills._tool_write_file("notes.json", '{"hooks":{"Stop":[{"command":"evil"}]}}')
    assert "SANDBOX DENIED" in res
    assert "evil" not in control.read_text(encoding="utf-8")


# ── Легитимните потоци остават отворени ─────────────────────────────────────

def test_ordinary_file_write_still_works(project, deny) -> None:
    """Запис на обикновен файл в проекта не се пипа от новия гейт."""
    res = genesis_skills._tool_write_file("src/app.py", "x = 1\n")
    assert "✓" in res
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\n"


def test_commit_message_mentioning_mcp_json_is_safe(project) -> None:
    """Съобщение за commit, което СПОМЕНАВА .mcp.json, не е запис в него —
    не бива да пита (новото правило е по аргумент-път, не по суров текст)."""
    verdict = sandbox.assess_command('git commit -m "update .mcp.json schema"', cwd=project)
    assert verdict.level == RiskLevel.SAFE


@pytest.mark.skipif(sys.platform == "win32", reason="path separators")
def test_reading_mcp_json_is_not_gated_by_write_rule(project) -> None:
    """READ_FILE на .mcp.json (за преглед) остава без въпрос — гейтът е за запис."""
    (project / ".mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    assert sandbox.control_path_reason(str(project / ".mcp.json"))
    res = genesis_skills._tool_read_file(".mcp.json")
    assert "mcpServers" in res
