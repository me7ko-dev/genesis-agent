"""/permissions (2026-10-15): списъкът с одобрените команди, добавяне и
махане на правило за проекта; от телефона — само списък и махане, а BLOCKED
не минава и оттам."""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import chat_commands, permissions, sandbox
from genesis_agent.sandbox import RiskLevel, RiskVerdict, SandboxPolicy


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    permissions.workspace = root
    return root


def _confirm(cmd: str = "pip install requests") -> RiskVerdict:
    v = sandbox.assess_command(cmd)
    assert v.level == RiskLevel.CONFIRM, (cmd, v)
    return v


def _seal_rules(root: Path, rules: list[str]) -> None:
    permissions._save(permissions.project_key(root), rules)


# ── /permissions ────────────────────────────────────────────────────────────

def _cmd(text: str, ws: Path) -> list[str]:
    said: list[str] = []
    res = chat_commands.handle(text, messages=None, workspace=ws, out=said.append,
                               ask=lambda q: "не")
    assert res is not None and res.prompt is None
    return said


def test_permissions_add_list_remove(repo) -> None:
    said = _cmd("/permissions add pytest *", repo)
    assert "pytest *" in said[0] and any("*" in s for s in said[1:])
    _cmd("/permissions add npm test", repo)
    permissions.remember("pip install requests")
    listing = "\n".join(_cmd("/permissions", repo))
    assert "pytest *" in listing and "npm test" in listing and "pip install requests" in listing
    assert permissions.project_rules(repo) == ["pytest *", "npm test"]
    assert "премахнато" in _cmd("/permissions remove npm test", repo)[0]
    assert "премахнато" in _cmd("/permissions remove pip install requests", repo)[0]
    assert permissions.project_rules(repo) == ["pytest *"]
    assert permissions.session_rules(repo) == []
    assert "Няма правило" in _cmd("/permissions remove git push", repo)[0]


def test_permissions_add_refuses_a_chain(repo) -> None:
    said = _cmd("/permissions add git status; rm -rf x", repo)
    assert said[0].startswith("❌")
    assert permissions.project_rules(repo) == []


def test_permissions_clear_forgets_the_session_only(repo) -> None:
    _cmd("/permissions add npm test", repo)
    permissions.remember("pip install requests")
    _cmd("/permissions clear", repo)
    assert permissions.session_rules(repo) == [] and permissions.project_rules(repo) == ["npm test"]


def test_rules_survive_a_restart_but_session_does_not(repo) -> None:
    _cmd("/permissions add npm test", repo)
    permissions.remember("pip install requests")
    permissions.clear_session()          # нов процес: паметта е празна
    assert permissions.project_rules(repo) == ["npm test"]
    assert permissions.session_rules(repo) == []


def test_permissions_is_a_builtin_command() -> None:
    assert "/permissions" in chat_commands.BUILTIN
    assert "разрешения" in chat_commands.RESERVED


# ── телефонът и десктопът ───────────────────────────────────────────────────

def _remote():
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs
    return rs, rs.RemoteSession(lambda text, ui: None)


class UI:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, text: str) -> None:
        self.infos.append(text)

    def warn(self, text: str) -> None:
        self.warns.append(text)


def _phone(rs, session, text: str, ws: Path) -> UI:
    ui = UI()
    msgs = deque([{"role": "system", "content": "s"}], maxlen=50)
    assert rs.phone_command(text, ui, session, messages=msgs, workspace=ws)[1] is None
    return ui


def test_the_phone_cannot_add_a_rule(repo) -> None:
    rs, session = _remote()
    ui = _phone(rs, session, "/permissions add pip install *", repo)
    assert ui.warns and "терминала" in ui.warns[0]
    assert permissions.project_rules(repo) == []


def test_the_phone_can_list_and_remove(repo) -> None:
    rs, session = _remote()
    permissions.add("npm test", repo)
    ui = _phone(rs, session, "/permissions", repo)
    assert "npm test" in "\n".join(ui.infos)
    _phone(rs, session, "/permissions remove npm test", repo)
    assert permissions.project_rules(repo) == []
    assert "/permissions" in rs.PHONE_HELP


def _phone_policy(session) -> SandboxPolicy:
    # Същото като rs.serve.
    return SandboxPolicy(mode="interactive",
                         confirm_fn=lambda op, verdict: session.confirm(op, list(verdict.reasons)))


def test_blocked_from_the_phone_is_refused_without_a_card(repo, monkeypatch) -> None:
    _rs, session = _remote()
    _seal_rules(repo, ["rm -rf /"])
    permissions.remember("rm -rf /")
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("ran"))
    res = sandbox.run_shell("rm -rf /", policy=_phone_policy(session))
    assert res.blocked and "SANDBOX BLOCKED" in res.stderr
    assert not [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]


def test_a_rule_also_covers_commands_asked_from_the_phone(repo, monkeypatch) -> None:
    _rs, session = _remote()
    permissions.add("pip install *", repo)
    ok = sandbox.SandboxResult(ok=True, stdout="", stderr="", returncode=0)
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: ok)
    assert not sandbox.run_shell("pip install requests", policy=_phone_policy(session)).blocked
    assert not [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]


def test_the_phone_still_answers_yes_no(repo) -> None:
    _rs, session = _remote()
    result: dict = {}

    def go() -> None:
        result["d"] = sandbox._decide("pip install requests", _confirm(), _phone_policy(session),
                                      shell=True)

    t = threading.Thread(target=go)
    t.start()
    deadline = time.time() + 5
    card = None
    while card is None and time.time() < deadline:
        card = next((e for e in session.events_after(0)["events"] if e["type"] == "confirm"), None)
        time.sleep(0.01)
    assert card is not None
    assert session.answer(card["id"], True)
    t.join(5)
    assert result["d"] == (True, "")
    # „Изпълни“ от телефона е еднократно — не се помни за сесията.
    assert permissions.session_rules(repo) == []
