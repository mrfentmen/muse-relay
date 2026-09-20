#!/usr/bin/env python3
"""Send a message to the muse-relay bus.

Usage: send.py [--room NAME] "message text"
Posts the plain string "<nick>: <message>" to the main bus, or to a room
with --room. Rooms are separate lists; the default room is the legacy
'muse-bus' list, so existing setups keep working unchanged.
Idempotent: skips the send if the identical message is already at the tail.
Never prints the token.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, bus_get, bus_key, bus_push, clean_room)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to post to (default: main bus)")
    ap.add_argument("message", help="message text")
    args = ap.parse_args()
    if not args.message.strip():
        print("message text is empty", file=sys.stderr)
        return 2
    try:
        room = clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    body = f"{NICK}: {args.message.strip()}"
    # Idempotency: never post the same body twice in a row.
    try:
        tail = bus_get(-1, -1, key=key)
        if tail and tail[-1] == body:
            print("DUPLICATE_SKIPPED: identical message already at tail")
            return 0
    except Exception as e:
        print(f"WARNING: tail check failed ({e}); sending anyway",
              file=sys.stderr)
    try:
        print(bus_push(body, key=key)[:200])
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
