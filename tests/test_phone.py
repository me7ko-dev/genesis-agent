"""`genesis phone` — Genesis на самия телефон (Android, Termux).

Процесите се пускат наистина: вместо `genesis serve` тече малък сървър,
който отговаря на /api/hello като него. Истинският `genesis serve` в Termux
се проверява в CI (.github/workflows/termux.yml)."""
from __future__ import annotations

import sys
import textwrap
from urllib.parse import parse_qs, urlparse

import pytest

from genesis_agent import phone
from genesis_agent import remote_server as rs

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="процесите на телефона са POSIX")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(phone, "GENESIS_HOME", tmp_path / ".genesis")
    monkeypatch.setenv("GENESIS_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.setattr(phone, "_termux", lambda *a: False)
    return tmp_path


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAKE_SERVE = textwrap.dedent("""
    import json, os, signal, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(sys.argv[1])
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"app": "genesis", "v": 1, "name": "Pixel",
                               "cwd": os.getcwd()}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    print("fake genesis serve", flush=True)
    try:
        HTTPServer(("127.0.0.1", port), H).serve_forever()
    except KeyboardInterrupt:
        print("stopped by SIGINT", flush=True)
""")


@pytest.fixture
def fake_serve(monkeypatch, tmp_path):
    script = tmp_path / "genesis_fake_serve.py"
    script.write_text(FAKE_SERVE, encoding="utf-8")
    # „serve" и „genesis" в командния ред: stop() спира само своя процес.
    monkeypatch.setattr(phone, "_serve_argv", lambda port: [sys.executable, str(script), str(port), "serve"])
    return script


def test_app_link_opens_the_app_with_the_pairing_url() -> None:
    pairing = rs.pairing_url("127.0.0.1", 8765, bytes(range(32)), "Pixel 8")
    link = phone.app_link(pairing)
    parsed = urlparse(link)
    assert (parsed.scheme, parsed.netloc) == ("genesisremote", "pair")
    # Целият адрес с ключа (след #) минава непокътнат в `u`.
    assert parse_qs(parsed.query)["u"] == [pairing]
    assert "#" not in link


def test_workspace_defaults_to_genesis_in_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GENESIS_WORKSPACE", raising=False)
    monkeypatch.setattr(phone.Path, "home", lambda: tmp_path)
    assert phone.workspace() == tmp_path / "genesis"


def test_hello_is_none_when_nothing_listens() -> None:
    assert phone.hello(_free_port(), timeout=0.5) is None


@pytest.mark.parametrize("args, code", [([], 2), (["--help"], 0), (["nope"], 2),
                                        (["start", "--port", "x"], 2), (["start", "--x"], 2)])
def test_bad_or_help_arguments(args, code, capsys) -> None:
    assert phone.main(args) == code
    out = capsys.readouterr().out
    assert "genesis phone" in out or "--port" in out


@posix_only
def test_start_status_stop(home, fake_serve, capsys) -> None:
    port = _free_port()
    assert phone.start(port, wait=20) == 0
    info = phone.hello(port)
    assert info and info["name"] == "Pixel"
    # Тече в работната папка, която е и GENESIS_WORKSPACE.
    assert info["cwd"] == str(home / "work")
    pid = phone._read_pid()
    assert pid and phone._is_ours(pid)

    assert phone.start(port) == 0            # втори път: вече тече, нов процес няма
    assert phone._read_pid() == pid
    assert phone.status(port) == 0

    assert phone.stop(port) == 0
    assert phone.hello(port, timeout=0.5) is None
    assert not phone.pid_file().exists()
    # SIGINT: serve() го приема като Ctrl+C и прибира паметта, преди да излезе.
    assert "stopped by SIGINT" in phone.tail()
    assert "вече тече" in capsys.readouterr().out


@posix_only
def test_start_reports_a_crash_with_the_log(home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(phone, "_serve_argv", lambda port: [
        sys.executable, "-c", "import sys; print('no API keys here'); sys.exit(3)"])
    assert phone.start(_free_port(), wait=20) == 1
    out = capsys.readouterr().out
    assert "код 3" in out and "no API keys here" in out
    assert not phone.pid_file().exists()


@posix_only
def test_stop_leaves_a_foreign_process_alone(home, capsys) -> None:
    # pid от стар файл, който вече е на друг процес (тук: самият pytest).
    phone.pid_file().parent.mkdir(parents=True)
    phone.pid_file().write_text(str(__import__("os").getpid()), encoding="utf-8")
    assert phone.stop(_free_port()) == 0
    assert "не тече" in capsys.readouterr().out
    assert not phone.pid_file().exists()


@posix_only
def test_pair_starts_genesis_and_opens_the_app(home, fake_serve, monkeypatch) -> None:
    opened: list[tuple] = []
    monkeypatch.setattr(phone, "_termux", lambda *a: opened.append(a) or True)
    monkeypatch.setattr(rs, "_config_path", lambda: home / ".genesis" / "remote.json")
    monkeypatch.setattr(rs, "machine_name", lambda: "Pixel 8")
    port = _free_port()
    try:
        assert phone.pair(port) == 0
    finally:
        phone.stop(port)
    _tool, link = next(a for a in opened if a[0] == "termux-open-url")
    pairing = parse_qs(urlparse(link).query)["u"][0]
    assert pairing.startswith(f"http://127.0.0.1:{port}/#k=")
    assert pairing.endswith("&n=Pixel%208")


def test_update_reinstalls_from_the_same_branch_then_restarts(monkeypatch) -> None:
    from genesis_agent import version_info
    src = version_info.Source(url="https://github.com/me7ko-dev/genesis-agent", commit="abc", ref="feat/x")
    monkeypatch.setattr(version_info, "installed_source", lambda: src)
    runs: list[list[str]] = []
    monkeypatch.setattr(phone.subprocess, "run",
                        lambda argv, **kw: runs.append(argv) or phone.subprocess.CompletedProcess(argv, 0))
    order: list[str] = []
    monkeypatch.setattr(phone, "stop", lambda port: order.append("stop") or 0)
    monkeypatch.setattr(phone, "start", lambda port: order.append("start") or 0)
    assert phone.update(8765) == 0
    assert all(r[-1] == "genesis-agent[mobile] @ git+https://github.com/me7ko-dev/genesis-agent@feat/x"
               for r in runs)
    assert "--force-reinstall" in runs[1]      # същата версия 0.2.0 — без това pip не обновява
    assert order == ["stop", "start"]


def test_a_failed_update_keeps_the_running_genesis(monkeypatch) -> None:
    monkeypatch.setattr(phone.subprocess, "run",
                        lambda argv, **kw: phone.subprocess.CompletedProcess(argv, 1))
    monkeypatch.setattr(phone, "stop", lambda port: pytest.fail("stopped a working Genesis"))
    assert phone.update(8765) == 1
