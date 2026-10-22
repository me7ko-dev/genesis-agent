"""Regressions for the memory bugs an audit reproduced on 2026-10-22
(workspace_memory, memory, episodic_memory, the REMEMBER handler)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import workspace_memory as wm

ROOT = Path(__file__).resolve().parent.parent

# Изглеждат като истински ключове (цифри, главни букви, точната дължина), за
# да ги хване всяко правило; не са ничии.
_GH = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0dF3gH6"
_STRIPE = "sk_live_" + "51HxQ2eLkdIwHu7ix0a9b8c7Z"


@pytest.fixture(autouse=True)
def _own_workspace(monkeypatch, tmp_path):
    # set_workspace е глобално и на двата модула — всеки тест го връща.
    monkeypatch.setattr(gs, "_WORKSPACE", gs._WORKSPACE)
    monkeypatch.setattr(wm, "_workspace", wm._norm(tmp_path / "project"))


def _env(tmp_path: Path, data: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               GENESIS_DATA_DIR=str(data), GENESIS_HISTORY_DIR=str(tmp_path / "history"),
               PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    env.pop("GENESIS_MEMORY_DIR", None)
    return env


def test_secrets_given_to_remember_reach_neither_other_prompts_nor_the_export(tmp_path) -> None:
    from genesis_agent import deliver
    shop, other = tmp_path / "shop", tmp_path / "other"
    shop.mkdir()
    other.mkdir()
    (shop / "app.py").write_text("print('shop')\n", encoding="utf-8")
    gs.set_workspace(shop)
    replies = [
        gs.dispatch_tool_call("REMEMBER", {"kind": "preference", "topic": "stripe",
                                           "value": f"ключът е {_STRIPE}"}),
        gs.dispatch_tool_call("REMEMBER", {"kind": "decision",
                                           "value": f"деплой с GITHUB_TOKEN={_GH}",
                                           "why": "операторът го даде"}),
        gs.dispatch_tool_call("TASK_ADD", {"title": "пусни деплоя",
                                           "next_step": f"git push https://me:{_GH}@github.com/a/b"}),
    ]
    assert all(".env" in r for r in replies), replies
    # Запис отпреди поправката: вече е в базата, не бива да излезе навън.
    with wm._conn() as c:
        c.execute("INSERT INTO decisions (what, why, created_at, workspace) VALUES (?, ?, ?, ?);",
                  (f"стар запис с {_GH}", "", wm._now(), wm._norm(shop)))
    gs.set_workspace(other)
    brief = wm.briefing()
    report = deliver.export(shop, tmp_path / "out.zip", run_tests=False).report
    assert "stripe" in brief and "деплой с GITHUB_TOKEN" in report and "пусни деплоя" in report
    for text in [brief, report, *replies]:
        assert _GH not in text and _STRIPE not in text, text


def test_a_corrupt_memory_database_is_set_aside_and_memory_keeps_working(tmp_path) -> None:
    junk = b"not a database at all " * 200
    # (а) повредени persistent_memory.db и episodes.db при старт на чата
    data = tmp_path / "data"
    data.mkdir()
    (data / "persistent_memory.db").write_bytes(junk)
    (data / "episodes.db").write_bytes(junk)
    code = ("import genesis_terminal_agent as g, genesis_skills as s; "
            "print(g._conv_mem is not None, g._memory_context is not None, "
            "s.memory_record_episode is not None)")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=_env(tmp_path, data),
                       capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[-3:] == ["True", "True", "True"], r.stdout + r.stderr
    assert (data / "persistent_memory.db.corrupt").read_bytes() == junk
    assert (data / "episodes.db.corrupt").read_bytes() == junk

    # (б) повредена workspace_memory.db по време на работа — две сесии подред
    Path(wm.DB_PATH).write_bytes(junk)
    gs.set_workspace(tmp_path / "project")
    for _ in range(2):
        for name, args in (("REMEMBER", {"kind": "decision", "value": "ползваме SQLite"}),
                           ("TASK_ADD", {"title": "довърши CLI"})):
            out = gs.dispatch_tool_call(name, args)
            assert "Грешка" not in out, out
    assert Path(str(wm.DB_PATH) + ".corrupt").read_bytes() == junk
    assert [d["what"] for d in wm.list_decisions()] == ["ползваме SQLite"]

    # (в) и епизодите, и KV паметта в същия процес
    from genesis_agent import episodic_memory as em
    from genesis_agent import memory
    Path(em.DB_PATH).write_bytes(junk)
    Path(memory.DB_PATH).write_bytes(junk)
    memory.memory_record_episode("цел", "резултат")
    memory.memory_store("k", "v")
    assert memory.memory_recall("k") == "v"
    assert [e["goal"] for e in em._fetch_all_episodes()] == ["цел"]


def test_a_preference_topic_in_another_case_replaces_the_old_value() -> None:
    wm.set_preference("език на комитите", "английски")
    wm.set_preference("Език на комитите", "български")
    wm.set_preference("ЕЗИК  НА КОМИТИТЕ", "само български, никога английски")
    assert list(wm.list_preferences().values()) == ["само български, никога английски"]
    assert ": английски" not in wm.briefing()


def test_the_preferences_block_of_the_briefing_is_bounded() -> None:
    for i in range(300):
        wm.set_preference(f"тема {i}", f"правило номер {i} " + "x" * 300)
    brief = wm.briefing()
    assert len(brief) < 6000, len(brief)
    assert "правило номер 299 " in brief          # най-новото е вътре
    assert "… и още" in brief


_RACER = """
import json, sys, time
from pathlib import Path
from genesis_agent import workspace_memory as wm
wm.DB_PATH = Path(sys.argv[1])
wm.set_workspace(sys.argv[2])
Path(sys.argv[4]).touch()
go = Path(sys.argv[3])
while not go.exists():
    time.sleep(0.001)
errors = []
for k in [0, 1, 2, 3, 4] * 2:
    for fn, text in ((wm.add_thread, "migrate to postgres %d" % k),
                     (wm.add_decision, "use postgres %d" % k)):
        try:
            fn(text)
        except Exception as e:
            errors.append(repr(e))
print(json.dumps(errors))
"""


def test_concurrent_writers_do_not_duplicate_threads_or_decisions(tmp_path, monkeypatch) -> None:
    racer = tmp_path / "racer.py"
    racer.write_text(_RACER, encoding="utf-8")
    db, folder = tmp_path / "race.db", tmp_path / "proj"
    folder.mkdir()
    env = _env(tmp_path, tmp_path / "data")
    errors: list[str] = []
    for rnd in range(3):                     # първият кръг е върху нова база
        go = tmp_path / f"go{rnd}"
        ready = [tmp_path / f"ready{rnd}_{n}" for n in range(8)]
        procs = [subprocess.Popen([sys.executable, str(racer), str(db), str(folder), str(go), str(r)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for r in ready]
        deadline = time.monotonic() + 60
        while not all(r.exists() for r in ready) and time.monotonic() < deadline:
            time.sleep(0.01)
        go.touch()
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
            errors += json.loads(out.strip().splitlines()[-1])
    monkeypatch.setattr(wm, "DB_PATH", db)
    wm.set_workspace(folder)
    assert errors == []
    assert len(wm.list_threads("all", 100)) == 5
    assert len(wm.list_decisions(100)) == 5


def test_a_remember_preference_without_topic_is_saved() -> None:
    from genesis_agent.tool_schemas import FULL_TOOLS
    [schema] = [t for t in FULL_TOOLS if t["function"]["name"] == "REMEMBER"]
    assert "topic" not in schema["function"]["parameters"]["required"]
    out = gs.dispatch_tool_call("REMEMBER", {"kind": "preference", "value": "винаги пиши type hints"})
    assert "✓" in out, out
    assert list(wm.list_preferences().values()) == ["винаги пиши type hints"]
