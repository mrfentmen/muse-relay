#!/usr/bin/env python3
"""The MOS worker loop: poll DMs, claim matching jobs, do the work,
heartbeat the lease, and report done/blocked.

A worker is one nick in one process (same convention as send.py/poll.py:
the nick comes from MUSE_RELAY_NICK). It registers its capabilities,
then repeatedly:

  1. heartbeat its capability registration and any claimed jobs,
  2. poll its DM dead-drop for JOB: announcements,
  3. claim announcements whose requirements its capabilities cover,
  4. run the handler; report done (with commit) or blocked (with reason).

The handler is caller-supplied: handler(jid, title, spec, workdir)
-> (commit_hash, note). Raise WorkerBlocked(reason) for a clean block;
any other exception becomes a blocked report with the traceback tail.
CommandHandler below is the one that lets a worker run unattended: it
shells out to whatever agent you point it at.

DM read offsets live in Redis so a restarted worker never re-reads
(and never misses) announcements — they ride on relay_common.bus_read,
so a trimmed DM list is reported instead of quietly skipped.

Four things keep a long handler from being sabotaged by its own crew:
  - the lease is renewed in the background while the handler runs
    (_keep_lease), so a build that outlives the lease is not swept
    mid-flight and handed to a second worker,
  - the job's OWN declared capabilities and role are re-checked before
    claiming (_matches), not just the ones in the DM text,
  - open jobs can be scanned when a DM was missed or the worker joined
    the crew late (scan_open); claiming is still atomic, so a scan can
    never steal live work,
  - claims are capped by max_jobs (_capacity), so a job it cannot work
    on right now stays open for the rest of the crew instead of sitting
    claimed and untouched with its lease ticking down.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
from mos import capabilities  # noqa: E402
from mos import roles as roles_mod  # noqa: E402

_ANNOUNCE = re.compile(r"JOB:(\d+)\s+(.*)")
_REQ = re.compile(r"\breq=([A-Za-z0-9_,-]+)")
_ROLE = re.compile(r"\brole=([A-Za-z0-9_,-]+)")
_COMMIT_LINE = re.compile(r"^\s*COMMIT\s+([0-9a-f]{7,40})\s*$", re.M)
_HASH_LINE = re.compile(r"^[0-9a-f]{7,40}$")


class WorkerBlocked(Exception):
    """Raise from a handler to mark the job blocked with a reason."""


def parse_announcement(body):
    """Parse 'JOB:<id> <title> [req=a,b] [role=x]'.

    Returns (jid, title, reqs) or None. Roles are deliberately NOT read
    from here — the job record is the authority (jobs.roles_of), so a
    hand-written DM cannot widen what a job asks for.
    """
    m = _ANNOUNCE.search(body)
    if not m:
        return None
    jid, rest = m.group(1), m.group(2).strip()
    reqs = set()
    rm = _REQ.search(rest)
    if rm:
        reqs = {r for r in rm.group(1).split(",") if r}
        rest = rest[:rm.start()].strip()
    ro = _ROLE.search(rest)
    if ro:
        rest = rest[:ro.start()].strip()
    return jid, rest, reqs


def parse_commit(text):
    """Pull a result commit hash out of handler output.

    Accepts an explicit 'COMMIT <hash>' line anywhere, else a bare hash
    as the last non-empty line. Returns None when there is none, so a
    handler that produced no commit is a blocked job rather than a
    silently "done" one with an empty hash.
    """
    m = _COMMIT_LINE.search(text or "")
    if m:
        return m.group(1)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if lines and _HASH_LINE.match(lines[-1]):
        return lines[-1]
    return None


class CommandHandler:
    """Run an external agent command as the job handler.

    This is what makes `mos.py worker --loop` an actual autonomous
    worker rather than a manual step: the command does the work, the
    worker does the queue.

    The command runs through the shell in the job's workdir with the
    spec on stdin, and must print the resulting commit hash — either a
    'COMMIT <hash>' line or a bare hash on the last line. It also gets:

      MUSE_JOB_ID, MUSE_JOB_TITLE, MUSE_JOB_ROOM, MUSE_JOB_WORKDIR,
      MUSE_JOB_ROLE  (the first role this worker holds, if any)

    A non-zero exit, a timeout, or missing output raises WorkerBlocked
    with the tail of the output, so the crew sees why it stopped.
    """

    def __init__(self, command, workdir=".", role="", timeout=None, env=None):
        if not command or not command.strip():
            raise ValueError("handler command required")
        self.command = command
        self.workdir = workdir
        self.role = role
        self.timeout = timeout
        self.env = dict(env or {})

    def __call__(self, jid, title, spec, workdir):
        env = dict(os.environ)
        env.update({
            "MUSE_JOB_ID": str(jid),
            "MUSE_JOB_TITLE": title or "",
            "MUSE_JOB_WORKDIR": workdir or self.workdir,
            "MUSE_JOB_ROLE": self.role or "",
        })
        env.update(self.env)
        try:
            p = subprocess.run(
                self.command, shell=True, cwd=workdir or self.workdir,
                input=spec or "", capture_output=True, text=True,
                timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            raise WorkerBlocked(
                f"handler timed out after {self.timeout}s: {self.command}")
        out, err = p.stdout or "", p.stderr or ""
        if p.returncode != 0:
            raise WorkerBlocked(
                f"handler exited {p.returncode}: "
                f"{(err.strip() or out.strip())[-400:]}")
        commit = parse_commit(out)
        if not commit:
            raise WorkerBlocked(
                f"handler produced no commit hash (print 'COMMIT <hash>' or "
                f"a bare hash as the last line); output tail: "
                f"{out.strip()[-300:]}")
        return commit, (out.strip()[-400:] or "handler finished")


class Worker:
    # A lease is beaten at a third of its length, but never faster than
    # this — a short lease still must not become a busy loop.
    LEASE_BEAT_MIN = 30.0

    def __init__(self, nick=None, dm_secret="", caps=(), roles="",
                 workdir=".", lease=1800, max_jobs=1, scan_open=False,
                 lease_beat=None):
        self.nick = nick or rc.NICK
        self.dm_secret = dm_secret
        self.caps = capabilities.parse_list(",".join(caps)) if caps else set()
        self.lease_beat = (float(lease_beat) if lease_beat is not None
                           else max(self.LEASE_BEAT_MIN, lease / 3.0))
        # Validate roles up front: a typo would register a worker no job
        # can ever match, which just idles forever with no explanation.
        self.roles = ",".join(roles_mod.parse_roles(roles)) if roles else ""
        self.role_set = capabilities.parse_list(self.roles)
        self.workdir = workdir
        self.lease = lease
        self.max_jobs = max_jobs
        self.scan_open = scan_open
        # One nick per process: take over the module nicks like the
        # existing scripts do via MUSE_RELAY_NICK.
        if self.nick != rc.NICK:
            rc.NICK = self.nick
            jobs.NICK = self.nick

    # -- registration -------------------------------------------------
    def register(self):
        capabilities.register(
            self.nick, self.caps, roles=self.roles,
            max_jobs=self.max_jobs, dm_secret=self.dm_secret)
        return True

    def heartbeat(self, claimed=None):
        """Touch capability registration; renew leases on my claims.

        `claimed` lets a caller that just listed them hand its view over
        instead of making this scan the job index a second time.
        """
        capabilities.touch(self.nick)
        renewed = []
        for jid in (self._my_claimed() if claimed is None else claimed):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_code = jobs.main(["heartbeat", jid])
            if rc_code == 0:
                renewed.append(jid)
        return renewed

    def _my_claimed(self):
        try:
            ids = jobs._all_ids()
        except Exception:
            return []
        out = []
        for jid in ids:
            h = jobs._hgetall(jobs._job_key(jid))
            if h.get("status") == "claimed" and h.get("claim") == self.nick:
                out.append(jid)
        return out

    # -- DM polling ----------------------------------------------------
    def _dm_key(self):
        return rc.bus_key(rc.dm_room(self.dm_secret))

    def _seen_key(self):
        return f"{capabilities.CAPNS}:dseen:{self.nick}"

    def _seen_get(self):
        try:
            v = json.loads(rc.api_get(f"get/{self._seen_key()}"))["result"]
            return int(v or 0)
        except Exception:
            return 0

    def _seen_set(self, n):
        try:
            rc.api_get(f"set/{self._seen_key()}/{n}")
        except Exception:
            pass

    def poll_dms(self):
        """Return [(jid, title, reqs)] from new DM announcements.

        Uses the absolute cursor from bus_read so a trimmed DM list can
        never make a worker skip a JOB announcement in silence — the
        exact failure mode that would leave a crew idle forever.
        """
        if not self.dm_secret:
            return []
        seen = self._seen_get()
        try:
            msgs, cursor, lost = rc.bus_read(seen, key=self._dm_key())
        except Exception:
            return []
        if lost:
            print(f"WARNING: {lost} DM announce(s) were trimmed off the "
                  f"bus before worker '{self.nick}' read them — check for "
                  f"jobs that were dispatched but never claimed",
                  file=sys.stderr)
        self._seen_set(cursor)
        out = []
        for m in msgs:
            if not isinstance(m, str) or m.startswith(self.nick + ":"):
                continue
            parsed = parse_announcement(m)
            if parsed:
                out.append(parsed)
        return out

    def scan_open_jobs(self, limit=25):
        """Open jobs matching my caps and role, newest first.

        The DM is the normal dispatch path; this is the self-healing
        backstop for a missed or rejected DM, a DM list that got
        trimmed, or a worker that joined the crew after the dispatch.
        The job record — not the DM text — says what the job needs, and
        claiming is still an atomic SET NX, so a scan can never take
        work away from a live claim. Returns [(jid, title, reqs)].
        """
        if not self.caps:
            return []
        try:
            ids = jobs._all_ids()
        except Exception:
            return []
        out = []
        for jid in ids[-limit:]:
            h = jobs._hgetall(jobs._job_key(jid))
            if h.get("status") != "open":
                continue
            # A job waiting on a dependency is not claimable yet, so it
            # is not a candidate: offering it would only produce a
            # refused claim every cycle.
            if not jobs.deps_ready(jid, h)[0]:
                continue
            reqs = jobs.reqs_of(h)
            if self._matches(reqs, h):
                out.append((str(jid), h.get("title", ""), reqs))
        return out

    # -- claiming + running --------------------------------------------
    def _matches(self, reqs, h=None):
        """Can this worker take the job described by h?

        Capabilities must cover the job's requirement set, and when the
        job names roles the worker must hold at least one of them (the
        role decides WHICH brief the job runs under — see roles/).
        """
        if not reqs.issubset(self.caps):
            return False
        need = jobs.roles_of(h or {})
        if need and not (need & self.role_set):
            return False
        return True

    @contextlib.contextmanager
    def _keep_lease(self, jid):
        """Renew jid's lease in the background while the handler runs.

        A real build outlives a short lease. Without this the job is
        swept mid-flight and handed to a second worker while the first
        is still writing files, which is how one job silently becomes
        two. Beats every lease_beat seconds (a third of the lease, and
        at least LEASE_BEAT_MIN); a failed beat is reported and never
        interrupts the work.

        The beat swallows jobs.main's stdout so a heartbeat never
        interleaves with the caller's output. Safe because the caller is
        blocked inside the handler while beats run; a handler that
        itself writes to sys.stdout from another thread should not.
        """
        every = self.lease_beat
        stop = threading.Event()

        def beat():
            while not stop.wait(every):
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        code = jobs.main(["heartbeat", jid])
                    if code != 0:
                        print(f"WARNING: lease heartbeat for {jid} exited "
                              f"{code}", file=sys.stderr)
                except Exception as e:
                    print(f"WARNING: lease heartbeat failed for {jid} "
                          f"({e})", file=sys.stderr)

        thread = threading.Thread(target=beat, daemon=True,
                                  name=f"lease-{jid}")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)

    def _capacity(self, claimed=None):
        """How many more jobs this worker may hold at once.

        max_jobs has to bind locally as well as at dispatch: without
        this a worker claims every job it matches in one cycle and then
        works them one at a time, leaving the rest claimed, untouched,
        and looking "in progress" to the whole crew while their leases
        tick down. `claimed` is the caller's own list of live claims, so
        the index is not scanned twice in one cycle.
        """
        try:
            limit = max(1, int(self.max_jobs))
        except (TypeError, ValueError):
            limit = 1
        held = len(self._my_claimed()) if claimed is None else len(claimed)
        return max(0, limit - held)

    def _claim(self, jid):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            rc_code = jobs.main(
                ["claim", jid, "--lease", str(self.lease)])
        out = buf.getvalue()
        return rc_code == 0 and f"CLAIMED {jid}" in out, out + err.getvalue()

    def _finish(self, jid, commit, note):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_code = jobs.main(
                ["done", jid, "--commit", commit, "--note", note or ""])
        return rc_code == 0

    def _block(self, jid, reason):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_code = jobs.main(
                ["blocked", jid, "--reason", reason[:500]])
        return rc_code == 0

    def candidates(self):
        """Jobs to consider this cycle: DM announcements, then open scans.

        De-duplicated by job id, DMs first — the DM is the directed
        dispatch and the scan only fills in what it missed.
        """
        seen = set()
        out = []
        for jid, title, reqs in self.poll_dms():
            if jid not in seen:
                seen.add(jid)
                out.append((jid, title, reqs))
        if self.scan_open:
            for jid, title, reqs in self.scan_open_jobs():
                if jid not in seen:
                    seen.add(jid)
                    out.append((jid, title, reqs))
        return out

    def run_once(self, handler):
        """One poll/claim/work cycle. Returns a report dict.

        The claim budget is checked before every claim, so a job this
        worker cannot work on right now stays open for the rest of the
        crew instead of being taken and left alone.
        """
        report = {"claimed": [], "done": [], "blocked": [],
                  "skipped": [], "deferred": [], "errors": []}
        self.register()
        mine = self._my_claimed()
        self.heartbeat(mine)
        budget = self._capacity(mine)
        for jid, title, reqs in self.candidates():
            h, spec = jobs.job_get(jid)
            if h is None:
                report["errors"].append(f"no such job: {jid}")
                continue
            # The job record — not the DM text — is what decides the
            # match, so a hand-written DM cannot widen a job's needs.
            if not self._matches(reqs | jobs.reqs_of(h), h):
                report["skipped"].append(jid)
                continue
            if budget <= 0:
                report["deferred"].append(jid)
                continue
            ok, detail = self._claim(jid)
            if not ok:
                report["errors"].append(f"claim {jid}: {detail.strip()[-200:]}")
                continue
            report["claimed"].append(jid)
            budget -= 1
            try:
                with self._keep_lease(jid):
                    commit, note = handler(jid, title, spec or "",
                                           self.workdir)
                if self._finish(jid, commit, note):
                    report["done"].append(jid)
                else:
                    report["errors"].append(f"done {jid}: finish failed")
            except WorkerBlocked as e:
                self._block(jid, str(e) or "blocked")
                report["blocked"].append(jid)
            except Exception:
                tb = traceback.format_exc(limit=3)
                self._block(jid, f"handler crashed: {tb[-400:]}")
                report["blocked"].append(jid)
        return report

    def run_loop(self, handler, interval=60, max_cycles=None, out=None):
        """Work the queue until interrupted (or max_cycles times).

        This is the unattended mode: poll, claim, run the handler,
        report. A cycle that raises is reported and retried — a flaky
        bus read must never take a crew worker down. Returns the number
        of cycles completed.
        """
        out = out or sys.stdout
        cycles = 0
        try:
            while True:
                try:
                    rep = self.run_once(handler)
                    self._report(rep, out)
                except Exception as e:
                    print(f"worker loop error: {e}", file=sys.stderr)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    return cycles
                time.sleep(interval)
        except KeyboardInterrupt:
            print("worker stopping", file=sys.stderr)
            return cycles

    def _report(self, rep, out):
        """One line per cycle so an unattended worker is observable."""
        print(f"cycle {self.nick}: claimed={len(rep['claimed'])} "
              f"done={len(rep['done'])} blocked={len(rep['blocked'])} "
              f"skipped={len(rep['skipped'])} "
              f"deferred={len(rep.get('deferred', []))} "
              f"errors={len(rep['errors'])}",
              file=out, flush=True)
        for e in rep["errors"]:
            print(f"WARNING: {e}", file=sys.stderr)
