"""Одит на изпълнението в sandbox.py 2026-10-27 (_run, _preexec, stop_process).

Всичко е възпроизведено с истински подпроцеси: `yes` през run_shell уби самия
Genesis (OOM — изходът се трупаше без таван); `sleep 30 & echo hi` чакаше
целия таймаут и връщаше „Timeout“ без код; Ctrl-C оставяше командата жива;
детето четеше терминала на оператора; RLIMIT_AS спираше java, fetch() в
Node 22 и 64 нишки в Python; stop_process не спираше децата на излязла обвивка
и дете, което не спира от SIGTERM. Тестовете падат срещу стария код."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis_agent import sandbox
from genesis_agent.sandbox import SandboxPolicy

ALLOW = SandboxPolicy(mode="allow")
ROOT = Path(__file__).resolve().parent.parent


def _alive(pid: int) -> bool:
    """Жив и не зомби. Зомбито на сирак в контейнер без init стои вечно,
    затова на Linux се гледа състоянието в /proc, не само os.kill(pid, 0)."""
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in (b"Z", b"X")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for(path: Path, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"{path.name} не се появи"


@pytest.fixture(autouse=True)
def _no_stop_or_snapshot(monkeypatch):
    monkeypatch.setattr(sandbox, "cancel_check", None)
    monkeypatch.setattr(sandbox, "before_exec", None)


# ── Фоново дете, което държи изхода: командата свършва с кода си ─────────────

def test_a_background_child_holding_the_output_does_not_wait_for_the_timeout(
        tmp_path, monkeypatch) -> None:
    """`python app.py & sleep 1; curl …`: командата свърши с 0, но детето
    наследи stdout (изрично — и на Windows, както го подава bash) и communicate() чакаше до таймаута (120 s в RUN_CMD) —
    после „Timeout“, returncode None. Сега: кратко чакане, детето се спира,
    кодът и изходът са на командата."""
    monkeypatch.setattr(sandbox, "_LEFTOVER_GRACE_S", 0.3, raising=False)
    pid_file = tmp_path / "leftover.pid"
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
            " stdout=sys.stdout, stderr=sys.stderr)\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "print('hi', flush=True)\n")
    t0 = time.monotonic()
    res = sandbox._run([sys.executable, "-c", code], cwd=tmp_path, policy=ALLOW, timeout=8)
    took = time.monotonic() - t0
    assert took < 4, f"чака {took:.1f} s — фоновото дете държи изхода"
    assert res.returncode == 0 and res.ok, res.stderr
    assert res.stdout.strip() == "hi"
    assert "RUN_BG" in res.stderr
    if sys.platform.startswith("linux"):
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(pid), "фоновото дете остана живо"


def test_a_late_line_from_a_short_background_child_is_still_read(tmp_path, monkeypatch) -> None:
    """Обратната страна: дете, което свършва в рамките на чакането, не се убива.
    Чакането е вдигнато: на Windows CI самият старт на python е до ~1 s."""
    monkeypatch.setattr(sandbox, "_LEFTOVER_GRACE_S", 10.0)
    code = ("import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(0.3); print(\"late\", flush=True)'], stdout=sys.stdout)\n"
            "print('early', flush=True)\n")
    res = sandbox._run([sys.executable, "-c", code], cwd=tmp_path, policy=ALLOW, timeout=8)
    assert res.ok and res.stdout.split() == ["early", "late"], res
    assert "RUN_BG" not in res.stderr


# ── Таван на изхода ─────────────────────────────────────────────────────────

def test_huge_output_is_capped_keeping_the_head_and_the_tail(tmp_path) -> None:
    """`yes` с таймаут 3 s през run_shell: Genesis убит от OOM killer-а;
    300 MB изход → 1067 MB RSS. Пази се началото и краят, средата се брои."""
    lines = 1_200_000            # ~14 MB
    code = ("import sys\n"
            f"sys.stdout.buffer.write(b''.join(b'line %d\\n' % i for i in range({lines})))\n")
    res = sandbox._run([sys.executable, "-c", code], cwd=tmp_path, policy=ALLOW, timeout=30)
    assert res.ok, res.stderr
    assert len(res.stdout) < 5 * 1024 * 1024      # таванът е 4 MB, изходът — ~14 MB
    out = res.stdout.splitlines()
    assert out[0] == "line 0" and out[-1] == f"line {lines - 1}"
    assert any("пропуснати" in line for line in out)
    # Срязано по край на ред — няма половин ред около бележката.
    assert all(line.startswith(("line ", "… [sandbox")) for line in out)


def test_small_output_is_untouched(tmp_path) -> None:
    res = sandbox._run([sys.executable, "-c", "print('a\\nb')"], cwd=tmp_path, policy=ALLOW,
                       timeout=30)
    assert res.stdout == "a\nb\n" and res.stderr == ""


# ── Ctrl-C по време на командата ─────────────────────────────────────────────

def test_ctrl_c_during_a_command_kills_it(tmp_path, monkeypatch) -> None:
    """Детето е в своя сесия — SIGINT от терминала не го стига. KeyboardInterrupt
    излизаше от _run, а командата продължаваше: маркерът ѝ се появяваше."""
    marker = tmp_path / "marker.txt"
    t0 = time.monotonic()

    def ctrl_c() -> bool:
        if time.monotonic() - t0 > 0.2:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(sandbox, "cancel_check", ctrl_c)
    code = f"import time; time.sleep(0.8); open({str(marker)!r}, 'w').write('x')"
    with pytest.raises(KeyboardInterrupt):
        sandbox._run([sys.executable, "-c", code], cwd=tmp_path, policy=ALLOW, timeout=30)
    time.sleep(max(0.0, 1.3 - (time.monotonic() - t0)))
    assert not marker.exists(), "командата преживя Ctrl-C"


# ── stdin не е терминалът на оператора ───────────────────────────────────────

_STDIN_HARNESS = r"""
import sys
from pathlib import Path
from genesis_agent import sandbox
code = "import sys; print('READ:' + repr(sys.stdin.read()))"
res = sandbox._run([sys.executable, "-c", code], cwd=Path(sys.argv[1]),
                   policy=sandbox.SandboxPolicy(mode="allow"), timeout=10)
print(res.stdout.strip())
"""


def test_the_command_does_not_read_the_operators_stdin(tmp_path) -> None:
    """Измерено през pty: `input()` в командата върна какво написа операторът,
    а npm init/createsuperuser висяха до таймаута. stdin е празен (DEVNULL)."""
    harness = tmp_path / "harness.py"
    harness.write_text(_STDIN_HARNESS, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT), GENESIS_HOME=str(tmp_path / "gh"))
    out = subprocess.run([sys.executable, str(harness), str(tmp_path)], input=b"typed-by-operator\n",
                         capture_output=True, env=env, timeout=60, cwd=str(tmp_path), check=False)
    text = out.stdout.decode("utf-8", "replace")
    assert "READ:''" in text, (text, out.stderr.decode("utf-8", "replace")[-500:])


# ── Таван на паметта: не адресното пространство ──────────────────────────────

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA брои mmap само на Linux")
def test_reserved_address_space_is_not_counted_but_real_memory_is(tmp_path) -> None:
    """JVM и WebAssembly резервират адресно пространство без да го ползват:
    с RLIMIT_AS при 2048 MB `java -version`, fetch() в Node 22 и
    ThreadPoolExecutor(64) падаха. Тук — същата резервация (PROT_NONE) срещу
    таван от 256 MB; истинско заделяне над тавана пак спира."""
    policy = SandboxPolicy(mode="allow", max_memory_mb=256)
    reserve = ("import mmap\n"
               "m = mmap.mmap(-1, 4 << 30, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,"
               " prot=0)\n"   # PROT_NONE

               "print('reserved')\n")
    res = sandbox._run([sys.executable, "-c", reserve], cwd=tmp_path, policy=policy, timeout=30)
    assert res.ok and res.stdout.strip() == "reserved", res.stderr[-300:]
    hog = "b = bytearray(512 * 1024 * 1024)\nprint('allocated')\n"
    res = sandbox._run([sys.executable, "-c", hog], cwd=tmp_path, policy=policy, timeout=30)
    assert not res.ok and "MemoryError" in res.stderr


# ── stop_process: цялата група, и при излязла обвивка ────────────────────────

@pytest.mark.skipif(os.name != "posix", reason="група процеси — POSIX; Windows не е поправян тук")
def test_stop_process_stops_the_children_of_a_shell_that_already_exited(tmp_path) -> None:
    """`sleep 30 &` в RUN_BG: обвивката излиза веднага, stop_process виждаше
    излязъл процес и се връщаше — BG_KILL казваше „спряна“, а sleep живееше."""
    proc, why = sandbox.start_shell("sleep 30 & echo $! > sleep.pid", cwd=tmp_path,
                                    log=tmp_path / "bg.log", policy=ALLOW)
    assert proc is not None, why
    pid_file = tmp_path / "sleep.pid"
    _wait_for(pid_file)
    proc.wait(timeout=5)
    deadline = time.monotonic() + 2
    while not pid_file.read_text().strip() and time.monotonic() < deadline:
        time.sleep(0.02)
    pid = int(pid_file.read_text())
    assert _alive(pid)
    try:
        sandbox.stop_process(proc)
        assert not _alive(pid), "децата на излязлата обвивка оцеляха"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


@pytest.mark.skipif(os.name != "posix", reason="група процеси — POSIX; Windows не е поправян тук")
def test_stop_process_kills_a_child_that_ignores_sigterm(tmp_path, monkeypatch) -> None:
    """dash форква командата; SIGTERM убива dash, wait() на водача свършва,
    а дете, което не спира от SIGTERM, продължаваше."""
    monkeypatch.setattr(sandbox, "_STOP_GRACE_S", 0.3, raising=False)
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "stubborn.py"
    script.write_text("import os, signal, time\n"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                      "time.sleep(30)\n", encoding="utf-8")
    exe = sys.executable
    proc, why = sandbox.start_shell(f'"{exe}" "{script}"; true', cwd=tmp_path,
                                    log=tmp_path / "bg.log", policy=ALLOW)
    assert proc is not None, why
    _wait_for(pid_file)
    time.sleep(0.1)
    pid = int(pid_file.read_text())
    try:
        t0 = time.monotonic()
        sandbox.stop_process(proc)
        assert time.monotonic() - t0 < 3
        assert not _alive(pid), "детето, което не спира от SIGTERM, оцеля"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


@pytest.mark.skipif(os.name != "posix", reason="група процеси — POSIX")
def test_stop_process_never_signals_a_group_it_does_not_lead(monkeypatch) -> None:
    """Процес извън своя сесия: killpg на групата му би ударил и Genesis."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sent: list[int] = []
    monkeypatch.setattr(sandbox.os, "killpg", lambda pgid, sig: sent.append(pgid))
    try:
        sandbox.stop_process(proc)
        assert proc.poll() is not None
        assert sent == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="група процеси — POSIX")
def test_exit_stops_the_children_of_a_background_shell_that_already_exited(tmp_path) -> None:
    """stop_all() при изход спираше само задачите с жив водач: `sleep 30 &` в
    RUN_BG — обвивката излиза веднага, а sleep преживяваше Genesis (2026-10-27)."""
    from genesis_agent import background
    started = background.start("sleep 30 & echo $! > sleep.pid", cwd=tmp_path)
    assert "▶" in started, started
    pid_file = tmp_path / "sleep.pid"
    deadline = time.monotonic() + 3
    while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.02)
    pid = int(pid_file.read_text())
    try:
        assert not background._running(), "обвивката би трябвало вече да е излязла"
        background.stop_all()
        deadline = time.monotonic() + 2
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(pid), "фоновият sleep преживя изхода"
    finally:
        if _alive(pid):
            os.kill(pid, 9)
