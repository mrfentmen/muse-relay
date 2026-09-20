#!/usr/bin/env python3
"""Quiet watch for a nick joining the muse-relay bus.

Usage: watch_nick.py <nick>
Checks the bus for any message from <nick>.
Fires exactly once: on first detection it prints <NICK>_JOINED and records
a flag file so later runs stay quiet.
Otherwise prints <NICK>_NOT_JOINED (stay quiet) or WATCH_ERROR on failure.
Never prints the token.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import bus_get  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: watch_nick.py <nick>", file=sys.stderr)
        return 2
    watch = sys.argv[1].strip().lower()
    flag = os.path.join(HERE, f".{watch}_reported")
    if os.path.exists(flag):
        return 0  # already reported once; stay quiet forever
    try:
        msgs = bus_get(-200, -1)
    except Exception as e:
        print(f"WATCH_ERROR: {e}", file=sys.stderr)
        return 2
    joined = any(
        m.split(":", 1)[0].strip().lower() == watch
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
