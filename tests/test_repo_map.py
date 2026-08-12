"""genesis_agent.repo_map — search and project detection.

`search_code` has two backends (ripgrep, pure Python). Every search test runs
against BOTH, because the whole point of the fallback is that a machine without
ripgrep gets the same answers — a fallback nobody exercises is a fallback that
quietly rots.
"""
from __future__ import annotations

import pytest

from genesis_agent import repo_map


@pytest.fixture(params=["auto", "python"])
def backend(request, monkeypatch):
    """'auto' uses ripgrep when installed; 'python' forces the fallback."""
    if request.param == "python":
        monkeypatch.setattr(repo_map.shutil, "which", lambda _: None)
    return request.param


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def handle(x):\n    return x + 1\n\n\ndef ignore(x):\n    return x\n",
        encoding="utf-8")
    (tmp_path / "pkg" / "util.js").write_text("function handle(x) { return x; }\n",
                                              encoding="utf-8")
    (tmp_path / "notes.md").write_text("handle это документация\n", encoding="utf-8")
    junk = tmp_path / "node_modules" / "dep"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("function handle() {}\n", encoding="utf-8")
    return tmp_path


def test_finds_matches_with_file_and_line(tree, backend) -> None:
    hits = repo_map.search_code("def handle", tree)
    assert len(hits) == 1
    assert hits[0].path.endswith("core.py")
    assert hits[0].line == 1


def test_skips_dependency_directories(tree, backend) -> None:
    """node_modules is the difference between 3 hits and 30000."""
    paths = [h.path for h in repo_map.search_code("handle", tree)]
    assert not any("node_modules" in p for p in paths)
    assert len(paths) == 3  # core.py, util.js, notes.md


def test_glob_narrows_the_search(tree, backend) -> None:
    hits = repo_map.search_code("handle", tree, glob="*.py")
    assert len(hits) == 1
    assert hits[0].path.endswith("core.py")


def test_no_matches_is_an_empty_list_not_an_error(tree, backend) -> None:
    assert repo_map.search_code("нищоТакова", tree) == []


def test_max_results_is_honoured(tree, backend) -> None:
    assert len(repo_map.search_code("handle", tree, max_results=2)) == 2


def test_invalid_regex_raises_valueerror(tree, monkeypatch) -> None:
    monkeypatch.setattr(repo_map.shutil, "which", lambda _: None)
    with pytest.raises(ValueError):
        repo_map.search_code("unclosed(", tree)


def test_missing_path_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        repo_map.search_code("x", tmp_path / "nope")


# ── detect_project ───────────────────────────────────────────────────────────

def test_detects_python_and_pytest(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    info = repo_map.detect_project(tmp_path)
    assert info.language == "python"
    assert "pytest" in info.test_command
    assert "app.py" in info.entry_points


def test_package_json_without_test_script_claims_no_command(tmp_path) -> None:
    """A marker file is not a promise the command exists.

    Claiming `npm test` here sends the repair loop chasing a shell error
    ("Missing script: test") instead of the bug it was asked to fix.
    """
    (tmp_path / "package.json").write_text('{"name":"x","scripts":{"build":"tsc"}}',
                                           encoding="utf-8")
    (tmp_path / "index.js").write_text("//\n", encoding="utf-8")
    info = repo_map.detect_project(tmp_path)
    assert info.test_command == ""
    assert info.language == "javascript"


def test_package_json_with_test_script_is_used(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
    assert repo_map.detect_project(tmp_path).test_command == "npm test"


def test_makefile_without_test_target_is_skipped(tmp_path) -> None:
    (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n", encoding="utf-8")
    assert repo_map.detect_project(tmp_path).test_command == ""


def test_repo_map_reports_missing_tests_and_missing_git(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = repo_map.repo_map(tmp_path)
    assert "не са открити" in out
    assert "НЕ (няма version control)" in out


class TestTestsWithoutAPackagingMarker:
    """Found live (2026-08-12) running `genesis fix` on a two-file project.

    Detection asked only "is there a marker file?", so `calc.py` +
    `test_calc.py` with no pyproject.toml reported no test command at all. The
    repair loop's test gate — repo_agent's whole notion of a verdict — never
    ran: it burned all 8 rounds, and a repair that was actually correct came
    back labelled "промените НЕ са проверени". A missing marker is no more a
    promise than a present one (see test_package_json_without_test_script).
    """

    def test_root_level_test_file_is_enough(self, tmp_path) -> None:
        (tmp_path / "calc.py").write_text("def mean(v): return sum(v)/len(v)\n",
                                          encoding="utf-8")
        (tmp_path / "test_calc.py").write_text("from calc import mean\n", encoding="utf-8")
        info = repo_map.detect_project(tmp_path)
        assert "pytest" in info.test_command
        assert info.language == "python"

    def test_suffix_style_names_count_too(self, tmp_path) -> None:
        (tmp_path / "calc_test.py").write_text("def test_x(): pass\n", encoding="utf-8")
        assert "pytest" in repo_map.detect_project(tmp_path).test_command

    def test_a_tests_directory_counts(self, tmp_path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_calc.py").write_text("def test_x(): pass\n",
                                                          encoding="utf-8")
        assert "pytest" in repo_map.detect_project(tmp_path).test_command

    def test_a_marker_file_still_wins(self, tmp_path) -> None:
        """Ordering must not change for projects that already declared how they
        are tested — this is a fallback, not a new first rule."""
        (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}',
                                               encoding="utf-8")
        (tmp_path / "test_thing.py").write_text("def test_x(): pass\n", encoding="utf-8")
        assert repo_map.detect_project(tmp_path).test_command == "npm test"

    def test_a_plain_module_named_test_something_deep_inside_does_not_count(
        self, tmp_path
    ) -> None:
        """Deliberately narrow: `pkg/test_helpers.py` is a helper module at
        least as often as it is a test suite, and claiming a test command that
        collects nothing is the failure this fix exists to prevent."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "test_helpers.py").write_text("HELPER = 1\n", encoding="utf-8")
        assert repo_map.detect_project(tmp_path).test_command == ""

    def test_no_python_tests_at_all_still_reports_nothing(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert repo_map.detect_project(tmp_path).test_command == ""


# ── find_files (GLOB) ────────────────────────────────────────────────────

def test_find_files_matches_by_name_pattern(tree) -> None:
    hits = repo_map.find_files("**/*.py", tree)
    assert any(h.endswith("core.py") for h in hits)
    assert not any(h.endswith(".js") for h in hits)


def test_find_files_skips_dependency_directories(tree) -> None:
    hits = repo_map.find_files("**/*.js", tree)
    assert not any("node_modules" in h for h in hits)
    assert any(h.endswith("util.js") for h in hits)


def test_find_files_no_matches_returns_empty_list(tree) -> None:
    assert repo_map.find_files("**/*.nonexistent", tree) == []


def test_find_files_missing_root_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        repo_map.find_files("*.py", tmp_path / "nope")
