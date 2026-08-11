"""genesis_agent.local_repair_agent — the emergency fallback that patches
code when the main Brain is unreachable, previously untested. PatternFixer
is pure regex logic (thorough coverage below); TinyLLM's network calls are
mocked; LocalRepairAgent._test_code executes the candidate code through
genesis_agent.sandbox in deny mode (it used to shell out to a bare
`subprocess.run([sys.executable, "-c", code])` — a real sandbox bypass on a
production path, fixed 2026-08-12, see TestTestCodeGoesThroughSandbox)."""
from __future__ import annotations

import pytest

from genesis_agent.local_repair_agent import LocalRepairAgent, PatternFixer, TinyLLM


class TestFixIndentation:
    def test_replaces_tabs_with_spaces_when_error_matches(self) -> None:
        code = "def f():\n\treturn 1\n"
        out = PatternFixer.fix_indentation(code, "IndentationError: expected an indented block")
        assert out == "def f():\n    return 1\n"

    def test_tab_error_also_matches(self) -> None:
        code = "def f():\n\treturn 1\n"
        assert PatternFixer.fix_indentation(code, "TabError: inconsistent use of tabs") is not None

    def test_unrelated_error_returns_none(self) -> None:
        assert PatternFixer.fix_indentation("x\t=1", "NameError: x is not defined") is None

    def test_no_tabs_to_fix_returns_none(self) -> None:
        assert PatternFixer.fix_indentation("def f():\n    return 1\n", "IndentationError: x") is None


class TestFixMissingImport:
    def test_adds_pip_install_snippet(self) -> None:
        out = PatternFixer.fix_missing_import("import requests\n", "No module named 'requests'")
        assert "pip', 'install', 'requests'" in out
        assert out.endswith("import requests\n")

    def test_uses_only_the_top_level_package(self) -> None:
        out = PatternFixer.fix_missing_import("", "No module named 'foo.bar.baz'")
        assert "'foo'" in out
        assert "'foo.bar.baz'" not in out

    def test_no_match_returns_none(self) -> None:
        assert PatternFixer.fix_missing_import("code", "SyntaxError: invalid syntax") is None

    def test_does_not_double_insert_if_already_present(self) -> None:
        already = "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', 'x', '-q'], capture_output=True)\nrest"
        assert PatternFixer.fix_missing_import(already, "No module named 'x'") is None


class TestFixNoneType:
    def test_inserts_a_guard_before_the_offending_line(self) -> None:
        code = "x = None\nx.upper()\n"
        error = "TypeError: 'NoneType' object has no attribute 'upper', line 2"
        out = PatternFixer.fix_none_type(code, error)
        assert out is not None
        lines = out.split("\n")
        assert "if x is None:" in lines[1]

    def test_no_line_number_returns_none(self) -> None:
        assert PatternFixer.fix_none_type("x.upper()", "TypeError: NoneType has no upper") is None

    def test_unrelated_error_returns_none(self) -> None:
        assert PatternFixer.fix_none_type("code", "KeyError: 'x'") is None

    def test_line_number_out_of_range_returns_none(self) -> None:
        assert PatternFixer.fix_none_type("x=1", "TypeError: NoneType, line 99") is None


class TestFixKeyError:
    def test_replaces_bracket_access_with_get(self) -> None:
        code = 'd = {}\nprint(d["missing"])\n'
        out = PatternFixer.fix_key_error(code, "KeyError: 'missing'")
        assert out == 'd = {}\nprint(d.get("missing"))\n'

    def test_no_key_error_returns_none(self) -> None:
        assert PatternFixer.fix_key_error("code", "NameError: x") is None

    def test_key_not_present_returns_none(self) -> None:
        assert PatternFixer.fix_key_error("d['a']", "KeyError: 'b'") is None


class TestFixFileNotFound:
    def test_adds_makedirs_and_import_os(self) -> None:
        out = PatternFixer.fix_file_not_found(
            "open('/tmp/x/y.txt')", "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/x/y.txt'"
        )
        assert "import os" in out
        assert "os.makedirs(os.path.dirname('/tmp/x/y.txt')" in out

    def test_does_not_double_import_os(self) -> None:
        out = PatternFixer.fix_file_not_found(
            "import os\nopen('/tmp/x/y.txt')", "FileNotFoundError: '/tmp/x/y.txt'"
        )
        assert out.count("import os") == 1

    def test_no_match_returns_none(self) -> None:
        assert PatternFixer.fix_file_not_found("code", "KeyError: 'x'") is None


class TestFixEncoding:
    def test_adds_utf8_encoding_to_bare_open(self) -> None:
        out = PatternFixer.fix_encoding("open('f.txt')", "UnicodeDecodeError: 'utf-8' codec can't decode")
        assert "encoding='utf-8'" in out

    def test_leaves_open_with_encoding_already_set_alone(self) -> None:
        code = "open('f.txt', encoding='latin-1')"
        assert PatternFixer.fix_encoding(code, "UnicodeDecodeError: codec") is None

    def test_no_match_returns_none(self) -> None:
        assert PatternFixer.fix_encoding("code", "KeyError: 'x'") is None


class TestFixUndefinedName:
    def test_defines_the_missing_name_after_imports(self) -> None:
        code = "import os\nprint(missing_var)\n"
        out = PatternFixer.fix_undefined_name(code, "name 'missing_var' is not defined")
        lines = out.split("\n")
        assert lines[1] == "missing_var = None  # auto-fix: NameError"

    def test_builtins_are_never_redefined(self) -> None:
        assert PatternFixer.fix_undefined_name("print(x)", "name 'print' is not defined") is None

    def test_no_match_returns_none(self) -> None:
        assert PatternFixer.fix_undefined_name("code", "KeyError: 'x'") is None


class TestTryAll:
    def test_returns_first_matching_fix_in_priority_order(self) -> None:
        # IndentationError check runs before the KeyError check.
        code = "d = {}\nif True:\n\tprint(d['x'])\n"
        error = "IndentationError: unexpected indent"
        fixed, desc = PatternFixer.try_all(code, error)
        assert fixed is not None
        assert "\t" not in fixed
        assert "отстъп" in desc

    def test_returns_none_when_nothing_matches(self) -> None:
        fixed, desc = PatternFixer.try_all("print(1)", "ZeroDivisionError: division by zero")
        assert fixed is None
        assert desc == ""


class TestTinyLLMDetect:
    def test_detects_lm_studio_when_it_answers_with_models(self, monkeypatch) -> None:
        class _Resp:
            status_code = 200
            def json(self):
                return {"data": [{"id": "some-model"}]}

        monkeypatch.setattr(
            "genesis_agent.local_repair_agent.requests.get",
            lambda url, timeout=3: _Resp() if "1234" in url else (_ for _ in ()).throw(RuntimeError("no ollama")),
        )
        llm = TinyLLM()
        assert llm.is_available() is True
        assert llm.model == "some-model"

    def test_falls_back_to_ollama_when_lm_studio_is_absent(self, monkeypatch) -> None:
        class _Resp:
            status_code = 200
            def json(self):
                return {"models": [{"name": "llama3.2"}]}

        def _get(url, timeout=3):
            if "1234" in url:
                raise ConnectionError("no lm studio")
            return _Resp()

        monkeypatch.setattr("genesis_agent.local_repair_agent.requests.get", _get)
        llm = TinyLLM()
        assert llm.is_available() is True
        assert llm.ollama_model == "llama3.2"

    def test_neither_available_returns_false(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.local_repair_agent.requests.get",
            lambda url, timeout=3: (_ for _ in ()).throw(ConnectionError("down")),
        )
        assert TinyLLM().is_available() is False

    def test_availability_is_cached_after_first_check(self, monkeypatch) -> None:
        calls = []

        def _get(url, timeout=3):
            calls.append(url)
            raise ConnectionError("down")

        monkeypatch.setattr("genesis_agent.local_repair_agent.requests.get", _get)
        llm = TinyLLM()
        llm.is_available()
        llm.is_available()
        # 2 endpoints probed once each, not twice.
        assert len(calls) == 2


class TestExtractCode:
    def test_extracts_python_fenced_block(self) -> None:
        text = "Here you go:\n```python\nprint(1)\n```\nDone."
        assert TinyLLM._extract_code(text) == "print(1)"

    def test_extracts_plain_fenced_block(self) -> None:
        text = "```\nprint(1)\n```"
        assert TinyLLM._extract_code(text) == "print(1)"

    def test_extracts_raw_code_without_fences(self) -> None:
        assert TinyLLM._extract_code("import os\nprint(os.getcwd())") == "import os\nprint(os.getcwd())"

    def test_returns_none_for_prose(self) -> None:
        assert TinyLLM._extract_code("I could not fix this error, sorry.") is None


class TestLocalRepairAgentRepair:
    def test_pattern_fix_that_passes_returns_immediately(self, monkeypatch) -> None:
        agent = LocalRepairAgent()
        monkeypatch.setattr(agent.llm, "is_available", lambda: False)
        result = agent.repair("def f():\n\treturn 1\n", "IndentationError: bad indent")
        assert result.fixed is True
        assert result.method == "pattern"
        assert result.rounds == 1
        assert "\t" not in result.code

    def test_no_llm_and_pattern_insufficient_stops_after_one_round(self, monkeypatch) -> None:
        agent = LocalRepairAgent()
        monkeypatch.setattr(agent.llm, "is_available", lambda: False)
        # A syntax error pattern fixes don't touch -> pattern fix returns
        # None, LLM unavailable -> loop must break after round 1, not retry
        # MAX_ROUNDS times.
        result = agent.repair("def f(:\n    pass\n", "SyntaxError: invalid syntax")
        assert result.fixed is False
        assert result.method == "none"

    def test_llm_fix_that_passes_is_used_when_pattern_fails(self, monkeypatch) -> None:
        agent = LocalRepairAgent()
        monkeypatch.setattr(agent.llm, "is_available", lambda: True)
        monkeypatch.setattr(agent.llm, "model", "tiny-model", raising=False)
        monkeypatch.setattr(agent.llm, "fix", lambda code, error: "print('fixed')")
        result = agent.repair("this is not python(((", "SyntaxError: invalid syntax")
        assert result.fixed is True
        assert result.method == "llm"

    def test_exhausts_all_rounds_when_nothing_works(self, monkeypatch) -> None:
        from genesis_agent.local_repair_agent import MAX_ROUNDS
        agent = LocalRepairAgent()
        monkeypatch.setattr(agent.llm, "is_available", lambda: True)
        monkeypatch.setattr(agent.llm, "model", "tiny-model", raising=False)
        monkeypatch.setattr(agent.llm, "fix", lambda code, error: "still not python(((")
        result = agent.repair("this is not python(((", "SyntaxError: invalid syntax")
        assert result.fixed is False
        assert result.rounds == MAX_ROUNDS


class TestTestCode:
    def test_syntax_error_is_caught_without_a_subprocess(self) -> None:
        result = LocalRepairAgent._test_code("def f(:\n")
        assert result["ok"] is False
        assert "SyntaxError" in result["error"]

    def test_runtime_success_reports_ok(self) -> None:
        result = LocalRepairAgent._test_code("print('hi')")
        assert result["ok"] is True

    def test_runtime_error_reports_stderr(self) -> None:
        result = LocalRepairAgent._test_code("raise RuntimeError('boom')")
        assert result["ok"] is False
        assert "boom" in result["error"]


class TestTestCodeGoesThroughSandbox:
    """SECURITY regression (fixed 2026-08-12).

    _test_code used to run LLM-generated repair code via a bare
    `subprocess.run([sys.executable, "-c", code])`. sandbox.py documents
    itself as the single choke point for exactly this, and going around it
    skipped all four of its guarantees at once: the minimal-env whitelist
    (so the code under repair could read the parent process's entire
    os.environ, API keys included), CPU/memory/file resource limits, the
    process-group kill on timeout, and the SAFE/CONFIRM/BLOCKED risk gate.
    It is reachable in production — autonomous_loop calls emergency_repair
    whenever a mission exhausts its rounds — and PatternFixer.fix_missing_import
    deliberately injects a `pip install <name parsed from traceback text>`
    line into the very code that then got executed unsandboxed.
    """

    def test_dangerous_code_is_refused_not_executed(self) -> None:
        result = LocalRepairAgent._test_code("import os\nos.system('rm -rf /')")
        assert result["ok"] is False

    def test_it_actually_calls_the_sandbox(self, monkeypatch) -> None:
        """Pin down the routing itself: a future refactor must not quietly
        go back to raw subprocess just because the assertions above would
        still pass by coincidence."""
        called = {}

        def fake_run_python(code, **kw):
            called["code"] = code
            called["policy_mode"] = getattr(kw.get("policy"), "mode", None)
            return type("R", (), {"blocked": False, "ok": True, "stdout": "", "stderr": ""})()

        from genesis_agent import sandbox
        monkeypatch.setattr(sandbox, "run_python", fake_run_python)

        result = LocalRepairAgent._test_code("print('hi')")
        assert result["ok"] is True
        assert called["code"] == "print('hi')"
        assert called["policy_mode"] == "deny"

    def test_sandbox_block_is_reported_as_a_failure_not_a_pass(self, monkeypatch) -> None:
        def fake_run_python(code, **kw):
            return type("R", (), {"blocked": True, "ok": False,
                                   "stdout": "", "stderr": "[SANDBOX BLOCKED] nope"})()

        from genesis_agent import sandbox
        monkeypatch.setattr(sandbox, "run_python", fake_run_python)

        result = LocalRepairAgent._test_code("whatever")
        assert result["ok"] is False
        assert "sandbox" in result["error"].lower()


class TestEmergencyRepair:
    def test_uses_a_singleton_agent(self) -> None:
        from genesis_agent.local_repair_agent import get_repair_agent
        assert get_repair_agent() is get_repair_agent()

    def test_emergency_repair_delegates_to_the_singleton(self, monkeypatch) -> None:
        from genesis_agent import local_repair_agent as lra
        agent = lra.get_repair_agent()
        monkeypatch.setattr(agent, "repair", lambda code, error, stdout="": "sentinel")
        assert lra.emergency_repair("code", "err", "out") == "sentinel"
