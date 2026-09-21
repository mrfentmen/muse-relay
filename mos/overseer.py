#!/usr/bin/env python3
"""The MOS overseer: dispatches work, verifies results, requeues failures.

One overseer per crew. It:
  - posts jobs (via jobs.py) and DMs capable workers (via send.py --dm),
  - runs `tick()` periodically: sweeps expired leases, prunes stale
    capability registrations, announces jobs nobody is coming for, and
    verifies done-but-unverified jobs,
  - verifies a done job's machine-readable acceptance criteria against
    the worker's branch before the job counts as truly done,
  - hands a failed job back with the failure report as its note — or
    dead-letters it once its attempts are gone, so a spec nobody can
    satisfy stops consuming the crew every cycle.

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

# Silence is the scariest crew failure: a job can sit open for hours
# because its DM never landed or because nobody can take it, and nothing
# says so. tick() alerts on open jobs older than STUCK_AFTER, at most
# once every STUCK_ALERT_EVERY seconds per job (so a loop does not spam
# the bus).
STUCK_AFTER = int(os.environ.get("MUSE_RELAY_STUCK_AFTER", "1800"))
STUCK_ALERT_EVERY = int(
    os.environ.get("MUSE_RELAY_STUCK_ALERT_EVERY", "3600"))


def _say(room, text):
    """Best-effort announcement; never fails the state change."""
    try:
        key = rc.bus_key(room)
        rc.bus_push(f"{rc.NICK}: {text}", key=key)
        rc.bus_trim(key=key)
        rc.presence_beat(key)
    except Exception as e:
        print(f"WARNING: announce failed ({e})", file=sys.stderr)


def _post_job(title, spec, accept, room, lease, branch, base, reqs=(),
              files="", roles=()):
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
    if reqs:
        argv += ["--req", ",".join(reqs)]
    if files:
        argv += ["--files", files]
    if roles:
        argv += ["--role", ",".join(roles)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_code = jobs.main(argv)
    if rc_code != 0:
        raise RuntimeError(f"job post failed: {buf.getvalue()}")
    for line in buf.getvalue().splitlines():
        if line.startswith("JOB "):
            return line.split()[1]
    raise RuntimeError(f"could not parse job id from: {buf.getvalue()!r}")


def _accept_with_files_only(accept, files, base):
    """Add a files-only check when a job declares files AND a base.

    Declaring files is only half the contract: jobs.py stops two jobs
    claiming the same file, and this stops a worker writing outside the
    set it claimed. It needs a base to diff against, so it is only
    added when the dispatch names one.
    """
    if not files or not base:
        return accept
    if "files-only:" in (accept or ""):
        return accept
    line = f"files-only: {files}"
    return (accept.rstrip() + "\n" + line) if (accept or "").strip() else line


def dispatch(title, spec, accept="", requirements=(), room="",
             lease=LEASE_DEFAULT, branch="", base="", roles=(), files=""):
    """Post a job and DM every worker whose caps and role fit it.

    `files` declares the file set the job owns; jobs.py enforces it at
    claim time so two workers never get handed the same file, and (with
    a base to diff against) dispatch adds a files-only acceptance check
    so the job is failed when its branch writes outside that set.
    Returns (jid, dm_results) where dm_results is
    [(nick, dm_ok, detail)].
    """
    if not spec or not spec.strip():
        raise ValueError("spec must not be empty")
    accept = _accept_with_files_only(accept, files, base)
    reqs = [r.strip().lower() for r in requirements if r and r.strip()]
    want_roles = [r.strip().lower() for r in roles if r and r.strip()]
    jid = _post_job(title, spec, accept, room, lease, branch, base,
                    reqs=reqs, files=files, roles=want_roles)
    dm_results = []
    for nick in capabilities.find_for(reqs, roles=want_roles):
        rec = capabilities.get(nick) or {}
        secret = rec.get("dm", "")
        if not secret:
            dm_results.append((nick, False, "no DM secret registered"))
            continue
        text = f"JOB:{jid} {title}"
        if reqs:
            text += f" req={','.join(reqs)}"
        if want_roles:
            text += f" role={','.join(want_roles)}"
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
            if os.path.exists(wt_path):
                # A previous verify died before its cleanup, leaving the
                # worktree registration and directory behind. Without
                # this the job would fail forever with "worktree path
                # already exists" — a stuck job, not a failing one.
                print(f"WARNING: reclaiming stale worktree {wt_path}",
                      file=sys.stderr)
                gitops.worktree_reclaim(repo, wt_path)
            gitops.worktree_add(repo, branch, wt_path)
            workdir = wt_path
        ok, results = acceptance.verify(h.get("accept", ""), workdir,
                                       base=h.get("base", ""))
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
    # Acceptance failed: hand it back for another attempt while it has
    # attempts left, and dead-letter it when it does not — with the
    # report kept as the note either way, so the evidence survives.
    state = jobs.settle_failed(jid, h, h.get("room", ""),
                               reason="acceptance failed")
    jobs._hset(jobs._job_key(jid), {
        "note": f"acceptance failed:\n{report}"[-2000:]})
    if state == "dead":
        return False, report + "\n[DEAD-LETTERED] out of attempts"
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


def _dependency_is_doomed(h):
    """True when a job waits on something that can never become done.

    A blocked or dead-lettered dependency will never finish on its own,
    so every job queued behind it is stalled too — and that stall is
    otherwise completely silent.
    """
    for dep in jobs.deps_of(h):
        dh, _ = jobs.job_get(dep)
        if dh is None or dh.get("status") in ("blocked", "dead"):
            return True
    return False


def _stuck_jobs(now=None):
    """Open jobs nobody is coming for: [(jid, reason, room)].

    A job legitimately waiting on a dependency that can still finish is
    pending, not stuck — the job it waits on is what should alarm. A job
    waiting on one that is blocked, dead-lettered or gone is stuck, and
    would otherwise wait in silence forever.
    """
    now = int(now if now is not None else time.time())
    out = []
    try:
        ids = jobs._all_ids()
    except Exception:
        return out
    for jid in ids:
        h = jobs._hgetall(jobs._job_key(jid))
        if h.get("status") != "open":
            continue
        try:
            created = int(h.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0
        if not created or now - created < STUCK_AFTER:
            continue
        try:
            last = int(h.get("stuck_alerted_at") or 0)
        except (TypeError, ValueError):
            last = 0
        if last and now - last < STUCK_ALERT_EVERY:
            continue
        ready, why = jobs.deps_ready(jid, h)
        if not ready:
            if not _dependency_is_doomed(h):
                continue
            out.append((str(jid), f"waiting on {why}", h.get("room", "")))
            continue
        reqs = jobs.reqs_of(h)
        want = jobs.roles_of(h)
        if not capabilities.find_for(reqs, roles=want):
            need = ",".join(sorted(reqs)) or "any"
            if want:
                need += f" roles={','.join(sorted(want))}"
            reason = f"no capable worker registered (needs {need})"
        else:
            reason = f"open {now - created}s with no claim"
        out.append((str(jid), reason, h.get("room", "")))
    return out


def _alert_stuck(report):
    """Announce the jobs nobody is coming for, once per alert window."""
    now = int(time.time())
    for jid, reason, room in _stuck_jobs(now):
        _say(room, f"STUCK:{jid} {reason}")
        report["stuck"].append(jid)
        try:
            jobs._hset(jobs._job_key(jid), {"stuck_alerted_at": now})
        except Exception as e:
            report["errors"].append(f"stuck stamp {jid}: {e}")


def tick(repo=None, workdir_base=None):
    """One overseer pass. Returns a report dict."""
    report = {"swept": [], "pruned": [], "verified": [], "failed": [],
              "dead": [], "stuck": [], "errors": []}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        jobs.main(["sweep"])
    for line in buf.getvalue().splitlines():
        if line.startswith("REQUEUED "):
            report["swept"].append(line.split()[1])
        elif line.startswith("DEAD_LETTERED "):
            report["dead"].append(line.split()[1])
    try:
        report["pruned"] = capabilities.prune_stale()
    except Exception as e:
        report["errors"].append(f"prune_stale: {e}")
    try:
        _alert_stuck(report)
    except Exception as e:
        report["errors"].append(f"stuck alerts: {e}")
    for jid in _done_unverified():
        try:
            ok, _ = verify_done(jid, repo=repo, workdir_base=workdir_base)
            (report["verified"] if ok else report["failed"]).append(jid)
        except Exception as e:
            report["errors"].append(f"verify {jid}: {e}")
    return report


def tick_lines(rep):
    """Render a tick report as one line per event; ['TICK_CLEAN'] when
    nothing happened. Shared by the CLI and the loop so both print the
    same thing."""
    lines = [f"SWEPT {jid}" for jid in rep["swept"]]
    lines += [f"DEAD_LETTERED {jid}" for jid in rep.get("dead", [])]
    lines += [f"PRUNED {nick}" for nick in rep["pruned"]]
    lines += [f"VERIFIED {jid}" for jid in rep["verified"]]
    lines += [f"ACCEPTANCE_FAILED {jid}" for jid in rep["failed"]]
    lines += [f"STUCK {jid}" for jid in rep.get("stuck", [])]
    return lines or ["TICK_CLEAN"]


def _report_tick(rep, out):
    """Print one tick report: events on out, errors on stderr."""
    for line in tick_lines(rep):
        print(line, file=out, flush=True)
    for e in rep["errors"]:
        print(f"ERROR {e}", file=sys.stderr)


def tick_loop(repo=None, workdir_base=None, interval=60, max_cycles=None,
              out=None):
    """Run tick() repeatedly — the overseer's autonomous mode.

    Sweeping leases, pruning dead registrations and verifying finished
    work are things nobody should have to remember to run by hand. A
    failed cycle is reported and retried rather than killing the loop,
    because the bus being briefly unreachable must not stop the crew.
    Returns the number of cycles completed.
    """
    out = out or sys.stdout
    cycles = 0
    try:
        while True:
            try:
                rep = tick(repo=repo, workdir_base=workdir_base)
                _report_tick(rep, out)
            except Exception as e:
                print(f"WARNING: overseer tick failed ({e}); retrying",
                      file=sys.stderr)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return cycles
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\noverseer stopping", file=sys.stderr)
        return cycles
