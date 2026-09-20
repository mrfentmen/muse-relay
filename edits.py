#!/usr/bin/env python3
"""Edit protocol for the muse-relay bus.

Wire format (one bus message):

    EDIT <msg_id> <new text>

Only the original nick may edit its own message; an edit appends the
displaced text to the message's edit_history and sets its edited flag
(see store.py). send.py records every sent message in the room's local
store and prints its id as "MSG_ID <id>" — pass that id here.

CLI: edits.py [--room NAME | --dm SECRET] <msg_id> <new text...>

Applies the edit to the room's local store (rejecting other nicks'
messages and unknown ids with a clear error), then announces
"<nick>: EDIT <msg_id> <new text>" on the bus so other participants'
polls render it with an (edited) marker. The store is local per
machine, so remote participants render the marker rather than rewrite
their copy; on a shared store dir (same machine) polls apply the edit
for real.

Max text length is enforced, same as send (relay_common.MAX_TEXT).
Never prints the token.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    MAX_TEXT, NICK, bus_key, bus_push, bus_trim, clean_room, dm_room,
    nick_conflict_holder, presence_beat, room_touch)
import store  # noqa: E402
from store import EditError  # noqa: E402,F401  (re-exported for callers)

EDIT_RE = re.compile(r"^EDIT ([A-Za-z0-9._-]+)(?: (.*))?$", re.DOTALL)


def parse_edit_text(text):
    """Parse an 'EDIT <msg_id> <new text>' message body.

    Returns (msg_id, new_text), or None when the line isn't an edit.
    """
    m = EDIT_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2)


def apply_edit(st, msg_id, editor_nick, new_text, ts=None):
    """Validate and apply an edit on store st; returns the entry.

    Raises EditError with a clear message for empty or too-long text
    (same limit as send), an unknown message id, or an editor who
    isn't the message's original author.
    """
    if not new_text or not new_text.strip():
        raise EditError("new text is empty")
    if len(new_text) > MAX_TEXT:
        raise EditError(f"text too long ({len(new_text)} chars; "
                        f"max {MAX_TEXT}) — use --blob for long content")
    return st.edit(msg_id, editor_nick, new_text, ts=ts)


def record_incoming(st, line, my_nick):
    """Record an incoming bus line in the room's store (best-effort).

    poll.py and watch.py call this so locally-seen messages get store
    ids — that's what makes FWD/SAVE-by-id work for other nicks'
    messages, not just our own sends. Skips our own lines (send.py
    already recorded them), EDIT protocol lines (they mutate existing
    entries instead of being messages), and anything that isn't
    "<nick>: <text>". Returns the stored entry, or None when skipped.
    """
    nick, sep, text = line.partition(": ")
    if not sep or not nick or not text:
        return None
    if nick == my_nick:
        return None
    try:
        import relay_common as rc
        if my_nick == rc.NICK and nick in rc.own_prefixes():
            return None  # own line under our display name
    except Exception:
        pass
    if parse_edit_text(text):
        return None
    try:
        entries = st.entries()
        if entries and entries[-1].get("text") == text \
                and entries[-1].get("nick") == nick:
            return None  # duplicate line (e.g. re-delivered); keep one id
        return st.append(nick, text)
    except Exception:
        return None


def render_incoming(line, room):
    """Render one incoming bus line for display, applying EDIT lines.

    For "<nick>: EDIT <id> <new text>" where the room's store knows the
    id and the nick is the original author: apply the edit to the store
    and render "<nick>: <new text> (edited)". Anything else renders the
    raw line with an " (edited)" marker appended; a spoofed edit of a
    known message (wrong nick) is rejected with a stderr warning.
    Non-edit lines render unchanged.
    """
    nick, sep, text = line.partition(": ")
    if not sep:
        return line
    parsed = parse_edit_text(text)
    if not parsed:
        return line
    msg_id, new_text = parsed
    try:
        st = store.open_store(room)
        entry = st.get(msg_id)
    except Exception as e:
        print(f"WARNING: store read failed ({e})", file=sys.stderr)
        return f"{line} (edited)"
    if entry is not None:
        try:
            apply_edit(st, msg_id, nick, new_text)
        except EditError as e:
            print(f"WARNING: rejected edit of {msg_id} by {nick}: {e}",
                  file=sys.stderr)
        else:
            return f"{nick}: {new_text} (edited)"
    return f"{line} (edited)"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edits.py")
    ap.add_argument("--room", default=None,
                    help="room holding the message (default: main bus)")
    ap.add_argument("--dm", default=None, metavar="SECRET",
                    help="dead-drop room derived from SECRET "
                         "(overrides --room)")
    ap.add_argument("msg_id",
                    help="message id, as printed by send.py (MSG_ID <id>)")
    ap.add_argument("text", nargs="+", help="replacement text")
    args = ap.parse_args(argv)
    try:
        room = dm_room(args.dm) if args.dm else clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    text = " ".join(args.text).strip()
    try:
        st = store.open_store(room)
        apply_edit(st, args.msg_id, NICK, text)
    except EditError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    try:
        print(bus_push(f"{NICK}: EDIT {args.msg_id} {text}", key=key)[:200])
    except Exception as e:
        print(f"RELAY_ERROR: announcement failed ({e}); the edit is "
              f"applied locally but others won't see it", file=sys.stderr)
        return 2
    try:
        presence_beat(key)
    except Exception as e:
        print(f"WARNING: presence heartbeat failed ({e})", file=sys.stderr)
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}'",
              file=sys.stderr)
    try:
        room_touch(key)
    except Exception as e:
        print(f"WARNING: room touch failed ({e})", file=sys.stderr)
    try:
        bus_trim(key=key)
    except Exception as e:
        print(f"WARNING: bus trim failed ({e})", file=sys.stderr)
    print(f"EDITED {args.msg_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
