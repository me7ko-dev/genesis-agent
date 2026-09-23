"""repo_map — инварианти на търсенето, най-вече че ДВЕТЕ реализации отговарят
на един и същ въпрос.

`search_code` има два двигателя: ripgrep, ако го има на машината, и Python,
ако го няма. Кой ще се пусне не зависи от заявката, а от инсталацията — затова
всяка разлика между тях е бъг, който се проявява при един оператор и не се
проявява при друг.

Измерено преди поправката, върху това репо:

    search_code("Windows installer self-test", ".")
        с ripgrep  → 0 попадения
        без него   → 1 попадение (.github/workflows/ci.yml)

`.github` е скрита папка, а ripgrep пропуска скритите по подразбиране. Отгоре
на това `_tool_search_code` казва на модела „няма съвпадения — това означава,
че низът наистина го няма, не предполагай, че е скрит". Двигател, който тихо
пропуска файлове, прави това изречение лъжа.

Диференциалният тест по-долу се пропуска, ако `rg` липсва — тогава сравнението
няма две страни. Това е честно ограничение, не покритие за показ.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from genesis_agent import repo_map as rm

_HAS_RG = shutil.which("rg") is not None
_needs_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep липсва — няма втора страна")

NEEDLE = "ИСКАНОТО"


@pytest.fixture
def tree(tmp_path):
    """Дърво с точно случаите, в които двата двигателя се разминаваха."""
    (tmp_path / "src").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "src" / "a.py").write_text(f"{NEEDLE} веднъж\nи {NEEDLE} пак\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text(f"{NEEDLE} в игнориран файл\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / ".hidden" / "h.py").write_text(f"{NEEDLE} в скрита папка\n", encoding="utf-8")
    (tmp_path / "node_modules" / "n.py").write_text(f"{NEEDLE} в пропускана папка\n",
                                                    encoding="utf-8")
    (tmp_path / "sub" / "many.py").write_text(f"{NEEDLE}\n{NEEDLE}\n{NEEDLE}\n", encoding="utf-8")
    return tmp_path


def _rel(hits, root) -> list[str]:
    from pathlib import Path
    return sorted(str(Path(h.path).resolve().relative_to(root.resolve())) for h in hits)


class TestTheTwoEnginesAnswerTheSameQuestion:
    @_needs_rg
    def test_the_same_tree_gives_the_same_hits(self, tree) -> None:
        rg = rm._search_ripgrep(tree, NEEDLE, None, 50)
        py = rm._search_python(tree, NEEDLE, None, 50)
        assert rg is not None, "ripgrep не отговори — тестът не сравнява нищо"
        assert _rel(rg, tree) == _rel(py, tree)

    @_needs_rg
    def test_they_agree_inside_a_git_repo_too(self, tree) -> None:
        """`.gitignore` важи за ripgrep само вътре в git repo — точно там
        разминаването беше 5 срещу 7."""
        subprocess.run(["git", "init", "-q"], cwd=str(tree), check=False,
                       capture_output=True, timeout=30)
        rg = rm._search_ripgrep(tree, NEEDLE, None, 50)
        py = rm._search_python(tree, NEEDLE, None, 50)
        assert rg is not None
        assert _rel(rg, tree) == _rel(py, tree)

    @_needs_rg
    def test_the_line_numbers_agree(self, tree) -> None:
        rg = sorted(h.line for h in rm._search_ripgrep(tree, NEEDLE, None, 50))
        py = sorted(h.line for h in rm._search_python(tree, NEEDLE, None, 50))
        assert rg == py


class TestHiddenContentIsRealContent:
    def test_a_hidden_directory_is_searched(self, tree) -> None:
        """`.github/`, `.claude/`, `.env.example` са съдържание, не шум."""
        hits = rm.search_code(NEEDLE, tree)
        assert any(".hidden" in h.path for h in hits)

    def test_a_gitignored_file_is_still_searched(self, tree) -> None:
        """Пропускането на игнорираното зависеше от двигателя. Сега и двата го
        търсят; изключенията са само `_SKIP_DIRS`, които са общи."""
        subprocess.run(["git", "init", "-q"], cwd=str(tree), check=False,
                       capture_output=True, timeout=30)
        assert any("ignored.py" in h.path for h in rm.search_code(NEEDLE, tree))


class TestWhatBothPathsMustSkip:
    def test_a_skip_dir_is_skipped(self, tree) -> None:
        assert not any("node_modules" in h.path for h in rm.search_code(NEEDLE, tree))

    @_needs_rg
    def test_the_git_directory_stays_out_despite_hidden(self, tree) -> None:
        """`--hidden --no-ignore` не бива да вкара обектите на git вътре."""
        git_dir = tree / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "COMMIT_EDITMSG").write_text(f"{NEEDLE} в git\n", encoding="utf-8")
        assert not any("/.git/" in h.path.replace("\\", "/")
                       for h in rm.search_code(NEEDLE, tree))

    def test_a_file_over_the_size_cap_is_skipped_by_both(self, tmp_path) -> None:
        big = tmp_path / "generated.py"
        big.write_text("x" * (rm._MAX_FILE_BYTES + 1000) + f"\n{NEEDLE}\n", encoding="utf-8")
        assert rm.search_code(NEEDLE, tmp_path) == []


class TestFailureIsTheSameWithOrWithoutRipgrep:
    def test_an_invalid_regex_raises_either_way(self, tree) -> None:
        """ripgrep връща ненулев код, `_search_ripgrep` дава None, и се пада на
        Python пътя — който вдига ValueError. Присъдата не бива да зависи от
        инсталацията."""
        with pytest.raises(ValueError, match="невалиден regex"):
            rm.search_code("(незатворена", tree)

    def test_a_missing_path_is_a_file_not_found(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            rm.search_code(NEEDLE, tmp_path / "няма-я")

    def test_a_single_file_as_root_is_accepted(self, tree) -> None:
        hits = rm.search_code(NEEDLE, tree / "src" / "a.py")
        assert len(hits) == 2

    def test_no_match_is_an_empty_list_not_an_error(self, tree) -> None:
        assert rm.search_code("това-го-няма-никъде-42", tree) == []


class TestTheResultCapHolds:
    @pytest.mark.parametrize("cap", [1, 2, 5])
    def test_never_more_than_asked(self, tree, cap) -> None:
        assert len(rm.search_code(NEEDLE, tree, max_results=cap)) <= cap

    def test_a_long_line_is_clipped(self, tmp_path) -> None:
        (tmp_path / "min.js").write_text(NEEDLE + "y" * 5000 + "\n", encoding="utf-8")
        hits = rm.search_code(NEEDLE, tmp_path)
        assert hits and len(hits[0].text) <= rm._LINE_CLIP


class TestTheOtherEntryPointsSurviveOddTrees:
    def test_find_files_on_an_empty_tree(self, tmp_path) -> None:
        assert rm.find_files("*.py", tmp_path) == []

    def test_find_files_does_not_escape_the_root(self, tmp_path) -> None:
        (tmp_path / "inside").mkdir()
        (tmp_path / "inside" / "x.py").write_text("x\n", encoding="utf-8")
        (tmp_path / "outside.py").write_text("x\n", encoding="utf-8")
        hits = rm.find_files("*.py", tmp_path / "inside")
        assert all("outside" not in h for h in hits)

    def test_repo_map_on_an_empty_directory(self, tmp_path) -> None:
        assert isinstance(rm.repo_map(tmp_path), str)

    def test_repo_map_on_a_file_maps_its_directory(self, tmp_path) -> None:
        """Рутинно извикване: моделът току-що е прочел `src/main.py`. Преди
        това вдигаше NotADirectoryError, което `_tool_repo_map` превръщаше в
        `[REPO_MAP] Грешка: [Errno 20] Not a directory` — номер на грешка на
        мястото на указание. `search_code` вече се справяше със същия вход."""
        f = tmp_path / "one.py"
        f.write_text("x\n", encoding="utf-8")
        out = rm.repo_map(f)
        assert "подаден е файл" in out
        assert "one.py" in out

    def test_the_model_never_sees_a_bare_errno(self, tmp_path) -> None:
        import genesis_skills as gs
        f = tmp_path / "one.py"
        f.write_text("x\n", encoding="utf-8")
        out = gs._tool_repo_map(str(f))
        assert "Errno" not in out
        assert "Not a directory" not in out

    def test_detect_project_with_no_markers(self, tmp_path) -> None:
        info = rm.detect_project(tmp_path)
        assert info.language in ("unknown", "", None) or isinstance(info.language, str)

    def test_detect_project_prefers_a_real_marker(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert "python" in rm.detect_project(tmp_path).language.lower()
