#!/usr/bin/env python3
"""Per-room activity digest from the bus.

Usage: digest.py [--room NAME] [--hours 24] [--json]

Reports, per room: message count on the bus, top talkers, and busiest
hour. Counts and top talkers are pure Redis arithmetic over the room's
list (LRANGE, paged so big rooms don't blow up); no LLM involved.

Honest limitation: bus list items are plain "<nick>: <text>" with no
timestamps, so anything time-based (--hours filtering, busiest hour)
is computed from your LOCAL message store, which records a ts per
entry. Rooms (or time windows) with no local store data report "n/a"
for those fields instead of inventing numbers.

Never prints the token.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    ROOMDIR_KEY, api_get, bus_get, bus_key, clean_room)
import store  # noqa: E402

PAGE = 1000  # LRANGE chunk size; big rooms stream, never slurp
TOP_N = 5


def list_rooms():
    """Room keys from the room directory, main bus first."""
    try:
        data = json.loads(api_get(f"zrange/{ROOMDIR_KEY}/0/-1"))
        keys = data.get("result", []) or []
    except Exception:
        keys = []
    rooms = []
    seen = set()
    for key in ["muse-bus"] + list(keys):
        room = store.room_for_key(key)
        if room not in seen:
            seen.add(room)
            rooms.append(room)
    return rooms


def digest_room(room, hours):
    """Stats dict for one room."""
    key = bus_key(room)
    talkers = Counter()
    bus_messages = 0
    start = 0
    while True:
        try:
            chunk = bus_get(start, start + PAGE - 1, key=key)
        except Exception as e:
            raise RuntimeError(f"bus read failed for {key}: {e}")
        if not chunk:
            break
        for line in chunk:
            bus_messages += 1
            if isinstance(line, str):
                nick, sep, _ = line.partition(": ")
                if sep and nick:
                    talkers[nick] += 1
        if len(chunk) < PAGE:
            break
        start += PAGE

    top = [{"nick": n, "count": c}
           for n, c in talkers.most_common(TOP_N)]

    # Time-based stats come from the local store (bus items have no ts).
    recent_messages = None
    busiest_hour = None
    try:
        st = store.open_store(room)
        cutoff = time.time() - hours * 3600
        recent = [e for e in st.entries()
                  if isinstance(e.get("ts"), (int, float))
                  and e["ts"] >= cutoff]
        recent_messages = len(recent)
        per_hour = Counter(time.localtime(e["ts"]).tm_hour
                           for e in recent)
        if per_hour:
            hour, count = per_hour.most_common(1)[0]
            busiest_hour = {"hour": hour, "count": count}
    except Exception:
        pass  # no local store data: leave time fields null

    return {
        "room": room or "main",
        "key": key,
        "bus_messages": bus_messages,
        "top_talkers": top,
        "hours": hours,
        "recent_messages": recent_messages,
        "busiest_hour": busiest_hour,
    }


def _fmt_hour(h):
    return f"{h['hour']:02d}:00" if h else "n/a"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest.py")
    ap.add_argument("--room", default=None,
                    help="digest one room (default: all known rooms)")
    ap.add_argument("--hours", type=float, default=24,
                    help="window for time-based stats, from the local "
                    "store (default: 24)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON output")
    args = ap.parse_args(argv)
    if args.hours <= 0:
        print("ERROR: --hours must be positive", file=sys.stderr)
        return 2

    try:
        rooms = [clean_room(args.room)] if args.room else list_rooms()
        digests = [digest_room(r, args.hours) for r in rooms]
    except RuntimeError as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(digests, indent=2))
        return 0
    if not digests:
        print("(no rooms seen yet)")
        return 0
    for d in digests:
        label = "main" if d["room"] == "main" else f"#{d['room']}"
        print(f"{label} — {d['bus_messages']} on bus")
        if d["top_talkers"]:
            tops = ", ".join(
                f"{t['nick']} ({t['count']})" for t in d["top_talkers"])
            print(f"  top talkers: {tops}")
        else:
            print("  top talkers: —")
        rm = d["recent_messages"]
        rm_s = str(rm) if rm is not None else "n/a (no local store)"
        print(f"  last {args.hours:g}h (local store): {rm_s} messages; "
              f"busiest hour: {_fmt_hour(d['busiest_hour'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
