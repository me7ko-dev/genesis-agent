"""cloud/ — изпълнението на задачи на сървъра. Без Docker и без мрежа: тук се
проверяват обещанията (таваните, изходът само към модели, таксуването), а
истинският контейнер е проверен ръчно — виж cloud/README.md."""
from __future__ import annotations

import io
import json
from itertools import pairwise
from pathlib import Path

import pytest

from cloud.egress import proxy
from cloud.runner import launch, task

# ── egress: само CONNECT към API на модели, порт 443 ─────────────────────────

@pytest.mark.parametrize("target", [
    "api.groq.com:443",
    "integrate.api.nvidia.com:443",
    "eu.api.groq.com:443",          # поддомейн на разрешен
    "API.GROQ.COM:443",
])
def test_egress_allows_model_apis(target) -> None:
    assert proxy.is_allowed(target, proxy.DEFAULT_ALLOW)[0]


@pytest.mark.parametrize("target, why", [
    ("example.com:443", "извън списъка"),
    ("api.groq.com.evil.io:443", "извън списъка"),   # само суфикс, не поддомейн
    ("evilapi.groq.com:443", "извън списъка"),        # не е поддомейн на api.groq.com
    ("api.groq.com:80", "порт 80"),
    ("api.groq.com:22", "порт 22"),
    ("1.1.1.1:443", "IP адрес"),
    ("[::1]:443", "IP адрес"),
    ("169.254.169.254:443", "IP адрес"),              # metadata на облака
    ("api.groq.com", "без порт"),
    ("api groq.com:443", "невалиден хост"),
])
def test_egress_refuses_everything_else(target, why) -> None:
    assert proxy.is_allowed(target, proxy.DEFAULT_ALLOW) == (False, why)


def test_egress_allowlist_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EGRESS_ALLOW", " Api.Groq.com , ollama.com. ")
    assert proxy.allowed_hosts() == ("api.groq.com", "ollama.com")
    monkeypatch.setenv("EGRESS_ALLOW", "")
    assert proxy.allowed_hosts() == proxy.DEFAULT_ALLOW


# ── launch: всеки таван е в `docker run` ─────────────────────────────────────

def _args(tmp_path: Path, **kw) -> list[str]:
    return launch.docker_args("genesis-task-x", tmp_path, "задача", **kw)


def _pairs(args: list[str]) -> set[tuple[str, str]]:
    return {(a, b) for a, b in pairwise(args)}


def test_every_limit_is_in_the_docker_command(tmp_path) -> None:
    pairs = _pairs(_args(tmp_path, limits=launch.Limits(cpus="2", memory="2g", pids=100)))
    for flag in [("--cpus", "2"), ("--memory", "2g"), ("--memory-swap", "2g"),
                 ("--pids-limit", "100"), ("--cap-drop", "ALL"),
                 ("--security-opt", "no-new-privileges"), ("--user", "10001:10001"),
                 ("--network", launch.JOBS_NETWORK)]:
        assert flag in pairs, flag
    args = _args(tmp_path)
    assert "--read-only" in args and "--rm" in args


def test_only_the_task_folder_is_mounted(tmp_path) -> None:
    args = _args(tmp_path)
    mounts = [b for a, b in pairwise(args) if a == "-v"]
    assert mounts == [f"{tmp_path.resolve()}:/work:rw"]


def test_traffic_goes_through_egress_except_localhost(tmp_path) -> None:
    env = {b.split("=", 1)[0]: b.split("=", 1)[1]
           for a, b in pairwise(_args(tmp_path)) if a == "-e"}
    assert env["HTTPS_PROXY"] == env["https_proxy"] == launch.EGRESS_URL
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_the_task_text_is_one_argument_after_the_image(tmp_path) -> None:
    text = "; rm -rf / && echo $(whoami)"
    args = launch.docker_args("n", tmp_path, text, image="img")
    assert args[-2:] == ["img", text]   # списък, без shell — нищо не се тълкува


def test_keys_and_runtime_are_optional(tmp_path) -> None:
    assert "--env-file" not in _args(tmp_path)
    pairs = _pairs(_args(tmp_path, env_file="/srv/keys.env", runtime="runsc"))
    assert ("--env-file", "/srv/keys.env") in pairs and ("--runtime", "runsc") in pairs


class _FakeProc:
    def __init__(self, lines: list[str], code: int = 0, stderr: str = "") -> None:
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.stderr = io.StringIO(stderr)
        self._code = code

    def wait(self) -> int:
        return self._code


def _run(monkeypatch, tmp_path, lines, code=0, stderr=""):
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: _FakeProc(lines, code, stderr))
    seen = []
    res = launch.run_task("задача", tmp_path / "ws", on_event=seen.append)
    return res, seen


def test_events_stream_and_the_bill_comes_from_done(monkeypatch, tmp_path) -> None:
    done = {"kind": "done", "ok": True, "error": "", "seconds": 4.2,
            "tokens": {"total_tokens": 6500, "calls": 3}}
    res, seen = _run(monkeypatch, tmp_path, [
        json.dumps({"kind": "assistant", "text": "правя го"}),
        "Traceback шум, не е JSON",
        json.dumps(done)])
    assert [e["kind"] for e in seen] == ["assistant", "done"]
    assert res.ok and res.tokens == {"total_tokens": 6500, "calls": 3}


def test_a_container_that_dies_without_done_is_a_failure(monkeypatch, tmp_path) -> None:
    res, _ = _run(monkeypatch, tmp_path, [], code=137, stderr="Killed\n")
    assert not res.ok and res.error == "Killed" and res.tokens == {}


def test_done_ok_but_nonzero_exit_is_a_failure(monkeypatch, tmp_path) -> None:
    res, _ = _run(monkeypatch, tmp_path,
                  [json.dumps({"kind": "done", "ok": True, "tokens": {}})], code=1)
    assert not res.ok


def test_the_task_folder_is_created(monkeypatch, tmp_path) -> None:
    _run(monkeypatch, tmp_path, [])
    assert (tmp_path / "ws").is_dir()


# ── task: разходът на задачата е само нейният ────────────────────────────────

def test_tokens_count_only_lines_after_the_start(tmp_path) -> None:
    log = tmp_path / "budget_log.jsonl"
    old = {"prompt_tokens": 999, "completion_tokens": 1, "total_tokens": 1000}
    new = {"prompt_tokens": 1500, "completion_tokens": 200, "total_tokens": 1700}
    log.write_text(json.dumps(old) + "\n", encoding="utf-8")
    skip = task._log_lines(log)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(new) + "\n\nне е json\n" + json.dumps(new) + "\n")
    assert task.tokens_since(log, skip) == {
        "prompt_tokens": 3000, "completion_tokens": 400, "total_tokens": 3400, "calls": 2}


def test_a_missing_log_costs_nothing(tmp_path) -> None:
    assert task.tokens_since(tmp_path / "none.jsonl", 0)["total_tokens"] == 0


def test_an_empty_task_is_refused_without_calling_a_model(capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert task.main([]) == 2
    done = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert done["kind"] == "done" and not done["ok"]


# ── egress: истинският процес на локален порт, без мрежа навън ────────────────

async def _ask_proxy(request: bytes) -> bytes:
    import asyncio
    server = await asyncio.start_server(
        lambda r, w: proxy.handle(r, w, proxy.DEFAULT_ALLOW), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(200), 5)
        writer.close()
        return reply
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("request_line", [
    b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",   # не е CONNECT
    b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",                          # извън списъка
    b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",                      # metadata IP
    b"CONNECT api.groq.com:22 HTTP/1.1\r\n\r\n",                          # друг порт
])
def test_the_running_proxy_answers_403(request_line) -> None:
    import asyncio
    assert asyncio.run(_ask_proxy(request_line)).startswith(b"HTTP/1.1 403")
