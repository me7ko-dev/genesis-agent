"""Bench скриптовете от край до край — истинският чат срещу фалшив доставчик.

Защо (2026-10-11): във вълна 4 харнесът беше поправен с пускания срещу фалшив
OpenAI-съвместим доставчик в чужда папка. Тук той е в репото
(tests/fake_openai_provider.py), за да хване CI следващото счупване, преди
операторът да похарчи квота на лаптопа. Без ключове: GENESIS_MODEL_GATEWAY
праща всеки отдалечен доставчик към фалшивия, GROQ_API_KEY е фиктивен.

Всичко върви в подпроцеси с отделни GENESIS_HOME и GENESIS_DATA_DIR: чатът
пише budget_log, provider_stats и model_check — без GENESIS_DATA_DIR те
отиваха в genesis_agent/ на checkout-а (измерено 2026-10-11).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_openai_provider as fp

PROJECTS = ROOT / "bench" / "projects"
# Само стандартната библиотека в скритите тестове — CI няма flask/bs4.
PROJECT = "sync-cli"
CHAT_TIMEOUT = 240

# Ключ на доставчик от средата на машината не бива да стига до чата: веригата
# ще е друга, а заявката пак ще отиде към фалшивия шлюз с истинския ключ.
_SECRET = re.compile(r"(_API_KEY(_\d+)?|_TOKEN|^GOOGLE_CLOUD_PROJECT)$")


def _env(tmp_path: Path, url: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not _SECRET.search(k) and not k.startswith("GENESIS_")}
    env.update(GENESIS_MODEL_GATEWAY=url, GROQ_API_KEY="fake-key",
               GENESIS_HOME=str(tmp_path / "genesis_home"),
               GENESIS_DATA_DIR=str(tmp_path / "data"),
               GENESIS_LOCAL_MODEL="",                       # без проба на локален модел
               GENESIS_NO_PROJECT_ENV="1",                   # без ключовете от .env на checkout-а
               XDG_DATA_HOME=str(tmp_path / "xdg"),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                          check=False)


def _bench_projects(tmp_path: Path, script: list[list[dict]]) -> tuple[subprocess.CompletedProcess, dict, list]:
    out = tmp_path / "out"
    with fp.FakeProvider(script) as fake:
        proc = _run([str(ROOT / "scripts" / "bench_projects.py"), "--only", PROJECT, "--runs", "1",
                     "--out", str(out), "--test-python", sys.executable,
                     "--timeout", str(CHAT_TIMEOUT)], _env(tmp_path, fake.url), tmp_path)
        requests = list(fake.requests)
    results = json.loads((out / "results.json").read_text("utf-8"))
    return proc, results, requests


def _reference() -> dict[str, str]:
    return fp.reference_files(PROJECTS / PROJECT / "reference")


def test_the_reference_through_the_real_chat_is_a_pass(tmp_path):
    proc, results, requests = _bench_projects(tmp_path, fp.solution_script(_reference()))
    run = results["runs"][0]
    detail = proc.stdout[-3000:] + proc.stderr[-2000:]
    # Чатът е стигнал до фалшивия доставчик през шлюза, с фиктивния ключ,
    # и е минал целия сценарий (запис, собствен тест, pytest).
    assert requests and all(r["path"].endswith("/chat/completions") for r in requests), detail
    assert {r["auth"] for r in requests} == {"Bearer fake-key"}, detail
    assert max(r["step"] for r in requests) >= 3, detail
    assert proc.returncode == 0, detail
    assert results["complete"] is True
    assert not run["infra"] and run["passed"] == run["total"] > 1, run
    assert results["summary"][PROJECT]["ok"] == 1
    # Токените и моделите идват от бюджетния лог в GENESIS_DATA_DIR — същият
    # път в bench-а и в чата; иначе bench_all не би имал база (answered()).
    assert run["tokens"] > 0 and run["models"], run
    assert (tmp_path / "data" / "budget_log.jsonl").is_file()
    assert "✅ sync-cli #1" in proc.stdout


def test_a_broken_solution_through_the_real_chat_is_a_fail(tmp_path):
    files = _reference()
    good = 'args.target or env.get("SYNC_TARGET")'
    assert good in files["sync.py"]
    # Предимството аргументи > среда е обърнато за target — само един от
    # скритите тестове пада. (За source скритите тестове не го проверяват:
    # същата грешка там минава 10/10 — измерено 2026-10-11.)
    files["sync.py"] = files["sync.py"].replace(good, 'env.get("SYNC_TARGET") or args.target')
    proc, results, _ = _bench_projects(tmp_path, fp.solution_script(files))
    run = results["runs"][0]
    assert proc.returncode == 1, proc.stdout[-3000:]
    assert not run["infra"], run
    assert 0 < run["passed"] < run["total"], run
    assert results["summary"][PROJECT]["ok"] == 0
    assert "❌ sync-cli #1" in proc.stdout


def test_bench_fix_through_the_real_repair_loop(tmp_path):
    fixed = ("def median(xs):\n    s = sorted(xs)\n    n = len(s)\n"
             "    if n % 2:\n        return s[n // 2]\n    return (s[n // 2 - 1] + s[n // 2]) / 2\n")
    script = [[{"name": "READ_FILE", "arguments": {"path": "stats.py"}}],
              [{"name": "WRITE_FILE", "arguments": {"path": "stats.py", "content": fixed}}]]
    out = tmp_path / "fix"
    with fp.FakeProvider(script) as fake:
        proc = _run([str(ROOT / "scripts" / "bench_fix.py"), "--only", "median", "--out", str(out)],
                    _env(tmp_path, fake.url), tmp_path)
        requests = list(fake.requests)
    detail = proc.stdout[-3000:] + proc.stderr[-2000:]
    # repair() сам пуска теста след записа и спира, щом е зелен.
    assert requests and max(r["step"] for r in requests) >= 1, detail
    assert proc.returncode == 0, detail
    data = json.loads((out / "results.json").read_text("utf-8"))
    r = data["runs"][0]
    assert r["fixed"] and not r["test_touched"] and not r["infra"], r
    assert data["summary"]["median"]["ok"] == 1 and data["summary"]["median"]["tokens"] > 0


def test_bench_fix_a_claimed_but_missing_fix_is_a_failure(tmp_path):
    # Моделът казва „Готово.“, без да пипне нищо: не е поправено, не е infra.
    out = tmp_path / "fix"
    with fp.FakeProvider([]) as fake:
        proc = _run([str(ROOT / "scripts" / "bench_fix.py"), "--only", "median", "--out", str(out)],
                    _env(tmp_path, fake.url), tmp_path)
    assert proc.returncode == 1, proc.stdout[-3000:] + proc.stderr[-2000:]
    r = json.loads((out / "results.json").read_text("utf-8"))["runs"][0]
    assert not r["fixed"] and not r["infra"], r


def test_an_empty_answer_is_a_model_failure_in_both_benches(tmp_path):
    # Моделът отговаря (и е таксуван — редът е в бюджетния лог), но с празно
    # съдържание. bench_projects го брои за грешен пуск; bench_fix го броеше
    # за infra („моделът не отговори“) и проектът изпадаше от сравнението в
    # bench_all — регресия до празни отговори нямаше как да се види
    # (измерено 2026-10-11 с този фалшив доставчик).
    out = tmp_path / "fix"
    with fp.FakeProvider([], final="") as fake:
        env = _env(tmp_path, fake.url)
        fix = _run([str(ROOT / "scripts" / "bench_fix.py"), "--only", "median", "--out", str(out)],
                   env, tmp_path)
        projects = _run([str(ROOT / "scripts" / "bench_projects.py"), "--only", PROJECT, "--runs", "1",
                         "--out", str(tmp_path / "out"), "--test-python", sys.executable,
                         "--timeout", str(CHAT_TIMEOUT)], env, tmp_path)
    r = json.loads((out / "results.json").read_text("utf-8"))["runs"][0]
    assert not r["fixed"] and not r["infra"], (r, fix.stdout[-2000:])
    assert fix.returncode == 1
    run = json.loads((tmp_path / "out" / "results.json").read_text("utf-8"))["runs"][0]
    assert not run["infra"] and run["passed"] < run["total"], run
    assert projects.returncode == 1


def test_a_provider_that_is_down_is_infra_in_both_benches(tmp_path):
    # Обратната страна: нито един таксуван отговор → infra, не грешка на модела.
    out = tmp_path / "fix"
    env = _env(tmp_path, "http://127.0.0.1:9")      # нищо не слуша там
    fix = _run([str(ROOT / "scripts" / "bench_fix.py"), "--only", "median", "--out", str(out)], env, tmp_path)
    projects = _run([str(ROOT / "scripts" / "bench_projects.py"), "--only", PROJECT, "--runs", "1",
                     "--out", str(tmp_path / "out"), "--test-python", sys.executable,
                     "--timeout", str(CHAT_TIMEOUT)], env, tmp_path)
    assert json.loads((out / "results.json").read_text("utf-8"))["runs"][0]["infra"] is True
    assert json.loads((tmp_path / "out" / "results.json").read_text("utf-8"))["runs"][0]["infra"] is True
    assert fix.returncode == 1 and projects.returncode == 1


def test_bench_all_dry_run_plans_against_the_baseline_and_runs_nothing(tmp_path):
    env = _env(tmp_path, "http://127.0.0.1:9")    # нищо не бива да се вика
    base = tmp_path / "genesis_home" / "bench" / "2026-09-30-history"
    base.mkdir(parents=True)
    every = sorted(p.name for p in PROJECTS.iterdir() if (p / "task.txt").is_file())
    summary = {n: {"runs": 2, "ok": 2, "tests": 1.0, "seconds": 30, "tokens": 5000, "infra": 0} for n in every}
    (base / "results.json").write_text(json.dumps({"complete": True, "summary": summary, "runs": []}),
                                       encoding="utf-8")
    out = tmp_path / "all"
    proc = _run([str(ROOT / "scripts" / "bench_all.py"), "--dry-run", "--steps", "projects,acceptance,fix",
                 "--out", str(out)], env, tmp_path, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert str(base / "results.json") in proc.stdout
    assert f"{len(every)} проекта" in proc.stdout
    assert "GENESIS_ACCEPTANCE=1" in proc.stdout and "bench_fix.py" in proc.stdout
    assert not out.exists()
    assert not (tmp_path / "data" / "budget_log.jsonl").exists()


def test_the_chat_under_test_reads_no_project_env(tmp_path):
    # .env на checkout-а е документираното място за ключове на лаптопа: с него
    # истинските OPENROUTER/HF/NVIDIA ключове влизаха в чата под тест и
    # веригата ставаше друга (ревю 2026-10-11 — с .env с OPENROUTER_API_KEY
    # чатът пробваше openrouter модели срещу шлюза). Тук се проверява самият
    # подпроцес — без да се пише в .env на checkout-а.
    probe = "import genesis_agent.paths as p, json; print(json.dumps(list(p.ENV_FILES)))"
    proc = _run(["-c", probe], _env(tmp_path, "http://127.0.0.1:9"), ROOT, timeout=60)
    assert proc.returncode == 0, proc.stderr
    files = [Path(f) for f in json.loads(proc.stdout.strip().splitlines()[-1])]
    assert ROOT / ".env" not in files
    assert files == [tmp_path / "genesis_home" / ".env"]


@pytest.mark.parametrize("name", sorted(p.parent.name for p in PROJECTS.glob("*/reference")))
def test_every_reference_has_a_script_the_fake_can_serve(name):
    # Сценарият за ръчното пускане над всички еталони (виж модула на фалшивия
    # доставчик) — всеки файл е WRITE_FILE с относителен път с `/`.
    script = fp.solution_script(fp.reference_files(PROJECTS / name / "reference"))
    writes = [c["arguments"]["path"] for step in script for c in step if c["name"] == "WRITE_FILE"]
    assert writes and all("\\" not in p and not p.startswith("/") for p in writes)
    assert script[-1][0]["name"] == "RUN_CMD"
