#!/usr/bin/env python3
"""Crew overview and preflight: mos.py status / mos.py doctor.

`status` answers "what is happening right now" on one screen: who is
registered, what each agent can take and is already holding, how the
queue breaks down by state, and how long the oldest open job has waited.

`doctor` answers "why is nothing happening" before you start guessing:
is the REST URL set, is the token file readable, is Redis reachable, do
the role names in roles/ still resolve, is the working directory a git
repo, does the crew secret file parse. It never prints the token or a
key's value — only whether each piece is there.

Both read the same Redis state the workers and overseer use, so neither
needs special privileges or extra configuration.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
from mos import capabilities  # noqa: E402
from mos import gitops  # noqa: E402
from mos import roles as roles_mod  # noqa: E402

# Every status a job can hold, in the order the crew should care about.
JOB_STATES = ("open", "claimed", "done", "blocked", "dead")


def snapshot():
    """One pass over the crew state. Returns a JSON-friendly dict.

    Reads the job index once and derives the per-agent load from it,
    rather than asking each module to re-scan the queue.
    """
    try:
        agents = capabilities.agents()
    except Exception as e:
        return {"error": f"capability registry unreadable: {e}"}
    try:
        ids = jobs._all_ids()
    except Exception as e:
        return {"error": f"job index unreadable: {e}"}
    rows = []
    for jid in ids:
        h = jobs._hgetall(jobs._job_key(jid))
        if h:
            rows.append((str(jid), h))
    counts = {state: 0 for state in JOB_STATES}
    load = {}
    oldest = None
    now = int(time.time())
    for jid, h in rows:
        status = h.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        if status == "claimed" and h.get("claim"):
            load[h["claim"]] = load.get(h["claim"], 0) + 1
        if status == "open":
            try:
                created = int(h.get("created_at") or 0)
            except (TypeError, ValueError):
                created = 0
            age = now - created if created else 0
            if oldest is None or age > oldest["age"]:
                oldest = {"jid": jid, "age": age,
                          "title": h.get("title", "")}
    return {"agents": agents, "jobs": rows, "counts": counts, "load": load,
            "oldest_open": oldest, "now": now}


def status_lines(snap=None):
    """Human-readable crew overview, one fact per line."""
    snap = snap or snapshot()
    if snap.get("error"):
        return [f"ERROR {snap['error']}"]
    agents, counts = snap["agents"], snap["counts"]
    now = snap["now"]
    lines = []
    if not agents:
        lines.append("AGENTS none registered")
    for nick in sorted(agents):
        r = agents[nick]
        try:
            age = now - int(r.get("updated", "0"))
        except (TypeError, ValueError):
            age = -1
        fresh = "fresh" if 0 <= age <= capabilities.STALE_AFTER else f"stale({age}s)"
        lines.append(
            f"AGENT {nick} caps={r.get('caps') or '-'} "
            f"roles={r.get('roles') or '-'} "
            f"jobs={snap['load'].get(nick, 0)}/{r.get('max_jobs') or '1'} "
            f"dm={'yes' if r.get('dm') else 'no'} {fresh}")
    for state in JOB_STATES:
        lines.append(f"JOBS {state}={counts.get(state, 0)}")
    if snap["oldest_open"]:
        o = snap["oldest_open"]
        lines.append(f"OLDEST_OPEN {o['jid']} {o['age']}s {o['title']}")
    lines.append(f"TOTAL_JOBS {len(snap['jobs'])}")
    return lines


def _token_check():
    """Is the token file readable and non-empty? Never returns its value."""
    path = rc.TOKEN_FILE
    if not os.path.exists(path):
        return False, f"no token file at {path}"
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        return False, f"cannot read {path}: {e}"
    if not token:
        return False, f"{path} is empty"
    return True, f"{path} ({len(token)} chars, value not shown)"


def doctor_checks(secret_file="", repo=None):
    """Preflight checks as [(name, ok, detail)] in the order to fix them."""
    out = []

    def add(name, ok, detail):
        out.append((name, bool(ok), detail))

    add("url", bool(rc.RELAY_URL),
        rc.RELAY_URL or "MUSE_RELAY_URL is not set (see README setup)")
    ok, detail = _token_check()
    add("token", ok, detail)
    add("nick", bool(rc.NICK), rc.NICK or "MUSE_RELAY_NICK is empty")
    add("instance", bool(rc.INSTANCE_ID),
        rc.INSTANCE_ID or "MUSE_RELAY_INSTANCE_ID is empty")
    try:
        pong = json.loads(rc.api_get("ping"))["result"]
        add("redis", pong == "PONG", f"ping -> {pong}")
    except Exception as e:
        add("redis", False, f"unreachable: {e}")
    names = roles_mod.list_roles()
    add("roles", bool(names),
        f"{len(names)} role skill(s): {', '.join(names)}" if names
        else "no role skills found in roles/")
    try:
        # Read the index directly: capabilities.agents() swallows a
        # transport failure and returns {}, which would report "zero
        # agents" for a bus that is simply unreachable.
        raw = json.loads(
            rc.api_get(f"zrange/{capabilities._index()}/0/-1"))["result"] or []
        add("registry", True, f"{len(raw)} agent(s) registered")
    except Exception as e:
        add("registry", False, f"unreadable: {e}")
    if repo:
        if gitops.is_repo(repo):
            try:
                branch = gitops.current_branch(repo)
                dirty = "" if gitops.clean_tree(repo) else " (dirty tree)"
                add("repo", True, f"{repo} on {branch}{dirty}")
            except gitops.GitError as e:
                add("repo", False, str(e))
        else:
            add("repo", False, f"not a git repo: {repo}")
    if secret_file:
        keyring, _, err = jobs._read_keyring(secret_file)
        add("secret", err is None,
            err or f"{len(keyring)} key(s): {', '.join(sorted(keyring))}")
    return out


def doctor_lines(checks):
    return [f"{'OK' if ok else 'FAIL'} {name}: {detail}"
            for name, ok, detail in checks]


def doctor_ok(checks):
    """True when every check passed."""
    return all(ok for _, ok, _ in checks)
