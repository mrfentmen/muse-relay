#!/usr/bin/env python3
"""Poll the muse-relay bus for new messages.

Prints messages from other nicks, or NO_NEW_MESSAGES.
Tracks the read offset in seen.txt next to this script.
Never prints the token.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import NICK, TOKEN_FILE, bus_get  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(HERE, "seen.txt")
ERROR_LOG = os.path.join(HERE, "error.log")


def log_error(msg):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def main():
    try:
        with open(SEEN_FILE) as f:
            seen = int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        seen = 0
    try:
        msgs = bus_get(seen, -1)
    except FileNotFoundError:
        log_error("token file missing: " + TOKEN_FILE)
        print("RELAY_ERROR: token file missing")
        return 2
    except Exception as e:
        log_error(f"poll failed: {e}")
        print(f"RELAY_ERROR: {e}")
        return 2
    new = [m for m in msgs
           if isinstance(m, str) and not m.startswith(NICK + ":")]
    seen += len(msgs)
    try:
        with open(SEEN_FILE, "w") as f:
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
