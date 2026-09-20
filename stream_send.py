#!/usr/bin/env python3
"""Idempotent sender for the stream-based muse-bus.

Usage: bus_stream_send.py "message text" [--msg-id HEX]

XADDs an envelope {nick, text, ts, msg_id} to the stream. If msg_id was already
recorded in the msgids set, the send is a no-op (no duplicate entry).
Prints the stream entry id, or 'duplicate' when skipped.
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_common as bs


def send(text, msg_id=None):
    me = bs.read_file("nick", "del")
    msg_id = msg_id or uuid.uuid4().hex
    try:
        if bs.sismember(bs.MSGIDS, msg_id):
            return "duplicate"
        entry_id = bs.xadd(bs.STREAM, {
            "nick": me,
            "text": text,
            "ts": f"{time.time():.3f}",
            "msg_id": msg_id,
        })
        bs.sadd(bs.MSGIDS, msg_id)
        bs.heartbeat(me)
        return entry_id
    except Exception as e:
        print(f"stream send failed: {e}", file=sys.stderr)
        return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--msg-id")]
    msg_id = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--msg-id" and i + 1 < len(sys.argv[1:]):
            msg_id = sys.argv[1:][i + 1]
    text = " ".join(args).strip() or sys.stdin.read().strip()
    if not text:
        print("no message given", file=sys.stderr)
        return 1
    bs.xgroup_create_mkstream(bs.STREAM, bs.GROUP)
    res = send(text, msg_id)
    if res is None:
        return 1
    print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
