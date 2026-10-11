"""Готова ли е машината да пуска задачи — по истинския път на задача.

    sudo python3 -m cloud.check [--jobs /srv/jobs] [--web http://127.0.0.1:8080]

/healthz на уеб сървъра и на шлюза казват само „процесът е жив“. Деплой одит
2026-10-11: up.sh отчете „готово“, докато шлюзът беше в restarting, а /healthz
през Caddy беше 200 при уеб сървър без никакъв шлюз. Тук всяка проверка е
нещо, което задача наистина прави; първата неуспешна спира с код 1:

  - Docker отговаря, образите ги има, помощникът (шлюз или прокси) е Up и не
    се рестартира;
  - в мрежа на задача (launch.open_task_network), с всички тавани на
    launch.docker_args: няма пряк изход, помощникът отговаря (шлюзът — 200,
    проксито — 403 за example.com), / е само за четене, uid 10001, без
    capabilities, no_new_privs;
  - 1 GB памет при таван 256 MB → убито (137); 500 процеса при таван 64 → спира;
  - задача без ключ за модел → ok:false (не се таксува като успех), и след нея
    не остава нито контейнер, нито мрежа от пуснатите тук (задачите на клиенти
    на живия сървър не се броят и не се пипат);
  - свободно място за /srv/jobs; по желание — /healthz на уеб сървъра.

Не вика истински модел: задачата е без ключове, а пробите пускат python -c.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from cloud.runner import launch

# Какво проверява кодът в контейнера; на машината се чете като JSON.
_PROBE = r"""
import json, os, socket, sys, urllib.request
mode, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
r = {"uid": os.getuid()}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    r["direct"] = "open"
except OSError:
    r["direct"] = "closed"
try:
    open("/etc/genesis-probe", "w").close()
    r["rootfs"] = "writable"
except OSError:
    r["rootfs"] = "read-only"
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
r["cap_eff"] = int(status["CapEff"].strip(), 16)
r["no_new_privs"] = int(status.get("NoNewPrivs", "0").strip())
try:
    if mode == "gateway":
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r["helper"] = direct.open(f"http://{host}:{port}/healthz", timeout=5).status
    else:
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        r["helper"] = int(s.recv(64).split()[1])
except Exception as e:
    r["helper"] = f"{type(e).__name__}: {e}"
print(json.dumps(r))
"""
_MEMORY = "b = bytearray(1 << 30); print('alive')"
_PIDS = r"""
import subprocess
procs = []
try:
    for _ in range(500):
        procs.append(subprocess.Popen(["sleep", "30"]))
except OSError:
    pass
print(len(procs))
for p in procs:
    p.kill()
"""


class CheckFailed(Exception):
    pass


def _ok(cond: bool, what: str, detail: str = "") -> None:
    if not cond:
        raise CheckFailed(f"{what}{': ' + detail if detail else ''}")


def helper() -> tuple[str, str]:
    """(режим, адрес) според това кой помощник върви — както го е пуснал up.sh."""
    for mode, url in (("gateway", launch.GATEWAY_URL), ("proxy", launch.EGRESS_URL)):
        name = launch.helper_container(url)
        r = launch._docker("inspect", "-f", "{{.State.Running}} {{.State.Restarting}}", name)
        if r.returncode == 0:
            _ok(r.stdout.strip() == "true false", f"{name} не е Up",
                f"Running/Restarting = {r.stdout.strip()} (docker logs {name})")
            return mode, url
    raise CheckFailed("не върви нито genesis-gateway, нито genesis-egress — пусни ./cloud/up.sh")


def _in_task_network(url: str, code: str, args: list[str], limits: launch.Limits,
                     workspace: Path, started: set[str]) -> tuple[int, str]:
    """`python -c code args` в образа на задачите, с тавани и мрежа като задача.
    Името на контейнера се добавя в `started` — за проверката „нищо не остава“."""
    suffix = uuid.uuid4().hex[:12]
    name, network = f"{launch.TASK_PREFIX}{suffix}", f"{launch.NETWORK_PREFIX}{suffix}"
    started.add(name)
    host = launch.helper_container(url)
    error = launch.open_task_network(network, host)
    _ok(not error, "мрежа на задача", error)
    try:
        argv = launch.docker_args(name, workspace, "", limits=limits, network=network)
        # docker_args завършва с „образ, текст“: тук входът е python -c, не task.py.
        argv = [*argv[1:-2], "--entrypoint", "python", argv[-2], "-c", code, *args]
        r = launch._docker(*argv, timeout=limits.seconds + 30)
        return r.returncode, r.stdout.strip()
    finally:
        launch.close_task_network(network, name, host)


def leftovers(started: set[str]) -> tuple[list[str], list[str]]:
    """Кои от контейнерите в `started` (и мрежите им) още ги има.

    Само своите: ревю 2026-10-11 — броеше се всеки genesis-task-* контейнер и
    genesis-job-* мрежа на машината, а deploy.sh пуска проверката веднага след
    рестарта на genesis-web; при жива задача на клиент деплоят излизаше
    „неуспешен“. Мрежата на задача е NETWORK_PREFIX + същия суфикс."""
    nets = {launch.NETWORK_PREFIX + c[len(launch.TASK_PREFIX):] for c in started}
    ps = launch._docker("ps", "-a", "--format", "{{.Names}}",
                        "--filter", f"name=^{launch.TASK_PREFIX}")
    ls = launch._docker("network", "ls", "--format", "{{.Name}}")
    # Вълна 5, 2026-10-11: паднал `docker ps`/`network ls` даваше празен
    # списък и „✓ не остава контейнер или мрежа“ без нищо проверено.
    for r in (ps, ls):
        _ok(r.returncode == 0, "списъкът на Docker", (r.stderr or "").strip()[-200:])
    return (sorted(c for c in ps.stdout.split() if c in started),
            sorted(n for n in ls.stdout.split() if n in nets))


def run_checks(jobs: Path, web: str | None, min_free_gb: float,
               say: Callable[[str], None] = print) -> None:
    r = launch._docker("info", "--format", "{{.ServerVersion}}")
    _ok(r.returncode == 0, "Docker не отговаря", r.stderr.strip()[-200:])
    say(f"✓ Docker {r.stdout.strip()}")
    r = launch._docker("image", "inspect", "-f", "{{.Id}}", launch.IMAGE)
    _ok(r.returncode == 0, f"няма образ {launch.IMAGE}", "пусни ./cloud/build.sh")
    mode, url = helper()
    say(f"✓ режим: {mode} ({launch.helper_container(url)} е Up)")
    host = launch.helper_container(url)
    port = str(int(url.rsplit(":", 1)[1].strip("/")))
    started: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="genesis-check-") as tmp:
        ws = Path(tmp) / "work"
        launch.prepare_workspace(ws)
        code, out = _in_task_network(url, _PROBE, [mode, host, port], launch.Limits(seconds=60), ws,
                                     started)
        try:
            got = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            raise CheckFailed(f"пробата в контейнера не върна резултат (код {code}): {out[-300:]}")
        _ok(got["direct"] == "closed", "задачата има пряк изход навън")
        expected = 200 if mode == "gateway" else 403
        _ok(got["helper"] == expected, f"{host} от мрежата на задачата",
            f"очаквано {expected}, получено {got['helper']}")
        _ok(got["rootfs"] == "read-only", "/ в контейнера е записваем")
        _ok(got["uid"] == launch.RUNNER_UID and got["cap_eff"] == 0 and got["no_new_privs"] == 1,
            "права в контейнера", json.dumps(got))
        say(f"✓ изолация: без изход, {host} → {expected}, read-only, uid {got['uid']}, без capabilities")
        code, out = _in_task_network(url, _MEMORY, [], launch.Limits(memory="256m", seconds=60), ws,
                                     started)
        _ok(code == 137 and "alive" not in out, "таванът на паметта", f"код {code}")
        code, out = _in_task_network(url, _PIDS, [], launch.Limits(pids=64, seconds=60), ws,
                                     started)
        _ok(code == 0 and out.isdigit() and int(out) < 64, "таванът на процесите", f"{code} {out}")
        say(f"✓ тавани: 1 GB при 256 MB → 137, процеси {out}/64")
        # Задача без ключове — като на клиент, но без да вика модел: в режим с
        # шлюз жетон няма, затова пак е „без ключ“.
        res = launch.run_task("кажи здравей", ws, limits=launch.Limits(seconds=120),
                              egress_url=url)
        started.add(res.container)
        _ok(not res.ok and not res.timed_out, "задача без модел трябва да е ok:false",
            f"ok={res.ok} timed_out={res.timed_out} {res.error}")
        say(f"✓ задача без модел → ok:false ({res.error[:80]})")
    left, nets = leftovers(started)
    _ok(not left and not nets, "след задачите остана нещо", f"контейнери {left}, мрежи {nets}")
    say("✓ не остава контейнер или мрежа")
    free = shutil.disk_usage(jobs if jobs.exists() else jobs.parent).free / 2 ** 30
    _ok(free >= min_free_gb, f"място за {jobs}", f"{free:.1f} GB < {min_free_gb} GB")
    say(f"✓ свободно за задачите: {free:.1f} GB")
    if web:
        try:
            with urllib.request.urlopen(f"{web.rstrip('/')}/healthz", timeout=10) as resp:
                status = resp.status
        except OSError as e:
            raise CheckFailed(f"уеб сървърът {web}: {e}") from None
        _ok(status == 200, f"уеб сървърът {web}", f"HTTP {status}")
        say(f"✓ уеб сървърът отговаря ({web})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Проверка, че задачите могат да тръгнат и са затворени.")
    p.add_argument("--jobs", type=Path, default=Path("/srv/jobs"))
    p.add_argument("--web", help="напр. http://127.0.0.1:8080 — проверява и /healthz")
    p.add_argument("--min-free-gb", type=float, default=5.0)
    a = p.parse_args(argv)
    try:
        run_checks(a.jobs, a.web, a.min_free_gb)
    except CheckFailed as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print("всичко е наред")
    return 0


if __name__ == "__main__":
    sys.exit(main())
