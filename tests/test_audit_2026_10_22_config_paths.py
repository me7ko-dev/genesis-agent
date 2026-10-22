"""Одит 2026-10-22, подсистема „config-paths“: .env файловете, config.yaml,
празните променливи за пътища, `genesis update`, аргументите на подкомандите и
въвеждането на ключове в `genesis setup`. Всеки тест е падал преди поправката."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis_agent import cli, paths

ROOT = Path(__file__).resolve().parent.parent


# ── 1. празен `KEY=` в .env на проекта не скрива ключа в ~/.genesis/.env ─────

def test_an_empty_assignment_in_the_project_env_does_not_hide_the_home_key(tmp_path, monkeypatch):
    from genesis_agent import brain, setup_wizard
    project_env, home_env = tmp_path / "project.env", tmp_path / "home.env"
    # `cp .env.example .env` и попълнен само един ключ — останалите са `NAME=`.
    project_env.write_text("GROQ_API_KEY=\nOPENROUTER_API_KEY=sk-or-filled\n", encoding="utf-8")
    home_env.write_text("GROQ_API_KEY=gsk_real_from_setup\n", encoding="utf-8")
    files = (str(project_env), str(home_env))
    monkeypatch.setattr(paths, "ENV_FILES", files)
    monkeypatch.setattr(brain, "ENV_FILES", files)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert paths.read_env_files("GROQ_API_KEY") == "gsk_real_from_setup"
    assert paths.get_secret("GROQ_API_KEY") == "gsk_real_from_setup"
    keys = brain._load_keys()
    assert keys["GROQ_API_KEY"] == "gsk_real_from_setup"
    assert keys["OPENROUTER_API_KEY"] == "sk-or-filled"
    assert setup_wizard._existing("GROQ_API_KEY") == "gsk_real_from_setup"
    # Непразна стойност в проекта пак печели (документираният ред).
    project_env.write_text("GROQ_API_KEY=gsk_project\n", encoding="utf-8")
    assert paths.get_secret("GROQ_API_KEY") == "gsk_project"
    assert brain._load_keys()["GROQ_API_KEY"] == "gsk_project"


# ── 2. config.yaml с грешна форма: ясно съобщение, не AttributeError ────────

_BAD_SHAPES = {
    "workspace: ~/proj\n": "workspace",
    "- a\n- b\n": "config.yaml",
    "storage: ~/hist\n": "storage",
    "models: [1, 2]\n": "models",
    "models:\n  fallback_models: groq/x\n": "fallback_models",
    "models:\n  coding_models:\n    - groq/x\n": "coding_models",
}


@pytest.mark.parametrize("text", list(_BAD_SHAPES))
def test_a_config_yaml_of_the_wrong_shape_is_named_not_a_traceback(tmp_path, monkeypatch, text):
    from genesis_agent import brain, model_check
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(paths.ConfigError) as err:
        paths.load_config(cfg)
    assert _BAD_SHAPES[text] in str(err.value)
    # Зареждачите на веригите не гърмят — казва го `genesis models` (виж 4.).
    monkeypatch.setattr(brain, "CONFIG_PATH", cfg)
    monkeypatch.setattr(model_check, "CONFIG_PATH", cfg)
    monkeypatch.setattr(brain, "_dead_models", set)
    monkeypatch.setattr("genesis_agent.free_models.cached", list)
    assert brain._load_chain() == []
    assert brain._load_coding_chain() == []
    assert brain._load_light_chain() == []
    assert brain._load_premium_chain() == []
    assert model_check.configured_models() == []


def test_the_chat_names_the_bad_key_and_exits_1_without_a_traceback(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("workspace: ~/proj\n", encoding="utf-8")
    code = ("import sys; from pathlib import Path; import genesis_agent.paths as p; "
            "p.CONFIG_PATH = Path(sys.argv[1]); import genesis_terminal_agent")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-c", code, str(cfg)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, stdin=subprocess.DEVNULL, check=False)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "Traceback" not in out and "AttributeError" not in out, out
    assert "workspace" in out and "path:" in out, out


@pytest.mark.parametrize("text", ["models:\n", "models:\n  default_provider: groq\n  fallback_models:\n"])
def test_an_empty_section_or_list_is_empty_not_a_traceback(tmp_path, text):
    # Ревю 2026-10-22: празни `models:` / `fallback_models:` (изтрито всичко
    # под тях) минаваха проверката като None и чатът пак падаше със стектрейс.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    data = paths.load_config(cfg)
    assert isinstance(data["models"], dict)
    assert data["models"].get("fallback_models", []) == []
    code = ("import sys; from pathlib import Path; import genesis_agent.paths as p; "
            "p.CONFIG_PATH = Path(sys.argv[1]); import genesis_terminal_agent")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-c", code, str(cfg)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, stdin=subprocess.DEVNULL, check=False)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "Traceback" not in out, out


def test_a_value_yaml_cannot_build_is_a_config_error(tmp_path, monkeypatch, capsys):
    # Ревю 2026-10-22: PyYAML вдига гол ValueError (не YAMLError) за
    # `2026-13-01` или `!!int abc` — `genesis models` гърмеше със стектрейс.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  default_provider: groq\nupdated: 2026-13-01\n", encoding="utf-8")
    with pytest.raises(paths.ConfigError, match="month"):
        paths.load_config(cfg)
    monkeypatch.setattr(paths, "CONFIG_PATH", cfg)
    assert cli.main(["models"]) == 1
    assert "month" in capsys.readouterr().out


# ── 3. празна променлива за път = „по подразбиране“, не текущата папка ──────

def test_empty_path_variables_mean_the_default_not_the_current_directory(tmp_path):
    code = ("from genesis_agent import paths, config\n"
            "print(paths.GENESIS_HOME); print(paths.ENV_FILE); print(paths.history_dir())\n"
            "print(config.SANDBOX_DIR)\n"
            "try:\n"
            "    from genesis_agent import cryptography_utils\n"
            "    print(cryptography_utils.KEY_DIR)\n"
            "except ImportError:\n"
            "    print(paths.GENESIS_HOME)\n")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONPATH=str(ROOT),
               GENESIS_HOME="", XDG_DATA_HOME="", GENESIS_HISTORY_DIR="",
               GENESIS_SANDBOX_DIR="", GENESIS_KEY_DIR="")
    work = tmp_path / "project"
    work.mkdir()
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(work), env=env,
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 5, proc.stdout
    for line in lines:
        assert Path(line).is_absolute(), lines
    assert Path(lines[0]).name == ".genesis" and Path(lines[1]).name == ".env"
    assert Path(lines[4]) == Path(lines[0])
    assert not (work / ".env").exists()


def test_a_relative_xdg_data_home_is_ignored(monkeypatch):
    monkeypatch.delenv("GENESIS_HISTORY_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert paths.history_dir() == Path.home() / ".local" / "share" / "genesis-agent" / "history"
    monkeypatch.setenv("GENESIS_HISTORY_DIR", "  ")
    assert paths.history_dir().is_absolute()


# ── 4. счупен config.yaml: doctor и `genesis models` го казват ──────────────

def test_doctor_and_models_report_a_config_yaml_that_does_not_parse(tmp_path, monkeypatch, capsys):
    from genesis_agent import brain, doctor, model_check
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models:\n  default_provider: groq\nworkspace:\n\tpath: x\n", encoding="utf-8")
    for module in (paths, brain, model_check):
        monkeypatch.setattr(module, "CONFIG_PATH", cfg)

    checks = doctor.check_config(tmp_path)
    assert checks[0].status == "fail"
    assert "ред 4" in checks[0].detail and "колона 1" in checks[0].detail
    assert "интервал" in checks[0].fix
    assert ("config.yaml", doctor.check_config) in doctor.probes()

    assert cli.main(["models"]) == 1
    assert "ред 4" in capsys.readouterr().out
    assert cli.main(["models", "--check"]) == 1
    assert "ред 4" in capsys.readouterr().out

    cfg.write_text("models:\n  default_provider: groq\n", encoding="utf-8")
    assert doctor.check_config(tmp_path)[0].status == "ok"


# ── 5. `genesis update`: http.client грешки са „не знам сега“, не срив ──────

def test_update_check_survives_http_client_errors(monkeypatch):
    import http.client

    from genesis_agent import version_info as vi

    def bad_status(*_a, **_k):
        raise http.client.BadStatusLine("garbage")
    monkeypatch.setattr(vi.urllib.request, "urlopen", bad_status)
    assert vi.latest_commit("o/r", "main") is None
    assert vi.latest_release_commit("o/r") is None
    assert vi.changelog("o/r", "a", "b") is None

    class _CutOff:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, *_a):
            raise http.client.IncompleteRead(b"")
    monkeypatch.setattr(vi.urllib.request, "urlopen", lambda *_a, **_k: _CutOff())
    assert vi.latest_commit("o/r", "main") is None
    monkeypatch.setattr(vi, "installed_source",
                        lambda: vi.Source(url="https://github.com/o/r", commit="abc1234", ref="main"))
    assert "Не можах да питам GitHub" in vi.update_report(version="0.2.0")


# ── 6. подкомандите не пренебрегват --help и непознати опции ────────────────

@pytest.fixture
def no_delegates(monkeypatch):
    """Нищо истинско не тръгва: съветникът, веригата, уменията, GitHub, чатът."""
    ran: list[str] = []
    monkeypatch.setattr("genesis_agent.setup_wizard.run", lambda: ran.append("setup") or 0)
    monkeypatch.setattr(cli, "_models", lambda args: ran.append("models") or 0)
    monkeypatch.setattr("genesis_agent.skill_loader.format_skill_list", lambda: ran.append("skills") or "")
    monkeypatch.setattr("genesis_agent.version_info.update_report",
                        lambda **_k: ran.append("update") or "")
    monkeypatch.setattr(cli, "_chat", lambda: ran.append("chat") or 0)
    return ran


@pytest.mark.parametrize("argv", [["setup", "--help"], ["setup", "-h"], ["models", "--help"],
                                  ["skills", "--help"], ["update", "--help"], ["-c", "--help"]])
def test_subcommand_help_prints_usage_without_running_it(no_delegates, capsys, argv):
    assert cli.main(argv) == 0
    assert no_delegates == []
    assert "genesis" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["setup", "--bogus"], ["models", "--chek"], ["skills", "x"],
                                  ["update", "--force"], ["-c", "--bogus"], ["-r", "abc"],
                                  ["chat", "hello"]])
def test_unknown_subcommand_options_are_refused_with_2(no_delegates, capsys, argv):
    assert cli.main(argv) == 2
    assert no_delegates == []
    assert "Непозната опция" in capsys.readouterr().out


@pytest.mark.parametrize("argv,ran", [(["setup"], "setup"), (["models", "--refresh", "--check"], "models"),
                                      (["skills"], "skills"), (["update"], "update"), (["-c"], "chat"),
                                      (["-r", "3"], "chat"), (["--resume"], "chat"), ([], "chat")])
def test_valid_subcommand_arguments_still_run(no_delegates, argv, ran):
    assert cli.main(argv) == 0
    assert no_delegates == [ran]


# ── 7. ключът не се вижда на екрана, докато се въвежда ─────────────────────

def test_setup_reads_keys_without_echo_on_a_terminal(monkeypatch, capsys):
    from genesis_agent import setup_wizard as sw

    class _Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hf_SECRETTOKEN123456")

    def echoing_input(prompt=""):
        raise AssertionError("input() показва ключа на екрана")
    monkeypatch.setattr("builtins.input", echoing_input)

    assert sw._prompt_key("    HF_TOKEN = ") == "hf_SECRETTOKEN123456"
    out = capsys.readouterr().out
    assert "HF_TOKEN = " in out
    assert "hf_SECRETTOKEN123456" not in out
    assert "3456" in out          # маскирано потвърждение какво е поставено
