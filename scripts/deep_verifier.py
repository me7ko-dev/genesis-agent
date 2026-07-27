#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_verifier.py — дълбока верификация на умения, които искат външни пакети.

Пускаш го при нужда. Той:
  1. Смята кои пакети иска всяко needs_deps умение.
  2. Инсталира САМО познати легитимни пакети (allowlist) в ИЗОЛИРАНА директория
     (.verify_libs, чрез pip --target) — никога halюцинирани/непознати имена
     (защита от typo-squat/зловреден код).
  3. Пуска умението с PYTHONPATH сочещ само .verify_libs, но първо през риск-
     гейта на sandbox-а (умение, което прави rm -rf/os.system, се блокира дори
     да импортира numpy).
  4. Обновява verified статуса в skills.json.

БЕЗОПАСНОСТ:
  - Пакетите се теглят от PyPI само ако са в ALLOWLIST. Умение с непознат импорт
    се маркира 'unknown_pkg' и се ПРОПУСКА (не се инсталира нищо).
  - Всичко влиза в .verify_libs/ (изолирана), не в системния Python.
  - Тежкият стек (torch/tensorflow/...) е зад отделен флаг --heavy.

Употреба:
    python3 scripts/deep_verifier.py                  # dry-run: отчет по нива
    python3 scripts/deep_verifier.py --install-light   # инсталира лекия стек
    python3 scripts/deep_verifier.py --run             # верифицира каквото е налично
    python3 scripts/deep_verifier.py --install-light --run --apply   # пълен лек цикъл
    python3 scripts/deep_verifier.py --heavy --install --run --apply # + тежкия стек (много GB!)
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from genesis_agent import sandbox  # noqa: E402

SKILLS_DIR = ROOT / "skills"
SKILLS_JSON = SKILLS_DIR / "skills.json"
# Изолирана директория за пакети (pip install --target). Не пипа системния/
# потребителския Python — умения се пускат с PYTHONPATH сочещ само тук.
LIBS = ROOT / ".verify_libs"
_CODE_RE = re.compile(r"```python\n(.*?)\n```", re.DOTALL)

# import-име → PyPI-име. САМО познати, легитимни пакети. Каквото не е тук —
# не се инсталира (третира се като непознато/рисково).
ALLOWLIST_LIGHT: dict[str, str] = {
    "numpy": "numpy", "pandas": "pandas", "sklearn": "scikit-learn",
    "scipy": "scipy", "matplotlib": "matplotlib", "requests": "requests",
    "bs4": "beautifulsoup4", "PIL": "Pillow", "yaml": "PyYAML",
    "cv2": "opencv-python-headless", "networkx": "networkx", "nltk": "nltk",
    "sqlalchemy": "SQLAlchemy", "pydantic": "pydantic", "aiohttp": "aiohttp",
    "flask": "Flask", "fastapi": "fastapi", "psutil": "psutil",
    "joblib": "joblib", "tqdm": "tqdm", "seaborn": "seaborn",
    "plotly": "plotly", "statsmodels": "statsmodels", "dateutil": "python-dateutil",
    "pytz": "pytz", "click": "click", "rich": "rich", "httpx": "httpx",
    "cryptography": "cryptography", "jwt": "PyJWT", "bcrypt": "bcrypt",
    "regex": "regex", "lxml": "lxml", "openpyxl": "openpyxl",
    # ── Разширение 2026-07-24: реални PyPI библиотеки от unknown_pkg,
    # САМОСТОЯТЕЛНИ (не искат външен сървър/API ключ/хардуер/system binary,
    # затова реално могат да минат self-test честно, не само да се инсталират).
    "sympy": "sympy", "jsonschema": "jsonschema", "urllib3": "urllib3",
    "shapely": "shapely", "textblob": "textblob", "pyvis": "pyvis",
    "geopy": "geopy", "deap": "deap", "rdflib": "rdflib",
    "watchdog": "watchdog", "PyPDF2": "PyPDF2", "hyperopt": "hyperopt",
    "IPython": "ipython", "SALib": "SALib", "yfinance": "yfinance",
    "music21": "music21", "skimage": "scikit-image", "pyarrow": "pyarrow",
    "polars": "polars", "coverage": "coverage", "pytest": "pytest",
    "onnxruntime": "onnxruntime", "pmdarima": "pmdarima", "dask": "dask",
    "dash": "dash", "geopandas": "geopandas", "optuna": "optuna",
    "shap": "shap", "lime": "lime", "scrapy": "Scrapy",
    "prophet": "prophet", "folium": "folium", "numba": "numba",
    "selenium": "selenium", "aiosqlite": "aiosqlite", "graphviz": "graphviz",
    "chess": "python-chess", "blessed": "blessed", "nacl": "PyNaCl",
    "mss": "mss",
}
ALLOWLIST_HEAVY: dict[str, str] = {
    "torch": "torch", "torchvision": "torchvision", "transformers": "transformers",
    "tensorflow": "tensorflow", "keras": "keras", "spacy": "spacy",
    "gymnasium": "gymnasium", "xgboost": "xgboost", "lightgbm": "lightgbm",
    "torch_geometric": "torch-geometric", "torchaudio": "torchaudio",
    "stable_baselines3": "stable-baselines3", "gym": "gym", "qiskit": "qiskit",
}

# Умишлено НЕ в allowlist-а — реални библиотеки, но self-test честно НЕ може
# да мине без външен сървър/API ключ/креденшъли/binary извън pip:
# boto3/botocore (AWS креди), psycopg2/redis/aioredis/kafka/neo4j/py2neo
# (нужен работещ сървър), docker/kubernetes (нужен daemon/клъстер),
# openai/tweepy/googleapiclient/google (API ключове), webdriver_manager/
# playwright/pytesseract (нужен browser driver/OCR binary извън pip), sounddevice/
# pyttsx3/speech_recognition (нужен аудио хардуер), scapy (нужни root права),
# pyspark/apache_beam/airflow/prefect (тежки orchestration платформи, нисък ROI).
# (selenium Е в allowlist-а — пакетът си инсталира честно; дали self-test-ът му
# минава зависи дали кодът реално стартира browser driver — честен deep_failed
# е приемлив изход, не проблем на allowlist-а.)

_STD = set(sys.stdlib_module_names)


def extract_code(text: str) -> str | None:
    m = _CODE_RE.search(text)
    return m.group(1).strip() if m else None


def external_imports(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    mods: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module.split(".")[0])
    return {m for m in mods if m not in _STD}


def classify(imports: set[str], heavy: bool) -> tuple[str, set[str]]:
    """Връща (категория, pypi_пакети_за_инсталиране)."""
    allow = dict(ALLOWLIST_LIGHT)
    if heavy:
        allow.update(ALLOWLIST_HEAVY)
    unknown = imports - set(ALLOWLIST_LIGHT) - set(ALLOWLIST_HEAVY)
    if unknown:
        return "unknown_pkg", set()
    needs_heavy = imports & set(ALLOWLIST_HEAVY)
    if needs_heavy and not heavy:
        return "needs_heavy", set()
    pkgs = {allow[m] for m in imports if m in allow}
    return ("installable_heavy" if needs_heavy else "installable_light"), pkgs


def pip_install(pkgs: set[str]) -> None:
    if not pkgs:
        return
    LIBS.mkdir(exist_ok=True)
    print(f"[pip] Инсталирам {len(pkgs)} пакета в изолирания {LIBS.name}/ ...")
    # ПООТДЕЛНО, не наведнъж — иначе ЕДИН несъвместим пакет (напр. tensorflow
    # без wheel за нова Python версия) проваля ЦЕЛИЯ batch и не се инсталира нищо.
    failed: list[str] = []
    for pkg in sorted(pkgs):
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--target", str(LIBS), pkg],
            check=False, timeout=1800,
        )
        if r.returncode != 0:
            failed.append(pkg)
    if failed:
        print(f"[pip] ⚠ несъвместими/неуспешни ({len(failed)}): {', '.join(failed)}")


def installed_modules() -> set[str]:
    """Топ-нивелни модули, налични в изолираната .verify_libs директория."""
    if not LIBS.exists():
        return set()
    mods: set[str] = set()
    for p in LIBS.iterdir():
        name = p.name
        if name.endswith((".dist-info", ".egg-info", "__pycache__")):
            continue
        if p.is_dir():
            mods.add(name.lower())
        elif name.endswith(".py"):
            mods.add(name[:-3].lower())
    return mods


def run_skill_isolated(code: str, timeout: int = 30) -> tuple[bool, str]:
    """Пуска умението с PYTHONPATH=.verify_libs, но първо през риск-гейта."""
    verdict = sandbox.assess_code(code)
    if verdict.level == sandbox.RiskLevel.BLOCKED:
        return False, "blocked: " + "; ".join(verdict.reasons)
    root = ROOT / ".sandbox_run"
    root.mkdir(exist_ok=True)
    script = root / "deepverify_tmp.py"
    script.write_text(code, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(LIBS)}
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(root),
                              capture_output=True, text=True, timeout=timeout, env=env)
        ok = proc.returncode == 0
        return ok, (proc.stdout if ok else proc.stderr)[:300]
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout}s"
    finally:
        script.unlink(missing_ok=True)


def main() -> None:
    heavy = "--heavy" in sys.argv
    do_install = "--install" in sys.argv or "--install-light" in sys.argv
    do_run = "--run" in sys.argv
    apply = "--apply" in sys.argv

    idx = json.loads(SKILLS_JSON.read_text(encoding="utf-8"))
    targets = [s for s in idx["skills"] if s.get("verification", {}).get("method") == "needs_deps"]

    buckets: Counter = Counter()
    to_install: set[str] = set()
    # (entry, category, pypi_pkgs, import_names, code) — двете различни имена се
    # пазят отделно: pypi_pkgs за инсталиране, import_names за проверка какво е
    # реално наличено (PyYAML се инсталира, но се импортира като yaml — различни имена!).
    plan: list[tuple[dict, str, set[str], set[str], str]] = []

    for s in targets:
        md = ROOT / s["file_path"]
        code = extract_code(md.read_text(encoding="utf-8", errors="replace")) if md.exists() else None
        if not code:
            buckets["no_code"] += 1
            continue
        imports = external_imports(code)
        cat, pkgs = classify(imports, heavy)
        buckets[cat] += 1
        allow = dict(ALLOWLIST_LIGHT)
        if heavy:
            allow.update(ALLOWLIST_HEAVY)
        import_names = {m for m in imports if m in allow}
        plan.append((s, cat, pkgs, import_names, code))
        if cat.startswith("installable"):
            to_install |= pkgs

    print(f"=== DEEP VERIFIER — {len(targets)} умения с нужда от пакети ===")
    for cat, n in buckets.most_common():
        print(f"  {cat:18}: {n}")
    installable = buckets["installable_light"] + buckets["installable_heavy"]
    print(f"\n  → покриваеми с allowlist{' (+heavy)' if heavy else ' (light)'}: {installable}")
    print(f"  → уникални пакети за инсталиране: {len(to_install)}")
    print(f"  → непокриваеми (непознат/halюциниран импорт): {buckets['unknown_pkg']}")
    if not heavy:
        print(f"  → искат тежък стек (--heavy за тях): {buckets['needs_heavy']}")

    if do_install:
        pip_install(to_install)

    if not do_run:
        print("\n(dry-run. Флагове: --install-light | --heavy --install | --run | --apply)")
        return

    have = installed_modules()
    verified_now = 0
    ran = 0
    for s, cat, pkgs, import_names, code in plan:
        if not cat.startswith("installable"):
            continue
        if not {m.lower() for m in import_names} <= have:
            continue  # пакетите не са инсталирани — пропускаме
        ran += 1
        ok, detail = run_skill_isolated(code, timeout=20)
        method = "deep_verified" if ok else "deep_failed"
        s["verified"] = ok
        s["verification"] = {"verified": ok, "method": method, "detail": detail[:300]}
        if ok:
            verified_now += 1
        if ran % 100 == 0:
            print(f"  ... пуснати {ran} (verified: {verified_now})")

    print(f"\n  Пуснати: {ran} | Нововерифицирани: {verified_now}")
    if apply:
        SKILLS_JSON.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  ✓ skills.json обновен.")
    else:
        print("  (без --apply промените не са записани.)")


if __name__ == "__main__":
    main()
