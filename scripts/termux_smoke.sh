#!/data/data/com.termux/files/usr/bin/bash
# CI (native.yml, задачата termux): Genesis на телефона, без телефон.
# termux/termux-docker е истинският Termux — libc-то на Android (bionic),
# пакетите на Termux, без /bin/sh и /usr — на x86_64. Тук минава всичко,
# с което Android се различава от обикновен Linux: инсталацията
# (scripts/install-termux.sh), `genesis phone`, криптираният протокол на
# приложението и командите на агента през sandbox-а.
#
#   docker run --rm -v "$PWD:/src:ro" termux/termux-docker:x86_64 bash /src/scripts/termux_smoke.sh
set -euo pipefail

# /src е само за четене и чужд потребител: pip сглобява в самото копие.
rm -rf "$HOME/src" && cp -r /src "$HOME/src" && rm -rf "$HOME/src/build" "$HOME/src"/*.egg-info

echo "::group::environment"
env | grep -E '^(LD_PRELOAD|PREFIX|TERMUX|ANDROID_)' | sort || true
ls "$PREFIX/lib" | grep -i termux-exec || true
echo "::endgroup::"

echo "::group::install-termux.sh"
GENESIS_SRC="$HOME/src" GENESIS_CI=1 bash "$HOME/src/scripts/install-termux.sh"
echo "::endgroup::"

grep -q '^allow-external-apps = true' "$HOME/.termux/termux.properties"
test -x "$HOME/.termux/boot/genesis"

echo "::group::genesis phone start"
genesis phone start || { genesis phone log; exit 1; }
genesis phone status
echo "::endgroup::"

PY="$HOME/.genesis/venv/bin/python"

echo "::group::the app's protocol against Genesis on the phone"
"$PY" - <<'EOF'
import json, secrets, time, urllib.request
from pathlib import Path

from genesis_agent import remote_server as rs
from genesis_agent.phone import app_link

key = rs.load_or_create_key()
hello = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/hello", timeout=10))
assert hello["app"] == "genesis" and hello["key_id"] == rs.key_id(key), hello


def call(op, **args):
    payload = {"ts": int(time.time() * 1000), "rid": secrets.token_hex(8), "op": op, **args}
    body = json.dumps(rs.Cipher(key).seal(payload, rs._REQ_AAD)).encode()
    req = urllib.request.Request("http://127.0.0.1:8765/api/v1", data=body,
                                 headers={"Content-Type": "application/json"})
    env = json.load(urllib.request.urlopen(req, timeout=30))
    return rs.Cipher(key).open(env, rs._RES_AAD + payload["rid"].encode())


status = call("status")
print("status:", status)
assert status["ok"] and status["workspace"] == str(Path.home() / "genesis"), status
cmd = call("command", name="status")
print("command status:", json.dumps(cmd, ensure_ascii=False)[:300])
assert cmd.get("ok"), cmd
print("pair link:", app_link(rs.pairing_url("127.0.0.1", 8765, key, rs.machine_name()))[:40] + "…")
EOF
echo "::endgroup::"

echo "::group::commands through the sandbox, as the agent runs them"
"$PY" - <<'EOF'
import os
from pathlib import Path

from genesis_agent import sandbox
from genesis_agent.paths import is_android

assert is_android(), "Termux Python should look like Android"
assert sandbox._shell_argv("true")[0] != "/bin/sh", sandbox._shell_argv("true")
work = Path.home() / "genesis"
(work / "hello.js").write_text("#!/usr/bin/env node\nconsole.log('node via env shebang')\n")
os.chmod(work / "hello.js", 0o755)
for cmd, want in [("echo shell ok", "shell ok"),
                  ("python3 -c 'print(6*7)'", "42"),
                  ("node -e 'console.log(1+1)'", "2"),
                  ("./hello.js", "node via env shebang"),
                  ("git --version", "git version"),
                  ("echo $TMPDIR", "/data/data/com.termux/files/usr/tmp")]:
    r = sandbox.run_shell(cmd, cwd=work)
    print(f"$ {cmd}\n{r.stdout.strip()}\n{r.stderr.strip()}")
    assert want in r.stdout, (cmd, r)
r = sandbox.run_python("import sys; print(sys.platform)", cwd=work)
assert r.ok, r
print("run_python:", r.stdout.strip())
EOF
echo "::endgroup::"

echo "::group::pytest (the Android-specific parts)"
"$PY" -m pip install --quiet pytest
cd "$HOME/src"
"$PY" -m pytest -q -p no:cacheprovider tests/test_phone.py tests/test_remote_server.py
echo "::endgroup::"

genesis phone stop
if genesis phone status; then echo "still running after stop" >&2; exit 1; fi
echo "Termux: всичко мина."
