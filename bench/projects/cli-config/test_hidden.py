import csv
import importlib.util
import json
import subprocess
import sys

SCRIPT = importlib.util.find_spec("pricer").origin
INPUT = "продукт;категория;доставна\nВода;напитки;1,10\nХляб;храни;2,50\nСирене;храни;10,00\n"


def run(tmp_path, *args, text=INPUT):
    (tmp_path / "in.csv").write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, SCRIPT, "in.csv", "out.csv", *args], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60, check=False)


def prices(tmp_path):
    with open(tmp_path / "out.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert rows[0][-1].strip().lower() == "продажна"
    return {r[0]: r[-1] for r in rows[1:]}


def test_defaults_round_up(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert prices(tmp_path) == {"Вода": "1,75", "Хляб": "3,90", "Сирене": "15,60"}


def test_an_exact_step_is_not_pushed_up_by_float_noise(tmp_path):
    """26,25 × 1,1 × 1,2 = 34,65 точно; с float 34.650000000000006 → „нагоре“ = 34,70."""
    (tmp_path / "config.json").write_text(json.dumps({"markup": 10}), encoding="utf-8")
    r = run(tmp_path, text="продукт;категория;доставна\nКафе;храни;26,25\n")
    assert r.returncode == 0, r.stderr
    assert prices(tmp_path) == {"Кафе": "34,65"}


def test_config_json_in_the_current_folder_and_categories(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"markup": 30, "categories": {"напитки": 25}}),
                                          encoding="utf-8")
    assert run(tmp_path).returncode == 0
    assert prices(tmp_path)["Вода"] == "1,65"  # 1,10 × 1,25 × 1,2 = 1,65 точно


def test_markup_flag_beats_the_config_but_not_the_categories(tmp_path):
    cfg = tmp_path / "other.json"
    cfg.write_text(json.dumps({"markup": 10, "vat": 20, "round_to": 0.1, "categories": {"напитки": 25}}),
                   encoding="utf-8")
    r = run(tmp_path, "--config", str(cfg), "--markup", "50")
    assert r.returncode == 0, r.stderr
    assert prices(tmp_path) == {"Вода": "1,70", "Хляб": "4,50", "Сирене": "18,00"}


def test_broken_config_and_missing_input_exit_2_without_traceback(tmp_path):
    (tmp_path / "config.json").write_text("{markup: 30", encoding="utf-8")
    r = run(tmp_path)
    assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout and (r.stderr + r.stdout).strip()
    (tmp_path / "config.json").write_text(json.dumps({"markup": "много"}), encoding="utf-8")
    r = run(tmp_path)
    assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout
    (tmp_path / "config.json").unlink()
    r = subprocess.run([sys.executable, SCRIPT, "няма.csv", "out.csv"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr + r.stdout
