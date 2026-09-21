"""„Това новата версия ли е" — въпрос, на който досега нямаше отговор.

`genesis --version` печаташе един и същ номер преди и след обновяване, защото
номерът се вдига рядко, а кодът се мени всеки ден. Оттам и разумното, но
скъпо хрумване „да изтрия всичко и да инсталирам наново" — което трие и
ключовете, и паметта.

pip вече записва отговора при инсталация от git: `direct_url.json` (PEP 610)
носи точния комит и поискания клон. Проверено срещу реална инсталация от
клона на 2026-09-20; формата по-долу е копие на истинския файл.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import version_info as vi

_REAL = {
    "url": "https://github.com/me7ko-dev/genesis-agent",
    "vcs_info": {"commit_id": "dc7f65a089f2521a25c9903b302133109109fa62",
                 "requested_revision": "claude/token-upgrade-ipe4yg",
                 "vcs": "git"},
}


def _installed(monkeypatch, payload) -> None:
    """Подменя точно това, което pip е оставил на диска."""
    text = payload if isinstance(payload, str) else json.dumps(payload)

    class _Dist:
        def read_text(self, name):
            return text if name == "direct_url.json" else None

    monkeypatch.setattr("importlib.metadata.distribution", lambda _n: _Dist())


class TestItReadsWhatPipWrote:
    def test_the_commit_and_branch_come_through(self, monkeypatch) -> None:
        _installed(monkeypatch, _REAL)
        src = vi.installed_source()
        assert src.commit.startswith("dc7f65a")
        assert src.ref == "claude/token-upgrade-ipe4yg"

    def test_the_version_line_names_the_commit(self, monkeypatch) -> None:
        _installed(monkeypatch, _REAL)
        line = vi.describe("0.2.0")
        assert "0.2.0" in line and "dc7f65a" in line
        assert "claude/token-upgrade-ipe4yg" in line

    def test_a_checkout_says_nothing_extra(self, monkeypatch) -> None:
        """Чекаут за разработка няма direct_url.json. Това не е грешка."""
        monkeypatch.setattr("importlib.metadata.distribution",
                            lambda _n: (_ for _ in ()).throw(Exception("няма")))
        assert vi.installed_source() is None
        assert vi.describe("0.2.0") == "genesis-agent 0.2.0"

    @pytest.mark.parametrize("payload", ["не е json", "{}", '{"url": "x"}',
                                         '{"vcs_info": {}}'])
    def test_a_broken_record_is_not_a_crash(self, monkeypatch, payload) -> None:
        _installed(monkeypatch, payload)
        assert vi.installed_source() is None


class TestTheRepositoryIsParsedFromTheUrl:
    @pytest.mark.parametrize("url", [
        "https://github.com/me7ko-dev/genesis-agent",
        "https://github.com/me7ko-dev/genesis-agent.git",
        "git+https://github.com/me7ko-dev/genesis-agent",
        "ssh://git@github.com/me7ko-dev/genesis-agent.git",
    ])
    def test_the_usual_forms_all_work(self, url) -> None:
        assert vi.Source(url=url).owner_repo == "me7ko-dev/genesis-agent"

    def test_a_non_github_url_gives_nothing(self) -> None:
        assert vi.Source(url="https://gitlab.com/a/b").owner_repo == ""


class TestAskingGitHub:
    def _answer(self, monkeypatch, payload, *, fail=None):
        class _Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, _n=None): return json.dumps(payload).encode()

        def _open(_req, timeout=None):
            if fail:
                raise fail
            return _Response()

        monkeypatch.setattr(vi.urllib.request, "urlopen", _open)

    def test_the_head_commit_is_returned(self, monkeypatch) -> None:
        self._answer(monkeypatch, {"sha": "abc123"})
        assert vi.latest_commit("a/b", "main") == "abc123"

    def test_no_network_is_not_a_crash(self, monkeypatch) -> None:
        """Мрежата липсва по-често от всичко друго. Това не бива да чупи
        команда, чийто отговор е просто „не знам сега"."""
        self._answer(monkeypatch, {}, fail=OSError("няма мрежа"))
        assert vi.latest_commit("a/b", "main") is None

    def test_an_unexpected_answer_is_not_a_crash(self, monkeypatch) -> None:
        self._answer(monkeypatch, ["друг формат"])
        assert vi.latest_commit("a/b", "main") is None

    def test_no_repo_means_no_request(self, monkeypatch) -> None:
        self._answer(monkeypatch, {}, fail=AssertionError("не биваше да пита"))
        assert vi.latest_commit("", "main") is None


class TestChangelogBetweenTwoCommits:
    def _answer(self, monkeypatch, payload, *, fail=None):
        class _Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, _n=None): return json.dumps(payload).encode()

        def _open(_req, timeout=None):
            if fail:
                raise fail
            return _Response()

        monkeypatch.setattr(vi.urllib.request, "urlopen", _open)

    def test_subjects_come_back_newest_first(self, monkeypatch) -> None:
        self._answer(monkeypatch, {"commits": [
            {"commit": {"message": "fix: старото\n\nтяло"}},
            {"commit": {"message": "feat: новото"}},
        ]})
        assert vi.changelog("a/b", "aaa", "bbb") == ["feat: новото", "fix: старото"]

    def test_more_commits_than_the_limit_keeps_the_newest(self, monkeypatch) -> None:
        self._answer(monkeypatch, {"commits": [
            {"commit": {"message": f"commit {i}"}} for i in range(5)
        ]})
        assert vi.changelog("a/b", "aaa", "bbb", limit=2) == ["commit 4", "commit 3"]

    def test_no_network_is_not_a_crash(self, monkeypatch) -> None:
        self._answer(monkeypatch, {}, fail=OSError("няма мрежа"))
        assert vi.changelog("a/b", "aaa", "bbb") is None

    def test_an_unexpected_answer_is_not_a_crash(self, monkeypatch) -> None:
        self._answer(monkeypatch, {"nope": True})
        assert vi.changelog("a/b", "aaa", "bbb") is None

    def test_missing_arguments_mean_no_request(self, monkeypatch) -> None:
        self._answer(monkeypatch, {}, fail=AssertionError("не биваше да пита"))
        assert vi.changelog("", "aaa", "bbb") is None
        assert vi.changelog("a/b", "", "bbb") is None
        assert vi.changelog("a/b", "aaa", "") is None


class TestTheReportAnswersThePlainQuestion:
    def _both(self, monkeypatch, latest, *, subjects=None):
        _installed(monkeypatch, _REAL)
        monkeypatch.setattr(vi, "latest_commit", lambda *a, **k: latest)
        monkeypatch.setattr(vi, "changelog", lambda *a, **k: subjects)

    def test_up_to_date_says_so(self, monkeypatch) -> None:
        self._both(monkeypatch, _REAL["vcs_info"]["commit_id"])
        assert "най-новото" in vi.update_report(version="0.2.0")

    def test_behind_names_the_newer_commit_and_the_command(self, monkeypatch) -> None:
        self._both(monkeypatch, "f" * 40)
        out = vi.update_report(version="0.2.0")
        assert "fffffff" in out
        assert "pipx install --force" in out
        assert "claude/token-upgrade-ipe4yg" in out

    def test_behind_lists_what_the_update_actually_brings(self, monkeypatch) -> None:
        self._both(monkeypatch, "f" * 40, subjects=["fix(x): нещо", "feat(y): друго"])
        out = vi.update_report(version="0.2.0")
        assert "fix(x): нещо" in out
        assert "feat(y): друго" in out

    def test_no_changelog_is_not_a_crash(self, monkeypatch) -> None:
        """Compare API-то може да е недостъпно, докато /commits е отговорило."""
        self._both(monkeypatch, "f" * 40, subjects=None)
        out = vi.update_report(version="0.2.0")
        assert "pipx install --force" in out

    def test_an_unreachable_github_still_gives_the_command(self, monkeypatch) -> None:
        """Отговор „не знам" пак трябва да е полезен."""
        self._both(monkeypatch, None)
        out = vi.update_report(version="0.2.0")
        assert "pipx install --force" in out

    def test_a_checkout_is_told_to_use_git_pull(self, monkeypatch) -> None:
        monkeypatch.setattr(vi, "installed_source", lambda: None)
        assert "git pull" in vi.update_report(version="0.2.0")


class TestTheVersionHasOneSource:
    def test_the_cli_reports_the_package_version(self) -> None:
        """Беше на три места с три различни стойности, а `--version` печаташе
        третата. Версия, която не съвпада със себе си, изглежда като отговор."""
        import genesis_agent
        from genesis_agent import cli
        assert cli.__version__ is genesis_agent.__version__
