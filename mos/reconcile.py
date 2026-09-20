#!/usr/bin/env python3
"""Restart/failover recovery for MOS. Idempotent — safe to run at every
overseer/worker startup and on a schedule.

Does three things:
  1. Requeues jobs whose claim lease expired (via jobs sweep).
  2. Drops capability registrations whose heartbeat went stale.
  3. Reports anything needing a human: done-but-unverified jobs and
     blocked jobs.

Returns a report dict; also prints a human-readable summary.
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
from mos import capabilities  # noqa: E402


def redis_ok():
    """True when the relay backend answers."""
    try:
        rc.api_get("ping")
        return True
    except Exception:
        return False


def sweep_leases():
    """Requeue lease-expired claims. Returns [jid]."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        jobs.main(["sweep"])
    out = []
    for line in buf.getvalue().splitlines():
        if line.startswith("REQUEUED "):
            out.append(line.split()[1])
    return out


def done_unverified():
    """Ids of jobs done but not yet acceptance-verified."""
    try:
        ids = json.loads(
            rc.api_get(f"zrange/{jobs._index_key()}/0/-1"))["result"] or []
    except Exception:
        return []
    return [jid for jid in ids
            if (lambda h: h.get("status") == "done"
                and h.get("verified") != "1")
            (jobs._hgetall(jobs._job_key(jid)))]


def blocked_jobs():
    """Ids of jobs currently blocked, with their reasons."""
    try:
        ids = json.loads(
            rc.api_get(f"zrange/{jobs._index_key()}/0/-1"))["result"] or []
    except Exception:
        return []
    out = []
    for jid in ids:
        h = jobs._hgetall(jobs._job_key(jid))
        if h.get("status") == "blocked":
            out.append((jid, h.get("note", "")[:200]))
    return out


def reconcile():
    """Run full recovery. Returns a report dict."""
    report = {"redis": redis_ok(), "requeued": [], "pruned": [],
              "unverified": [], "blocked": []}
    if not report["redis"]:
        return report
    try:
        report["requeued"] = sweep_leases()
    except Exception as e:
        print(f"WARNING: sweep failed ({e})", file=sys.stderr)
    try:
        report["pruned"] = capabilities.prune_stale()
    except Exception as e:
        print(f"WARNING: prune failed ({e})", file=sys.stderr)
    report["unverified"] = done_unverified()
    report["blocked"] = blocked_jobs()
    return report


def main(argv=None):
    rep = reconcile()
    print(f"redis: {'OK' if rep['redis'] else 'UNREACHABLE'}")
    for jid in rep["requeued"]:
        print(f"REQUEUED {jid}")
    for nick in rep["pruned"]:
        print(f"PRUNED {nick}")
    for jid in rep["unverified"]:
        print(f"UNVERIFIED {jid}")
    for jid, reason in rep["blocked"]:
        print(f"BLOCKED {jid}: {reason}")
    if not any([rep["requeued"], rep["pruned"], rep["unverified"],
                rep["blocked"]]):
        print("RECONCILE_CLEAN")
    return 0 if rep["redis"] else 2


if __name__ == "__main__":
    sys.exit(main())
