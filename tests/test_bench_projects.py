"""scripts/bench_projects.py — the parts that decide the numbers, without a model."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bench_projects", ROOT / "scripts" / "bench_projects.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)


LOG = """\
  [Brain] ↪ модел: groq/openai/gpt-oss-120b
  [Brain] ✗ groq/openai/gpt-oss-120b след 0.3s: HTTP_429: {"error":"rate"} → следващ
  [Brain] ✗ nvidia/nvidia/nemotron-3-ultra-550b-a55b след 30.0s: timeout → следващ
↪ Отговорено от → ollama / gpt-oss:120b FREE (временно — пинът си остава groq/openai/gpt-oss-120b)
↪ Отговорено от → ollama / gpt-oss:120b FREE
  [Brain] ✗ groq/openai/gpt-oss-120b след 0.2s: HTTP_429: {} → следващ
"""


def test_parse_log_counts_drop_reasons():
    assert bp.parse_log(LOG) == {"HTTP_429": 2, "timeout": 1}


def test_usage_since_counts_every_call_including_the_pinned_model():
    lines = [
        '{"provider": "groq", "model": "openai/gpt-oss-120b", "total_tokens": 3000}',
        '{"provider": "groq", "model": "openai/gpt-oss-120b", "total_tokens": 1000}',
        '{"provider": "ollama", "model": "gpt-oss:120b", "total_tokens": 500}',
        'half a line from a crash',
    ]
    assert bp.usage_since(lines) == (4500, {"groq/openai/gpt-oss-120b": 2, "ollama/gpt-oss:120b": 1})


def test_parse_pytest_summary():
    assert bp.parse_pytest("..F.\n1 failed, 3 passed in 0.12s\n") == (3, 4)
    assert bp.parse_pytest("5 passed in 0.03s") == (5, 5)


def test_parse_pytest_collection_error_is_a_failure_not_zero_of_zero():
    out = "ERROR test_hidden.py - ModuleNotFoundError: No module named 'egn'\n1 error in 0.1s"
    assert bp.parse_pytest(out) == (0, 1)
    assert bp.parse_pytest("") == (0, 1)


def test_summarize_and_table():
    runs = [
        {"project": "egn", "passed": 5, "total": 5, "seconds": 20, "tokens": 1000},
        {"project": "egn", "passed": 3, "total": 5, "seconds": 40, "tokens": 3000},
    ]
    s = bp.summarize(runs)["egn"]
    assert s["runs"] == 2 and s["ok"] == 1
    assert abs(s["tests"] - 0.8) < 1e-9
    assert s["seconds"] == 30 and s["tokens"] == 2000
    table = bp.format_table(bp.summarize(runs), before={"egn": {"ok": 0, "runs": 2, "tests": 0.5, "seconds": 60}})
    assert "1/2" in table and "0/2, 50%, 60 s" in table


def test_every_project_has_a_task_and_hidden_tests():
    projects = [p for p in bp.PROJECTS_DIR.iterdir() if p.is_dir()]
    assert projects
    for p in projects:
        assert (p / "task.txt").is_file(), p.name
        assert (p / "test_hidden.py").is_file(), p.name
