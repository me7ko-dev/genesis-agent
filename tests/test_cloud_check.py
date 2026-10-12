"""cloud/check.py без Docker: всяка `docker` команда минава през фалшив Docker."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cloud import check
from cloud.runner import launch

LIVE = f"{launch.TASK_PREFIX}live00000000"          # задача на клиент, която върви
LIVE_NET = f"{launch.NETWORK_PREFIX}live00000000"


class Machine:
    """Docker на сървър, на който върви задача на клиент. `leak` — контейнерите
    и мрежите на самата проверка остават (rm/network rm не ги махат)."""

    def __init__(self, leak: bool = False) -> None:
        self.leak = leak
        self.containers = {LIVE}
        self.networks = {LIVE_NET, "bridge", "genesis-jobs"}

    def __call__(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        a = list(args)
        out, code = "", 0
        if a[0] == "info":
            out = "27.0.0"
        elif a[0] == "inspect":
            out = "true false"
        elif a[:2] == ["network", "create"]:
            self.networks.add(a[-1])
        elif a[:2] == ["network", "rm"] and not self.leak:
            self.networks.discard(a[2])
        elif a[:2] == ["network", "ls"]:
            out = "\n".join(sorted(self.networks))
        elif a[:2] == ["rm", "-f"] and not self.leak:
            self.containers.difference_update(a[2:])
        elif a[0] == "ps":
            out = "\n".join(sorted(self.containers))
        elif a[0] == "run":
            self.containers.add(a[a.index("--name") + 1])
            code, out = self._probe(a)
        return subprocess.CompletedProcess(["docker", *a], code, out, "")

    @staticmethod
    def _probe(a: list[str]) -> tuple[int, str]:
        if check._PROBE in a:
            return 0, json.dumps({"uid": launch.RUNNER_UID, "direct": "closed", "rootfs": "read-only",
                                  "cap_eff": 0, "no_new_privs": 1, "helper": 200})
        if check._MEMORY in a:
            return 137, ""
        return 0, "63"


@pytest.fixture
def machine(monkeypatch):
    def install(leak: bool = False) -> Machine:
        m = Machine(leak)
        monkeypatch.setattr(launch, "_docker", m)

        def run_task(text: str, workspace: Path, **kw: Any) -> launch.Result:
            name = f"{launch.TASK_PREFIX}task00000000"
            m.containers.add(name)
            if not leak:
                m.containers.discard(name)
            return launch.Result(False, False, 1, 0.1, error="няма ключ за модел", container=name)

        monkeypatch.setattr(launch, "run_task", run_task)
        return m
    return install


def test_a_live_customer_task_does_not_fail_the_check(machine, tmp_path) -> None:
    """Ревю 2026-10-11: проверката броеше ВСЕКИ genesis-task-* контейнер и
    genesis-job-* мрежа на машината. deploy.sh я пуска веднага след рестарта на
    genesis-web — при жива задача на клиент деплоят излизаше „неуспешен“."""
    m = machine()
    said: list[str] = []
    check.run_checks(tmp_path, None, 0.0, say=said.append)
    assert any("не остава контейнер или мрежа" in s for s in said)
    assert LIVE in m.containers and LIVE_NET in m.networks     # чуждото не се пипа


def test_what_the_check_itself_started_must_be_gone(machine, tmp_path) -> None:
    machine(leak=True)
    with pytest.raises(check.CheckFailed, match="след задачите остана нещо") as e:
        check.run_checks(tmp_path, None, 0.0, say=lambda s: None)
    assert "task00000000" in str(e.value) and "live" not in str(e.value)


def test_a_failing_docker_listing_is_not_nothing_left(machine, tmp_path, monkeypatch) -> None:
    """Вълна 5, 2026-10-11: `docker ps` с грешка даваше празен списък и
    проверката казваше „не остава контейнер или мрежа“, без да е видяла нищо."""
    m = machine(leak=True)

    def broken(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        if args[0] == "ps" or args[:2] == ("network", "ls"):
            return subprocess.CompletedProcess(["docker", *args], 1, "", "Cannot connect")
        return m(*args, timeout=timeout)

    monkeypatch.setattr(launch, "_docker", broken)
    with pytest.raises(check.CheckFailed, match="списъкът на Docker"):
        check.leftovers({f"{launch.TASK_PREFIX}task00000000"})


def test_run_task_says_which_container_it_used(tmp_path, fake_docker) -> None:
    fake_docker.answers[("network", "create")] = (1, "", "pool exhausted")
    res = launch.run_task("x", tmp_path / "w")
    assert res.container.startswith(launch.TASK_PREFIX)
