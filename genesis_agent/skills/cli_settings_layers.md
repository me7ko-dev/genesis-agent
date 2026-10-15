---
name: cli_settings_layers
category: domain
min_score: 3
description: Настройки на CLI от файл (ini/json), средата и аргументите — по-късното печели;
  store_true/append/число с default=None, иначе смачкват файла; стойностите се връщат както са
  дошли (str(Path("/data/a")) на Windows е "\data\a"); счупен файл → stderr и код 2.
triggers:
- настройките идват от файл средата и аргументите
- аргументите средата файла
- аргументите > средата > файла
- config.ini средата аргументите
- config.json --config
- командния ред --config
- configparser argparse
version: '1.0'
author: Genesis
last_updated: '2026-10-12T01:40:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-12 (bench sync-cli,
claude-opus-5, 8/10): `load_settings` върна `str(Path("/data/a"))` — на Windows
това е `\data\a`, а от функцията се искаше стойността, както е дошла от
config.ini/средата/аргументите. Другите класически капани на същата задача
(argparse): `store_true` има default False и „задава“ dry_run винаги, тоест
смачква `dry_run = yes` от файла; `action="append"` удължава default-а, вместо
да замени списъка от файла; числов аргумент с default смачква стойността от
config-а.

## Python Код
```python
"""Настройки от няколко места: подразбиране → файл → среда → аргументи.

Факти (Python 3.10+, docs.python.org argparse/configparser):
- argparse: store_true има default=False, число/низ — default=None само ако го
  оставиш. Сложи default=None НАВСЯКЪДЕ и сливай само стойностите, които не са
  None — иначе аргумент, който никой не е подал, смачква файла и средата.
- action="append" ДОБАВЯ към default-а. Списък, който аргументите трябва да
  ЗАМЕНЯТ: default=None, а не default=списъка_от_файла.
- Върни стойностите каквито са дошли. str(Path("/data/a")) на Windows е
  "\\data\\a" — Path(...) едва там, където пътят се ползва (копиране, четене).
- configparser: cp.read(path) връща [] за липсващ файл — не е грешка.
  section.getboolean() разбира yes/no/true/false/on/off/1/0; невалидно → ValueError.
  "*.tmp, *.log" → [p.strip() for p in raw.split(",") if p.strip()].
- Средата: подай dict (по подразбиране os.environ) — тестовете подават свой.
  Празен низ в средата не е „зададено“.
- Счупен файл (json.JSONDecodeError, нечислова стойност, ValueError) → ясно
  съобщение на stderr и изход с код 2, без traceback: main(argv) -> int и
  `raise SystemExit(main())`. argparse сам излиза с 2 при грешен аргумент.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from collections.abc import Mapping

DEFAULTS = {"host": None, "port": 8080, "tags": [], "verbose": False}
ENV_VARS = {"host": "APP_HOST", "port": "APP_PORT"}


class SettingsError(ValueError):
    """Счупен или невалиден файл с настройки — за съобщение и код 2."""


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def from_ini(path: str, section: str = "app") -> dict:
    cp = configparser.ConfigParser()
    try:
        if not cp.read(path, encoding="utf-8") or not cp.has_section(section):
            return {}
        sec = cp[section]
        out: dict = {}
        if sec.get("host"):
            out["host"] = sec["host"]                    # както е — без Path()
        if "port" in sec:
            out["port"] = sec.getint("port")
        if "tags" in sec:
            out["tags"] = _split(sec["tags"])
        if "verbose" in sec:
            out["verbose"] = sec.getboolean("verbose")
        return out
    except (configparser.Error, ValueError) as e:
        raise SettingsError(f"{path}: {e}") from None


def from_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: не е JSON ({e.msg}, ред {e.lineno})") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: очаквам обект {{...}}")
    port = data.get("port", DEFAULTS["port"])
    if isinstance(port, bool) or not isinstance(port, (int, float)):
        raise SettingsError(f"{path}: port трябва да е число")
    return data


def load_settings(argv: list[str], env: Mapping[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="app")
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--tag", dest="tags", action="append", default=None)   # заменя, не удължава
    ap.add_argument("--verbose", action="store_true", default=None)        # None = не е подаден
    args = ap.parse_args(argv)

    settings = dict(DEFAULTS, tags=list(DEFAULTS["tags"]))
    settings.update(from_json(args.config) if args.config.endswith(".json") else from_ini(args.config))
    for key, var in ENV_VARS.items():
        if env.get(var):
            settings[key] = int(env[var]) if key == "port" else env[var]
    for key in DEFAULTS:
        value = getattr(args, key)
        if value is not None:
            settings[key] = value
    return settings


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings(sys.argv[1:] if argv is None else argv)
    except SettingsError as e:
        print(f"Грешка в настройките: {e}", file=sys.stderr)
        return 2
    if not settings["host"]:
        print("Липсва host (--host, APP_HOST или config.ini)", file=sys.stderr)
        return 2
    print(settings)
    return 0


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        ini = Path(d, "c.ini")
        ini.write_text("[app]\nhost = /data/a\nport = 81\ntags = x, y\nverbose = yes\n", encoding="utf-8")
        s = load_settings(["--config", str(ini)], {})
        assert s == {"host": "/data/a", "port": 81, "tags": ["x", "y"], "verbose": True}, s
        s = load_settings(["--config", str(ini)], {"APP_HOST": "/env/h", "APP_PORT": ""})
        assert (s["host"], s["port"], s["verbose"]) == ("/env/h", 81, True)    # store_true не смачква yes
        s = load_settings(["--config", str(ini), "--host", "/arg/h", "--tag", "z"], {"APP_HOST": "/env/h"})
        assert (s["host"], s["tags"]) == ("/arg/h", ["z"])                      # append заменя файла
        s = load_settings(["--config", str(Path(d, "няма.ini"))], {})
        assert s == DEFAULTS                                                    # липсващ файл — подразбиране
        bad = Path(d, "c.json")
        bad.write_text("{port: 1", encoding="utf-8")
        assert main(["--config", str(bad)]) == 2                                # без traceback
        bad.write_text('{"host": "h", "port": "осем"}', encoding="utf-8")
        assert main(["--config", str(bad)]) == 2
        Path(d, "ok.ini").write_text("[app]\nverbose = може би\n", encoding="utf-8")
        assert main(["--config", str(Path(d, "ok.ini")), "--host", "h"]) == 2
    print("OK")
```
