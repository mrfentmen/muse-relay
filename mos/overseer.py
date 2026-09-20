#!/usr/bin/env python3
"""The MOS overseer: dispatches work, verifies results, requeues failures.

One overseer per crew. It:
  - posts jobs (via jobs.py) and DMs capable workers (via send.py --dm),
  - runs `tick()` periodically: sweeps expired leases, prunes stale
    capability registrations, and verifies done-but-unverified jobs,
  - verifies a done job's machine-readable acceptance criteria against
    the worker's branch before the job counts as truly done,
  - requeues jobs whose acceptance fails, with the failure report as note.

All state stays in Redis; the bus carries announcements only.
"""
import contextlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
import send  # noqa: E402
from mos import acceptance, capabilities  # noqa: E402
from mos import gitops  # noqa: E402

LEASE_DEFAULT = 1800


def _say(room, text):
    """Best-effort announcement; never fails the state change."""
    try:
        key = rc.bus_key(room)
        rc.bus_push(f"{rc.NICK}: {text}", key=key)
        rc.bus_trim(key=key)
        rc.presence_beat(key)
    except Exception as e:
        print(f"WARNING: announce failed ({e})", file=sys.stderr)


def _post_job(title, spec, accept, room, lease, branch, base):
    """Post via jobs.main, capturing the new job id."""
    argv = ["post", "--title", title, "--spec", spec, "--lease", str(lease)]
    if accept:
        argv += ["--accept", accept]
    if room:
        argv += ["--room", room]
    if branch:
        argv += ["--branch", branch]
    if base:
        argv += ["--base", base]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_code = jobs.main(argv)
    if rc_code != 0:
        raise RuntimeError(f"job post failed: {buf.getvalue()}")
    for line in buf.getvalue().splitlines():
        if line.startswith("JOB "):
            return line.split()[1]
    raise RuntimeError(f"could not parse job id from: {buf.getvalue()!r}")


def dispatch(title, spec, accept="", requirements=(), room="",
             lease=LEASE_DEFAULT, branch="", base=""):
    """Post a job and DM every capable worker.

    Returns (jid, dm_results) where dm_results is
    [(nick, dm_ok, detail)].
    """
    if not spec or not spec.strip():
        raise ValueError("spec must not be empty")
    reqs = [r.strip().lower() for r in requirements if r and r.strip()]
    jid = _post_job(title, spec, accept, room, lease, branch, base)
    dm_results = []
    for nick in capabilities.find_for(reqs):
        rec = capabilities.get(nick) or {}
        secret = rec.get("dm", "")
        if not secret:
            dm_results.append((nick, False, "no DM secret registered"))
            continue
        text = f"JOB:{jid} {title}"
        if reqs:
            text += f" req={','.join(reqs)}"
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            rc_code = send.main(["--dm", secret, text])
        ok = rc_code == 0
        dm_results.append(
            (nick, ok, buf.getvalue().strip() or err.getvalue().strip()))
    _say(room, f"DISPATCH:{jid} -> {len(dm_results)} worker(s)")
    return jid, dm_results


def _set_verified(jid, ok, report):
    jobs._hset(jobs._job_key(jid), {
        "verified": "1" if ok else "0",
        "verified_at": str(int(time.time())),
        "verify_report": report[-2000:],
    })


def verify_done(jid, repo=None, workdir_base=None):
    """Verify a done job's acceptance criteria.

    Checks out the worker's branch (via git worktree) when repo/branch
    are available; otherwise verifies against `repo` directly.
    On success marks verified=1. On failure requeues the job to open
    with the failure report as its note.

    Returns (ok, report_text).
    """
    h, _ = jobs.job_get(jid)
    if h is None:
        return False, f"no such job: {jid}"
    if h.get("status") != "done":
        return False, f"job {jid} is {h.get('status')}, not done"
    if h.get("verified") == "1":
        return True, "already verified"

    workdir = repo or "."
    wt_path = None
    branch = h.get("branch", "")
    try:
        if repo and branch and workdir_base:
            wt_path = os.path.join(workdir_base, f"verify-{jid}")
            gitops.worktree_add(repo, branch, wt_path)
            workdir = wt_path
        ok, results = acceptance.verify(h.get("accept", ""), workdir)
        report = acceptance.format_report(results)
    finally:
        if wt_path:
            try:
                gitops.worktree_remove(repo, wt_path)
            except Exception as e:
                print(f"WARNING: worktree cleanup failed ({e})",
                      file=sys.stderr)

    if ok:
        _set_verified(jid, True, report)
        _say(h.get("room", ""), f"VERIFIED:{jid}")
        return True, report
    # Acceptance failed: reopen for another worker, keep the evidence.
    jobs._hset(jobs._job_key(jid), {
        "status": "open",
        "note": f"acceptance failed:\n{report}"[-2000:],
    })
    jobs._hdel(jobs._job_key(jid), "claim", "claim_host", "claimed_at",
               "lease_until", "result_commit", "verified", "verified_at",
               "verify_report")
    try:
        rc.api_get(f"del/{jobs._claim_key(jid)}")
    except Exception:
        pass
    _say(h.get("room", ""), f"REQUEUE:{jid} acceptance failed")
    return False, report


def _done_unverified():
    """Job ids with status done and no verified=1 stamp."""
    try:
        ids = json.loads(
            rc.api_get(f"zrange/{jobs._index_key()}/0/-1"))["result"] or []
    except Exception:
        return []
    out = []
    for jid in ids:
        h = jobs._hgetall(jobs._job_key(jid))
        if h.get("status") == "done" and h.get("verified") != "1":
            out.append(jid)
    return out


def tick(repo=None, workdir_base=None):
    """One overseer pass. Returns a report dict."""
    report = {"swept": [], "pruned": [], "verified": [], "failed": [],
              "errors": []}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        jobs.main(["sweep"])
    for line in buf.getvalue().splitlines():
        if line.startswith("REQUEUED "):
            report["swept"].append(line.split()[1])
    try:
        report["pruned"] = capabilities.prune_stale()
    except Exception as e:
        report["errors"].append(f"prune_stale: {e}")
    for jid in _done_unverified():
        try:
            ok, _ = verify_done(jid, repo=repo, workdir_base=workdir_base)
            (report["verified"] if ok else report["failed"]).append(jid)
        except Exception as e:
            report["errors"].append(f"verify {jid}: {e}")
    return report
