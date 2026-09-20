#!/usr/bin/env python3
"""Who's online on the muse-relay bus.

Usage: presence.py
Prints one nick per line for every nick with a live heartbeat (activity
in the last ~2 minutes), or NO_ONE_ONLINE. Heartbeats are sent
automatically by send.py, poll.py and watch.py whenever they contact
the bus, so this only ever shows nicks that are actually around.
Never prints the token.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import presence_list  # noqa: E402


def main():
    try:
        nicks = presence_list()
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    if nicks:
        for n in nicks:
            print(n)
    else:
        print("NO_ONE_ONLINE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
