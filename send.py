#!/usr/bin/env python3
"""Send a message to the muse-relay bus.

Usage: send.py "message text"
Posts the plain string "<nick>: <message>".
Idempotent: skips the send if the identical message is already at the tail.
Never prints the token.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import NICK, bus_get, bus_push  # noqa: E402


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: send.py "message text"', file=sys.stderr)
        return 2
    body = f"{NICK}: {sys.argv[1].strip()}"
    # Idempotency: never post the same body twice in a row.
    try:
        tail = bus_get(-1, -1)
        if tail and tail[-1] == body:
            print("DUPLICATE_SKIPPED: identical message already at tail")
            return 0
    except Exception as e:
        print(f"WARNING: tail check failed ({e}); sending anyway",
              file=sys.stderr)
    try:
        print(bus_push(body)[:200])
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
