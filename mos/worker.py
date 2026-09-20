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

DM read offsets live in Redis so a restarted worker never re-reads
(and never misses) announcements.
"""
import contextlib
import io
import json
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
from mos import capabilities  # noqa: E402

_ANNOUNCE = re.compile(r"JOB:(\d+)\s+(.*)")
_REQ = re.compile(r"\breq=([A-Za-z0-9_,-]+)\s*$")


class WorkerBlocked(Exception):
    """Raise from a handler to mark the job blocked with a reason."""


def parse_announcement(body):
    """Parse 'JOB:<id> <title> [req=a,b]' -> (jid, title, reqs) or None."""
    m = _ANNOUNCE.search(body)
    if not m:
        return None
    jid, rest = m.group(1), m.group(2).strip()
    reqs = set()
    rm = _REQ.search(rest)
    if rm:
        reqs = {r for r in rm.group(1).split(",") if r}
        rest = rest[:rm.start()].strip()
    return jid, rest, reqs


class Worker:
    def __init__(self, nick=None, dm_secret="", caps=(), roles="",
                 workdir=".", lease=1800, max_jobs=1):
        self.nick = nick or rc.NICK
        self.dm_secret = dm_secret
        self.caps = {c.strip().lower() for c in caps if c and c.strip()}
        self.roles = roles
        self.workdir = workdir
        self.lease = lease
        self.max_jobs = max_jobs
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

    def heartbeat(self):
        """Touch capability registration; renew leases on my claims."""
        capabilities.touch(self.nick)
        renewed = []
        for jid in self._my_claimed():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_code = jobs.main(["heartbeat", jid])
            if rc_code == 0:
                renewed.append(jid)
        return renewed

    def _my_claimed(self):
        try:
            ids = json.loads(
                rc.api_get(f"zrange/{jobs._index_key()}/0/-1"))["result"] or []
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
        """Return [(jid, title, reqs)] from new DM announcements."""
        if not self.dm_secret:
            return []
        seen = self._seen_get()
        try:
            msgs = rc.bus_get(seen, -1, key=self._dm_key())
        except Exception:
            return []
        self._seen_set(seen + len(msgs))
        out = []
        for m in msgs:
            if not isinstance(m, str) or m.startswith(self.nick + ":"):
                continue
            parsed = parse_announcement(m)
            if parsed:
                out.append(parsed)
        return out

    # -- claiming + running --------------------------------------------
    def _matches(self, reqs):
        return reqs.issubset(self.caps)

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

    def run_once(self, handler):
        """One poll/claim/work cycle. Returns a report dict."""
        report = {"claimed": [], "done": [], "blocked": [],
                  "skipped": [], "errors": []}
        self.register()
        self.heartbeat()
        for jid, title, reqs in self.poll_dms():
            if not self._matches(reqs):
                report["skipped"].append(jid)
                continue
            ok, detail = self._claim(jid)
            if not ok:
                report["errors"].append(f"claim {jid}: {detail.strip()[-200:]}")
                continue
            report["claimed"].append(jid)
            h, spec = jobs.job_get(jid)
            try:
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

    def run_loop(self, handler, interval=60):
        """Run forever until interrupted."""
        try:
            while True:
                try:
                    self.run_once(handler)
                except Exception as e:
                    print(f"worker loop error: {e}", file=sys.stderr)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("worker stopping", file=sys.stderr)
