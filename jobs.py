#!/usr/bin/env python3
"""Job queue for crew builds on the muse-relay bus.

An overseer posts work; workers claim it atomically, report progress,
and finish with a commit hash. State lives in Redis; the bus carries
only short wire messages (JOB:/CLAIM:/DONE:/...) so specs never clog
chat. See ROADMAP.md ("the crew") for the full design.

Usage:
  jobs.py post --title T --spec S|--spec - [--accept A] [--branch B]
               [--base C] [--room R] [--lease SECS]
  jobs.py list [--status open|claimed|done|blocked|all]
  jobs.py show <id>
  jobs.py claim <id> [--room R] [--lease SECS]
  jobs.py heartbeat <id> [--lease SECS]
  jobs.py progress <id> --note TEXT [--room R]
  jobs.py done <id> --commit HASH [--note TEXT] [--room R]
  jobs.py blocked <id> --reason TEXT [--room R]
  jobs.py requeue <id> [--room R]
  jobs.py sweep [--room R]

Keys (MUSE_RELAY_JOBNS overrides the muse-bus:job prefix):
  <ns>seq            INCR job counter
  <ns>:<id>          hash: title/room/branch/base/accept/status/claim/...
  <ns>:<id>:spec     full spec text (POST body, never on the bus)
  <ns>:<id>:claim    claim mutex: SET NX EX <lease> (first claim wins)
  <ns>index          zset id -> created_at

Claiming is atomic via SET NX: exactly one worker wins the race. The
mutex carries a TTL (default 30 min); the worker renews it with
heartbeat/progress. If the mutex expires while the job is still
"claimed", any claim (or requeue/sweep) treats the job as abandoned and
reopens it. Wire messages (JOB:, CLAIM:, ...) are best-effort chat
announcements; the Redis state is the source of truth.

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
    INSTANCE_ID, NICK, api_get, api_post, bus_key, bus_push, bus_trim,
    clean_room, nick_holder, presence_beat)

NS = os.environ.get("MUSE_RELAY_JOBNS", "muse-bus:job")
LEASE_DEFAULT = 1800  # 30 minutes


def _q(s):
    return urllib.parse.quote(str(s), safe="")


def _seq_key():
    return f"{NS}seq"


def _job_key(jid):
    return f"{NS}:{jid}"


def _spec_key(jid):
    return f"{NS}:{jid}:spec"


def _claim_key(jid):
    return f"{NS}:{jid}:claim"


def _index_key():
    return f"{NS}index"


def _hgetall(key):
    # _hset URL-quotes field/value for the REST path; Upstash decodes each
    # path segment before storing, so what comes back is already the
    # original value. Do NOT unquote again here — a second decode would
    # corrupt any literal "%XX" text in a stored value.
    raw = json.loads(api_get(f"hgetall/{key}"))["result"] or []
    it = iter(raw)
    return dict(zip(it, it))


def _hset(key, mapping):
    pairs = []
    for f, v in mapping.items():
        if v is None or v == "":
            continue  # empty path segments 400 on Upstash REST
        pairs.append(f"{_q(f)}/{_q(v)}")
    if pairs:
        api_get(f"hset/{key}/{'/'.join(pairs)}")


def _hdel(key, *fields):
    if fields:
        api_get(f"hdel/{key}/" + "/".join(_q(f) for f in fields))


def _valid_id(jid):
    return jid.isdigit() and int(jid) > 0


def job_get(jid):
    """Return (fields dict, spec str) or (None, None) for unknown id."""
    if not _valid_id(jid):
        return None, None
    h = _hgetall(_job_key(jid))
    if not h:
        return None, None
    try:
        spec = json.loads(api_get(f"get/{_spec_key(jid)}"))["result"] or ""
    except Exception:
        spec = ""
    return h, spec


def _creator_warning(h):
    """Warn when the job poster's nick is now held by someone else."""
    poster, host = h.get("created_by"), h.get("created_host")
    if not poster or not host:
        return None
    holder = nick_holder(poster)
    if holder and holder != host:
        return (f"WARNING: job was posted by '{poster}' on host '{host}' "
                f"but that nick is now claimed by '{holder}' — verify "
                f"before working it")
    return None


def _announce(room, text):
    """Best-effort wire message; never fails the state change."""
    try:
        key = bus_key(room)
        bus_push(f"{NICK}: {text}", key=key)
        bus_trim(key=key)
        presence_beat(key)
    except Exception as e:
        print(f"WARNING: announce failed ({e})", file=sys.stderr)


def _mutex_holder(jid):
    try:
        return json.loads(api_get(f"get/{_claim_key(jid)}"))["result"]
    except Exception:
        return "?"


def _requeue_fields(jid, h, room):
    """Reset a claimed job to open (lease expired or overseer override)."""
    _hdel(_job_key(jid), "claim", "claim_host", "claimed_at", "lease_until")
    _hset(_job_key(jid), {"status": "open"})
    try:
        api_get(f"del/{_claim_key(jid)}")
    except Exception:
        pass
    _announce(room, f"REQUEUE:{jid}")


def _claim_or_requeue(jid, h):
    """If h is claimed but its mutex is gone, the lease expired: reopen.

    Returns the (possibly refreshed) fields dict."""
    if h.get("status") == "claimed" and _mutex_holder(jid) is None:
        _requeue_fields(jid, h, h.get("room") or "")
        h, _ = job_get(jid)
    return h


def cmd_post(args):
    spec = (sys.stdin.read() if args.spec == "-"
            else args.spec)
    if not spec.strip():
        print("ERROR: --spec is empty", file=sys.stderr)
        return 2
    try:
        room = clean_room(args.room)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    lease = args.lease
    if lease < 60:
        print("ERROR: --lease needs at least 60s", file=sys.stderr)
        return 2
    try:
        jid = str(json.loads(api_get(f"incr/{_seq_key()}"))["result"])
        now = int(time.time())
        _hset(_job_key(jid), {
            "title": args.title, "room": room, "branch": args.branch,
            "base": args.base, "accept": args.accept, "status": "open",
            "lease": lease, "created_by": NICK, "created_host": INSTANCE_ID,
            "created_at": now,
        })
        api_post(f"set/{_spec_key(jid)}", spec)
        api_get(f"zadd/{_index_key()}/{now}/{jid}")
    except Exception as e:
        print(f"RELAY_ERROR: post failed ({e})", file=sys.stderr)
        return 2
    _announce(room, f"JOB:{jid} {args.title}")
    print(f"JOB {jid}")
    return 0


def cmd_list(args):
    try:
        ids = json.loads(api_get(f"zrange/{_index_key()}/0/-1"))["result"] or []
    except Exception as e:
        print(f"RELAY_ERROR: list failed ({e})", file=sys.stderr)
        return 2
    rows = []
    for jid in ids:
        h = _hgetall(_job_key(jid))
        if not h:
            continue
        status = h.get("status", "?")
        if args.status != "all" and status != args.status:
            continue
        rows.append((jid, status, h.get("claim", "-"), h.get("title", "")))
    if not rows:
        print("NO_JOBS")
        return 0
    print(f"{'ID':<6}{'STATUS':<10}{'CLAIM':<12}TITLE")
    for jid, status, claim, title in rows:
        print(f"{jid:<6}{status:<10}{claim:<12}{title}")
    return 0


def cmd_show(args):
    h, spec = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    for f in ("title", "status", "room", "branch", "base", "accept",
              "claim", "claim_host", "claimed_at", "lease_until",
              "result_commit", "note", "created_by", "created_host",
              "created_at", "lease"):
        if h.get(f):
            print(f"{f}: {h[f]}")
    w = _creator_warning(h)
    if w:
        print(w, file=sys.stderr)
    print("--- spec ---")
    print(spec)
    return 0


def _require_claimer(h, jid):
    if h.get("claim") != NICK:
        print(f"ERROR: job {jid} is claimed by '{h.get('claim', '?')}', "
              f"not you", file=sys.stderr)
        return False
    return True


def cmd_claim(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    h = _claim_or_requeue(args.id, h)
    if h.get("status") != "open":
        print(f"ERROR: job {args.id} is {h.get('status')}"
              + (f" (claimed by {h.get('claim')})"
                 if h.get("status") == "claimed" else ""),
              file=sys.stderr)
        return 2
    lease = args.lease
    if lease < 60:
        print("ERROR: --lease needs at least 60s", file=sys.stderr)
        return 2
    try:
        r = json.loads(api_get(
            f"set/{_claim_key(args.id)}/{_q(NICK)}/NX/EX/{lease}"))["result"]
    except Exception as e:
        print(f"RELAY_ERROR: claim failed ({e})", file=sys.stderr)
        return 2
    if r != "OK":
        print(f"ERROR: lost the race — job {args.id} claimed by "
              f"'{_mutex_holder(args.id)}'", file=sys.stderr)
        return 2
    now = int(time.time())
    try:
        _hset(_job_key(args.id), {
            "status": "claimed", "claim": NICK, "claim_host": INSTANCE_ID,
            "claimed_at": now, "lease_until": now + lease, "lease": lease,
        })
    except Exception as e:
        # Roll back the mutex so the job stays open and claimable instead
        # of claimed-but-unworkable until the lease expires.
        # Best-effort: never mask the original error.
        try:
            api_get(f"del/{_claim_key(args.id)}")
        except Exception:
            pass
        print(f"RELAY_ERROR: claim record failed ({e})", file=sys.stderr)
        return 2
    w = _creator_warning(h)
    if w:
        print(w, file=sys.stderr)
    _announce(args.room or h.get("room") or "", f"CLAIM:{args.id} by {NICK}")
    print(f"CLAIMED {args.id} by {NICK}")
    return 0


def _renew_lease(jid, h, lease):
    """Renew the claim mutex; returns True on success."""
    ck = _claim_key(jid)
    try:
        r = json.loads(
            api_get(f"set/{ck}/{_q(NICK)}/XX/EX/{lease}"))["result"]
        if r != "OK":  # mutex expired out from under us; re-acquire
            r = json.loads(
                api_get(f"set/{ck}/{_q(NICK)}/NX/EX/{lease}"))["result"]
        if r != "OK":
            return False
        _hset(_job_key(jid), {"lease_until": int(time.time()) + lease,
                              "lease": lease})
        return True
    except Exception:
        return False


def cmd_heartbeat(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if not _require_claimer(h, args.id):
        return 2
    lease = args.lease or int(h.get("lease", LEASE_DEFAULT))
    if _renew_lease(args.id, h, lease):
        print(f"LEASE_RENEWED {args.id} {lease}s")
        return 0
    print(f"ERROR: lost the lease on job {args.id}", file=sys.stderr)
    return 2


def cmd_progress(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if not _require_claimer(h, args.id):
        return 2
    lease = int(h.get("lease", LEASE_DEFAULT))
    if not _renew_lease(args.id, h, lease):
        print(f"ERROR: lost the lease on job {args.id}", file=sys.stderr)
        return 2
    try:
        _hset(_job_key(args.id), {"note": args.note})
    except Exception as e:
        print(f"RELAY_ERROR: note save failed ({e})", file=sys.stderr)
        return 2
    _announce(args.room or h.get("room") or "", f"PROGRESS:{args.id} {args.note}")
    print(f"PROGRESS {args.id}")
    return 0


def cmd_done(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if not _require_claimer(h, args.id):
        return 2
    try:
        _hset(_job_key(args.id), {
            "status": "done", "result_commit": args.commit,
            "note": args.note, "done_at": int(time.time()),
        })
        _hdel(_job_key(args.id), "claim", "claim_host", "claimed_at",
              "lease_until")
        api_get(f"del/{_claim_key(args.id)}")
    except Exception as e:
        print(f"RELAY_ERROR: done failed ({e})", file=sys.stderr)
        return 2
    _announce(args.room or h.get("room") or "", f"DONE:{args.id} {args.commit}")
    print(f"DONE {args.id} {args.commit}")
    return 0


def cmd_blocked(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if not _require_claimer(h, args.id):
        return 2
    try:
        _hset(_job_key(args.id), {"status": "blocked", "note": args.reason})
        _hdel(_job_key(args.id), "claim", "claim_host", "claimed_at",
              "lease_until")
        api_get(f"del/{_claim_key(args.id)}")
    except Exception as e:
        print(f"RELAY_ERROR: blocked failed ({e})", file=sys.stderr)
        return 2
    _announce(args.room or h.get("room") or "", f"BLOCKED:{args.id} {args.reason}")
    print(f"BLOCKED {args.id}")
    return 0


def cmd_requeue(args):
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if h.get("status") != "claimed":
        print(f"ERROR: job {args.id} is {h.get('status')}, not claimed",
              file=sys.stderr)
        return 2
    if _mutex_holder(args.id) is not None:
        print(f"ERROR: lease on job {args.id} still active "
              f"(held by '{h.get('claim')}')", file=sys.stderr)
        return 2
    try:
        _requeue_fields(args.id, h, args.room or h.get("room") or "")
    except Exception as e:
        print(f"RELAY_ERROR: requeue failed ({e})", file=sys.stderr)
        return 2
    print(f"REQUEUED {args.id}")
    return 0


def cmd_sweep(args):
    try:
        ids = json.loads(api_get(f"zrange/{_index_key()}/0/-1"))["result"] or []
    except Exception as e:
        print(f"RELAY_ERROR: sweep failed ({e})", file=sys.stderr)
        return 2
    n = 0
    for jid in ids:
        h, _ = job_get(jid)
        if h and h.get("status") == "claimed" and \
                _mutex_holder(jid) is None:
            try:
                _requeue_fields(jid, h, args.room or h.get("room") or "")
                print(f"REQUEUED {jid}")
                n += 1
            except Exception as e:
                print(f"WARNING: requeue {jid} failed ({e})",
                      file=sys.stderr)
    if not n:
        print("SWEEP_CLEAN")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobs.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post", help="post a job")
    p.add_argument("--title", required=True)
    p.add_argument("--spec", required=True,
                   help="spec text ('-' = stdin)")
    p.add_argument("--accept", default="", help="acceptance criteria")
    p.add_argument("--branch", default="", help="worker branch name")
    p.add_argument("--base", default="", help="base commit")
    p.add_argument("--room", default="", help="announce room")
    p.add_argument("--lease", type=int, default=LEASE_DEFAULT)
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--status", default="all",
                   choices=["all", "open", "claimed", "done", "blocked"])
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show a job and its spec")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomically claim a job")
    p.add_argument("id")
    p.add_argument("--room", default="", help="announce room override")
    p.add_argument("--lease", type=int, default=LEASE_DEFAULT)
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("heartbeat", help="renew the claim lease")
    p.add_argument("id")
    p.add_argument("--lease", type=int, default=0,
                   help="new lease secs (0 = keep current)")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("progress", help="note progress + renew lease")
    p.add_argument("id")
    p.add_argument("--note", required=True)
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_progress)

    p = sub.add_parser("done", help="finish a job with a commit hash")
    p.add_argument("id")
    p.add_argument("--commit", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("blocked", help="mark a job blocked, release it")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_blocked)

    p = sub.add_parser("requeue", help="reopen a lease-expired claim")
    p.add_argument("id")
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_requeue)

    p = sub.add_parser("sweep",
                       help="requeue all lease-expired claims")
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_sweep)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
