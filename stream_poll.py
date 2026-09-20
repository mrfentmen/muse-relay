#!/usr/bin/env python3
"""Consumer-group poller for the stream-based muse-bus.

Usage: bus_stream_poll.py [--consumer NAME] [--count N] [--reclaim-ms MS]

Reads new entries for this consumer, reclaims entries left pending by a
crashed reader (XAUTOCLAIM), prints one line per message from other nicks as
`nick: text`, ACKs everything it printed, and refreshes presence heartbeat.

Exit 0 always; prints nothing when there is nothing new.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_common as bs


def poll(consumer, count=50, reclaim_ms=60000):
    bs.xgroup_create_mkstream(bs.STREAM, bs.GROUP)
    me = bs.read_file("nick", "del")
    lines = []
    seen_ids = []

    # 1) reclaim entries orphaned by a crashed consumer (crash recovery)
    try:
        orphaned, _cursor = bs.xautoclaim(bs.STREAM, bs.GROUP, consumer,
                                          min_idle_ms=reclaim_ms, count=count)
    except Exception:
        orphaned = []
    # 2) fresh entries for this consumer
    try:
        fresh = bs.xreadgroup(bs.STREAM, bs.GROUP, consumer, count=count)
    except Exception as e:
        print(f"stream poll failed: {e}", file=sys.stderr)
        return []

    for entry_id, fields in orphaned + fresh:
        frm = fields.get("nick", "unknown")
        text = fields.get("text", "")
        if frm != me:
            lines.append(f"{frm}: {text}")
        seen_ids.append(entry_id)

    if seen_ids:
        try:
            bs.xack(bs.STREAM, bs.GROUP, *seen_ids)
        except Exception as e:
            print(f"xack failed: {e}", file=sys.stderr)
    bs.heartbeat(me)
    return lines


def main():
    consumer = bs.read_file("nick", "del")
    count, reclaim_ms = 50, 60000
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--consumer" and i + 1 < len(args):
            consumer = args[i + 1]; i += 2
        elif args[i] == "--count" and i + 1 < len(args):
            count = int(args[i + 1]); i += 2
        elif args[i] == "--reclaim-ms" and i + 1 < len(args):
            reclaim_ms = int(args[i + 1]); i += 2
        else:
            i += 1
    for line in poll(consumer, count, reclaim_ms):
        print(line)


if __name__ == "__main__":
    main()
