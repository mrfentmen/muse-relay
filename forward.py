#!/usr/bin/env python3
"""Forward a stored message into another room.

Usage: forward.py [--room NAME | --dm SECRET] <msg_id> <#target-room>

Reposts the message with that store id into another room as:

    <original nick> (via <forwarder>): <text>

Wire format (also accepted directly on the bus, like EDIT):

    FWD <msg_id> <#target-room>

The message body is looked up in the source room's local store — ids
come from send.py's "MSG_ID <id>" output, or from poll.py/watch.py,
which record incoming messages so other nicks' lines get ids too.

Forwarding a forwarded message keeps a single (via ...) hop: when the
entry's nick matches the via-pattern's forwarder (the "<pusher>: <orig>
(via <pusher>): <body>" shape poll.py records), the original nick is
kept and only the forwarder is replaced — never stacked into chains.
A "(via ...)" pattern that is just message content is left alone.
An unknown id is a clear error; the target room is sanitized like any
room name (the leading # is optional).

Never prints the token.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, bus_key, bus_push, bus_trim, clean_room, dm_room,
    nick_conflict_holder, presence_beat, room_touch)
import store  # noqa: E402
from store import EditError  # noqa: E402,F401  (consistent error shape)

# "FWD <id> <#room>" — bus-side protocol line (after "<nick>: ")
FWD_RE = re.compile(r"^FWD ([A-Za-z0-9._-]+) (#?)([A-Za-z0-9_-]*)$")

# Matches the body of a previously forwarded message:
#   "<original nick> (via <forwarder>): <text>"
VIA_RE = re.compile(r"^(.*?) \(via ([^)]+)\): (.*)$", re.DOTALL)


def parse_fwd_text(text):
    """Parse a 'FWD <msg_id> <#room>' body -> (msg_id, target_room)."""
    m = FWD_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(3)


def via_parts(text):
    """(orig_nick, forwarder, body) for a forwarded text, else None."""
    m = VIA_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def forward_text(entry):
    """Render the wire text for forwarding store entry.

    A message that is itself a forward keeps its single (via ...) hop:
    the original nick is preserved, only the forwarder is replaced —
    never '<a> (via <b> (via <c>))' chains.

    A genuine prior forward has the shape "<pusher>: <orig> (via
    <pusher>): <body>" on the bus, so after poll.py/watch.py records it
    the entry's nick equals the via-pattern's forwarder — that's when we
    collapse to a single hop. If they differ, the "(via ...)" in the
    text is just message content and the forward is attributed to the
    entry's author with one new hop.
    """
    prior = via_parts(entry["text"])
    if prior and prior[1] == entry["nick"]:
        orig_nick, _, body = prior
        return f"{orig_nick} (via {NICK}): {body}"
    return f"{entry['nick']} (via {NICK}): {entry['text']}"


def do_forward(room, msg_id, target_room, st=None):
    """Look up msg_id in room's store, repost it into target_room.

    Returns (entry, wire_text) or raises EditError with a clear message
    for an unknown id. `st` lets callers reuse an open store (tests).
    """
    try:
        if st is None:
            st = store.open_store(room)
        entry = st.get(msg_id)
    except Exception as e:
        raise EditError(f"store read failed: {e}")
    if entry is None:
        raise EditError(f"no such message: {msg_id}")
    text = forward_text(entry)
    try:
        tkey = bus_key(target_room)
        bus_push(f"{NICK}: {text}", key=tkey)
        bus_trim(key=tkey)
        presence_beat(tkey)
        room_touch(tkey)
    except Exception as e:
        raise EditError(f"forward push failed: {e}")
    return entry, text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forward.py")
    ap.add_argument("--room", default=None,
                    help="room holding the message (default: main bus)")
    ap.add_argument("--dm", default=None, metavar="SECRET",
                    help="dead-drop room derived from SECRET "
                         "(overrides --room)")
    ap.add_argument("msg_id",
                    help="message id, as printed by send.py (MSG_ID <id>) "
                         "or assigned by poll.py/watch.py to incoming "
                         "messages")
    ap.add_argument("target", metavar="#target-room",
                    help="room to repost into ('#' optional)")
    args = ap.parse_args(argv)
    try:
        room = dm_room(args.dm) if args.dm else clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    try:
        target = clean_room(args.target.lstrip("#"))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not target:
        print("ERROR: target room is empty", file=sys.stderr)
        return 2
    try:
        entry, text = do_forward(room, args.msg_id, target)
    except EditError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    print(f"FORWARDED {args.msg_id} -> {bus_key(target)}: {text[:120]}")
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}'",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
