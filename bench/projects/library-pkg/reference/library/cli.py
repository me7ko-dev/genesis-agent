import argparse
import sys

from .service import Library


def main(argv=None):
    ap = argparse.ArgumentParser(prog="library")
    ap.add_argument("--db", default="library.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("books")
    args = ap.parse_args(argv)
    lib = Library(args.db)
    if args.cmd == "books":
        for b, free in lib.books():
            print(f"{b.isbn}  {b.title} — {b.author}  {free}/{b.copies}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
