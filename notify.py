#!/usr/bin/env python3
"""Post a notification to a bus room (the relay-side notify CLI).

Usage: notify.py [--room R] [--severity info|warning|critical]
                 [--topic T] [--target NICK] <text...>

Posts one bus message in the notification system's line format:

    <nick>: NOTIFY:<id> <severity> [<topic>] <text>

with "@<target>" right after the topic when --target names one
nick. The id is deterministic (sha256 of nick|topic|text|second,
the same scheme intel.store uses), so the OS-side durable
record, the viewer's Notifications panel, and any later ack all
name the same notification. The durable record itself lives in
the OS store (intel/notify.py); this CLI is the lightweight
poster for relay-side tools that only need the line on the bus.

The viewer's Notifications panel renders NOTIFY lines (and
ALERT lines) with an unread badge; warning/critical lines from
other nicks also fire a browser Notification there, and the
phone push fan-out rides the normal viewer poll hooks.

Never prints the token.
"""
import argparse
import hashlib
import sys
import time

import relay_common as rc

SEVERITIES = ("info", "warning", "critical")


def notify_id(nick, topic, text, ts):
    """Deterministic notification id (intel.store's scheme)."""
    basis = "auto:" + "|".join([str(nick), str(topic), str(text), str(ts)])
    return "ntf-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def format_line(nick, severity, topic, text, target=None, ts=None):
    """The NOTIFY line (without the "<nick>: " bus prefix)."""
    ts = int(time.time()) if ts is None else int(ts)
    mention = f" @{target}" if target else ""
    line = (f"NOTIFY:{notify_id(nick, topic, text, ts)} {severity} "
            f"[{topic}]" + mention + f" {text}")
    return " ".join(line.splitlines())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default=None,
                    help="room to post to (default: main bus)")
    ap.add_argument("--severity", default="info",
                    help="info, warning or critical (default: info)")
    ap.add_argument("--topic", default="general")
    ap.add_argument("--target", default=None,
                    help="one nick to target (default: everyone)")
    ap.add_argument("text", nargs="+", help="notification text")
    args = ap.parse_args(argv)

    if args.severity not in SEVERITIES:
        print(f"ERROR: severity must be one of {', '.join(SEVERITIES)}",
              file=sys.stderr)
        return 2
    text = " ".join(args.text).strip()
    if not text:
        print("ERROR: empty notification text", file=sys.stderr)
        return 2
    line = format_line(rc.NICK, args.severity, args.topic, text,
                       target=args.target)
    key = rc.bus_key(args.room)
    try:
        print(rc.bus_push(f"{rc.NICK}: {line}", key=key)[:200])
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    print(f"NOTIFIED {key} {line.split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
