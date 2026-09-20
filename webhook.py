#!/usr/bin/env python3
"""Bridge room messages to an external webhook.

Usage: webhook.py --url URL_OR_ENV [--room R] [--once] [--interval N]

POSTs each new "<nick>: <text>" line as JSON
{"nick", "text", "room", "ts"} (ts = delivery time; bus items carry no
timestamps). --url takes a literal https:// URL or the NAME of an
environment variable holding one.

--once posts everything new since the last run and exits; the default
loops like watch.py. Read offsets live in local webhook-seen files, so
a restart never re-posts (or skips) messages.

Delivery is retried with backoff (1s, 2s, 4s, 8s); after five failed
attempts it fails loudly (stderr + non-zero exit) WITHOUT advancing
the offset, so the message is retried on the next run.

Security: the URL is never stored on the bus/Redis, never written to
any file, and never appears in logs or error messages. It lives only
in the process's argv/environ.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    bus_get, bus_key, clean_room, seen_path)

MAX_ATTEMPTS = 5
BACKOFFS = (1, 2, 4, 8)  # seconds between the 5 attempts


def resolve_url(spec):
    """Literal URL, or an env var name holding one. Never logs the value."""
    s = (spec or "").strip()
    if s.startswith(("http://", "https://")):
        return s
    val = os.environ.get(s, "").strip()
    if not val:
        raise ValueError(f"env var {s!r} is not set or empty")
    if not val.startswith(("http://", "https://")):
        raise ValueError(f"env var {s!r} does not hold an http(s) URL")
    return val


def webhook_seen_path(room):
    """Offset file for webhook delivery (separate from poll's)."""
    base = seen_path(room)
    here, name = os.path.split(base)
    return os.path.join(here, f"webhook-{name}")


def read_offset(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(path, n):
    with open(path, "w") as f:
        f.write(str(n))


def post_one(url, payload, opener=None):
    """POST one JSON payload. Returns None on success, error str on
    failure. The URL never appears in the returned error."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "muse-relay-webhook/1.0"},
        method="POST")
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return None
            return f"webhook POST failed: HTTP {resp.status}"
    except Exception as e:
        return f"webhook POST failed: {type(e).__name__}: {e}"


def deliver(url, payload, opener=None, sleep=time.sleep):
    """POST with backoff. Returns None on success, error str after
    MAX_ATTEMPTS failures."""
    err = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            sleep(BACKOFFS[attempt - 1])
        err = post_one(url, payload, opener=opener)
        if err is None:
            return None
        print(f"WARNING: webhook attempt {attempt + 1}/{MAX_ATTEMPTS} "
              f"failed ({err}); retrying", file=sys.stderr)
    return err


def bridge_once(url, room, opener=None, sleep=time.sleep):
    """Post new messages for one room. Returns (posted, failed).

    On failure the offset is left at the failed message so the next
    run retries it."""
    key = bus_key(room)
    seen_file = webhook_seen_path(room)
    seen = read_offset(seen_file)
    try:
        msgs = bus_get(seen, -1, key=key)
    except Exception as e:
        print(f"RELAY_ERROR: bus read failed ({e})", file=sys.stderr)
        return 0, 1
    label = room if room else "main"
    posted = 0
    for m in msgs:
        seen += 1
        if not isinstance(m, str):
            write_offset(seen_file, seen)
            continue
        nick, sep, text = m.partition(": ")
        if not sep or not nick:
            write_offset(seen_file, seen)  # unparseable: skip, don't loop
            continue
        payload = {"nick": nick, "text": text, "room": label,
                   "ts": int(time.time())}
        err = deliver(url, payload, opener=opener, sleep=sleep)
        if err is not None:
            print(f"RELAY_ERROR: {err} — giving up after "
                  f"{MAX_ATTEMPTS} attempts; offset kept at message "
                  f"{seen - 1} for retry", file=sys.stderr)
            return posted, 1
        posted += 1
        write_offset(seen_file, seen)
    return posted, 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="webhook.py")
    ap.add_argument("--url", required=True, metavar="URL_OR_ENV",
                    help="webhook URL, or env var name holding one")
    ap.add_argument("--room", default=None,
                    help="room to bridge (default: main bus)")
    ap.add_argument("--once", action="store_true",
                    help="post new-since-last-run and exit (default: loop)")
    ap.add_argument("--interval", type=float, default=10,
                    help="seconds between polls in loop mode (default: 10)")
    args = ap.parse_args(argv)
    try:
        room = clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("ERROR: --interval must be positive", file=sys.stderr)
        return 2
    try:
        url = resolve_url(args.url)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    label = f"room '{room}'" if room else "main bus"
    if args.once:
        posted, failed = bridge_once(url, room)
        print(f"WEBHOOK_POSTED {posted} from {label}")
        return 2 if failed else 0
    print(f"bridging {label} -> webhook every {args.interval:g}s "
          f"(Ctrl-C to stop)", flush=True)
    try:
        while True:
            posted, failed = bridge_once(url, room)
            if posted:
                print(f"WEBHOOK_POSTED {posted} from {label}", flush=True)
            if failed:
                return 2
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
