"""Истинският `genesis serve` (remote_server) с фалшив агент, фалшив Whisper и
фалшив edge-tts — за tests/media.test.ts: картинките и гласът от Desktop
през същия протокол (2026-10-15). Без ключове и без мрежа.

Печата един ред JSON {"port": N} и слуша, докато тестът не затвори stdin.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from genesis_agent import remote_server as rs

KEY = bytes(range(32))


def main() -> None:
    def runner(text: str, ui, images: list[dict] | None = None) -> None:
        ui.assistant(f"Видях {len(images or [])} картинки: {text}")

    def stt(audio: bytes, mime: str, language: str) -> str:
        return f"чух {len(audio)} байта {mime} [{language or '-'}]"

    def tts(text: str, name: str | None) -> tuple[bytes, str]:
        return b"ID3" + text.encode("utf-8"), "audio/mpeg"

    session = rs.RemoteSession(runner, state=rs.phone_state)
    server = rs.RemoteServer(KEY, session, name="тест-компютър",
                             status=lambda: {"model": "fake/model", "workspace": "/w"},
                             stt=stt, tts=tts)
    httpd = server.make_http("127.0.0.1", 0)
    print(json.dumps({"port": httpd.server_address[1]}), flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sys.stdin.read()


if __name__ == "__main__":
    main()
