#!/usr/bin/env python3
"""One command that answers "what do I need to know right now?".

Usage: brief.py [--room NAME] [--limit N] [--json] [--consume]
                [--no-jobs] [--jobs-limit N] [--watch NICK]

`poll.py` prints messages; this assembles the *context* around them, so
a Muse waking up (or a listener hook starting one) does not have to
re-read a scrollback to find out what it was asked, who is around, and
what work is in flight. It gathers:

  * who is online right now, with their status text
  * the room's topic and description
  * messages that @-mention this nick, newest first
  * the tail of the room, with your own lines marked
  * the job queue: what is open, what you hold, what is blocked or dead

Plain text by default (it reads fine in a terminal), `--json` for a
machine. The read cursor is NOT advanced unless you ask with --consume,
so asking for a brief never eats messages a `poll.py` on the same
machine has not seen yet.

Mentions that nothing from you follows are listed as unanswered — a hint,
not a verdict: this only reads the tail it was given, and a reply that
fell off the trim is genuinely gone.

Never prints the token.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    NICK, api_get, bus_key, bus_read, clean_room, own_prefixes,
    presence_list, seen_path, status_get_many)
import jobs  # noqa: E402

MENTION_RE = re.compile(r"(?<![A-Za-z0-9_-])@([A-Za-z0-9_-]+)")
BROADCAST = ("all", "everyone", "channel", "here")
DEFAULT_LIMIT = 30
DEFAULT_JOBS_LIMIT = 25


def mention_nicks(body):
    """Every @nick in a message body, lowercased, in order."""
    return [m.group(1).lower() for m in MENTION_RE.finditer(body or "")]


def addresses_me(body, me):
    """True when the body mentions any name in `me`, or everyone."""
    for n in mention_nicks(body):
        if n in BROADCAST or n in me:
            return True
    return False


def sender_of(body):
    """The nick a '<nick>: <text>' line came from, or '' when unprefixed."""
    head, sep, _ = (body or "").partition(":")
    return head.strip() if sep else ""


def topic_from(messages):
    """The room's standing topic: the newest TOPIC: line on the bus.

    Rooms carry a topic as a protocol message
    ('<nick>: TOPIC: <text>'), so it is read the same way every other
    reader in this repo reads it — off the bus, never from side state.
    """
    topic = ""
    for m in messages:
        if not isinstance(m, str):
            continue
        _, sep, rest = m.partition(": ")
        if sep and rest.startswith("TOPIC:"):
            topic = rest[len("TOPIC:"):].strip()
    return topic


def room_desc(key):
    """The room's description from its roominfo hash, or ''."""
    try:
        raw = api_get(f"hget/muse-bus:roominfo:{key}/desc")
        return json.loads(raw).get("result") or ""
    except Exception:
        return ""


def job_summary(limit):
    """Job counts plus the most interesting rows, in one request.

    Never fatal: a bus with no job queue (or a broken one) still gets a
    brief, with the jobs section reporting what went wrong.
    """
    try:
        rows = jobs.job_rows(limit=limit)
    except Exception as e:
        return {"error": str(e), "counts": {}, "rows": []}
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    interesting = [r for r in rows
                   if r["status"] != "done" or r["claim"] == NICK]
    return {"counts": counts, "rows": interesting[:limit]}


def build(args):
    """Assemble the brief as a dict (the shape --json prints)."""
    key = bus_key(clean_room(args.room))
    me = {p.lower() for p in own_prefixes()}
    if args.watch:
        me.update(n.lower() for n in args.watch)

    try:
        consumed = 0
        msgs, cursor, lost = bus_read(consumed, key=key)
    except Exception as e:
        raise SystemExit(f"RELAY_ERROR: {e}")

    lines = [m for m in msgs if isinstance(m, str)]
    tail = lines[-args.limit:]
    recent = []
    mentions = []
    for m in tail:
        who = sender_of(m)
        mine = who.lower() in me
        row = {"from": who, "text": m, "mine": mine,
               "mentions_me": (not mine) and addresses_me(m, me)}
        recent.append(row)
        if row["mentions_me"]:
            mentions.append(row)

    online = []
    try:
        nicks = presence_list()
        statuses = status_get_many(nicks)
        online = [{"nick": n, "status": statuses.get(n) or ""}
                  for n in nicks]
    except Exception as e:
        online = []
        note = f"presence unavailable ({e})"
    else:
        note = ""

    # A mention is 'unanswered' when no line of ours comes after it.
    last_mine = max((i for i, r in enumerate(recent) if r["mine"]),
                    default=-1)
    unanswered = [r for i, r in enumerate(recent)
                  if r["mentions_me"] and i > last_mine]

    return {
        "nick": NICK,
        "ts": int(time.time()),
        "room": args.room or "main",
        "key": key,
        "topic": topic_from(lines),
        "desc": room_desc(key),
        "online": online,
        "note": note,
        "lost": lost,
        "unread": len(msgs),
        "cursor": cursor,
        "mentions": mentions,
        "unanswered": unanswered,
        "recent": recent,
        "jobs": {} if args.no_jobs else job_summary(args.jobs_limit),
    }


def render(b):
    """Human-readable brief."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(b["ts"]))
    out = [f"BUS BRIEF — {b['nick']} — {when}",
           f"room: {b['room']}"
           + (f" — {b['desc']}" if b["desc"] else ""),
           f"topic: {b['topic']}" if b["topic"] else "topic: (none)",
           f"unread: {b['unread']}"]

    out.append("")
    if b["online"]:
        out.append(f"WHO'S ONLINE ({len(b['online'])})")
        for p in b["online"]:
            out.append(f"- {p['nick']}" + (f" ({p['status']})"
                                           if p["status"] else ""))
    else:
        out.append("WHO'S ONLINE (none)")
        if b["note"]:
            out.append(f"- {b['note']}")

    if b["unanswered"]:
        out.append("")
        out.append(f"WAITING ON YOU ({len(b['unanswered'])})")
        for r in b["unanswered"]:
            out.append(f"- {r['text']}")
    if b["mentions"]:
        out.append("")
        out.append(f"MENTIONS ({len(b['mentions'])})")
        for r in b["mentions"]:
            out.append(f"- {r['text']}")

    out.append("")
    if b["recent"]:
        out.append(f"RECENT (last {len(b['recent'])})")
        for r in b["recent"]:
            out.append(f"- {r['text']}" + ("   <- you" if r["mine"] else ""))
    else:
        out.append("RECENT (nothing)")

    j = b["jobs"]
    if j:
        out.append("")
        if j.get("error"):
            out.append(f"JOBS (unavailable: {j['error']})")
        else:
            counts = j["counts"] or {}
            if counts:
                summary = " | ".join(f"{k} {v}"
                                     for k, v in sorted(counts.items()))
            else:
                summary = "none"
            out.append(f"JOBS ({summary})")
            for r in j["rows"]:
                claim = r["claim"] or "-"
                out.append(f"- #{r['id']} {r['status']:<8} {claim:<10} "
                           f"{r['title']}")
    if b["lost"]:
        out.append("")
        out.append(f"WARNING: {b['lost']} message(s) were trimmed off the "
                   f"bus before this brief read them")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Assemble the context an instance needs right now.")
    ap.add_argument("--room", default=None,
                    help="room to brief on (default: main bus)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
                    help=f"recent messages to include (default "
                         f"{DEFAULT_LIMIT})")
    ap.add_argument("--watch", default=[], action="append", metavar="NICK",
                    help="an extra name that counts as you for mentions")
    ap.add_argument("--json", action="store_true",
                    help="print the brief as JSON instead of text")
    ap.add_argument("--consume", action="store_true",
                    help="advance this instance's read cursor (default: "
                         "peek only, so poll.py still sees these)")
    ap.add_argument("--no-jobs", action="store_true",
                    help="skip the job-queue section")
    ap.add_argument("--jobs-limit", type=int, default=DEFAULT_JOBS_LIMIT,
                    metavar="N", help=f"job rows to list (default "
                                      f"{DEFAULT_JOBS_LIMIT})")
    args = ap.parse_args(argv)

    if args.limit < 1:
        print("ERROR: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.jobs_limit < 1:
        print("ERROR: --jobs-limit must be at least 1", file=sys.stderr)
        return 2
    try:
        clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        b = build(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(b, sort_keys=True))
    else:
        print(render(b))
    if args.consume:
        # seen_path is the file poll.py and watch.py share, so --consume
        # means "this instance has read these" to the rest of the repo.
        try:
            with open(seen_path(clean_room(args.room)), "w") as f:
                f.write(str(b["cursor"]))
        except OSError as e:
            print(f"WARNING: couldn't write cursor ({e})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
