import argparse
import sys
from datetime import date

from .errors import InsufficientStock, NotFound
from .quantities import format_qty
from .service import Inventory


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inventory")
    ap.add_argument("--db", default="inventory.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("sku")
    a.add_argument("name")
    a.add_argument("--unit", default="бр")
    a.add_argument("--min", default="0")
    m = sub.add_parser("move")
    m.add_argument("sku")
    m.add_argument("kind", choices=["in", "out"])
    m.add_argument("qty")
    m.add_argument("--date", default=None)
    sub.add_parser("report")
    i = sub.add_parser("import")
    i.add_argument("file")
    args = ap.parse_args(argv)
    try:
        inv = Inventory(args.db)
        if args.cmd == "add":
            inv.add_item(args.sku, args.name, unit=args.unit, min_qty=args.min)
        elif args.cmd == "move":
            on = date.fromisoformat(args.date) if args.date else None
            print(format_qty(inv.move(args.sku, args.kind, args.qty, on=on)))
        elif args.cmd == "report":
            for sku, name, qty, min_qty in inv.low_stock():
                print(f"{sku}\t{name}\t{format_qty(qty)}\t{format_qty(min_qty)}")
        elif args.cmd == "import":
            c = inv.import_csv(args.file)
            print(f"добавени: {c['added']}, обновени: {c['updated']}, пропуснати: {c['skipped']}")
    except (NotFound, InsufficientStock, ValueError, OSError) as e:
        print(f"Грешка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
