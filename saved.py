#!/usr/bin/env python3
"""Bookmark messages by store id.

Usage: saved.py save <id> [--room NAME | --dm SECRET]
       saved.py unsave <id>
       saved.py list [--room R] [--json]

Bookmarks live in the Redis list muse-bus:saved:<nick>, newest first,
capped at 200 entries. Each entry is a JSON blob carrying the message's
id, room, nick, text, original ts, and the time it was saved.

<id> is the store id printed by send.py ("MSG_ID <id>") — the same id
forward.py takes. Use --room/--dm to select the source room's store,
exactly like forward.py (default: the main bus).

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
    NICK, api_get, bus_get, clean_room, dm_room,
    nick_conflict_holder)
import store  # noqa: E402
from store import EditError  # noqa: E402,F401  (consistent error shape)

SAVE_CAP = 200


def save_key(nick=None):
    """Redis key holding (nick or our) saved messages, newest first."""
    return f"muse-bus:saved:{nick or NICK}"


def _blob(entry):
    return json.dumps({
        "id": entry["id"],
        "room": entry.get("room", ""),
        "nick": entry["nick"],
        "text": entry["text"],
        "ts": entry.get("ts"),
        "saved_ts": int(time.time()),
    }, separators=(",", ":"))


def do_save(room, msg_id):
    """Stash the stored message in our saved list. Returns the blob.

    Raises EditError for an unknown id.
    """
    try:
        st = store.open_store(room)
        entry = st.get(msg_id)
    except Exception as e:
        raise EditError(f"store read failed: {e}")
    if entry is None:
        raise EditError(f"no such message: {msg_id}")
    blob = _blob(entry)
    key = save_key()
    try:
        q = urllib.parse.quote(blob, safe="")
        api_get(f"lpush/{key}/{q}")
        api_get(f"ltrim/{key}/0/{SAVE_CAP - 1}")
    except Exception as e:
        raise EditError(f"save failed: {e}")
    return blob


def _read_all(key):
    """All saved blobs, newest first; skips unparsable entries."""
    out = []
    for raw in bus_get(0, -1, key=key):
        try:
            out.append((raw, json.loads(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def do_unsave(msg_id):
    """Remove every saved entry with this id. Returns the count removed."""
    key = save_key()
    try:
        pairs = _read_all(key)
    except Exception as e:
        raise EditError(f"save list read failed: {e}")
    removed = 0
    for raw, data in pairs:
        if data.get("id") == msg_id:
            try:
                q = urllib.parse.quote(raw, safe="")
                api_get(f"lrem/{key}/1/{q}")
                removed += 1
            except Exception as e:
                raise EditError(f"unsave failed: {e}")
    return removed


def do_list(room=None):
    """Saved entries, newest first, optionally filtered by room."""
    try:
        pairs = _read_all(save_key())
    except Exception as e:
        raise EditError(f"save list read failed: {e}")
    if room is not None:
        pairs = [(raw, d) for raw, d in pairs if d.get("room") == room]
    return [d for _, d in pairs]


def _room_label(room):
    return "main" if not room else f"#{room}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="saved.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_save = sub.add_parser("save", help="bookmark a stored message by id")
    p_save.add_argument("msg_id", help="message id, as printed by send.py "
                        "(MSG_ID <id>)")
    p_save.add_argument("--room", default=None,
                        help="room holding the message (default: main bus)")
    p_save.add_argument("--dm", default=None, metavar="SECRET",
                        help="dead-drop room derived from SECRET "
                        "(overrides --room)")

    p_unsave = sub.add_parser("unsave", help="remove a bookmark by id")
    p_unsave.add_argument("msg_id", help="message id to remove")

    p_list = sub.add_parser("list", help="list saved messages")
    p_list.add_argument("--room", default=None,
                        help="only show saves from this room")
    p_list.add_argument("--json", action="store_true",
                        help="machine-readable JSON output")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "save":
            room = dm_room(args.dm) if args.dm else clean_room(args.room)
            blob = do_save(room, args.msg_id)
            data = json.loads(blob)
            print(f"SAVED {data['id']} ({_room_label(data['room'])})")
        elif args.cmd == "unsave":
            removed = do_unsave(args.msg_id)
            if not removed:
                print(f"ERROR: no such saved message: {args.msg_id}",
                      file=sys.stderr)
                return 2
            print(f"UNSAVED {args.msg_id} ({removed} removed)")
        elif args.cmd == "list":
            room = clean_room(args.room) if args.room else None
            entries = do_list(room)
            if args.json:
                print(json.dumps(entries, indent=2))
            elif not entries:
                print("(no saved messages)")
            else:
                for e in entries:
                    print(f"[{e['id']}] {_room_label(e.get('room'))} "
                          f"{e['nick']}: {e['text']}")
    except EditError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"RELAY_ERROR: {e}", file=sys.stderr)
        return 2
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}'",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
