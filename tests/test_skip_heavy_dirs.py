"""detect_project, REPO_MAP, търсенето и размерът преди `genesis fix` не слизат
в node_modules/.venv (2026-10-09): rglob ги обхождаше целите и чак после ги
отхвърляше — 100k файла в node_modules = 2.4–3.3 s на всяко извикване."""
from __future__ import annotations

import os
import sys

import pytest

from genesis_agent import repo_agent, repo_map


@pytest.fixture
def project(tmp_path):
    tmp_path = tmp_path / "proj"   # tmp_path носи и genesis_data от conftest
    tmp_path.mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("x = 2\n", encoding="utf-8")
    for heavy in ("node_modules/pkg/lib", ".venv/lib/site", "build/out"):
        d = tmp_path / heavy
        d.mkdir(parents=True)
        (d / "big.py").write_text("x = 3\n" * 10, encoding="utf-8")
    return tmp_path


# Audit събитието „os.scandir“ хваща всеки обход — os.walk, pathlib.rglob и
# glob на всяка версия (3.13 вика scandir по път, който monkeypatch не вижда).
# Кукичката не се маха; активна е само докато тестът я държи.
_SEEN: list[str] | None = None


def _audit(event: str, args: tuple) -> None:
    if _SEEN is not None and event in ("os.scandir", "os.listdir") and args:
        try:
            _SEEN.append(os.fspath(args[0]))
        except TypeError:
            pass


sys.addaudithook(_audit)


@pytest.fixture
def scanned():
    global _SEEN
    _SEEN = []
    try:
        yield _SEEN
    finally:
        _SEEN = None


def _heavy(seen: list[str]) -> list[str]:
    return [p for p in seen if any(h in p.replace("\\", "/").split("/")
                                   for h in ("node_modules", ".venv", "build"))]


def test_detect_project_and_repo_map_do_not_enter_node_modules(project, scanned) -> None:
    info = repo_map.detect_project(project)
    text = repo_map.repo_map(project)
    assert sum(info.file_counts.values()) == 2       # само src/ и tests/
    assert "2 файла с код" in text
    assert _heavy(scanned) == []


def test_search_and_find_files_do_not_enter_node_modules(project, scanned) -> None:
    hits = repo_map._search_python(project, r"x = \d", None, 60)
    assert sorted(os.path.basename(h.path) for h in hits) == ["app.py", "test_app.py"]
    assert repo_map._search_python(project, r"x =", "*.py", 60)
    assert repo_map.find_files("**/*.py", project) == sorted(
        [os.path.join("src", "app.py"), os.path.join("tests", "test_app.py")])
    assert repo_map.find_files("test_*.py", project) == [os.path.join("tests", "test_app.py")]
    assert _heavy(scanned) == []


def test_find_files_with_a_path_pattern_still_works(project) -> None:
    assert repo_map.find_files("src/*.py", project) == [os.path.join("src", "app.py")]


def test_tree_size_does_not_enter_node_modules(project, scanned) -> None:
    size = repo_agent._tree_size_mb(project)
    assert 0 < size * 1024 * 1024 < 100            # двата малки файла, не big.py×3
    assert _heavy(scanned) == []


def test_tree_size_still_counts_a_nested_build_folder_with_code(tmp_path) -> None:
    # `pkg/build/` с код не е артефакт (одит 2026-10-07) — влиза в размера.
    (tmp_path / "pkg" / "build").mkdir(parents=True)
    (tmp_path / "pkg" / "build" / "builder.py").write_bytes(b"x" * 2048)
    assert repo_agent._tree_size_mb(tmp_path) * 1024 * 1024 >= 2048
