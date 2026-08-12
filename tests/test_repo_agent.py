"""genesis_agent.repo_agent — the safety net, not the LLM loop.

What is worth testing here is what must hold regardless of which model answers:
a checkpoint that really restores, a test result that is really the project's
own, history compaction that does not corrupt the conversation, and a summary
that never claims a fix the tests did not confirm. The model's behaviour inside
the loop is not simulated — a fake model proves nothing about a real one.
"""
from __future__ import annotations

import pytest

from genesis_agent import repo_agent

SRC = "def median(v):\n    return sorted(v)[len(v) // 2]\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "stats.py").write_text(SRC, encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n", encoding="utf-8")
    return root


# ── checkpoints ──────────────────────────────────────────────────────────────

def test_checkpoint_restores_edited_file(project) -> None:
    cp = repo_agent.create_checkpoint(project)
    (project / "stats.py").write_text("напълно друго\n", encoding="utf-8")
    repo_agent.restore_checkpoint(project)
    assert (project / "stats.py").read_text(encoding="utf-8") == SRC
    assert cp.exists()


def test_checkpoint_restore_removes_nothing_it_did_not_snapshot(project) -> None:
    """tar extraction overlays; a file created after the snapshot survives.

    Worth pinning down: restore is 'put the originals back', not 'wipe the
    directory'. A user who wrote notes.md mid-repair should not lose it.
    """
    repo_agent.create_checkpoint(project)
    (project / "notes.md").write_text("моите бележки\n", encoding="utf-8")
    repo_agent.restore_checkpoint(project)
    assert (project / "notes.md").exists()


def test_latest_checkpoint_picks_the_newest(project, monkeypatch) -> None:
    stamps = iter(["20260101-000000", "20260102-000000"])
    monkeypatch.setattr(repo_agent.time, "strftime", lambda *_a: next(stamps))
    repo_agent.create_checkpoint(project)
    second = repo_agent.create_checkpoint(project)
    assert repo_agent.latest_checkpoint(project) == second


def test_restore_without_checkpoint_says_so(project) -> None:
    assert "Няма намерена снимка" in repo_agent.restore_checkpoint(project)


def test_oversized_project_is_refused_not_silently_unprotected(project, monkeypatch) -> None:
    monkeypatch.setattr(repo_agent, "_tree_size_mb", lambda _root: 999.0)
    with pytest.raises(RuntimeError, match="прекалено голям"):
        repo_agent.create_checkpoint(project)


def test_snapshot_skips_dependency_dirs(project) -> None:
    junk = project / "node_modules" / "x"
    junk.mkdir(parents=True)
    (junk / "big.js").write_text("y" * 1000, encoding="utf-8")
    cp = repo_agent.create_checkpoint(project)
    import tarfile
    with tarfile.open(cp) as tar:
        assert not any("node_modules" in n for n in tar.getnames())


# ── tests as the verdict ─────────────────────────────────────────────────────

def test_run_tests_reports_pass_and_fail(project) -> None:
    (project / "tests" / "test_ok.py").write_text("def test_x():\n    assert True\n",
                                                  encoding="utf-8")
    good = repo_agent.run_tests(project, "python3 -m pytest -q")
    assert good.ran and good.passed

    (project / "tests" / "test_bad.py").write_text("def test_y():\n    assert False\n",
                                                   encoding="utf-8")
    bad = repo_agent.run_tests(project, "python3 -m pytest -q")
    assert bad.ran and not bad.passed
    assert "test_y" in bad.output


def test_no_test_command_means_not_run_rather_than_passed(project) -> None:
    res = repo_agent.run_tests(project, "")
    assert res.ran is False
    assert res.passed is False  # never confuse "unknown" with "green"


def test_repair_refuses_a_missing_directory(tmp_path) -> None:
    out = repo_agent.repair(tmp_path / "nope", "каквото и да е")
    assert out.success is False
    assert out.checkpoint is None  # nothing was created for a bad path


# ── history compaction ───────────────────────────────────────────────────────

def _tool_conversation(pairs: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "s"},
                        {"role": "user", "content": "задача"}]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "READ_FILE", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"резултат {i}"})
    return msgs


def test_short_history_is_left_alone(project) -> None:
    msgs = _tool_conversation(2)
    assert repo_agent._compact_history(msgs) == msgs


def test_compaction_keeps_system_task_and_recent_context(project) -> None:
    out = repo_agent._compact_history(_tool_conversation(20))
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "задача"
    assert "съкратени" in out[2]["content"]
    assert out[-1]["content"] == "резултат 19"
    assert len(out) < 42


def test_compaction_never_leaves_an_orphan_tool_result(project) -> None:
    """A `tool` message whose parent `tool_calls` was cut is invalid to the API.

    This is the shape that 400s, so it is asserted directly rather than
    inferred from the message count.
    """
    for pairs in range(8, 25):
        out = repo_agent._compact_history(_tool_conversation(pairs))
        seen_ids = set()
        for m in out:
            for tc in m.get("tool_calls") or []:
                seen_ids.add(tc["id"])
            if m.get("role") == "tool":
                assert m["tool_call_id"] in seen_ids, f"осиротял tool резултат при {pairs}"


def test_stall_nudge_fires_only_after_rounds_without_edits() -> None:
    msgs: list[dict] = []
    assert repo_agent._nudge_if_stalled(msgs, [], 1) is False
    assert msgs == []
    # Something was already edited — the loop is progressing, do not interrupt.
    assert repo_agent._nudge_if_stalled(msgs, ["stats.py"], 9) is False
    assert repo_agent._nudge_if_stalled(msgs, [], repo_agent._STALL_ROUNDS) is True
    assert "НИТО ЕДНА промяна" in msgs[-1]["content"]


class TestVerdictIsNeverStale:
    """Found by running `genesis fix` against a real broken project
    (2026-08-12). run_tests() was only reached in the "model returned no tool
    calls" branch, while `after` started life as `before`. A model that spends
    its whole round budget on tool calls — REPO_MAP, READ_FILE, GLOB,
    EDIT_FILE, RUN_CMD is already five — left the loop with a verdict computed
    from the state BEFORE its changes. Observed live: the fix was applied and
    correct, the tests passed, and the tool answered "НЕ е поправено" and
    recommended --revert, i.e. throw the working fix away. For a subsystem
    whose first principle is "THE TESTS ARE THE VERDICT", a verdict from stale
    data is worse than none.
    """

    def test_tests_are_rerun_when_the_loop_ends_without_a_verdict(
        self, project, monkeypatch
    ) -> None:
        runs: list[str] = []

        def _fake_run_tests(root, command):
            runs.append(command)
            # Red before any change, green once something has been touched.
            passed = len(runs) > 1
            return repo_agent.TestRun(ran=True, passed=passed, output="", command=command)

        monkeypatch.setattr(repo_agent, "run_tests", _fake_run_tests)
        # A brain that only ever makes tool calls, never a plain reply.
        class _ToolOnlyBrain:
            def __init__(self, *a, **kw) -> None:
                pass

            def complete(self, messages, tools=None):
                return type("R", (), {
                    "raw_text": "",
                    "tool_calls": [{"id": "1", "function": {
                        "name": "EDIT_FILE",
                        "arguments": '{"path": "stats.py", "old": "a", "new": "b"}'}}],
                })()

        monkeypatch.setattr(repo_agent, "Brain", _ToolOnlyBrain)
        monkeypatch.setattr("genesis_skills.dispatch_tool_call",
                            lambda name, args: "[EDIT_FILE: stats.py] ✓ 1 замяна")

        out = repo_agent.repair(str(project), "fix it", max_rounds=2)

        # Two calls: the "before" baseline and the final verdict pass.
        assert len(runs) == 2, "the tests must be re-run once the rounds are spent"
        assert out.success is True


class TestTouchedTracksRealChanges:
    """A refused edit is not a change. `touched` was filled from the tool's
    ARGUMENTS, so a failed EDIT_FILE counted as one — which also silenced
    _nudge_if_stalled (it only nudges while `touched` is empty), removing the
    "stop reading, act" push exactly when the model still had not changed
    anything."""

    def test_a_failed_edit_is_not_counted_as_a_change(self) -> None:
        assert repo_agent._edit_succeeded("[EDIT_FILE: a.py] ❌ Anchor-ът не е намерен") is False

    def test_a_successful_edit_is_counted(self) -> None:
        assert repo_agent._edit_succeeded("[EDIT_FILE: a.py] ✓ 1 замяна, +2/-1 реда") is True

    def test_tag_results_ignore_refused_edits(self, project) -> None:
        results = [
            "[EDIT_FILE: stats.py] ❌ Anchor-ът не е намерен в stats.py",
            "[WRITE_FILE: notes.md] ✓ записани 12 символа",
        ]
        paths = repo_agent._paths_from_tag_results(project, results)
        assert paths == ["notes.md"]
