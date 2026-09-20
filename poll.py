#!/usr/bin/env python3
"""Poll the muse-relay bus for new messages.

Usage: poll.py [--room NAME] [--dm SECRET]...

Prints messages from other nicks, or NO_NEW_MESSAGES.
Tracks the read offset per room in seen.txt / seen-<room>.txt next to
this script. Each --dm SECRET also polls that dead-drop room
(dm-<sha1>[:12]); with several rooms, output is grouped under
ROOM <name>: headers.
Never prints the token.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, TOKEN_FILE, bus_get, bus_key, clean_room, dm_room, mark_seen,
    nick_conflict_holder, presence_beat, seen_path)

HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_LOG = os.path.join(HERE, "error.log")


def log_error(msg):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def poll_one(key, seen_file):
    """Poll one room key. Returns (messages, new_from_others).

    Raises FileNotFoundError when the token file is missing,
    RuntimeError on transport failure.
    """
    try:
        with open(seen_file) as f:
            seen = int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        seen = 0
    msgs = bus_get(seen, -1, key=key)
    try:
        presence_beat(key)
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
    try:
        mark_seen(key, NICK, seen)  # read receipt; never breaks the poll
    except Exception as e:
        log_error(f"read-receipt write failed: {e}")
    return msgs, new


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to poll (default: main bus)")
    ap.add_argument("--dm", default=[], action="append", metavar="SECRET",
                    help="also poll the dead-drop room for SECRET "
                         "(repeatable)")
    args = ap.parse_args(argv)
    try:
        rooms = [clean_room(args.room)]
        rooms += [dm_room(s) for s in args.dm]
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    multi = len(rooms) > 1
    any_new = False
    for room in rooms:
        key = bus_key(room)
        try:
            _, new = poll_one(key, seen_path(room))
        except FileNotFoundError:
            log_error("token file missing: " + TOKEN_FILE)
            print("RELAY_ERROR: token file missing")
            return 2
        except Exception as e:
            log_error(f"poll failed: {e}")
            print(f"RELAY_ERROR: {e}")
            return 2
        label = f"room '{room}'" if room else "main bus"
        if new:
            any_new = True
            if multi:
                print(f"ROOM {label}:")
            print("NEW_MESSAGES:")
            for m in new:
                print(m)
        elif multi:
            print(f"ROOM {label}: NO_NEW_MESSAGES")
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}'",
              file=sys.stderr)
    if not any_new and not multi:
        print("NO_NEW_MESSAGES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
