#!/usr/bin/env python3
"""Listen for bus messages and wake this instance the moment they arrive.

Usage: listen.py [--room NAME]... [--mentions] [--hook CMD]
                 [--once] [--timeout SECS] [--interval SECS]
                 [--keepalive SECS] [--json] [--no-catch-up] [--count N]

The bus has no push of its own: every script in this repo asks the REST
API "anything new?" on a timer, and a message sits unseen until the next
tick. This script is the way out of that. Upstash serves PUBLISH over
REST but SUBSCRIBE only over a TCP connection, so the sender rings a
doorbell (`relay_common.bus_send` publishes) and this holds the other end
open with nothing but the standard library (`resp.py`) — no dependencies,
no polling, no commands burned while idle.

    # one Muse, woken for every message, its agent run on each one
    python3 listen.py --hook './wake-agent.sh'

    # heads-down mode: only when someone says @milo
    python3 listen.py --mentions --hook './wake-agent.sh'

    # what a listener would receive, without acting on it
    python3 listen.py --once --timeout 30

Pub/sub keeps NO history: a message published while this was
disconnected is already gone. So on every connect — and every reconnect
— this also drains the room list from an absolute cursor
(`relay_common.bus_read`) and replays what it missed, marked
`"replay": true` in the payload. Pub/sub for speed, the list for
correctness: nothing is silently lost either way, and a gap big enough
for `LTRIM` to outrun the cursor is reported instead of hidden.

With --hook CMD, each message is handed to CMD as JSON on stdin and in
the environment (MUSE_RELAY_FROM / _TEXT / _ROOM / _REPLAY / ...), so an
agent can act on the ping rather than a human reading a scrollback. A
hook that fails or times out is reported and stepped over; it never
stops the listener.

Without a TCP URL (MUSE_RELAY_TCP_URL) this degrades to polling the list
on --interval, which needs only the REST credentials you already have.
It says so on stderr, because a poll loop and a subscription are not the
same thing and you should know which one you are running.

Never prints the token.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    BUS, NICK, bus_key, bus_read, clean_room, live_channel, live_path,
    own_prefixes)
import resp  # noqa: E402

# @nick, matching bus.html: a mention is a word boundary, then @, then a
# nick. Kept in step with the viewer so a highlight and a wake agree.
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_-])@([A-Za-z0-9_-]+)")

# Mentions that mean "everybody on the bus", not one nick.
BROADCAST = ("all", "everyone", "channel", "here")

DEFAULT_INTERVAL = 5.0       # poll fallback: seconds between list reads
DEFAULT_KEEPALIVE = 30.0     # seconds between PINGs on the TCP link
DEFAULT_HOOK_TIMEOUT = 300.0
MAX_BACKOFF = 30.0


def _on_sigterm(signum, frame):
    """Let `kill` end the listener the same clean way Ctrl-C does."""
    raise KeyboardInterrupt


def mention_nicks(body):
    """Every @nick in a message body, in order, lowercased."""
    return [m.group(1).lower() for m in MENTION_RE.finditer(body or "")]


def mentions_me(body, me):
    """True when the body mentions any name in `me`, or everyone."""
    for n in mention_nicks(body):
        if n in BROADCAST or n in me:
            return True
    return False


def sender_of(body):
    """The nick a '<nick>: <text>' bus line came from, or '' when it has
    no prefix. Same convention every other reader in this repo uses."""
    head, sep, _ = (body or "").partition(":")
    if not sep:
        return ""
    return head.strip()


def room_label(key):
    """'muse-bus' -> 'main', 'muse-bus:room:eng' -> 'eng'."""
    if key == BUS or not key.startswith(BUS + ":room:"):
        return "main" if key == BUS else key
    return key[len(BUS + ":room:"):]


def make_event(key, body, channel="", replay=False, me=()):
    """One delivered message, as it goes out to --json and to the hook."""
    return {
        "event": "live",
        "kind": "msg",
        "channel": channel,
        "key": key,
        "room": room_label(key),
        "from": sender_of(body),
        "text": body,
        "mentions": mention_nicks(body),
        "mentions_me": mentions_me(body, set(me)),
        "replay": bool(replay),
        "ts": int(time.time()),
    }


def parse_envelope(raw, channel):
    """Decode a live-channel payload into an event dict, or None.

    Anything that is not this repo's envelope (another publisher sharing
    the channel, a truncated frame) reads as None and is skipped rather
    than crashing the listener.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("room")
    body = data.get("body")
    if not isinstance(key, str) or not isinstance(body, str):
        return None
    return {
        "event": "live",
        "kind": data.get("kind") or "msg",
        "channel": channel,
        "key": key,
        "room": room_label(key),
        "from": sender_of(body),
        "text": body,
        "ts": data.get("ts") or int(time.time()),
    }


def format_line(event):
    """Human line for one event."""
    tag = "REPLAY" if event.get("replay") else "LIVE"
    return f"{tag} [{event['room']}] {event['text']}"


def file_cursor(path):
    """Read an absolute read cursor from a file; 0 when absent or bad."""
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError, OSError):
        return 0


def write_cursor(path, value):
    """Best-effort cursor write; never breaks the listener."""
    try:
        with open(path, "w") as f:
            f.write(str(int(value)))
    except OSError as e:
        print(f"WARNING: couldn't write cursor {path} ({e})", file=sys.stderr)


def run_hook(cmd, event, timeout):
    """Hand one event to the instance's agent command.

    The event goes to stdin as one JSON line and to the environment, so a
    hook can be a shell one-liner or a full agent. Returns the exit code,
    or None when it failed to start or ran past `timeout`. Never raises:
    a broken hook must not take the listener down with it.
    """
    env = dict(os.environ)
    env.update({
        "MUSE_RELAY_EVENT": "live",
        "MUSE_RELAY_CHANNEL": str(event.get("channel") or ""),
        "MUSE_RELAY_KEY": event["key"],
        "MUSE_RELAY_ROOM": event["room"],
        "MUSE_RELAY_FROM": event["from"],
        "MUSE_RELAY_TEXT": event["text"],
        "MUSE_RELAY_REPLAY": "1" if event.get("replay") else "0",
        "MUSE_RELAY_TS": str(event["ts"]),
    })
    try:
        p = subprocess.run(cmd, shell=True, env=env, timeout=timeout,
                           input=json.dumps(event) + "\n", text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        print(f"HOOK_TIMEOUT after {timeout:g}s: {event['text'][:80]}",
              file=sys.stderr)
        return None
    except Exception as e:
        print(f"HOOK_ERROR {e}", file=sys.stderr)
        return None
    if p.stdout:
        sys.stdout.write(p.stdout)
        sys.stdout.flush()
    if p.stderr:
        sys.stderr.write(p.stderr)
        sys.stderr.flush()
    if p.returncode != 0:
        print(f"HOOK_EXIT {p.returncode}: {event['text'][:80]}",
              file=sys.stderr)
    return p.returncode


class Listener:
    """Delivery policy: filter, dedupe replays, print, hand to the hook."""

    def __init__(self, args):
        self.args = args
        # own_prefixes() is the canonical nick plus any display name a
        # rename gave it; mentions are compared case-insensitively.
        self.me = {p.lower() for p in own_prefixes()}
        if args.watch:
            self.me.update(n.lower() for n in args.watch)
        self.rooms = [bus_key(clean_room(r)) for r in (args.room or [])]
        self.delivered = 0
        # Bodies already delivered as a catch-up replay. A push that
        # duplicates one of them is the same message arriving twice (list
        # read + doorbell) and is dropped. send.py refuses to post the
        # same body twice in a row, so this cannot hide a real repeat.
        self.replayed = []
        self.hooked = True   # flips off after a hook failure, see deliver()
        # True when a catch-up read failed. A --once pass that could not
        # read the list cannot claim to have seen everything, so it exits
        # non-zero rather than reporting the same success as a quiet bus
        # (the REST token and the TCP URL are separate credentials, so
        # this is a plausible misconfiguration, not a freak one).
        self.catchup_failed = False

    # -- filtering ------------------------------------------------------
    def wanted(self, event):
        if self.rooms and event["key"] not in self.rooms:
            return False
        if self.args.mentions:
            return event.get("mentions_me") or mentions_me(event["text"],
                                                           self.me)
        return True

    def seen_replay(self, body):
        """True when this body was already delivered as a replay."""
        if not self.replayed:
            return False
        try:
            self.replayed.remove(body)
        except ValueError:
            return False
        return True

    # -- delivery -------------------------------------------------------
    def deliver(self, event):
        event["mentions"] = event.get("mentions") or mention_nicks(
            event["text"])
        event["mentions_me"] = event.get("mentions_me") or mentions_me(
            event["text"], self.me)
        if self.args.json:
            print(json.dumps(event, sort_keys=True))
        else:
            print(format_line(event))
        sys.stdout.flush()
        self.delivered += 1
        if self.args.hook and self.hooked:
            rc = run_hook(self.args.hook, event, self.args.hook_timeout)
            if rc is None:
                # A hook that cannot run will not run on the next message
                # either. Say so once and keep listening as a plain feed.
                print("NOTE: --hook disabled after a failure; still "
                      "listening", file=sys.stderr)
                self.hooked = False


def once_exit(listener, args):
    """Exit code for a finished --once pass.

    0 when the pass was clean — messages delivered, or a genuinely quiet
    bus — and 1 when it could not read the list, because then "nothing
    arrived" and "I could not look" are different answers and a wrapper
    script must be able to tell them apart.
    """
    if listener.catchup_failed:
        return 1
    return 0


def followed_keys(args):
    """Bus list keys this listener follows: --room each, else main."""
    return [bus_key(clean_room(r)) for r in args.room] or [BUS]


def catch_up_all(listener, args):
    """Replay every followed room from its own cursor file.

    Returns the replayed bodies so pushes that duplicate them can be
    dropped (the same message arriving twice: once from the list, once
    through the doorbell).
    """
    bodies = []
    for key in followed_keys(args):
        label = room_label(key)
        path = args.cursor or live_path(None if key == BUS else label)
        bodies.extend(catch_up(listener, path, key, label))
    return bodies


def catch_up(listener, cursor_file, key, label):
    """Replay messages the subscription missed, from the list cursor.

    Pub/sub has no history, so this is what makes the live path safe to
    use at all: a message posted during a disconnect is still on the list
    and still gets delivered. Advances the cursor and returns the bodies
    delivered, which the caller keeps for replay/push dedupe.
    """
    seen = file_cursor(cursor_file)
    try:
        msgs, cursor, lost = bus_read(seen, key=key)
    except Exception as e:
        print(f"CATCHUP_FAILED {label}: {e}", file=sys.stderr)
        listener.catchup_failed = True
        return []
    if lost:
        print(f"WARNING: {lost} message(s) in {label} were trimmed off the "
              f"bus before this listener read them (raise MUSE_RELAY_KEEP "
              f"or run a listener instead of polling)", file=sys.stderr)
    bodies = []
    for m in msgs:
        if not isinstance(m, str):
            continue
        event = make_event(key, m, channel=live_channel(), replay=True,
                           me=listener.me)
        bodies.append(m)
        if listener.wanted(event):
            listener.deliver(event)
    write_cursor(cursor_file, cursor)
    return bodies


def listen_tcp(listener, args, url):
    """Hold a subscription and deliver pushes until told to stop."""
    backoff = 1.0
    while True:
        sub = resp.Subscriber(url, [live_channel()])
        try:
            n = sub.connect()
        except resp.RespError as e:
            sub.close()
            if args.once:
                print(f"LISTEN_FAILED {e}", file=sys.stderr)
                return 2
            print(f"LISTEN_RETRY {e}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)
            continue
        backoff = 1.0
        print(f"LISTENING {live_channel()} as {NICK} "
              f"(tcp, {n} subscription(s); rooms: "
              f"{', '.join(room_label(k) for k in followed_keys(args))})")
        sys.stdout.flush()
        if not args.no_catch_up:
            listener.replayed.extend(catch_up_all(listener, args))
        # Deadlines and keepalives run on the monotonic clock: wall time
        # can jump (NTP, DST) and a listener must not care.
        deadline = (time.monotonic() + args.timeout) if args.once else None
        last_ping = time.monotonic()
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return once_exit(listener, args)
                slice_s = args.keepalive
                if deadline is not None:
                    slice_s = min(slice_s,
                                  max(0.05, deadline - time.monotonic()))
                event = sub.next_event(slice_s)
                if event is None:
                    if deadline is not None and time.monotonic() >= deadline:
                        return once_exit(listener, args)
                    if time.monotonic() - last_ping >= args.keepalive:
                        sub.ping()
                        last_ping = time.monotonic()
                    continue
                kind, channel, payload = event
                if kind != "message":
                    continue          # pong / subscribe bookkeeping
                last_ping = time.monotonic()
                ev = parse_envelope(payload, channel)
                if ev is None:
                    continue
                if not listener.wanted(ev):
                    continue
                if listener.seen_replay(ev["text"]):
                    continue          # already delivered by the catch-up
                listener.deliver(ev)
                if args.count and listener.delivered >= args.count:
                    return 0
        except resp.RespError as e:
            if args.once:
                # The one pass asked for ended on a broken link. That is
                # a failure, not a quiet bus.
                print(f"LISTEN_FAILED {e}", file=sys.stderr)
                return 1
            print(f"LISTEN_RETRY {e}", file=sys.stderr)
        except KeyboardInterrupt:
            return 0
        finally:
            sub.close()
        time.sleep(backoff)


def listen_poll(listener, args):
    """Fallback: read the list on an interval, no subscription needed.

    Same deliveries, same cursors, same filters as the subscription mode
    — only the transport differs, and this one costs a request per tick
    forever instead of nothing while idle.
    """
    keys = followed_keys(args)
    print(f"LISTENING {', '.join(room_label(k) for k in keys)} as {NICK} "
          f"(polling every {args.interval:g}s — set MUSE_RELAY_TCP_URL for "
          f"push)", file=sys.stderr)
    while True:
        for key in keys:
            label = room_label(key)
            cursor_file = args.cursor or live_path(
                None if key == BUS else label)
            seen = file_cursor(cursor_file)
            try:
                msgs, cursor, lost = bus_read(seen, key=key)
            except Exception as e:
                print(f"LISTEN_RETRY {e}", file=sys.stderr)
                if args.once:
                    # Nothing was read: report it, don't exit 0.
                    print(f"LISTEN_FAILED {label} unreadable",
                          file=sys.stderr)
                    return 1
                time.sleep(max(1.0, args.interval))
                continue
            if lost:
                print(f"WARNING: {lost} message(s) in {label} were trimmed "
                      f"off the bus before this listener read them",
                      file=sys.stderr)
            for m in msgs:
                if not isinstance(m, str):
                    continue
                ev = make_event(key, m, channel="", me=listener.me)
                if listener.wanted(ev):
                    listener.deliver(ev)
                    if args.count and listener.delivered >= args.count:
                        return 0
            write_cursor(cursor_file, cursor)
        if args.once:
            return 0
        time.sleep(max(0.05, args.interval))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Wake this instance when a message lands on the bus.")
    ap.add_argument("--room", default=[], action="append", metavar="NAME",
                    help="only deliver messages for this room (repeatable; "
                         "default: every room on the channel)")
    ap.add_argument("--mentions", action="store_true",
                    help="only deliver messages that mention this nick "
                         "(or @all) — the heads-down mode")
    ap.add_argument("--watch", default=[], action="append", metavar="NICK",
                    help="an extra name that counts as you for --mentions")
    ap.add_argument("--hook", default=None, metavar="CMD",
                    help="run CMD for each message; it gets the event as "
                         "JSON on stdin and in the environment")
    ap.add_argument("--hook-timeout", type=float,
                    default=DEFAULT_HOOK_TIMEOUT, metavar="SECS",
                    help=f"kill a hook that runs longer (default "
                         f"{DEFAULT_HOOK_TIMEOUT:g})")
    ap.add_argument("--url", default=None, metavar="URL",
                    help="redis TCP URL for the subscription "
                         "(default: $MUSE_RELAY_TCP_URL). Without one this "
                         "polls the list instead.")
    ap.add_argument("--cursor", default=None, metavar="PATH",
                    help="read-cursor file for the catch-up replay "
                         "(default: live-seen<room>.txt next to the "
                         "scripts)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    metavar="SECS",
                    help=f"poll interval in the fallback mode (default "
                         f"{DEFAULT_INTERVAL:g})")
    ap.add_argument("--keepalive", type=float, default=DEFAULT_KEEPALIVE,
                    metavar="SECS",
                    help=f"seconds between PINGs on the TCP link (default "
                         f"{DEFAULT_KEEPALIVE:g})")
    ap.add_argument("--once", action="store_true",
                    help="deliver what arrives, then exit (for scripts "
                         "and tests)")
    ap.add_argument("--timeout", type=float, default=5.0, metavar="SECS",
                    help="how long --once waits for a message (default 5)")
    ap.add_argument("--count", type=int, default=0, metavar="N",
                    help="stop after delivering N messages")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON event per line instead of a "
                         "human line")
    ap.add_argument("--no-catch-up", action="store_true",
                    help="skip the list replay on connect (live pushes "
                         "only — anything missed while down is lost)")
    args = ap.parse_args(argv)

    for name, value in (("--interval", args.interval),
                        ("--keepalive", args.keepalive),
                        ("--timeout", args.timeout),
                        ("--hook-timeout", args.hook_timeout)):
        if value <= 0:
            print(f"ERROR: {name} must be positive", file=sys.stderr)
            return 2
    try:
        for r in args.room:
            clean_room(r)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass   # not the main thread, or no signals here

    listener = Listener(args)
    url = args.url or os.environ.get("MUSE_RELAY_TCP_URL", "").strip()
    try:
        if url:
            return listen_tcp(listener, args, url)
        print("NOTE: no MUSE_RELAY_TCP_URL set — polling the list instead "
              "of holding a subscription", file=sys.stderr)
        return listen_poll(listener, args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
