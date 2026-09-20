#!/usr/bin/env python3
"""Send a message to the muse-relay bus.

Usage: send.py [--room NAME] [--dm SECRET] [--at SPEC] [--ttl SPEC]
               [--blob PATH] [--img FILE] [--clip] [--every DUR]
               [--typing] [--mod-add NICK] [--mod-del NICK]
               [--mute NICK] [--unmute NICK] [--desc TEXT] [--topic TEXT]
               ["message text"]
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
  --img FILE  like --blob but only for images (PNG/JPEG/GIF/WEBP, checked
              by magic bytes). Posts a BLOB: pointer. Combines with
              message text, not with --at/--ttl/--every/--blob.
  --clip      push the local clipboard as a clip: the bytes ride the
              same chunked-blob transport as --blob, and the pointer is
              stored at muse-bus:clip:<nick> (latest wins, 20-entry
              audit log). Posts nothing to any room; pull it on another
              machine with paste.py. Fails loudly when no clipboard
              tool exists (xclip/xsel/pbcopy/wl-paste/...) or when the
              clipboard is empty. Not with other modes.
  --every DUR recurring message, durations only, minimum 60s. Posts
              nothing now; timecapsule.py delivers it every DUR and
              reschedules. Not with --at/--ttl/--blob/--img.
  --typing    one-shot typing-indicator ping for the room; posts nothing.
  --mod-add N / --mod-del N
              add/remove a room moderator (muse-bus:mods:<room> set).
              Posts nothing.
  --mute N / --unmute N
              mute/unmute a nick in the room (muse-bus:muted:<room> set)
              and post a "<nick>: MUTE:<N>" / "UNMUTE:<N>" notice.
  --desc TEXT set the room's description (room directory); posts nothing.
  --status TEXT
              set your status ("heads down", "in a call"). Lives 120s,
              refreshed by every send/poll/watch contact like a
              heartbeat; presence.py shows it next to your nick. Empty
              string clears it. Obeys MAX_TEXT. Posts nothing.
  --topic TEXT post "<nick>: TOPIC: <text>" (empty string clears it).
  --dm SECRET private dead-drop room derived as dm-<sha1(secret)[:12]>.
              Overrides --room. This is obscurity, NOT encryption: anyone
              who guesses the secret (or can read the Redis) sees the
              messages.

Every immediate send is recorded in the room's local message store
(store.py) and prints its id as "MSG_ID <id>"; edit your own messages
with edits.py <id> <new text>. Message bodies are capped at MAX_TEXT
(relay_common) — use --blob for long content.

Never prints the token.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    MAX_TEXT, NICK, api_get, blob_expire, blob_put, bus_get, bus_key,
    bus_push, bus_trim, clean_room, clip_put, dm_room, img_put, mod_add,
    mod_del, mute_add, mute_del, nick_claim, nick_conflict_holder,
    pomo_register, presence_beat,
    recur_add, room_set_desc, room_touch, status_set, typing_ping)
import clipboard  # noqa: E402
import store  # noqa: E402

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


_POMO_UNIT_RE = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)


def parse_pomodoro_duration(spec):
    """'25m', '1h30m', '90s', '2h 15m' -> seconds. Rejects garbage."""
    s = (spec or "").strip()
    total, pos = 0, 0
    for m in _POMO_UNIT_RE.finditer(s):
        start = m.start()
        while pos < start and s[pos].isspace():
            pos += 1
        if start != pos:
            raise ValueError(
                f"not a pomodoro duration (try 25m, 1h30m, 90s): "
                f"{spec!r}")
        total += int(m.group(1)) * _UNIT_SECS[m.group(2).lower()]
        pos = m.end()
    while pos < len(s) and s[pos].isspace():
        pos += 1
    if pos != len(s) or total <= 0:
        raise ValueError(
            f"not a pomodoro duration (try 25m, 1h30m, 90s): {spec!r}")
    return total


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


def sanitize_basename(name):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name).strip())
    return (base or "blob")[:60]


def warn_nick_conflict():
    """Warn when another instance holds the claim on our nick."""
    holder = nick_conflict_holder()
    if holder:
        print(f"WARNING: nick '{NICK}' is claimed by instance '{holder}' — "
              f"your messages may be confused with theirs. Set "
              f"MUSE_RELAY_INSTANCE_ID uniquely or pick another nick.",
              file=sys.stderr)


def check_nick_reservation():
    """Reject when a different live instance holds our nick's reservation.

    Runs a fresh claim check first (first use of a nick reserves it via
    SET NX). Returns an error string when someone else holds the live
    reservation, else None. A failed check fails open — the bus must keep
    working when Redis is unreachable.
    """
    try:
        nick_claim()
    except Exception:
        return None
    holder = nick_conflict_holder()
    if holder:
        return (f"nick '{NICK}' is reserved by instance '{holder}' — "
                f"pick another nick (MUSE_RELAY_NICK) or set a unique "
                f"MUSE_RELAY_INSTANCE_ID")
    return None


def read_blob_source(path):
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def send_clip():
    """Push the local clipboard as this nick's latest clip.

    Bytes ride the --blob chunk transport; the pointer envelope goes to
    muse-bus:clip:<nick> (latest wins) plus the capped audit log. Posts
    nothing to any room — paste.py is the retrieval path. Returns the
    process exit code.
    """
    try:
        data = clipboard.clipboard_read()
    except clipboard.ClipboardError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not data:
        print("ERROR: clipboard is empty — nothing to clip",
              file=sys.stderr)
        return 2
    try:
        h, n = blob_put(data)
        clip_put(h, n, len(data))
    except Exception as e:
        print(f"RELAY_ERROR: clip store failed ({e})", file=sys.stderr)
        return 2
    print(f"CLIP_STORED muse-bus:clip:{NICK} BLOB:{h}:clipboard:{n} "
          f"({len(data)} bytes) — pull with paste.py")
    return 0


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
        presence_beat(key)
    except Exception as e:
        print(f"WARNING: presence heartbeat failed ({e})", file=sys.stderr)
    warn_nick_conflict()
    try:
        room_touch(key)
    except Exception as e:
        print(f"WARNING: room touch failed ({e})", file=sys.stderr)
    try:
        bus_trim(key=eph_key, keep=200)
    except Exception as e:
        print(f"WARNING: ephemeral trim failed ({e})", file=sys.stderr)
    return 0


def _record_sent(key, text):
    """Record a sent message in the room's local store (store.py).

    Best-effort: the message is already on the bus, so a store failure
    only costs the id. Returns the message id or None.
    """
    try:
        st = store.open_store(store.room_for_key(key))
        return st.append(NICK, text)["id"]
    except Exception as e:
        print(f"WARNING: store record failed ({e})", file=sys.stderr)
        return None


def post_message(text, key):
    """Normal immediate send: length guard, idempotency guard, push,
    store record, presence, trim."""
    if len(text) > MAX_TEXT:
        print(f"ERROR: message too long ({len(text)} chars; max "
              f"{MAX_TEXT}) — use --blob for long content", file=sys.stderr)
        return 2
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
    mid = _record_sent(key, text)
    if mid:
        print(f"MSG_ID {mid}")
    try:
        presence_beat(key)
    except Exception as e:
        print(f"WARNING: presence heartbeat failed ({e})", file=sys.stderr)
    warn_nick_conflict()
    try:
        room_touch(key)
    except Exception as e:
        print(f"WARNING: room touch failed ({e})", file=sys.stderr)
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
    ap.add_argument("--img", default=None, metavar="FILE",
                    help="like --blob but images only (PNG/JPEG/GIF/WEBP, "
                         "magic-byte checked); posts a BLOB: pointer")
    ap.add_argument("--clip", action="store_true",
                    help="push the local clipboard as this nick's clip "
                         "(muse-bus:clip:<nick>, latest wins); posts "
                         "nothing to any room; pull with paste.py")
    ap.add_argument("--every", default=None, metavar="DUR",
                    help="recurring message, durations only, minimum 60s. "
                         "Posts nothing now; timecapsule.py delivers it "
                         "every DUR and reschedules")
    ap.add_argument("--pomodoro", default=None, metavar="DUR",
                    help="start a pomodoro timer: posts "
                         "'POMODORO <nick> <secs> <label>' now and "
                         "schedules 'POMODORO-DONE <nick> <label>' via "
                         "timecapsule.py. Durations like 25m, 1h30m, 90s. "
                         "The label is the message text.")
    ap.add_argument("--typing", action="store_true",
                    help="one-shot typing-indicator ping; posts nothing")
    ap.add_argument("--mod-add", default=None, metavar="NICK",
                    help="add a room moderator; posts nothing")
    ap.add_argument("--mod-del", default=None, metavar="NICK",
                    help="remove a room moderator; posts nothing")
    ap.add_argument("--mute", default=None, metavar="NICK",
                    help="mute a nick in the room and post a MUTE: notice")
    ap.add_argument("--unmute", default=None, metavar="NICK",
                    help="unmute a nick in the room and post an UNMUTE: "
                         "notice")
    ap.add_argument("--desc", default=None, metavar="TEXT",
                    help="set the room description; posts nothing")
    ap.add_argument("--status", default=None, metavar="TEXT",
                    help="set your status text ('' clears it); lives 120s "
                         "and is refreshed by bus activity; posts nothing")
    ap.add_argument("--topic", default=None, metavar="TEXT",
                    help="post '<nick>: TOPIC: <text>' (empty clears)")
    ap.add_argument("message", nargs="?", default=None,
                    help="message text (optional with --blob/--img)")
    args = ap.parse_args(argv)
    if args.at and args.ttl:
        print("ERROR: --at and --ttl don't combine", file=sys.stderr)
        return 2
    if args.img and (args.at or args.ttl or args.every or args.blob
                      or args.pomodoro):
        print("ERROR: --img doesn't combine with --at/--ttl/--every/--blob/"
              "--pomodoro", file=sys.stderr)
        return 2
    if args.clip and (args.at or args.ttl or args.every or args.blob
                      or args.img or args.mute or args.unmute
                      or args.topic is not None or args.message
                      or args.typing or args.desc is not None
                      or args.mod_add or args.mod_del
                      or args.status is not None):
        print("ERROR: --clip doesn't combine with other modes or message "
              "text", file=sys.stderr)
        return 2
    if args.every and (args.at or args.ttl or args.blob or args.img):
        print("ERROR: --every doesn't combine with --at/--ttl/--blob/--img",
              file=sys.stderr)
        return 2
    if args.pomodoro and (args.at or args.ttl or args.every or args.blob
                          or args.img):
        print("ERROR: --pomodoro doesn't combine with --at/--ttl/--every/"
              "--blob/--img", file=sys.stderr)
        return 2
    if args.img and (args.mute or args.unmute or args.topic is not None):
        print("ERROR: --img doesn't combine with --mute/--unmute/--topic",
              file=sys.stderr)
        return 2
    ops = [bool(args.every), bool(args.typing), args.desc is not None,
           bool(args.mod_add), bool(args.mod_del),
           bool(args.mute), bool(args.unmute), args.topic is not None,
           args.status is not None, bool(args.pomodoro)]
    if sum(ops) > 1:
        print("ERROR: only one of --every/--typing/--desc/--mod-add/"
              "--mod-del/--mute/--unmute/--topic/--status/--pomodoro per "
              "invocation", file=sys.stderr)
        return 2
    if ((args.mute or args.unmute or args.topic is not None
         or args.status is not None) and args.message):
        print("ERROR: --mute/--unmute/--topic don't combine with message "
              "text", file=sys.stderr)
        return 2
    try:
        room = dm_room(args.dm) if args.dm else clean_room(args.room)
        key = bus_key(room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # First-come nick reservation: refuse to act as a nick a different
    # live instance holds. Unreserved nicks pass through untouched.
    err = check_nick_reservation()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    def beat():
        try:
            presence_beat(key)
        except Exception as e:
            print(f"WARNING: presence heartbeat failed ({e})",
                  file=sys.stderr)

    # One-shot ops that post nothing.
    if args.clip:
        rc_code = send_clip()
        if rc_code == 0:
            beat()
        return rc_code

    if args.typing:
        try:
            typing_ping(key, NICK)
        except Exception as e:
            print(f"RELAY_ERROR: typing ping failed ({e})", file=sys.stderr)
            return 2
        print(f"TYPING {key} {NICK}")
        return 0

    if args.every:
        try:
            every_secs = parse_duration(args.every)
        except ValueError:
            print(f"ERROR: --every needs a duration (30s, 10m, 2h, 1d), "
                  f"got {args.every!r}", file=sys.stderr)
            return 2
        if every_secs < 60:
            print("ERROR: --every needs at least 60s", file=sys.stderr)
            return 2
        text = args.message.strip() if args.message else ""
        if not text:
            print("ERROR: --every needs message text", file=sys.stderr)
            return 2
        try:
            when = recur_add(key, NICK, text, every_secs)
        except Exception as e:
            print(f"RELAY_ERROR: recur schedule failed ({e})",
                  file=sys.stderr)
            return 2
        beat()
        when_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
        print(f"RECURRING_EVERY {every_secs}s NEXT {when} ({when_s})")
        return 0

    if args.pomodoro:
        try:
            pomo_secs = parse_pomodoro_duration(args.pomodoro)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        label = args.message.strip() if args.message else ""
        if not label:
            print("ERROR: --pomodoro needs a label (the message text)",
                  file=sys.stderr)
            return 2
        now = int(time.time())
        end = now + pomo_secs
        # Post the start line first: no phantom timer/DONE without it.
        rc = post_message(f"POMODORO {NICK} {pomo_secs} {label}", key)
        if rc != 0:
            return rc
        try:
            pomo_register(key, NICK, label, pomo_secs, end)
            schedule_message(end, key, f"POMODORO-DONE {NICK} {label}")
        except Exception as e:
            print(f"RELAY_ERROR: pomodoro schedule failed ({e})",
                  file=sys.stderr)
            return 2
        return 0

    if args.desc is not None:
        try:
            room_set_desc(key, args.desc)
        except Exception as e:
            print(f"RELAY_ERROR: desc set failed ({e})", file=sys.stderr)
            return 2
        beat()
        print(f"DESC_SET {key}: {args.desc}")
        return 0

    if args.mod_add:
        try:
            mod_add(key, args.mod_add)
        except Exception as e:
            print(f"RELAY_ERROR: mod-add failed ({e})", file=sys.stderr)
            return 2
        beat()
        print(f"MOD_ADDED {key} {args.mod_add}")
        return 0

    if args.mod_del:
        try:
            mod_del(key, args.mod_del)
        except Exception as e:
            print(f"RELAY_ERROR: mod-del failed ({e})", file=sys.stderr)
            return 2
        beat()
        print(f"MOD_REMOVED {key} {args.mod_del}")
        return 0

    # Notice posts: mute/unmute/topic.
    if args.mute or args.unmute:
        target = args.mute or args.unmute
        verb = "MUTE" if args.mute else "UNMUTE"
        try:
            (mute_add if args.mute else mute_del)(key, target)
        except Exception as e:
            print(f"RELAY_ERROR: mute update failed ({e})", file=sys.stderr)
            return 2
        return post_message(f"{verb}:{target}", key)

    if args.topic is not None:
        return post_message(f"TOPIC: {args.topic}", key)

    if args.status is not None:
        text = args.status.strip()
        try:
            status_set(text)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"RELAY_ERROR: status set failed ({e})", file=sys.stderr)
            return 2
        beat()
        if text:
            print(f"STATUS_SET {NICK}: {text}")
        else:
            print(f"STATUS_CLEARED {NICK}")
        return 0

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

    if args.img:
        try:
            h, name, n = img_put(args.img)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"ERROR: can't read {args.img!r}: {e}", file=sys.stderr)
            return 2
        pointer = f"BLOB:{h}:{name}:{n}"
        print(f"IMAGE {args.img} -> {pointer}")
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
