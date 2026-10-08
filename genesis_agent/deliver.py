"""
genesis_agent.deliver — the project, handed over: a zip plus a report.

NEXT_STEPS Д.13: what the client receives is not the chat but the project —
the files, how to run them, what was assumed, and whether its tests pass right
now. Every one of those answers is taken from the project itself at the moment
of export (the README, the files, a real test run, the work memory of that
folder), never from what the model said it did: a report written from the
conversation would repeat whatever the conversation claimed.

    genesis export [PATH] [-o OUT.zip] [--no-tests]
    /export                       (in the chat — the current workspace)

Keys stay behind. A file the sandbox treats as a secret (`.env`, `*.pem`,
`id_rsa`, ...) is left out of the zip and the report says it was, by name, so
the recipient knows to supply their own.
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPORT_NAME = "GENESIS_REPORT.md"
# A project above this is not a deliverable; it is a disk image with code in it.
_MAX_INPUT_MB = 200

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".venv", "venv", "env", "dist", "build", "target",
    ".next", ".tox", ".gradle", ".verify_libs", ".idea", ".vscode",
}
_SKIP_SUFFIXES = {".pyc", ".pyo"}
_SKIP_NAMES = {".DS_Store", "Thumbs.db"}

# Тайни, които образецът на sandbox-а не познава (той пази команди, не
# предаване). Одит 2026-10-07: `.envrc`, `.streamlit/secrets.toml`,
# `certs/server.key`, `token.json`, `client_secret_*.json`, `master.key`,
# `service-account.json`, `.htpasswd` влизаха в архива.
_EXPORT_SECRETS = re.compile(
    r"(^|/)(\.envrc|\.htpasswd|token\.json|master\.key|credentials\.json|\.git-credentials)$"
    r"|\.(key|jks|keystore|kdbx|ppk|p8|ovpn)$"
    r"|(^|/)secrets?\.[a-z]+$|(^|/)client_secret[^/]*\.json$|service[-_]?account[^/]*\.json$",
    re.IGNORECASE)
# И по съдържание — частен ключ в иначе невинно име (`config.json`, `deploy.txt`).
_SECRET_CONTENT = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|\"private_key\"\s*:\s*\"-----BEGIN")
_SCAN_BYTES = 256 * 1024


def _secret_file(p: Path, rel: str) -> bool:
    from genesis_agent import sandbox
    if sandbox.sensitive_path_reason(rel) or sandbox.sensitive_path_reason(p.name):
        return True
    if _EXPORT_SECRETS.search(rel):
        return True
    try:
        with p.open("rb") as f:
            return bool(_SECRET_CONTENT.search(f.read(_SCAN_BYTES)))
    except OSError:
        return False


_RUN_HEADINGS = ("how to run", "running", "usage", "quick start", "quickstart",
                 "getting started", "install", "как се пуска", "пускане",
                 "стартиране", "употреба", "инсталация", "как да")
_ASSUME_HEADINGS = ("assumption", "допускания", "допускане", "предположения")


@dataclass
class Delivery:
    zip_path: Path
    report: str
    files: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    tests_passed: bool | None = None


# ── Files ────────────────────────────────────────────────────────────────────

def project_files(root: Path, *, exclude: Path | None = None) -> tuple[list[Path], list[str]]:
    """(files to ship, secret files withheld) — both relative-sortable."""
    keep: list[Path] = []
    withheld: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS and not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name in _SKIP_NAMES or p.suffix in _SKIP_SUFFIXES or p.is_symlink():
                continue
            if exclude is not None and p.resolve() == exclude:
                continue
            rel = p.relative_to(root).as_posix()
            if name == REPORT_NAME and p.parent == root:
                continue  # regenerated below; an old one would be stale
            if _secret_file(p, rel):
                withheld.append(rel)
                continue
            keep.append(p)
    return keep, withheld


# ── README ───────────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def readme_sections(root: Path) -> dict[str, str]:
    """Heading (lowercased) → body, for the project's README."""
    for name in ("README.md", "README.MD", "Readme.md", "readme.md", "README.txt", "README"):
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return {}
            break
    else:
        return {}
    lines = text.splitlines()
    heads = [(i, len(m.group(1)), m.group(2).strip().lower())
             for i, m in ((i, _HEADING.match(ln)) for i, ln in enumerate(lines)) if m]
    out: dict[str, str] = {}
    for k, (i, level, title) in enumerate(heads):
        # Тялото стига до следващото заглавие на същото или по-високо ниво —
        # подзаглавията („### Windows“ под „## Как се пуска“) са част от него.
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        out.setdefault(title, "\n".join(lines[i + 1:end]).strip())
    return out


def _section(sections: dict[str, str], keys: tuple[str, ...]) -> str:
    for heading, body in sections.items():
        if body and any(k in heading for k in keys):
            return body
    return ""


# ── How to run, when the README does not say ─────────────────────────────────

def _guess_run(root: Path, files: list[str]) -> list[str]:
    steps: list[str] = []
    names = set(files)
    if "requirements.txt" in names:
        steps.append("pip install -r requirements.txt")
    elif "pyproject.toml" in names:
        steps.append("pip install -e .")
    if "package.json" in names:
        steps.append("npm install")
        try:
            import json
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        for s in ("start", "dev"):
            if s in scripts:
                steps.append(f"npm run {s}" if s != "start" else "npm start")
                break
    for entry in ("main.py", "app.py", "cli.py", "manage.py", "server.py"):
        if entry in names:
            steps.append(f"python {entry}" + (" runserver" if entry == "manage.py" else ""))
            break
    else:
        top = [f for f in files if f.count("/") == 1]
        mains = sorted(f.split("/")[0] for f in top if f.endswith("/__main__.py"))
        clis = sorted(f[:-3].replace("/", ".") for f in top if f.endswith("/cli.py")
                      and "__main__" in _read(root / f))
        if mains:
            steps.append(f"python -m {mains[0]}")
        elif clis:
            steps.append(f"python -m {clis[0]}")
    if "index.html" in names and not steps:
        steps.append("отвори index.html в браузър")
    return steps


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _dependencies(root: Path, files: list[str]) -> list[str]:
    names = set(files)
    if "requirements.txt" in names:
        try:
            lines = (root / "requirements.txt").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", "-"))]
    if "pyproject.toml" in names:
        try:
            import tomllib  # type: ignore[import-not-found]  # 3.11+
        except ImportError:
            return []
        try:
            data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [str(d) for d in data.get("project", {}).get("dependencies", [])]
    if "package.json" in names:
        try:
            import json
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
        return [f"{k} {v}" for k, v in deps.items()]
    return []


# ── Tests ────────────────────────────────────────────────────────────────────

_SUMMARY = re.compile(r"\b\d+ (passed|failed|error|errors)\b.*")


def _portable(command: str) -> str:
    """`/home/x/.venv/bin/python -m pytest` → `python -m pytest`: пътят до
    интерпретатора е на тази машина, получателят има свой."""
    return re.sub(r'^("[^"]*[/\\]python[\d.]*(\.exe)?"|[^\s"]*[/\\]python[\d.]*(\.exe)?)(?=\s)',
                  "python", command, flags=re.IGNORECASE)


def _test_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        m = _SUMMARY.search(line)
        if m:
            return line.strip(" =")
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return lines[-1].strip()[:200] if lines else ""


# ── Work memory of this folder ───────────────────────────────────────────────

def _memory(root: Path) -> tuple[list[dict], list[dict]]:
    """(decisions, open threads) recorded for exactly this folder."""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception:
        return [], []
    previous = wm._workspace
    try:
        wm.set_workspace(root)
        decisions = wm.list_decisions(15)
        threads = wm.list_threads("open", 15) + wm.list_threads("blocked", 15)
    except Exception:
        return [], []
    finally:
        wm._workspace = previous
    return decisions, threads


# ── The report ───────────────────────────────────────────────────────────────

def _size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    n /= 1024
    return f"{n:.1f} KB" if n < 1024 else f"{n / 1024:.1f} MB"


def build_report(root: Path, files: list[Path], withheld: list[str], *,
                 run_tests: bool = True) -> tuple[str, bool | None]:
    """Markdown report and whether the tests passed (None: not run / none)."""
    from genesis_agent.repo_map import detect_project

    rel = [f.relative_to(root).as_posix() for f in files]
    info = detect_project(root)
    sections = readme_sections(root)
    lines = [f"# {root.name} — отчет за предаване",
             "",
             (f"Изготвен от Genesis Agent на {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
              "Всичко по-долу е взето от самия проект в този момент — файловете, README-то, "
              "истинско пускане на тестовете — не от разговора."),
             ""]

    lines += ["## Какво има", ""]
    total = 0
    code_lines = 0
    for f, r in zip(files, rel):
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        total += size
        if f.suffix in (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".go", ".rs", ".java", ".rb", ".php"):
            try:
                code_lines += f.read_text(encoding="utf-8", errors="replace").count("\n")
            except OSError:
                pass
    lines.append(f"{len(rel)} файла, {_size(total)}, ~{code_lines} реда код. "
                 f"Език: {info.language}.")
    lines.append("")
    shown = rel[:60]
    lines += [f"- `{r}`" for r in shown]
    if len(rel) > len(shown):
        lines.append(f"- … и още {len(rel) - len(shown)}")
    lines.append("")

    lines += ["## Как се пуска", ""]
    how = _section(sections, _RUN_HEADINGS)
    if how:
        lines += ["(от README-то на проекта)", "", how, ""]
    else:
        guess = _guess_run(root, rel)
        if guess:
            lines += ["README-то не го казва; по файловете на проекта:", "", "```"]
            lines += guess + ["```", ""]
        else:
            lines += ["Нито README-то, нито файловете казват как — **липсва**.", ""]
    deps = _dependencies(root, rel)
    if deps:
        lines += ["Зависимости: " + ", ".join(f"`{d}`" for d in deps[:30]), ""]
    try:
        from genesis_agent.py_deps import undeclared
        missing = undeclared(root)
    except Exception:
        missing = []
    if missing:
        lines += [("⚠️ Кодът внася пакети, които не са записани в requirements.txt / "
                   "pyproject.toml — на друга машина няма да тръгне без тях:"), ""]
        lines += [f"- `{pip}` (`import {mod}` в {', '.join(files[:3])})" for mod, pip, files in missing]
        lines.append("")

    lines += ["## Тестове", ""]
    passed: bool | None = None
    if not info.test_command:
        lines += ["Проектът няма тестове, които да се пуснат.", ""]
    elif not run_tests:
        lines += [f"Не са пускани при този експорт. Команда: `{_portable(info.test_command)}`", ""]
    else:
        from genesis_agent.repo_agent import run_tests as _run
        res = _run(root, info.test_command)
        if not res.ran:
            lines += [f"Не можаха да се пуснат: {res.output[:300]}", ""]
        else:
            passed = res.passed
            verdict = "✅ минават" if res.passed else "❌ падат"
            lines += [f"{verdict} — `{_test_summary(res.output)}`",
                      "", f"Команда: `{_portable(info.test_command)}`", ""]
            if not res.passed:
                lines += ["```", res.output[-1500:], "```", ""]

    assumptions = _section(sections, _ASSUME_HEADINGS)
    decisions, threads = _memory(root)
    lines += ["## Допускания и решения", ""]
    if assumptions:
        lines += ["(от README-то)", "", assumptions, ""]
    if decisions:
        for d in decisions:
            why = f" — {d['why']}" if d.get("why") else ""
            lines.append(f"- {d['what']}{why}")
        lines.append("")
    if not assumptions and not decisions:
        lines += ["Не са записани нито в README-то, нито в паметта на работата.", ""]

    if threads:
        lines += ["## Недовършено", ""]
        for t in threads:
            nxt = f" → следва: {t['next_step']}" if t.get("next_step") else ""
            lines.append(f"- [{t['status']}] {t['title']}{nxt}")
        lines.append("")

    if withheld:
        lines += ["## Нарочно извън архива", "",
                  "Файлове с ключове или тайни — получателят слага своите:", ""]
        lines += [f"- `{w}`" for w in withheld]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", passed


# ── Export ───────────────────────────────────────────────────────────────────

def export(project: str | Path, out: str | Path | None = None, *,
           run_tests: bool = True) -> Delivery:
    """Zip `project` with GENESIS_REPORT.md at its root. Raises ValueError
    with a human sentence when there is nothing sensible to export."""
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Няма такава папка: {root}")
    zip_path = (Path(out).expanduser() if out
                else root.parent / f"{root.name}-{datetime.now().strftime('%Y%m%d-%H%M')}.zip").resolve()
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_suffix(zip_path.suffix + ".zip")
    files, withheld = project_files(root, exclude=zip_path)
    if not files:
        raise ValueError(f"В {root} няма файлове за предаване.")
    total = sum(f.stat().st_size for f in files if f.exists())
    if total > _MAX_INPUT_MB * 1024 * 1024:
        raise ValueError(f"Проектът е {total // (1024 * 1024)} MB (таван {_MAX_INPUT_MB} MB) — "
                         "това не е предаване, а образ на диска. Изчисти данните/артефактите.")

    report, passed = build_report(root, files, withheld, run_tests=run_tests)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{root.name}/{REPORT_NAME}", report)
        for f in files:
            z.write(f, f"{root.name}/{f.relative_to(root).as_posix()}")
    tmp.replace(zip_path)
    return Delivery(zip_path, report, [f.relative_to(root).as_posix() for f in files],
                    withheld, passed)


def summary(d: Delivery) -> str:
    """The few lines the terminal prints."""
    tests = {True: "тестовете минават ✅", False: "тестовете ПАДАТ ❌",
             None: "без пуснати тестове"}[d.tests_passed]
    out = [f"📦 {d.zip_path}", f"   {len(d.files)} файла + {REPORT_NAME}, {tests}"]
    if d.withheld:
        out.append(f"   🔒 извън архива (тайни): {', '.join(d.withheld[:5])}"
                   + (" …" if len(d.withheld) > 5 else ""))
    return "\n".join(out)


_USAGE = """Употреба:
  genesis export [път-до-проекта] [-o изход.zip] [--no-tests]

Zip на проекта + GENESIS_REPORT.md: файловете, как се пуска, зависимостите,
резултатът от тестовете (пускат се сега), допусканията и недовършеното.
Ключове и тайни (.env, *.pem, …) остават извън архива.
По подразбиране: текущата папка → <папка>-<дата>.zip до нея.
Изходен код 1, ако тестовете падат (архивът пак се прави)."""


def main(args: list[str]) -> int:
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    project, out, run = ".", None, True
    i = 0
    positional: list[str] = []
    while i < len(args):
        a = args[i]
        if a in ("-o", "--out") and i + 1 < len(args):
            i += 1
            out = args[i]
        elif a == "--no-tests":
            run = False
        elif a.startswith("-"):
            print(f"Непозната опция: {a}\n\n{_USAGE}")
            return 2
        else:
            positional.append(a)
        i += 1
    if len(positional) > 1:
        print(_USAGE)
        return 2
    if positional:
        project = positional[0]
    try:
        d = export(project, out, run_tests=run)
    except (ValueError, OSError) as e:
        print(f"❌ {e}")
        return 1
    print(summary(d))
    return 0 if d.tests_passed is not False else 1
