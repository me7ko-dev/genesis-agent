"""Фалшив OpenAI-съвместим доставчик за пускане на bench-а без ключове.

Защо (2026-10-11): bench скриптовете бяха поправени във вълна 4, като се пускаха
срещу такъв доставчик в чужда папка — той не беше в репото, значи CI не можеше
да хване следващото счупване на харнеса, а първото пускане на лаптопа с истински
ключове беше без предпазна мрежа.

Как Genesis стига до него: `GENESIS_MODEL_GATEWAY=<url>` (същата кука, с която
онлайн услугата праща задачата към шлюза си — brain.provider_base_url) плюс
какъвто и да е ключ на доставчика, напр. `GROQ_API_KEY=fake`. Тогава заявката
отива на `<url>/groq/chat/completions`. Тук се приема всеки път, който свършва
на `/chat/completions` (и `/v1/chat/completions`).

Сценарият е списък от стъпки — всяка е списък от извиквания на инструменти
`{"name": "WRITE_FILE", "arguments": {...}}`; след последната идва крайният
текст. Коя стъпка е наред се смята от САМАТА заявка (колко assistant
съобщения с tool_calls вече има в нея), не от брояч: втори опит на заявката
или странична заявка без инструменти не разместват сценария. Заявка без
`tools` (резюме, памет) получава кратък текст.

    python tests/fake_openai_provider.py --script s.json      (или --reference <папка>)
печата `URL <адрес>` на първия ред и обслужва до Ctrl-C (за ръчни пускания).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FINAL = "Готово."

# Собствен тест, който сценарият пише до решението: истински агент пише свои
# тестове и ги пуска; тук само проверява, че всеки .py файл се компилира.
OWN_TEST = (
    "import pathlib\nimport py_compile\n\n\n"
    "def test_every_file_compiles():\n"
    "    for p in pathlib.Path(__file__).parent.rglob('*.py'):\n"
    "        py_compile.compile(str(p), doraise=True)\n"
)


def solution_script(files: dict[str, str], *, test_cmd: str = "python -m pytest -q") -> list[list[dict]]:
    """Сценарият на „добър агент“: по един WRITE_FILE за всеки файл, собствен
    тест, пускане на pytest през RUN_CMD; крайният отговор идва след това."""
    steps: list[list[dict]] = [[{"name": "WRITE_FILE", "arguments": {"path": p, "content": c}}]
                               for p, c in files.items()]
    steps.append([{"name": "WRITE_FILE", "arguments": {"path": "test_own.py", "content": OWN_TEST}}])
    steps.append([{"name": "RUN_CMD", "arguments": {"command": test_cmd}}])
    return steps


def reference_files(reference: Path) -> dict[str, str]:
    """Файловете на `reference/` с относителни пътища (с `/`)."""
    return {p.relative_to(reference).as_posix(): p.read_text("utf-8")
            for p in sorted(reference.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _step_index(messages: list[dict]) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
               and m.get("tool_calls"))


class FakeProvider:
    """HTTP сървър на 127.0.0.1 със свободен порт; `url` е готов за
    GENESIS_MODEL_GATEWAY. `requests` пази всяка заявка (път, модел, Authorization,
    имало ли е tools, коя стъпка) — тестът проверява, че моделът наистина е викан."""

    def __init__(self, script: list[list[dict]] | None = None, final: str = FINAL, *,
                 by_task: dict[str, list[list[dict]]] | None = None) -> None:
        self.script = script or []
        # Няколко проекта в едно пускане (bench_all): сценарият е този, чийто
        # ключ се среща в първото съобщение на потребителя (задачата).
        self.by_task = by_task or {}
        self.final = final
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # тих: изходът на pytest е за тестовете
                pass

            def do_POST(self) -> None:  # име от http.server
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": {"message": "bad json"}})
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": {"message": f"no route {self.path}"}})
                self._send(200, provider.reply(self.path, body, self.headers.get("Authorization")))

            def _send(self, code: int, data: dict) -> None:
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeProvider:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):  # как test_cloud_gateway: Self го няма в 3.10
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _script_for(self, messages: list[dict]) -> list[list[dict]]:
        task = next((str(m.get("content") or "") for m in messages
                     if isinstance(m, dict) and m.get("role") == "user"), "")
        return next((s for key, s in self.by_task.items() if key in task), self.script)

    def reply(self, path: str, body: dict, auth: str | None) -> dict:
        messages = body.get("messages") or []
        tools = bool(body.get("tools"))
        step = _step_index(messages) if tools else -1
        script = self._script_for(messages)
        with self._lock:
            self.requests.append({"path": path, "model": body.get("model"), "auth": auth,
                                  "tools": tools, "step": step})
            n = len(self.requests)
        message: dict[str, Any] = {"role": "assistant"}
        if tools and step < len(script):
            message["content"] = ""
            message["tool_calls"] = [
                {"id": f"call_{n}_{i}", "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for i, c in enumerate(script[step])]
            finish = "tool_calls"
        else:
            message["content"] = self.final if tools else "Добре."
            finish = "stop"
        return {"id": f"fake-{n}", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--script", type=Path, help="JSON: списък от стъпки (виж модула)")
    ap.add_argument("--reference", type=Path, help="папка с решение → сценарий на добър агент")
    args = ap.parse_args(argv)
    script: list[list[dict]] = []
    if args.script:
        script = json.loads(args.script.read_text("utf-8"))
    elif args.reference:
        script = solution_script(reference_files(args.reference))
    with FakeProvider(script) as fake:
        print(f"URL {fake.url}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
