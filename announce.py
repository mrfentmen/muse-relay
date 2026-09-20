#!/usr/bin/env python3
"""Announcement rooms: one nick's lines render, the rest are skipped.

Usage: announce.py set --room R      # this nick becomes the announcer
       announce.py clear --room R    # back to a normal room
       announce.py show --room R     # who (if anyone) is the announcer

The announcer's nick is stored at muse-bus:announce:<room-key>.
poll.py and watch.py honor it with --announce: only the announcer's
lines render; everyone else's are skipped client-side with a stderr
note.

HONEST LIMITS (this is not access control):
- The filter is client-side. Any client that doesn't pass --announce
  (or any hostile client) sees everything anyway.
- Nicks are unauthenticated on this bus. Setting announce mode proves
  only that you hold the nick claim right now, not that you're "staff".
- The mod gate below is a social convention enforced by this script,
  not by the server. Document it as such.

Never prints the token.
"""
import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, api_get, api_post, bus_key, clean_room,
    nick_conflict_holder)


def announce_key(key):
    """Redis key holding the announcer nick for a room bus key."""
    return f"muse-bus:announce:{key}"


def get_announcer(key):
    """Announcer nick for a room bus key, or None when not set."""
    try:
        data = json.loads(api_get(f"get/{announce_key(key)}"))
        return data.get("result") or None
    except Exception:
        return None


def announce_filter(key, messages):
    """Split messages into (kept, skipped) for an announce room.

    When no announcer is set, everything is kept and nothing is
    skipped — the caller should warn that --announce is a no-op.
    """
    announcer = get_announcer(key)
    if not announcer:
        return list(messages), 0
    prefix = announcer + ":"
    kept = [m for m in messages
            if isinstance(m, str) and m.startswith(prefix)]
    return kept, len(messages) - len(kept)


def _mods(key):
    """Nicks in the room's mods set (may be empty)."""
    try:
        data = json.loads(api_get(f"smembers/muse-bus:mods:{key}"))
        return set(data.get("result", []) or [])
    except Exception:
        return set()


def _gate(key):
    """Enforce the set/clear gate. Returns an error string or None."""
    holder = nick_conflict_holder()
    if holder:
        return (f"you don't hold the nick claim for '{NICK}' "
                f"(held by '{holder}') — can't change announce mode")
    mods = _mods(key)
    if mods and NICK not in mods:
        return (f"only room mods can change announce mode "
                f"(mods: {', '.join(sorted(mods))})")
    return None


CONVENTION_WARNING = (
    "WARNING: announce mode is a client-side convention, not access "
    "control — poll.py/watch.py --announce honor it, but any other "
    "client sees everything, and nicks are unauthenticated."
)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="announce.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (
            ("set", "become this room's announcer"),
            ("clear", "turn announce mode off for this room"),
            ("show", "show this room's announcer, if any")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--room", default=None,
                       help="room (default: main bus)")
    args = ap.parse_args(argv)
    try:
        room = clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    key = bus_key(room)
    akey = announce_key(key)

    if args.cmd == "show":
        ann = get_announcer(key)
        label = "main bus" if not room else f"#{room}"
        print(f"{label}: announcer is {ann}" if ann
              else f"{label}: not an announce room")
        return 0

    err = _gate(key)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "set":
            q = urllib.parse.quote(akey, safe="")
            api_get(f"set/{q}/{urllib.parse.quote(NICK, safe='')}")
            label = "main bus" if not room else f"#{room}"
            print(f"ANNOUNCE_SET {label} announcer={NICK}")
        else:  # clear
            api_get(f"del/{urllib.parse.quote(akey, safe='')}")
            label = "main bus" if not room else f"#{room}"
            print(f"ANNOUNCE_CLEARED {label}")
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    print(CONVENTION_WARNING, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
