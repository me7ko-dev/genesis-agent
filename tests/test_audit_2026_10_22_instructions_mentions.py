"""Одит 2026-10-22: инструкциите на проекта (GENESIS.md / AGENTS.md / CLAUDE.md
с @импорти — в системния промпт на ВСЯКА заявка) и @път в съобщението.

Всеки тест е находка, възпроизведена преди поправката."""
from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

from genesis_agent import mentions, project_instructions


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    return root


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")


# ── 1. ключове и тайни, които образецът на sandbox-а не познава ─────────────

_OPENSSH_KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
                "-----END OPENSSH PRIVATE KEY-----\n")
_SECRETS = {
    "config/master.key": "RAILS-MASTER-KEY",
    "certs/server.key": "-----BEGIN PRIVATE KEY-----\nTLS-KEY-BODY\n-----END PRIVATE KEY-----\n",
    ".streamlit/secrets.toml": "openai_key = 'STREAMLIT-SECRET'\n",
    "deploy/id.ppk": "PuTTY-User-Key-File-3: ssh-rsa\nPPK-SECRET\n",
    "creds/service-account.json": '{"private_key_id": "GCP-SA-SECRET"}',
    "terraform.tfvars": 'db_password = "TF-SECRET"\n',
    ".dev.vars": "API_TOKEN=CF-WORKER-SECRET\n",
    "keystore.jks": "JKS-SECRET",
    ".envrc": "export AWS_SECRET_ACCESS_KEY=ENVRC-SECRET\n",
    # Невинно име, частен ключ вътре — хваща се по съдържанието.
    "docs/deploy-notes.txt": _OPENSSH_KEY,
}
_MARKERS = ["RAILS-MASTER-KEY", "TLS-KEY-BODY", "STREAMLIT-SECRET", "PPK-SECRET",
            "GCP-SA-SECRET", "TF-SECRET", "CF-WORKER-SECRET", "JKS-SECRET",
            "ENVRC-SECRET", "b3BlbnNzaC1rZXkt"]


def test_secret_files_never_reach_the_system_prompt_through_imports(repo) -> None:
    # Клониран чужд репозиторий: AGENTS.md внася локално генерираните (в
    # .gitignore) тайни на оператора в промпта на всяка заявка към доставчика.
    _write(repo, _SECRETS)
    _write(repo, {"docs/style.md": "STYLE-RULE",
                  "AGENTS.md": " ".join(f"@{rel}" for rel in _SECRETS) + "\nСтил: @docs/style.md\n"})
    section = project_instructions.prompt_section(repo)
    assert "STYLE-RULE" in section            # обикновеният импорт си остава
    leaked = [m for m in _MARKERS if m in section]
    assert leaked == []


@pytest.mark.parametrize("rel", list(_SECRETS))
def test_secret_files_are_not_attached_by_mention(repo, rel) -> None:
    _write(repo, _SECRETS)
    text, notes = mentions.expand(f"виж @{rel}", repo)
    assert not [m for m in _MARKERS if m in text]
    assert len(notes) == 1 and "чувствителен" in notes[0], notes


# ── 2. .claude/CLAUDE.md — второто документирано място на Claude Code ───────

def test_dot_claude_claude_md_is_read(repo) -> None:
    ws = repo / "app"
    _write(repo, {".claude/CLAUDE.md": "RULE-DOTCLAUDE",
                  # Една на папка: горният CLAUDE.md печели над .claude/CLAUDE.md.
                  "app/CLAUDE.md": "APP-TOP", "app/.claude/CLAUDE.md": "APP-HIDDEN"})
    section = project_instructions.prompt_section(ws)
    assert "RULE-DOTCLAUDE" in section
    assert "APP-TOP" in section and "APP-HIDDEN" not in section


# ── 3. @README, @Makefile — импорт на файл без разширение ───────────────────

def test_imports_of_files_without_an_extension(repo) -> None:
    _write(repo, {
        # Примерът от документацията на Claude Code, дословно.
        "CLAUDE.md": ("See @README for project overview and @package.json for available "
                      "npm commands.\n- Git workflow @docs/git-instructions.md\n"
                      "Команди: @Makefile, образ: @Dockerfile.\n"
                      "Ползвай @property и пиши на @john; конфигурацията: @.git/config\n"),
        "README": "README-BODY", "package.json": '{"PKG": 1}',
        "docs/git-instructions.md": "GITINSTR", "Makefile": "MAKEBODY",
        "Dockerfile": "DOCKERBODY",
    })
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://me:GIT-TOKEN@github.com/x/y\n', encoding="utf-8")
    section = project_instructions.prompt_section(repo)
    for body in ("README-BODY", "PKG", "GITINSTR", "MAKEBODY", "DOCKERBODY"):
        assert body in section, body
    # Вътрешността на .git не е инструкция: там е и токенът в адреса на remote.
    assert "GIT-TOKEN" not in section
    assert "### @property" not in section and "### @john" not in section


# ── 4. двоичен файл като импорт ─────────────────────────────────────────────

def test_binary_import_does_not_flood_the_prompt(repo) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
    _write(repo, {"GENESIS.md": "Архитектура: @docs/arch.png\n", "docs/arch.png": png})
    section = project_instructions.prompt_section(repo)
    assert "Архитектура" in section
    assert "\x00" not in section and "�" not in section
    assert len(section) < 1000


# ── 5. @път, последван от българска/типографска пунктуация или :ред ─────────

@pytest.mark.parametrize("msg", [
    "виж „@src/api.py“",
    "виж «@src/api.py»",
    "виж @src/api.py…",
    "виж @src/api.py—после",
    "виж @src/api.py–после",
    "виж @src/api.py:2",
    "виж @src/api.py:2:5",
    "виж @src/api.py#L1-2",
    "виж @src/api.py и @./src/api.py",
])
def test_mention_with_punctuation_or_line_suffix_is_attached_once(repo, msg) -> None:
    _write(repo, {"src/api.py": "def handler():\n    return 42\n"})
    text, notes = mentions.expand(msg, repo)
    assert text.count("return 42") == 1, notes
    assert len(notes) == 1 and "прикачен файл" in notes[0], notes


# ── 6. кодов участък в ред и вложени огради ─────────────────────────────────

def test_imports_follow_markdown_code_rules(repo) -> None:
    _write(repo, {
        "GENESIS.md": ("Тест: `curl -d @payload.json http://x`\n"
                       "```\n~~~\n```\n@x.md\n"
                       "````\n```\n@in-fence.md\n````\n@after.md\n"),
        "payload.json": '{"PAYLOAD": 1}', "x.md": "XBODY",
        "in-fence.md": "INFENCE", "after.md": "AFTERBODY",
    })
    section = project_instructions.prompt_section(repo)
    assert "PAYLOAD" not in section            # @ в `код` не е импорт
    assert "XBODY" in section                  # ~~~ не затваря ``` блок
    assert "INFENCE" not in section            # ``` не затваря ```` блок
    assert "AFTERBODY" in section


# ── 7. голям файл като импорт ───────────────────────────────────────────────

def test_huge_import_is_not_read_whole(repo) -> None:
    _write(repo, {"GENESIS.md": "Лог: @logs/app.log\n"})
    (repo / "logs").mkdir()
    with open(repo / "logs" / "app.log", "wb") as fh:
        chunk = b"line of log text\n" * 65536          # ~1 MB
        fh.writelines(chunk for _ in range(30))
    del chunk
    tracemalloc.start()
    try:
        section = project_instructions.prompt_section(repo)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "line of log text" in section and len(section) < 20_000
    assert peak < 5 * 1024 * 1024, f"peak {peak // 1024} KB"


# ── 8. работна папка дълбоко под корена на репозиторията ────────────────────

def test_deep_workspace_still_gets_the_root_instructions(repo) -> None:
    ws = repo / "src" / "main" / "kotlin" / "com" / "acme" / "app" / "ui"
    ws.mkdir(parents=True)
    _write(repo, {"GENESIS.md": "ROOT-RULE", "src/main/AGENTS.md": "MID-RULE"})
    assert project_instructions._project_root(ws.resolve()) == repo.resolve()
    section = project_instructions.prompt_section(ws)
    assert "ROOT-RULE" in section and "MID-RULE" in section
    assert section.index("ROOT-RULE") < section.index("MID-RULE")
