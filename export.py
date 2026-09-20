#!/usr/bin/env python3
"""Export a room's message history to markdown or JSON.

Usage: export.py --room R --format md|json [--out FILE]

Reads the room's LOCAL message store (the durable, timestamped record
— see store.py) and streams it to stdout or FILE. The store is read
line by line and output is written incrementally, so huge rooms never
get slurped into memory.

- markdown: "## <nick> (<YYYY-MM-DD HH:MM:SS>)" followed by the text;
  edited messages keep an "(edited)" marker.
- json: one object per line: {"nick", "text", "ts"} (plus
  "edited": true when the message was edited).

Honest scope: this exports your machine's local history (your sends +
what you polled/watched), not the live bus window. A room you've
never seen has no local history to export.

Purely local — never touches the network and never prints the token.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
from relay_common import clean_room  # noqa: E402


def _iter_store_entries(path):
    """Yield valid store entry dicts, streaming line by line."""
    try:
        f = open(path, encoding="utf-8")
    except FileNotFoundError:
        return
    with f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                print(f"WARNING: store {path}: skipping bad line {i + 1}",
                      file=sys.stderr)
                continue
            if isinstance(e, dict) and e.get("id"):
                yield e


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (ValueError, TypeError, OverflowError):
        return "unknown time"


def _write_md(out, room, entries):
    label = "main" if not room else f"#{room}"
    out.write(f"# Export of {label} — "
              f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    for e in entries:
        nick = e.get("nick", "?")
        text = e.get("text", "")
        edited = " (edited)" if e.get("edited") else ""
        out.write(f"## {nick} ({_fmt_time(e.get('ts'))}){edited}\n\n")
        out.write(f"{text}\n\n")


def _write_json(out, room, entries):
    for e in entries:
        obj = {"nick": e.get("nick", "?"),
               "text": e.get("text", ""),
               "ts": e.get("ts")}
        if e.get("edited"):
            obj["edited"] = True
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="export.py")
    ap.add_argument("--room", default=None,
                    help="room to export (default: main bus)")
    ap.add_argument("--format", default="md", choices=("md", "json"),
                    help="output format (default: md)")
    ap.add_argument("--out", default=None, metavar="FILE",
                    help="write to FILE instead of stdout")
    args = ap.parse_args(argv)
    try:
        room = clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    st = store.open_store(room)
    entries = _iter_store_entries(st.path)

    out = sys.stdout
    close_out = False
    if args.out:
        try:
            out = open(args.out, "w", encoding="utf-8")
            close_out = True
        except OSError as e:
            print(f"ERROR: can't write {args.out!r}: {e}", file=sys.stderr)
            return 2
    try:
        if args.format == "md":
            _write_md(out, room, entries)
        else:
            _write_json(out, room, entries)
    finally:
        if close_out:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
