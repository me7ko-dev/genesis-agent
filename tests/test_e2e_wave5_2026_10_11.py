"""E2E проверката 2026-10-11 (вълна 5, интеграция): истинският цикъл с
фалшиви модели през сесии, които минават през няколко подсистеми — проект с
CRLF/cp1251 файлове, червен→зелен pytest, 429 и отрязан tool call, режим план,
ASK_USER, /undo след команда, фонова команда, `genesis fix` в подпапка на
monorepo с мръсно дърво, `genesis export`, телефонът и MCP. Всеки тест пада
без поправката си (пуснато срещу кода преди нея)."""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import zipfile
from collections import deque

import pytest

# _clean — автоматичната фикстура на веригата: без ключове, cooldown-и и статистика на оператора.
from test_brain_chain_hardening import FakeResp, _clean, _route, chain, ok  # noqa: F401

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import (
    brain,
    claim_check,
    deliver,
    edit_history,
    hooks,
    mentions,
    repo_agent,
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list = []

    def fake(messages, tools=None):
        seen.append(list(messages))
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


class _UI(gta.TurnUI):
    def __init__(self):
        self.log: list = []

    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): self.log.append(("assistant", text))
    def tool(self, name, result): self.log.append(("tool", name, result))
    def asked(self, q): self.log.append(("asked", q))
    def spinning(self, n): self.log.append(("spin", n))
    def warn(self, t): self.log.append(("warn", t))
    def info(self, t): self.log.append(("info", t))


def _fresh():
    return deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN)


def _warnings(ui):
    return [e[1] for e in ui.log if e[0] == "warn"]


# ── F1: /undo връща и conftest.py, създаден от скелето за тестове ───────────

def test_undo_removes_the_conftest_the_test_scaffold_created(ws, monkeypatch):
    _script(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "egn/egn.py", "content": "def ok(x):\n    return True\n"}, "w1"),
              _call("WRITE_FILE", {"path": "egn/tests/test_egn.py",
                                   "content": "from egn import ok\n\n\ndef test_ok():\n    assert ok(1)\n"},
                    "w2")]),
        ("Записах egn.py и теста му.", None), ("Пробвах.", None)])
    gta.run_turn(_fresh(), "направи egn", _UI())
    assert (ws / "egn" / "conftest.py").is_file()
    label, done = edit_history.undo()
    assert label == "направи egn"
    assert not (ws / "egn" / "conftest.py").exists(), done
    assert not (ws / "egn" / "egn.py").exists()


def test_undo_keeps_a_conftest_that_was_there_before(ws, monkeypatch):
    (ws / "egn").mkdir()
    (ws / "egn" / "conftest.py").write_text("# моят\n", encoding="utf-8")
    _script(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "egn/tests/test_egn.py", "content": "def test_a():\n    pass\n"},
                    "w1")]),
        ("Записах теста.", None), ("Пуснах го.", None)])
    gta.run_turn(_fresh(), "тест", _UI())
    edit_history.undo()
    assert (ws / "egn" / "conftest.py").read_text(encoding="utf-8") == "# моят\n"


# ── F2: модел в дълъг cooldown не купува 8 s пауза и втори платен кръг ──────


def test_a_429_on_the_only_keyed_model_does_not_pause_for_nothing(monkeypatch):
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    slept: list = []
    monkeypatch.setattr(brain.time, "sleep", slept.append)
    seen = _route(monkeypatch, lambda p: FakeResp(429, {"error": {"message": "rate"}}))
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}])
    assert [m for m, _ in seen] == ["A"] and slept == []
    assert r.raw_text.startswith("Error:") and "HTTP_429" in r.raw_text


def test_a_429_next_to_a_cut_write_does_not_pay_for_the_cut_twice(monkeypatch):
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A"), ("nvidia", "C")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("NVIDIA_API_KEY", "n")
    slept: list = []
    monkeypatch.setattr(brain.time, "sleep", slept.append)
    cut = json.dumps({"path": "big.py", "content": "x = 1\n" * 50})[:120]

    def handler(p):
        if p["model"] == "A":
            return FakeResp(429, {"error": {"message": "rate"}}, {"retry-after": "30"})
        tc = [{"id": "c1", "type": "function", "function": {"name": "WRITE_FILE", "arguments": cut}}]
        return ok(None, tool_calls=tc, finish="length", usage={"prompt_tokens": 9, "completion_tokens": 4096})

    seen = _route(monkeypatch, handler)
    r = brain.Brain(use_local=False).complete([{"role": "user", "content": "big.py"}],
                                              tools=[{"type": "function", "function": {"name": "WRITE_FILE"}}])
    assert [m for m, _ in seen] == ["A", "C"] and slept == []
    assert r.tool_calls[0]["function"]["arguments"] == cut


def test_a_short_429_still_gets_the_second_round(monkeypatch):
    monkeypatch.setattr(brain, "_load_chain", lambda: chain(("groq", "A")))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    clock = [1000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])
    monkeypatch.setattr(brain.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    answers = iter([FakeResp(429, {"error": {"message": "tpm"}}, {"retry-after": "2"}), ok("от A")])
    seen = _route(monkeypatch, lambda p: next(answers))
    assert brain.Brain(use_local=False).complete([{"role": "user", "content": "hi"}]).raw_text == "от A"
    assert [m for m, _ in seen] == ["A", "A"]


# ── F3: дифът на `genesis fix` от снимката пази CRLF и cp1251 ───────────────

def _git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True,
                          check=False).stdout


def test_fix_in_a_dirty_monorepo_subfolder_diffs_only_the_changed_line(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "cps")
    mono = tmp_path / "mono"
    svc = mono / "svc"
    (svc / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(mono)], check=True)
    _git(mono, "config", "user.email", "a@b")
    _git(mono, "config", "user.name", "a")
    (svc / "calc.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    (svc / "names.py").write_bytes("# -*- coding: cp1251 -*-\r\nNAME = 'Иван'\r\n".encode("cp1251"))
    (svc / "tests" / "test_calc.py").write_text(
        "from calc import add\nfrom names import NAME\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_name():\n    assert NAME == 'Иван Петров'\n", encoding="utf-8")
    (svc / "pyproject.toml").write_text(
        "[project]\nname='svc'\nversion='0'\n[tool.pytest.ini_options]\npythonpath=['.']\n", encoding="utf-8")
    _git(mono, "add", "-A")
    _git(mono, "commit", "-qm", "init")
    (mono / "web.js").write_text("x=2\n", encoding="utf-8")       # чужда мръсотия → снимка
    (svc / "notes.txt").write_text("мои бележки\n", encoding="utf-8")

    replies = iter([
        [_call("READ_FILE", {"path": "calc.py"}, "a"), _call("READ_FILE", {"path": "names.py"}, "b")],
        [_call("EDIT_FILE", {"path": "calc.py", "old": "return a - b", "new": "return a + b"}, "c"),
         _call("EDIT_FILE", {"path": "names.py", "old": "'Иван'", "new": "'Иван Петров'"}, "d")],
    ])

    class FakeBrain:
        def __init__(self, *a, **k):
            pass

        def complete(self, messages, tools=None):
            calls = next(replies, None)
            return type("R", (), {"raw_text": "" if calls else "Поправих.", "tool_calls": calls})()
    monkeypatch.setattr(repo_agent, "Brain", FakeBrain)
    out = repo_agent.repair(str(svc), "add вади, името е непълно",
                            test_command=f'"{sys.executable}" -m pytest -q', on_status=lambda m: None)
    assert out.success and out.checkpoint is not None
    assert out.files_touched == ["calc.py", "names.py"]
    changed = [ln for ln in out.diff.split("\n") if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    assert changed == ["-    return a - b\r", "+    return a + b\r",
                       "-NAME = 'Иван'\r", "+NAME = 'Иван Петров'\r"], out.diff
    assert (svc / "names.py").read_bytes() == "# -*- coding: cp1251 -*-\r\nNAME = 'Иван Петров'\r\n".encode("cp1251")


# ── F4: фонова команда (native background=true) не е свършило пускане ───────

_RED = "[RUN_CMD: python -m pytest -q]  (rc=1)\n..F\n1 failed, 2 passed"
_BG = ("[RUN_BG: python -m pytest -q] ▶ пуснато във фона като bg1. Изходът: BG_OUTPUT bg1; "
       "спиране: BG_KILL bg1.\n[BG_OUTPUT: bg1] (върви; python -m pytest -q)\n(нищо ново)")


def test_a_background_test_run_does_not_hide_the_red_one(ws, monkeypatch):
    monkeypatch.setattr(gs, "dispatch_tool_call",
                        lambda name, args: _BG if args.get("background") else _RED)
    claim = "Готово! Всички тестове минават."
    seen = _script(monkeypatch, [
        ("", [_call("RUN_CMD", {"command": "python -m pytest -q"}, "t1")]),
        ("", [_call("RUN_CMD", {"command": "python -m pytest -q", "background": True}, "t2")]),
        (claim, None), (claim, None)])
    ui = _UI()
    gta.run_turn(_fresh(), "оправи тестовете", ui)
    warnings = _warnings(ui)
    assert len(seen) == 4, "подкана след твърдението"
    assert any(w.startswith(claim_check.RED_TESTS_WARNING) for w in warnings), warnings


def test_a_background_run_alone_proves_no_green_tests_on_either_path():
    native = claim_check.counts_as_executed("RUN_CMD", "python -m pytest -q True", _BG)
    text = claim_check.executed_from_text_results([_BG])
    assert [native] == text == [("RUN_BG", "python -m pytest -q")]
    assert claim_check.unsupported_claims("Всички тестове минават.", [native])


# ── F5: `@файл` в cp1251 и UTF-16 стига до модела като текст ────────────────

def test_a_mention_reads_cp1251_and_utf16_like_read_file(tmp_path):
    (tmp_path / "lib.py").write_bytes("NAME = 'Здравей'\r\n".encode("cp1251"))
    (tmp_path / "req.txt").write_bytes("requests==2.32\r\n".encode("utf-16"))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02text")
    text, notes = mentions.expand("виж @lib.py, @req.txt и @blob.bin", tmp_path)
    assert "NAME = 'Здравей'\n" in text and "�" not in text
    assert "requests==2.32\n" in text and "﻿" not in text
    assert any("@blob.bin: двоичен" in n for n in notes)


# ── F6: export не праща вписан ключ от .mcp.json на проекта ─────────────────

def _export(root, out):
    deliver.export(root, out, run_tests=False)
    return [n.split("/", 1)[1] for n in zipfile.ZipFile(out).namelist()]


@pytest.mark.parametrize("server", [
    {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer 9f8e7d6c5b4a3928"}},
    {"url": "https://mcp.example.com/mcp", "headers": {"X-API-Key": "abcdef0123"}},
    {"command": "db-mcp", "env": {"DB_PASSWORD": "Tr0ub4dor&3xyz"}},
])
def test_export_withholds_an_mcp_json_with_a_literal_secret(tmp_path, server):
    root = tmp_path / "p"
    root.mkdir()
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": server}}), encoding="utf-8")
    assert _export(root, tmp_path / "o.zip") == ["app.py", deliver.REPORT_NAME]


def test_export_keeps_an_mcp_json_that_only_references_variables(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "gh": {"url": "https://api.githubcopilot.com/mcp/",
               "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}},
        "db": {"command": "db-mcp", "env": {"DB_PASSWORD": "${DB_PASSWORD}", "PATH": "/usr/bin",
                                            "LOG_LEVEL": "debug"}}}}), encoding="utf-8")
    assert ".mcp.json" in _export(root, tmp_path / "o.zip")


# ── F7: отказ от hook / режим план / оператора не доказва записа ────────────

def test_a_write_blocked_by_an_operator_hook_is_not_proof_of_writing(ws, monkeypatch):
    from genesis_agent import paths
    stop = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'no writes\'); sys.exit(2)"'
    (paths.GENESIS_HOME / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "WRITE_FILE", "command": stop}]}}), encoding="utf-8")
    seen = _script(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "notes.md", "content": "x"}, "w1")]),
        ("Записах notes.md с бележките.", None), ("Не успях — hook-ът го спря.", None)])
    ui = _UI()
    gta.run_turn(_fresh(), "запиши бележки в notes.md", ui)
    assert not (ws / "notes.md").exists()
    assert len(seen) == 3 and any("никой изпълнен" in w for w in _warnings(ui))


@pytest.mark.parametrize("result", [
    "[WRITE_FILE] ⛔ Спряно от hook на оператора:\nno",
    "[WRITE_FILE] ⏸ Режим план: нищо не се променя, докато операторът не одобри плана (/plan).",
    "[WRITE_FILE] ⏹ Спряно от оператора — не е изпълнено.",
    "[WRITE_FILE: a.py] ⛔ Това е маркер за изпуснато от историята, не съдържание",
], ids=["hook", "plan", "cancel", "elided"])
def test_gate_refusals_are_not_executed(result):
    assert claim_check.counts_as_executed("WRITE_FILE", "a.py x", result) is None


# ── F8: резултатите от текстови тагове стигат до оператора ──────────────────

def test_text_tag_results_reach_the_ui_and_the_headless_json(ws, monkeypatch, capsys):
    from genesis_agent import headless
    (ws / "calc.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    _script(monkeypatch, [
        ("[EDIT_FILE: calc.py]\nreturn a - b\n---GENESIS-REPLACE-WITH---\nreturn a + b\n[END_EDIT]", None),
        (f'[RUN_CMD: "{sys.executable}" -c "from calc import add; print(add(2, 3))"]', None),
        ("Поправих add: 2 + 3 дава 5.", None), ("Пробвах и с отрицателни.", None)])
    code = headless.run(["--json", "--cwd", str(ws), "оправи add в calc.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["tools"] == ["EDIT_FILE", "RUN_CMD"], out
    assert (ws / "calc.py").read_bytes() == b"def add(a, b):\r\n    return a + b\r\n"
