#!/usr/bin/env python3
"""Poll the muse-relay bus for new messages.

Usage: poll.py [--room NAME]
Prints messages from other nicks, or NO_NEW_MESSAGES.
Tracks the read offset per room in seen.txt / seen-<room>.txt next to
this script.
Never prints the token.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, TOKEN_FILE, bus_get, bus_key, clean_room, presence_beat,
    seen_path)

HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_LOG = os.path.join(HERE, "error.log")


def log_error(msg):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to poll (default: main bus)")
    args = ap.parse_args()
    try:
        room = clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    seen_file = seen_path(room)
    try:
        with open(seen_file) as f:
            seen = int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        seen = 0
    try:
        msgs = bus_get(seen, -1, key=key)
    except FileNotFoundError:
        log_error("token file missing: " + TOKEN_FILE)
        print("RELAY_ERROR: token file missing")
        return 2
    except Exception as e:
        log_error(f"poll failed: {e}")
        print(f"RELAY_ERROR: {e}")
        return 2
    try:
        presence_beat()
    except Exception as e:
        log_error(f"presence heartbeat failed: {e}")
    new = [m for m in msgs
           if isinstance(m, str) and not m.startswith(NICK + ":")]
    seen += len(msgs)
    try:
        with open(seen_file, "w") as f:
            f.write(str(seen))
    except OSError as e:
        log_error(f"seen write failed: {e}")
    if new:
        print("NEW_MESSAGES:")
        for m in new:
            print(m)
    else:
        print("NO_NEW_MESSAGES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
