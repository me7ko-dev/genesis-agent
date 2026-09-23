"""genesis_agent.self_update — обновяване, без да пипа файла, докато тече.

pipx подменя точно изпълнимия файл на инсталираното копие; докато Genesis
тече, Windows го държи заключен за запис. Затова обновяването никога не
тръгва от процеса, който трябва да бъде подменен: то чака оригиналният PID
да излезе, чак тогава намира истински работещ `pipx` (не по име на PATH —
sys.executable на ТОЗИ процес е интерпретаторът в pipx-венва на Genesis,
който няма pipx като модул) и записва резултата за следващото стартиране.

Реалният WinAPI път (`_pid_alive_win32`) няма смислен mock на Linux — той
тества самия ctypes, не логиката тук. Проверен е на живо само от
windows-latest CI leg-а (`skipif` по-долу); навсякъде другаде тестваме само
ДИСПЕЧЕРА (`pid_alive` вика правилния помощник според sys.platform).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from genesis_agent import self_update as su

# ── pid_alive — диспечер + реална POSIX логика ─────────────────────────────

class TestPidAliveDispatch:
    def test_win32_platform_calls_the_win32_helper(self, monkeypatch) -> None:
        monkeypatch.setattr(su.sys, "platform", "win32")
        monkeypatch.setattr(su, "_pid_alive_win32", lambda pid: "win32-called")
        monkeypatch.setattr(su, "_pid_alive_posix", lambda pid: (_ for _ in ()).throw(
            AssertionError("posix helper не биваше да се вика на win32")))
        assert su.pid_alive(123) == "win32-called"

    def test_any_other_platform_calls_the_posix_helper(self, monkeypatch) -> None:
        monkeypatch.setattr(su.sys, "platform", "linux")
        monkeypatch.setattr(su, "_pid_alive_posix", lambda pid: "posix-called")
        monkeypatch.setattr(su, "_pid_alive_win32", lambda pid: (_ for _ in ()).throw(
            AssertionError("win32 helper не биваше да се вика извън win32")))
        assert su.pid_alive(123) == "posix-called"


class TestPidAlivePosix:
    def test_the_current_process_is_alive(self) -> None:
        assert su._pid_alive_posix(os.getpid()) is True

    def test_a_missing_process_is_not_alive(self, monkeypatch) -> None:
        monkeypatch.setattr("os.kill", lambda pid, sig: (_ for _ in ()).throw(
            ProcessLookupError()))
        assert su._pid_alive_posix(999999) is False

    def test_a_process_we_cannot_signal_still_counts_as_alive(self, monkeypatch) -> None:
        """PermissionError значи процесът съществува — просто не е наш (напр.
        root процес). Погрешно False тук би пуснал pipx твърде рано."""
        monkeypatch.setattr("os.kill", lambda pid, sig: (_ for _ in ()).throw(
            PermissionError()))
        assert su._pid_alive_posix(1) is True

    def test_an_unexpected_os_error_is_not_a_crash(self, monkeypatch) -> None:
        monkeypatch.setattr("os.kill", lambda pid, sig: (_ for _ in ()).throw(
            OSError("нещо друго")))
        assert su._pid_alive_posix(1) is True


@pytest.mark.skipif(sys.platform != "win32", reason="реална WinAPI, само на Windows")
class TestPidAliveWin32Real:
    def test_the_current_process_is_alive(self) -> None:
        assert su._pid_alive_win32(os.getpid()) is True

    def test_a_pid_that_never_existed_is_not_alive(self) -> None:
        # PID-ове под 4 никога не са валидни потребителски процеси на Windows.
        assert su._pid_alive_win32(1) is False


# ── find_pipx — проверява чрез изпълнение, не по име ────────────────────────

class TestFindPipx:
    def _fake_run(self, ok_for: set[str]):
        def _run(argv, **kwargs):
            exe = argv[0]
            key = " ".join(argv[:-1])  # без --version
            if key in ok_for:
                return subprocess.CompletedProcess(argv, 0, stdout="pipx 1.7.1\n", stderr="")
            if exe == "definitely-missing":
                raise FileNotFoundError()
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")
        return _run

    def test_prefers_the_bare_pipx_command(self, monkeypatch) -> None:
        monkeypatch.setattr(su.subprocess, "run", self._fake_run({"pipx"}))
        assert su.find_pipx() == ["pipx"]

    def test_falls_back_to_py_dash_m_pipx(self, monkeypatch) -> None:
        monkeypatch.setattr(su.subprocess, "run", self._fake_run({"py -m pipx"}))
        assert su.find_pipx() == ["py", "-m", "pipx"]

    def test_a_candidate_that_is_not_installed_at_all_is_not_a_crash(self, monkeypatch) -> None:
        def _run(argv, **kwargs):
            raise FileNotFoundError()
        monkeypatch.setattr(su.subprocess, "run", _run)
        assert su.find_pipx() is None

    def test_a_hanging_candidate_is_skipped_not_fatal(self, monkeypatch) -> None:
        def _run(argv, **kwargs):
            if argv[0] == "pipx":
                raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
            return subprocess.CompletedProcess(argv, 0, stdout="pipx 1.7.1\n", stderr="")
        monkeypatch.setattr(su.subprocess, "run", _run)
        assert su.find_pipx() == ["py", "-m", "pipx"]

    def test_no_candidate_works_gives_none(self, monkeypatch) -> None:
        monkeypatch.setattr(su.subprocess, "run", self._fake_run(set()))
        assert su.find_pipx() is None


# ── report_pending / _write_state — round trip през диска ───────────────────

class TestStateRoundTrip:
    def test_nothing_written_yet_is_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        assert su.report_pending() is None

    def test_a_success_is_read_once_then_gone(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        su._write_state(ok=True, spec="git+https://github.com/a/b@main")
        out = su.report_pending()
        assert out is not None and "Обновено" in out and "git+https" in out
        assert su.report_pending() is None  # изтрито след първото четене

    def test_a_failure_names_the_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        su._write_state(ok=False, error="pipx install се провали")
        out = su.report_pending()
        assert out is not None and "не мина" in out and "pipx install се провали" in out

    def test_a_corrupt_state_file_is_not_a_crash(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "update_state.json").write_text("не е json", encoding="utf-8")
        assert su.report_pending() is None
        assert not (tmp_path / "update_state.json").exists()  # почистено, не оставено да троши пак

    def test_write_state_creates_genesis_home_if_missing(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "does" / "not" / "exist"
        monkeypatch.setattr(su, "GENESIS_HOME", target)
        su._write_state(ok=True, spec="x")
        assert (target / "update_state.json").exists()


# ── run_updater — целият откачен процес, mock-нат от край до край ──────────

class TestRunUpdater:
    def test_waits_for_the_old_pid_then_updates(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        calls = {"alive_checks": 0}

        def _alive(pid):
            calls["alive_checks"] += 1
            return calls["alive_checks"] < 3  # "излиза" на третата проверка

        monkeypatch.setattr(su, "pid_alive", _alive)
        monkeypatch.setattr(su.time, "sleep", lambda s: None)
        monkeypatch.setattr(su, "find_pipx", lambda: ["pipx"])

        def _run(argv, **kwargs):
            assert argv == ["pipx", "install", "--force",
                             "git+https://github.com/a/b@main"]
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(su.subprocess, "run", _run)

        rc = su.run_updater(4242, "https://github.com/a/b", "main")
        assert rc == 0
        assert calls["alive_checks"] == 3  # изчака, не тръгна веднага
        out = json.loads((tmp_path / "update_state.json").read_text())
        assert out["ok"] is True and "a/b@main" in out["spec"]

    def test_a_ref_less_source_omits_the_at_sign(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        monkeypatch.setattr(su, "pid_alive", lambda pid: False)
        monkeypatch.setattr(su, "find_pipx", lambda: ["pipx"])
        seen = {}

        def _run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(su.subprocess, "run", _run)
        su.run_updater(1, "https://github.com/a/b", "")
        assert seen["argv"][-1] == "git+https://github.com/a/b"

    def test_timeout_waiting_for_the_old_process_is_reported_not_thrown(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        monkeypatch.setattr(su, "pid_alive", lambda pid: True)  # никога не излиза
        monkeypatch.setattr(su.time, "sleep", lambda s: None)
        monkeypatch.setattr(su, "_WAIT_TIMEOUT_SECONDS", -1)  # изтекло от началото

        def _run(argv, **kwargs):
            raise AssertionError("pipx не биваше да се вика — старият процес не е излязъл")

        monkeypatch.setattr(su.subprocess, "run", _run)

        rc = su.run_updater(1, "https://github.com/a/b", "main")
        assert rc == 1
        out = json.loads((tmp_path / "update_state.json").read_text())
        assert out["ok"] is False and "не приключи" in out["error"]

    def test_no_pipx_found_is_reported_not_thrown(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        monkeypatch.setattr(su, "pid_alive", lambda pid: False)
        monkeypatch.setattr(su, "find_pipx", lambda: None)
        rc = su.run_updater(1, "https://github.com/a/b", "main")
        assert rc == 1
        out = json.loads((tmp_path / "update_state.json").read_text())
        assert out["ok"] is False and "pipx не е намерен" in out["error"]

    def test_a_failing_pipx_install_reports_its_stderr(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        monkeypatch.setattr(su, "pid_alive", lambda pid: False)
        monkeypatch.setattr(su, "find_pipx", lambda: ["pipx"])
        monkeypatch.setattr(su.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="version conflict"))
        rc = su.run_updater(1, "https://github.com/a/b", "main")
        assert rc == 1
        out = json.loads((tmp_path / "update_state.json").read_text())
        assert out["ok"] is False and "version conflict" in out["error"]

    def test_a_hanging_pipx_install_is_reported_not_thrown(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(su, "GENESIS_HOME", tmp_path)
        monkeypatch.setattr(su, "pid_alive", lambda pid: False)
        monkeypatch.setattr(su, "find_pipx", lambda: ["pipx"])

        def _run(argv, **k):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=600)

        monkeypatch.setattr(su.subprocess, "run", _run)
        rc = su.run_updater(1, "https://github.com/a/b", "main")
        assert rc == 1
        out = json.loads((tmp_path / "update_state.json").read_text())
        assert out["ok"] is False and "600" in out["error"]


# ── request_update — само НАСРОЧВА, никога не чака ───────────────────────────

class TestRequestUpdate:
    def test_posix_starts_a_new_session_and_does_not_block(self, monkeypatch) -> None:
        monkeypatch.setattr(su.sys, "platform", "linux")
        calls = []
        monkeypatch.setattr(su.subprocess, "Popen",
                            lambda argv, **kw: calls.append((argv, kw)))
        su.request_update(pid=999, url="https://github.com/a/b", ref="main")
        assert len(calls) == 1
        argv, kw = calls[0]
        assert argv[1:4] == ["-m", "genesis_agent.self_update", "--pid"]
        assert "999" in argv and "https://github.com/a/b" in argv and "main" in argv
        assert kw.get("start_new_session") is True
        assert "creationflags" not in kw

    def test_windows_uses_creationflags_not_start_new_session(self, monkeypatch) -> None:
        monkeypatch.setattr(su.sys, "platform", "win32")
        calls = []
        monkeypatch.setattr(su.subprocess, "Popen",
                            lambda argv, **kw: calls.append((argv, kw)))
        su.request_update(pid=999, url="https://github.com/a/b", ref="main")
        assert len(calls) == 1
        _argv, kw = calls[0]
        assert "creationflags" in kw
        assert "start_new_session" not in kw

    def test_argv_round_trips_through_main(self, monkeypatch) -> None:
        """--pid/--url/--ref от request_update трябва да минат обратно през
        _main() точно както заявката ги е подредила — не приближение отвън."""
        monkeypatch.setattr(su.sys, "platform", "linux")
        popen_calls = []
        monkeypatch.setattr(su.subprocess, "Popen",
                            lambda argv, **kw: popen_calls.append(argv))
        su.request_update(pid=4242, url="https://github.com/me7ko-dev/genesis-agent", ref="")
        argv = popen_calls[0]
        assert argv[:3] == [su.sys.executable, "-m", "genesis_agent.self_update"]

        seen = {}
        monkeypatch.setattr(
            su, "run_updater",
            lambda pid, url, ref: seen.update(pid=pid, url=url, ref=ref) or 0)
        su._main(argv[3:])  # точно каквото `-m genesis_agent.self_update` би получил
        assert seen == {"pid": 4242,
                        "url": "https://github.com/me7ko-dev/genesis-agent", "ref": ""}
