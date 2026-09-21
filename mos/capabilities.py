#!/usr/bin/env python3
"""Capability registry: which agents exist and what they can do.

Agents register their capabilities (e.g. python, testing, docs) and
heartbeat them. The overseer assigns jobs only to agents whose
capabilities cover the job's requirements, who are fresh (recent
heartbeat), and who have spare job capacity.

Keys (MUSE_RELAY_CAPNS overrides the muse-bus:cap prefix):
  <ns>:<nick>   hash: caps/roles/max_jobs/dm/host/updated
  <ns>:index    zset nick -> updated (for listing/pruning)

DM secrets: a worker shares its DM dead-drop secret at registration so
the overseer can dispatch via DM. DM rooms are obscurity, not
encryption — this matches the bus threat model (nicks are
unauthenticated, friendly closed crew). Never put real credentials here.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402

CAPNS = os.environ.get("MUSE_RELAY_CAPNS", "muse-bus:cap")
STALE_AFTER = int(os.environ.get("MUSE_RELAY_CAP_STALE", "900"))  # 15 min


def _key(nick):
    return f"{CAPNS}:{nick}"


def _index():
    return f"{CAPNS}:index"


def _hgetall(key):
    import json
    raw = json.loads(rc.api_get(f"hgetall/{key}"))["result"] or []
    it = iter(raw)
    return {k: v for k, v in zip(it, it)}


def register(nick, capabilities, roles="", max_jobs=1, dm_secret="", info=""):
    """Register (or refresh) an agent. capabilities is an iterable of str."""
    if not nick or not str(nick).strip():
        raise ValueError("nick required")
    caps = sorted({c.strip().lower() for c in capabilities if c and c.strip()})
    if not caps:
        raise ValueError("at least one capability required")
    now = int(time.time())
    fields = {
        "caps": ",".join(caps),
        "roles": roles,
        "max_jobs": str(int(max_jobs)),
        "host": rc.INSTANCE_ID,
        "updated": str(now),
    }
    if dm_secret:
        fields["dm"] = dm_secret
    if info:
        fields["info"] = info
    import urllib.parse
    pairs = "/".join(
        f"{urllib.parse.quote(k, safe='')}/{urllib.parse.quote(v, safe='')}"
        for k, v in fields.items() if v != "")
    if pairs:
        rc.api_get(f"hset/{_key(nick)}/{pairs}")
    rc.api_get(f"zadd/{_index()}/{now}/{urllib.parse.quote(nick, safe='')}")
    return True


def touch(nick):
    """Heartbeat: mark this agent fresh. Returns False if unknown."""
    if not get(nick):
        return False
    now = int(time.time())
    import urllib.parse
    rc.api_get(f"hset/{_key(nick)}/updated/{now}")
    rc.api_get(f"zadd/{_index()}/{now}/{urllib.parse.quote(nick, safe='')}")
    return True


def unregister(nick):
    import urllib.parse
    rc.api_get(f"del/{_key(nick)}")
    rc.api_get(f"zrem/{_index()}/{urllib.parse.quote(nick, safe='')}")
    return True


def get(nick):
    """Return the capability record dict, or None."""
    h = _hgetall(_key(nick))
    return h or None


def agents():
    """Return {nick: record} for every registered agent."""
    import json
    import urllib.parse
    try:
        ids = json.loads(rc.api_get(f"zrange/{_index()}/0/-1"))["result"] or []
    except Exception:
        return {}
    out = {}
    for raw in ids:
        nick = urllib.parse.unquote(raw)
        h = get(nick)
        if h:
            out[nick] = h
    return out


def _fresh(record, now=None):
    now = int(now if now is not None else time.time())
    try:
        return now - int(record.get("updated", "0")) <= STALE_AFTER
    except ValueError:
        return False


def parse_list(value):
    """Split a comma-separated registration field into a lowercase set."""
    return {p.strip().lower() for p in (value or "").split(",") if p.strip()}


def roles_of(record):
    """Roles a registered agent holds, lowercased (empty set when none)."""
    return parse_list(record.get("roles"))


def find_for(requirements, roles=None):
    """Nicks matching a job: caps covered, role held, fresh, capacity.

    requirements: iterable of str (all must be covered).
    roles: optional iterable of str; a candidate must hold AT LEAST one.
    Roles are what the roles/*.md skills describe (planner, builder,
    reviewer, ...), so a job can ask for a reviewer rather than merely
    for someone who happens to have the right capabilities.
    Returns a sorted nick list.
    """
    reqs = {r.strip().lower() for r in requirements if r and r.strip()}
    want_roles = {r.strip().lower() for r in (roles or []) if r and r.strip()}
    now = time.time()
    registrations = agents()
    counts = active_claims_of(registrations.keys())
    out = []
    for nick, rec in registrations.items():
        if not _fresh(rec, now):
            continue
        caps = {c for c in rec.get("caps", "").split(",") if c}
        if not reqs.issubset(caps):
            continue
        if want_roles and not (want_roles & roles_of(rec)):
            continue
        try:
            max_jobs = int(rec.get("max_jobs", "1"))
        except ValueError:
            max_jobs = 1
        if counts.get(nick, 0) >= max(1, max_jobs):
            continue
        out.append(nick)
    return sorted(out)


def active_claims_of(nicks):
    """Return {nick: claimed-job count} for the given nicks, in one pass.

    find_for used to call active_claims() per candidate, which re-read
    every job in the index for every agent (an N+1 over the job index).
    One pass over the index answers it for the whole crew.
    """
    import jobs
    want = set(nicks)
    counts = {n: 0 for n in want}
    if not want:
        return counts
    try:
        ids = jobs._all_ids()
    except Exception:
        return counts
    for jid in ids:
        h = jobs._hgetall(jobs._job_key(jid))
        if h.get("status") != "claimed":
            continue
        claim = h.get("claim")
        if claim in counts:
            counts[claim] += 1
    return counts


def prune_stale():
    """Unregister agents whose heartbeat is stale. Returns removed nicks."""
    now = time.time()
    removed = []
    for nick, rec in agents().items():
        if not _fresh(rec, now):
            unregister(nick)
            removed.append(nick)
    return sorted(removed)
