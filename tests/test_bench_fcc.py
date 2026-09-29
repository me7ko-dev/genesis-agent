"""scripts/bench_fcc.py — parsing and judging, on a made-up challenge (the real
ones are freeCodeCamp's and are not in the repo)."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("bench_fcc", ROOT / "scripts" / "bench_fcc.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

CHALLENGE = '''---
id: 000
title: "Challenge 7: Double It"
challengeType: 29
dashedName: challenge-7
---

# --description--

Return twice the number.

# --hints--

`double(2)` should return `4`.

```js
({test: () => { runPython(`
from unittest import TestCase
TestCase().assertEqual(double(2), 4)`)
}})
```

`double("a\\\\n")` should return `"a\\\\na\\\\n"`.

```js
({test: () => { runPython(`
from unittest import TestCase
TestCase().assertEqual(double("a\\\\n"), "a\\\\na\\\\n")`)
}})
```

`double(0)` should return `0`.

```js
({test: () => { runPython(`
    from unittest import TestCase
    TestCase().assertEqual(double(0), 0)
  `)
}})
```

# --seed--

## --seed-contents--

```py
def double(n):

    return n
```

# --solutions--

```py
def double(n):
    return n * 2
```
'''


def test_parse_reads_every_part():
    ch = bf.parse(CHALLENGE)
    assert ch["num"] == 7 and ch["title"] == "Challenge 7: Double It"
    assert ch["description"] == "Return twice the number."
    assert ch["seed"].startswith("def double(n):")
    assert "n * 2" in ch["solution"]
    assert ch["unsupported"] is None
    assert ch["tests"][0]["label"] == "`double(2)` should return `4`."
    # the JS template literal's \\n is the two characters \ and n in Python
    assert 'double("a\\n")' in ch["tests"][1]["code"]
    # indented test code is dedented, or exec would fail on it
    assert ch["tests"][2]["code"].startswith("from unittest")


def test_a_hint_that_is_not_plain_runpython_skips_the_challenge():
    text = CHALLENGE.replace("({test: () => { runPython(`\nfrom unittest import TestCase\n"
                             "TestCase().assertEqual(double(2), 4)`)\n}})",
                             "({test: () => assert.equal(runPython(`double(2)`), 4)})")
    assert bf.parse(text)["unsupported"].startswith("не е чист runPython")


def test_js_template_escapes():
    assert bf.js_template(r"a\nb\`c\\d\x41B\u{43}") == "a\nb`c\\dABC"


def test_pick_is_spread_and_stable():
    chs = [{"num": n} for n in range(1, 101)]
    got = [c["num"] for c in bf.pick(chs, 4)]
    assert got == [13, 38, 63, 88] and got == [c["num"] for c in bf.pick(chs, 4)]
    assert bf.pick(chs, 500) == chs


def test_task_shows_only_the_first_examples():
    task = bf.task_text(bf.parse(CHALLENGE), examples=1)
    assert "solution.py" in task and "def double(n):" in task
    assert "`double(2)` should return `4`." in task
    assert "double(0)" not in task


def test_run_tests_judges_each_test_on_its_own(tmp_path):
    ch = bf.parse(CHALLENGE)
    sol = tmp_path / "solution.py"
    sol.write_text("def double(n):\n    return n * 2 if n else 1\n", encoding="utf-8")
    res = bf.run_tests(sys.executable, sol, ch["tests"], tmp_path / "t")
    assert [r["ok"] for r in res] == [True, True, False]
    assert res[2]["error"].startswith("AssertionError")


def test_missing_solution_fails_every_test(tmp_path):
    ch = bf.parse(CHALLENGE)
    res = bf.run_tests(sys.executable, tmp_path / "solution.py", ch["tests"], tmp_path / "t")
    assert [r["error"] for r in res] == ["няма solution.py"] * 3


def test_solution_that_reads_input_on_import_does_not_hang(tmp_path):
    ch = bf.parse(CHALLENGE)
    sol = tmp_path / "solution.py"
    sol.write_text("def double(n):\n    return n * 2\nprint(double(int(input())))\n", encoding="utf-8")
    res = bf.run_tests(sys.executable, sol, ch["tests"][:1], tmp_path / "t")
    assert not res[0]["ok"] and "EOFError" in res[0]["error"]
