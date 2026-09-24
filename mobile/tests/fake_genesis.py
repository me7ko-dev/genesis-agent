"""The real `genesis serve` protocol stack with a stand-in agent, for
tests/interop.test.ts: the TypeScript client against the Python server.

Prints one JSON line {"port": N, "url": pairing URL} and serves until killed.
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
    session: rs.RemoteSession

    def runner(text: str, ui) -> None:
        if text == "опасно":
            allowed = session.confirm("rm -rf build", ["изтрива папка"], timeout=10)
            ui.info(f"allowed={allowed}")
            return
        with ui.thinking("Genesis мисли..."):
            pass
        ui.tool("RUN_CMD", "$ echo " + text + "\n" + text)
        ui.assistant(f"Получих: **{text}** ✅\n\n```python\nprint({text!r})\n```")

    session = rs.RemoteSession(runner)
    web = Path(sys.argv[sys.argv.index("--web") + 1]) if "--web" in sys.argv else None
    server = rs.RemoteServer(KEY, session, name="тест-компютър",
                             status=lambda: {"model": "fake/model", "workspace": "/w"},
                             web_root=web)
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
    httpd = server.make_http(host, 0)
    port = httpd.server_address[1]
    print(json.dumps({"port": port, "url": rs.pairing_url(host, port, KEY, "тест-компютър")}),
          flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sys.stdin.read()  # until the test closes our stdin


if __name__ == "__main__":
    main()
