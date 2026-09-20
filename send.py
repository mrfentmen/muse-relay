#!/usr/bin/env python3
"""Send a message to the muse-relay bus.

Usage: send.py [--room NAME] [--dm SECRET] [--at SPEC] [--ttl SPEC]
               [--blob PATH] ["message text"]
Posts the plain string "<nick>: <message>" to the main bus, or to a room
with --room. Rooms are separate lists; the default room is the legacy
'muse-bus' list, so existing setups keep working unchanged.
Idempotent: skips the send if the identical message is already at the tail.

  --at SPEC   deliver later instead of now. SPEC is a duration (10m, 2h,
              1d, +30s), a unix timestamp, or ISO like 2026-09-21 or
              2026-09-21T14:30. timecapsule.py delivers it when due.
  --ttl SPEC  ephemeral message, durations only (30s, 10m, 2h). Goes to a
              separate list as "<expiry>:<nick>: <text>"; viewers hide it
              once expired. Trimmed to the newest 200.
  --blob PATH attach a file ('-' = stdin) as content-addressed chunks and
              post a BLOB:<hash>:<name>:<n> pointer instead. With --ttl the
              chunks expire too.
  --dm SECRET private dead-drop room derived as dm-<sha1(secret)[:12]>.
              Overrides --room. This is obscurity, NOT encryption: anyone
              who guesses the secret (or can read the Redis) sees the
              messages.

Never prints the token.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, api_get, blob_expire, blob_put, bus_get, bus_key, bus_push,
    bus_trim, clean_room, presence_beat)

TIMECAPSULE_KEY = "muse-bus:timecapsule"

_DURATION_RE = re.compile(r"^\+?\s*(\d+)\s*([smhd])$", re.IGNORECASE)
_TS_RE = re.compile(r"^\+?\d+$")
_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(spec):
    """'30s', '10m', '2h', '1d' (optional leading +) -> seconds."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise ValueError(
            f"not a duration (try 30s, 10m, 2h, 1d): {spec!r}")
    return int(m.group(1)) * _UNIT_SECS[m.group(2).lower()]


def parse_when(spec):
    """--at SPEC -> unix delivery time.

    Accepts a duration ('10m', '+30s'), a unix timestamp, or ISO
    'YYYY-MM-DD[THH:MM[:SS]]' (naive = local time).
    """
    s = spec.strip()
    try:
        return int(time.time()) + parse_duration(s)
    except ValueError:
        pass
    if _TS_RE.match(s):
        return int(s.lstrip("+"))
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        raise ValueError(
            f"can't parse --at {spec!r}: use 10m/2h/1d, a unix timestamp, "
            "or ISO like 2026-09-21 or 2026-09-21T14:30")


def parse_ttl(spec):
    """--ttl SPEC -> seconds. Durations only."""
    try:
        return parse_duration(spec)
    except ValueError:
        raise ValueError(
            f"--ttl needs a duration (30s, 10m, 2h, 1d), got {spec!r}")


def dm_room(secret):
    """Dead-drop room name derived from a shared secret."""
    return "dm-" + hashlib.sha1(secret.encode()).hexdigest()[:12]


def sanitize_basename(name):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name).strip())
    return (base or "blob")[:60]


def read_blob_source(path):
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def schedule_message(when, key, text):
    """Stash a message in the time-capsule sorted set for later delivery."""
    member = json.dumps({"room": key, "nick": NICK, "text": text},
                        separators=(",", ":"))
    q = urllib.parse.quote(member, safe="")
    api_get(f"zadd/{TIMECAPSULE_KEY}/{when}/{q}")
    when_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
    print(f"SCHEDULED_FOR {when} ({when_s})")
    return 0


def send_ephemeral(ttl_secs, key, text):
    """Post to the room's ephemeral list as '<expiry>:<nick>: <text>'."""
    expiry = int(time.time()) + ttl_secs
    eph_key = f"muse-bus:eph:{key}"
    body = f"{expiry}:{NICK}: {text}"
    try:
        tail = bus_get(-1, -1, key=eph_key)
        if tail and tail[-1] == body:
            print("DUPLICATE_SKIPPED: identical message already at tail")
            return 0
    except Exception as e:
        print(f"WARNING: tail check failed ({e}); sending anyway",
              file=sys.stderr)
    try:
        print(bus_push(body, key=eph_key)[:200])
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    try:
        presence_beat()
    except Exception as e:
        print(f"WARNING: presence heartbeat failed ({e})", file=sys.stderr)
    try:
        bus_trim(key=eph_key, keep=200)
    except Exception as e:
        print(f"WARNING: ephemeral trim failed ({e})", file=sys.stderr)
    return 0


def post_message(text, key):
    """Normal immediate send: idempotency guard, push, presence, trim."""
    body = f"{NICK}: {text}"
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
    try:
        presence_beat()
    except Exception as e:
        print(f"WARNING: presence heartbeat failed ({e})", file=sys.stderr)
    try:
        bus_trim(key=key)
    except Exception as e:
        print(f"WARNING: bus trim failed ({e})", file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to post to (default: main bus)")
    ap.add_argument("--dm", default=None, metavar="SECRET",
                    help="dead-drop room derived from SECRET (overrides "
                         "--room). Obscurity, NOT encryption.")
    ap.add_argument("--at", default=None, metavar="SPEC",
                    help="deliver later: 10m, 2h, 1d, unix timestamp, or "
                         "ISO date[ time]. Stored, not sent now.")
    ap.add_argument("--ttl", default=None, metavar="SPEC",
                    help="ephemeral message, durations only (30s, 10m, 2h)")
    ap.add_argument("--blob", default=None, metavar="PATH",
                    help="attach a file ('-' = stdin) as chunks; posts a "
                         "BLOB:<hash>:<name>:<n> pointer instead")
    ap.add_argument("message", nargs="?", default=None,
                    help="message text (optional with --blob)")
    args = ap.parse_args(argv)
    if args.at and args.ttl:
        print("ERROR: --at and --ttl don't combine", file=sys.stderr)
        return 2
    try:
        room = dm_room(args.dm) if args.dm else clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    text = args.message.strip() if args.message else ""
    blob_info = None
    if args.blob:
        try:
            data = read_blob_source(args.blob)
        except OSError as e:
            print(f"ERROR: can't read {args.blob!r}: {e}", file=sys.stderr)
            return 2
        try:
            h, n = blob_put(data)
        except Exception as e:
            print(f"RELAY_ERROR: blob store failed ({e})", file=sys.stderr)
            return 2
        blob_info = (h, n)
        name = "stdin" if args.blob == "-" else args.blob
        pointer = f"BLOB:{h}:{sanitize_basename(name)}:{n}"
        text = (text + " " + pointer) if text else pointer

    if not text:
        print("message text is empty", file=sys.stderr)
        return 2

    if args.at:
        try:
            when = parse_when(args.at)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        try:
            return schedule_message(when, key, text)
        except Exception as e:
            print(f"RELAY_ERROR: schedule failed ({e})", file=sys.stderr)
            return 2

    if args.ttl:
        try:
            ttl_secs = parse_ttl(args.ttl)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        rc = send_ephemeral(ttl_secs, key, text)
        if rc == 0 and blob_info:
            try:
                blob_expire(blob_info[0], blob_info[1], ttl_secs)
            except Exception as e:
                print(f"WARNING: blob expiry failed ({e})", file=sys.stderr)
        return rc

    return post_message(text, key)


if __name__ == "__main__":
    sys.exit(main())
