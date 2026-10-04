"""scripts/loop_diff.py — the deterministic old-vs-new check for the turn loop.

A checkout compared with itself must come out identical: if this fails, the
tool (or the scripted world around run_turn) broke, and with it the proof
that a loop refactor changes nothing the model sees.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("loop_diff", ROOT / "scripts" / "loop_diff.py")
ld = importlib.util.module_from_spec(_spec)
sys.modules["loop_diff"] = ld
_spec.loader.exec_module(ld)


def test_this_checkout_against_itself_is_identical(capsys) -> None:
    assert ld.main([str(ROOT), str(ROOT), "--turns", "40"]) == 0
    assert "40 хода: 40 еднакви, 0 сриват стария цикъл (празен отговор), 0 различни" in capsys.readouterr().out


def test_the_turns_are_reproducible_and_varied() -> None:
    a, b = ld.make_turns(30, seed=7), ld.make_turns(30, seed=7)
    assert a == b
    kinds = {r[0] for t in a for r in t["replies"] if r[1] is None}
    assert {"Готово.", "Инсталирах пакета."} & kinds
    assert any(r[1] for t in a for r in t["replies"]), "има и native извиквания"


def test_a_difference_is_reported() -> None:
    same_turn = {"requests": [[{"role": "user", "content": "x"}]], "final": [], "events": [], "error": ""}
    other = {**same_turn, "final": [{"role": "assistant", "content": "друго"}]}
    assert ld.compare([same_turn, same_turn], [same_turn, other]) == (1, 0, [1])


def test_the_old_empty_reply_crash_is_counted_apart() -> None:
    old = {"requests": [["r1"]], "final": [], "events": [],
           "error": "TypeError: object of type 'NoneType' has no len()"}
    new = {"requests": [["r1"], ["r2"]], "final": [{"role": "assistant", "content": ""}],
           "events": [], "error": ""}
    assert ld.compare([old], [new]) == (0, 1, [])
