"""cloud/up.sh, build.sh и deploy/ — със запис на `docker` командите вместо
истински Docker. Истинските проверки са в cloud/README.md (измерено с Docker
29.8.2, 2026-10-11)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(os.name == "nt" or BASH is None,
                                reason="скриптовете за сървъра са bash (Linux)")

_FAKE = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\\n")
if args[:1] == ["inspect"]:
    print(os.environ.get("FAKE_STATE", "true 0"))
elif args[:2] == ["run", "--rm"]:
    sys.exit(int(os.environ.get("FAKE_PROBE_RC", "0")))
elif args[:1] == ["logs"]:
    print("ValueError: GATEWAY_SECRET трябва да е поне 32 знака")
"""


def _up(tmp_path: Path, keys: str | None, **env: str) -> tuple[int, str, list[list[str]]]:
    fake = tmp_path / "docker"
    fake.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    keys_path = tmp_path / "keys.env"
    if keys is not None:
        keys_path.write_text(keys, encoding="utf-8")
    run_env = {**os.environ, "DOCKER": str(fake), "FAKE_LOG": str(log), "GENESIS_UP_WAIT": "0",
               "GENESIS_KEYS": str(keys_path), "GENESIS_GATEWAY_DATA": str(tmp_path / "gw"),
               **env}
    p = subprocess.run([str(BASH), str(ROOT / "cloud" / "up.sh")], capture_output=True, check=False,
                       text=True, encoding="utf-8", env=run_env, cwd=tmp_path, timeout=60)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return p.returncode, p.stdout + p.stderr, calls


def _started(calls: list[list[str]]) -> list[str]:
    return [c[c.index("--name") + 1] for c in calls if c[:2] == ["run", "-d"]]


SECRET = "GATEWAY_SECRET=" + "s" * 40 + "\nGROQ_API_KEY=k\n"


def test_gateway_mode_starts_no_proxy(tmp_path) -> None:
    """D2 (деплой одит 2026-10-11): проксито се включваше в genesis-jobs и в
    режим с шлюз — задача отваряше CONNECT към openrouter.ai покрай шлюза."""
    code, out, calls = _up(tmp_path, SECRET)
    assert code == 0, out
    assert _started(calls) == ["genesis-gateway"]
    assert ["rm", "-f", "genesis-egress"] in calls
    assert ["network", "connect", "genesis-jobs", "genesis-gateway"] in calls
    assert not any("genesis-egress" in c and c[:2] == ["network", "connect"] for c in calls)


def test_proxy_mode_without_keys_removes_an_old_gateway(tmp_path) -> None:
    """D5d: след изтрит keys.env старият шлюз оставаше да върви."""
    code, out, calls = _up(tmp_path, None)
    assert code == 0, out
    assert _started(calls) == ["genesis-egress"]
    assert ["rm", "-f", "genesis-gateway"] in calls
    assert ["network", "connect", "genesis-jobs", "genesis-egress"] in calls


def test_keys_without_a_secret_mean_proxy_mode(tmp_path) -> None:
    """Одит 2026-10-11: keys.env само с GROQ_API_KEY пускаше шлюз, който
    падаше в цикъл, а up.sh печаташе „готово“."""
    code, out, calls = _up(tmp_path, "GROQ_API_KEY=k\n")
    assert code == 0 and _started(calls) == ["genesis-egress"], out
    assert "няма GATEWAY_SECRET" in out


@pytest.mark.parametrize("env", [{"FAKE_STATE": "true 3"}, {"FAKE_STATE": "false 0"},
                                 {"FAKE_PROBE_RC": "1"}])
def test_a_crash_looping_or_deaf_gateway_fails_the_script(tmp_path, env) -> None:
    """D5b: up.sh излизаше с 0 и „готово“, докато шлюзът беше в restarting."""
    code, out, _ = _up(tmp_path, SECRET, **env)
    assert code != 0 and "готово" not in out and "ГРЕШКА" in out


def test_a_proxy_that_lets_example_com_through_fails_the_script(tmp_path) -> None:
    code, out, _ = _up(tmp_path, None, FAKE_PROBE_RC="1")
    assert code != 0 and "403" in out


def test_helper_logs_have_a_size_cap(tmp_path) -> None:
    """D5c: json-file без max-size — проксито логва всеки отказ, без край."""
    for keys in (SECRET, None):
        _, _, calls = _up(tmp_path, keys)
        run = next(c for c in calls if c[:2] == ["run", "-d"])
        assert ("--log-opt", "max-size=10m") in set(pairwise(run))
        assert "--restart" in run and "--read-only" in run


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="chown иска root")
def test_root_leaves_the_keys_readable_by_web_and_gateway(tmp_path) -> None:
    """D5a: `chown 10003` оставяше keys.env 10003:root 600 — уеб сървърът
    (--env-file/--gateway-usage) падаше с PermissionError."""
    _up(tmp_path, SECRET)
    st = (tmp_path / "keys.env").stat()
    assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (0, 10003, 0o640)


def test_build_runs_from_any_folder_with_python3(tmp_path) -> None:
    """D5e: от друга папка build.sh падаше („Directory '.' is not installable“);
    Ubuntu Server няма команда `python`. Образите са и с етикет git sha."""
    text = (ROOT / "cloud" / "build.sh").read_text(encoding="utf-8")
    assert 'cd "$(dirname "$0")/.."' in text
    assert 'PYTHON="${PYTHON:-python3}"' in text and "\npython " not in text
    assert '-t "genesis-$image:$SHA"' in text


@pytest.mark.parametrize("script", sorted(p.relative_to(ROOT).as_posix() for p in
                                          [*ROOT.glob("cloud/*.sh"), *ROOT.glob("deploy/*.sh")]))
def test_shell_scripts_parse(script) -> None:
    p = subprocess.run([str(BASH), "-n", str(ROOT / script)], capture_output=True, text=True,
                       check=False)
    assert p.returncode == 0, p.stderr
