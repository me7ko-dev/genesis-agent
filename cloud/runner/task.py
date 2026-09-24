"""Една задача на клиент, в контейнера: текст → агентният цикъл → JSON редове.

Пуска се от `cloud/runner/launch.py` като входна точка на контейнера:

    python -m cloud.runner.task "преименувай снимките по дата"

stdout е поток от JSON редове (`{"kind": ...}`), по един на събитие, за да
може сървърът да ги препраща към браузъра, докато задачата върви. Последният
ред винаги е `{"kind": "done", ...}` — с разхода в токени и секунди, от които
после се таксува.

Тук няма кого да питаш: sandbox-ът отказва CONFIRM операциите (режим "deny").
Контейнерът е вторият слой — дори отказаното да мине, то е в изхвърлим
контейнер без мрежа, не на машина с чужди данни.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

MAX_TOOL_OUTPUT = 4000
# Уеб чатът (cloud/web): всяко съобщение е нов контейнер в СЪЩАТА папка, затова
# разговорът се пази тук. Само въпросите и крайните отговори — не изходите на
# инструментите — и с таван, за да не расте цената на всеки следващ ход.
HISTORY_FILE = Path(".genesis") / "history.json"
HISTORY_MAX_MESSAGES = 20
HISTORY_MAX_CHARS = 12000


def load_history(workspace: Path) -> list[dict[str, str]]:
    """Последните съобщения от разговора, в рамките на таваните."""
    try:
        raw = json.loads((workspace / HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    msgs = [{"role": m["role"], "content": m["content"]} for m in raw
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)] if isinstance(raw, list) else []
    msgs = msgs[-HISTORY_MAX_MESSAGES:]
    while msgs and sum(len(m["content"]) for m in msgs) > HISTORY_MAX_CHARS:
        msgs = msgs[1:]
    return msgs


def save_history(workspace: Path, history: list[dict[str, str]]) -> None:
    path = workspace / HISTORY_FILE
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(history[-HISTORY_MAX_MESSAGES:], ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass  # разговорът губи паметта си, задачата не бива да пада заради това


def emit(kind: str, **data: Any) -> None:
    print(json.dumps({"kind": kind, **data}, ensure_ascii=False), flush=True)


def _log_lines(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def tokens_since(path: Path, skip_lines: int) -> dict[str, int]:
    """Сбор на записите в budget_log.jsonl след първите `skip_lines` реда."""
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    try:
        with path.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < skip_lines or not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    total[k] += int(e.get(k) or 0)
                total["calls"] += 1
    except OSError:
        pass
    return total


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    text = " ".join(argv).strip() or sys.stdin.read().strip()
    if not text:
        emit("done", ok=False, error="празна задача", seconds=0.0,
             tokens=tokens_since(Path("/nonexistent"), 0))
        return 2

    os.environ.setdefault("GENESIS_WORKSPACE", "/work")
    t0 = time.monotonic()

    import genesis_terminal_agent as gta
    from genesis_agent import budget, sandbox

    # Внасянето на терминалния агент слага интерактивна политика — тук няма
    # терминал, затова изрично: CONFIRM операциите се отказват.
    sandbox.set_policy(sandbox.SandboxPolicy(mode="deny"))
    log_path = Path(budget.LOG_PATH)
    skip = _log_lines(log_path)

    last_reply = {"text": ""}

    class JsonTurnUI(gta.TurnUI):
        def assistant(self, text: str) -> None:
            if text.strip():
                last_reply["text"] = text
                emit("assistant", text=text)

        def tool(self, name: str, result: str) -> None:
            emit("tool", name=name, result=result[:MAX_TOOL_OUTPUT])

        def asked(self, question: str) -> None:
            emit("asked", text=question)

        def spinning(self, note: str) -> None:
            emit("warn", text=note)

        def warn(self, text: str) -> None:
            emit("warn", text=text)

        def info(self, text: str) -> None:
            emit("info", text=text)

    workspace = Path(os.environ["GENESIS_WORKSPACE"])
    history = load_history(workspace)
    system_prompt, _ = gta.build_system_prompt()
    messages: deque = deque([{"role": "system", "content": system_prompt}, *history],
                            maxlen=gta._HISTORY_MAXLEN)
    ok, error = True, ""
    try:
        gta.run_turn(messages, text, JsonTurnUI())
    except Exception as e:  # всяка грешка трябва да стигне до клиента
        ok, error = False, f"{type(e).__name__}: {e}"[:500]
    # Изчерпана верига не хвърля — идва като отговор „[Грешка: ...]". Без това
    # задачата, в която нито един модел не отговори, щеше да е „успешна" и
    # платена.
    if ok and last_reply["text"].lstrip().startswith("[Грешка"):
        ok, error = False, last_reply["text"].strip()[:500]
    if ok:
        save_history(workspace, [*history, {"role": "user", "content": text},
                                 {"role": "assistant", "content": last_reply["text"]}])
    emit("done", ok=ok, error=error, seconds=round(time.monotonic() - t0, 1),
         tokens=tokens_since(log_path, skip))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
