#!/usr/bin/env python3
"""Near-live watch for the muse-relay bus.

Usage: watch.py [--interval SECONDS] [--room NAME] [--dm SECRET]...

Polls continuously and prints new messages from other nicks as they
arrive — near-instant delivery with no server to run. Default interval
is 10 seconds. Ctrl-C stops it. Read offset is tracked per room, shared
with poll.py. Each --dm SECRET also watches that dead-drop room. EDIT
lines from other nicks (edits.py) render with an (edited) marker — and
are applied to the local message store when it knows the message.
Incoming messages from other nicks are recorded in the room's local
message store, so they get ids usable with forward.py/saved.py.

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
    NICK, TOKEN_FILE, bus_get, bus_key, clean_room, dm_room, mark_seen,
    nick_conflict_holder, presence_beat, seen_path)
import edits  # noqa: E402
import store  # noqa: E402


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


def watch_once(targets):
    """One poll iteration over all targets.

    targets: list of [label, key, seen_file, seen]. Returns the number
    of new messages from other nicks printed. Updates seen in place.
    Raises FileNotFoundError when the token file is missing.
    """
    shown = 0
    for t in targets:
        label, key, seen_file, seen = t
        msgs = bus_get(seen, -1, key=key)
        try:
            presence_beat(key)
        except Exception as e:
            print(f"WARNING: presence heartbeat failed ({e})",
                  file=sys.stderr)
        try:
            st = store.open_store(store.room_for_key(key))
        except Exception:
            st = None
        for m in msgs:
            if isinstance(m, str) and not m.startswith(NICK + ":"):
                print(edits.render_incoming(m, store.room_for_key(key)),
                      flush=True)
                shown += 1
                if st is not None:
                    edits.record_incoming(st, m, NICK)  # ids for FWD/SAVE
        seen += len(msgs)
        t[3] = seen
        write_seen(seen_file, seen)
        try:
            mark_seen(key, NICK, seen)  # read receipt; never breaks watch
        except Exception as e:
            print(f"WARNING: read-receipt write failed ({e})",
                  file=sys.stderr)
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}'",
              file=sys.stderr)
    return shown


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=10,
                    help="seconds between polls (default: 10)")
    ap.add_argument("--room", default=None,
                    help="room to watch (default: main bus)")
    ap.add_argument("--dm", default=[], action="append", metavar="SECRET",
                    help="also watch the dead-drop room for SECRET "
                         "(repeatable)")
    args = ap.parse_args(argv)
    if args.interval <= 0:
        print("interval must be positive", file=sys.stderr)
        return 2
    try:
        rooms = [clean_room(args.room)]
        rooms += [dm_room(s) for s in args.dm]
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    targets = []
    for room in rooms:
        key = bus_key(room)
        label = f"room '{room}'" if room else "main bus"
        targets.append([label, key, seen_path(room), read_seen(seen_path(room))])
    labels = ", ".join(t[0] for t in targets)
    print(f"watching {labels} as {NICK} every {args.interval:g}s "
          f"(Ctrl-C to stop)", flush=True)
    try:
        while True:
            try:
                watch_once(targets)
            except FileNotFoundError:
                print("RELAY_ERROR: token file missing: " + TOKEN_FILE,
                      file=sys.stderr)
                return 2
            except Exception as e:
                print(f"WARNING: poll failed ({e}); retrying",
                      file=sys.stderr)
                time.sleep(args.interval)
                continue
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
