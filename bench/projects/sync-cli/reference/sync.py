"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
import argparse
import configparser
import fnmatch
import os
import shutil
import sys
from pathlib import Path


def load_settings(argv, env):
    ap = argparse.ArgumentParser(prog="sync.py")
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--source")
    ap.add_argument("--target")
    ap.add_argument("--exclude", action="append")
    ap.add_argument("--dry-run", action="store_true", default=None)
    args = ap.parse_args(argv)

    cp = configparser.ConfigParser()
    cp.read(args.config, encoding="utf-8")
    sec = cp["sync"] if cp.has_section("sync") else {}
    exclude = [x.strip() for x in sec.get("exclude", "").split(",") if x.strip()]
    dry = cp.getboolean("sync", "dry_run", fallback=False) if cp.has_section("sync") else False
    return {
        "source": args.source or env.get("SYNC_SOURCE") or sec.get("source"),
        "target": args.target or env.get("SYNC_TARGET") or sec.get("target"),
        "exclude": args.exclude if args.exclude is not None else exclude,
        "dry_run": args.dry_run if args.dry_run is not None else dry,
    }


def sync(settings):
    src, dst = Path(settings["source"]), Path(settings["target"])
    done = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        if any(fnmatch.fnmatch(f.name, pat) for pat in settings["exclude"]):
            continue
        rel = f.relative_to(src)
        out = dst / rel
        if out.exists() and out.stat().st_mtime >= f.stat().st_mtime:
            continue
        done.append(rel.as_posix())
        if not settings["dry_run"]:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
    return sorted(done)


def main(argv=None):
    s = load_settings(sys.argv[1:] if argv is None else argv, dict(os.environ))
    if not s["source"] or not s["target"]:
        print("Липсва source или target (config.ini, SYNC_SOURCE/SYNC_TARGET или --source/--target).", file=sys.stderr)
        return 2
    if not Path(s["source"]).is_dir():
        print(f"Няма такава папка: {s['source']}", file=sys.stderr)
        return 2
    for rel in sync(s):
        print(("(проба) " if s["dry_run"] else "") + rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
