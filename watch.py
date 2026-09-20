#!/usr/bin/env python3
"""Near-live watch for the muse-relay bus.

Usage: watch.py [--interval SECONDS] [--room NAME]

Polls continuously and prints new messages from other nicks as they
arrive — near-instant delivery with no server to run. Default interval
is 10 seconds. Ctrl-C stops it. Read offset is tracked per room, shared
with poll.py.

Why not long-polling? Upstash's REST API has no clean blocking-pop
story, so a tight poll loop is the honest no-servers approach. For
cron-style checks, point the cron at poll.py every 15-30 seconds instead
of every few minutes.
Never prints the token.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, TOKEN_FILE, bus_get, bus_key, clean_room, mark_seen, presence_beat,
    seen_path)


def read_seen(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def write_seen(path, n):
    try:
        with open(path, "w") as f:
            f.write(str(n))
    except OSError as e:
        print(f"WARNING: seen write failed ({e})", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=10,
                    help="seconds between polls (default: 10)")
    ap.add_argument("--room", default=None,
                    help="room to watch (default: main bus)")
    args = ap.parse_args(argv)
    if args.interval <= 0:
        print("interval must be positive", file=sys.stderr)
        return 2
    try:
        room = clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    seen_file = seen_path(room)
    seen = read_seen(seen_file)
    label = f"room '{room}'" if room else "main bus"
    print(f"watching {label} as {NICK} every {args.interval:g}s "
          f"(Ctrl-C to stop)", flush=True)
    try:
        while True:
            try:
                msgs = bus_get(seen, -1, key=key)
            except FileNotFoundError:
                print("RELAY_ERROR: token file missing: " + TOKEN_FILE,
                      file=sys.stderr)
                return 2
            except Exception as e:
                print(f"WARNING: poll failed ({e}); retrying",
                      file=sys.stderr)
                time.sleep(args.interval)
                continue
            try:
                presence_beat(key)
            except Exception as e:
                print(f"WARNING: presence heartbeat failed ({e})",
                      file=sys.stderr)
            for m in msgs:
                if isinstance(m, str) and not m.startswith(NICK + ":"):
                    print(m, flush=True)
            seen += len(msgs)
            write_seen(seen_file, seen)
            try:
                mark_seen(key, NICK, seen)  # read receipt; never breaks watch
            except Exception as e:
                print(f"WARNING: read-receipt write failed ({e})",
                      file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
