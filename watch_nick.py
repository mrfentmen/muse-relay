#!/usr/bin/env python3
"""Quiet watch for a nick joining the muse-relay bus.

Usage: watch_nick.py [--room NAME] <nick>
Checks the bus (or room) for any message from <nick>.
Fires exactly once: on first detection it prints <NICK>_JOINED and records
a flag file so later runs stay quiet.
Otherwise prints <NICK>_NOT_JOINED (stay quiet) or WATCH_ERROR on failure.
Never prints the token.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import bus_get, bus_key, clean_room, display_name  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to watch (default: main bus)")
    ap.add_argument("nick", help="nick to watch for")
    args = ap.parse_args()
    watch = args.nick.strip().lower()
    if not watch:
        print("nick is empty", file=sys.stderr)
        return 2
    try:
        room = clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    suffix = f"_{room}" if room else ""
    flag = os.path.join(HERE, f".{watch}_reported{suffix}")
    if os.path.exists(flag):
        return 0  # already reported once; stay quiet forever
    try:
        msgs = bus_get(-200, -1, key=key)
    except Exception as e:
        print(f"WATCH_ERROR: {e}", file=sys.stderr)
        return 2
    # match the canonical nick or its display name (rename-aware)
    try:
        targets = {watch, display_name(watch).lower()}
    except Exception:
        targets = {watch}
    joined = any(
        m.split(":", 1)[0].strip().lower() in targets
        for m in msgs if isinstance(m, str) and ":" in m)
    if not joined:
        print(f"{watch.upper()}_NOT_JOINED")
        return 0
    with open(flag, "w") as f:
        f.write("reported\n")
    print(f"{watch.upper()}_JOINED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
