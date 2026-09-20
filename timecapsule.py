#!/usr/bin/env python3
"""Deliver scheduled time-capsule messages and recurring messages.

One-shot: pops every due item from the muse-bus:timecapsule sorted set
and posts it to its room as "<nick>: <text>". Also drains
muse-bus:recur the same way, except each delivered recurring item is
re-registered with score = now + every (its interval).
--loop [--interval 30]: keep sweeping for due items.

Each item is JSON {"room": <full room key>, "nick": <nick>, "text": <text>}
scored by unix delivery time (recurring items add "every": <seconds>).
Delivery is race-safe between concurrent runners: an item is only posted
if this process wins the ZREM for it. If the push then fails, the item
is re-queued 60s out so nothing is lost.
Never prints the token.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    RECUR_KEY, TOKEN_FILE, api_get, bus_push, bus_trim, room_touch)

TIMECAPSULE_KEY = "muse-bus:timecapsule"


def pop_due(now=None):
    """Deliver everything due; returns [(room, body)] delivered."""
    now = int(time.time() if now is None else now)
    data = json.loads(api_get(f"zrangebyscore/{TIMECAPSULE_KEY}/0/{now}"))
    due = data.get("result") or []
    delivered = []
    for member in due:
        if not isinstance(member, str):
            continue
        q = urllib.parse.quote(member, safe="")
        try:
            taken = json.loads(api_get(f"zrem/{TIMECAPSULE_KEY}/{q}"))
        except Exception as e:
            print(f"WARNING: zrem failed ({e}); skipping", file=sys.stderr)
            continue
        if not taken.get("result"):
            continue  # another runner claimed it
        try:
            item = json.loads(member)
            body = f"{item['nick']}: {item['text']}"
            room = item["room"]
        except (ValueError, KeyError, TypeError) as e:
            print(f"WARNING: bad capsule {member[:80]!r} ({e})",
                  file=sys.stderr)
            continue
        try:
            bus_push(body, key=room)
        except Exception as e:
            print(f"WARNING: delivery failed ({e}); re-queueing in 60s",
                  file=sys.stderr)
            try:
                api_get(f"zadd/{TIMECAPSULE_KEY}/{now + 60}/{q}")
            except Exception as e2:
                print(f"WARNING: re-queue failed ({e2}); lost: "
                      f"{member[:80]!r}", file=sys.stderr)
            continue
        try:
            bus_trim(key=room)
        except Exception as e:
            print(f"WARNING: trim failed ({e})", file=sys.stderr)
        print(f"DELIVERED {room} {body[:120]}", flush=True)
        delivered.append((room, body))
    return delivered


def pop_recur(now=None):
    """Deliver due recurring items and reschedule each by its interval.

    Same race-safe ZREM-claim pattern as pop_due. On success the item is
    re-registered with score = now + every; on push failure it is
    re-queued +60s like a time-capsule item. Returns [(room, body)].
    """
    now = int(time.time() if now is None else now)
    data = json.loads(api_get(f"zrangebyscore/{RECUR_KEY}/0/{now}"))
    due = data.get("result") or []
    delivered = []
    for member in due:
        if not isinstance(member, str):
            continue
        q = urllib.parse.quote(member, safe="")
        try:
            taken = json.loads(api_get(f"zrem/{RECUR_KEY}/{q}"))
        except Exception as e:
            print(f"WARNING: zrem failed ({e}); skipping", file=sys.stderr)
            continue
        if not taken.get("result"):
            continue  # another runner claimed it
        try:
            item = json.loads(member)
            body = f"{item['nick']}: {item['text']}"
            room = item["room"]
            every = int(item["every"])
        except (ValueError, KeyError, TypeError) as e:
            print(f"WARNING: bad recur {member[:80]!r} ({e})",
                  file=sys.stderr)
            continue
        try:
            bus_push(body, key=room)
        except Exception as e:
            print(f"WARNING: recur delivery failed ({e}); re-queueing in "
                  "60s", file=sys.stderr)
            try:
                api_get(f"zadd/{RECUR_KEY}/{now + 60}/{q}")
            except Exception as e2:
                print(f"WARNING: re-queue failed ({e2}); lost: "
                      f"{member[:80]!r}", file=sys.stderr)
            continue
        try:
            bus_trim(key=room)
        except Exception as e:
            print(f"WARNING: trim failed ({e})", file=sys.stderr)
        try:
            room_touch(room)
        except Exception as e:
            print(f"WARNING: room touch failed ({e})", file=sys.stderr)
        try:
            api_get(f"zadd/{RECUR_KEY}/{now + every}/{q}")
        except Exception as e:
            print(f"WARNING: reschedule failed ({e}); lost future runs: "
                  f"{member[:80]!r}", file=sys.stderr)
        print(f"RECUR_DELIVERED {room} {body[:120]}", flush=True)
        delivered.append((room, body))
    return delivered


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true",
                    help="keep sweeping instead of running once")
    ap.add_argument("--interval", type=float, default=30,
                    help="seconds between sweeps in --loop (default: 30)")
    args = ap.parse_args(argv)
    if args.interval <= 0:
        print("interval must be positive", file=sys.stderr)
        return 2
    if args.loop:
        print(f"sweeping {TIMECAPSULE_KEY} every {args.interval:g}s "
              "(Ctrl-C to stop)", flush=True)
    try:
        while True:
            try:
                pop_due()
                pop_recur()
            except FileNotFoundError:
                print("RELAY_ERROR: token file missing: " + TOKEN_FILE,
                      file=sys.stderr)
                return 2
            except Exception as e:
                print(f"WARNING: capsule sweep failed ({e}); retrying",
                      file=sys.stderr)
            if not args.loop:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
