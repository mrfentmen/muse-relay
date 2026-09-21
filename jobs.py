#!/usr/bin/env python3
"""Job queue for crew builds on the muse-relay bus.

An overseer posts work; workers claim it atomically, report progress,
and finish with a commit hash. State lives in Redis; the bus carries
only short wire messages (JOB:/CLAIM:/DONE:/...) so specs never clog
chat. See ROADMAP.md ("the crew") for the full design.

Usage:
  jobs.py post --title T --spec S|--spec - [--accept A] [--branch B]
               [--base C] [--room R] [--lease SECS]
               [--req python,testing] [--files a.py,b.py]
               [--role builder] [--depends-on 3,4] [--max-attempts N]
               [--allow-secrets]
  jobs.py list [--status open|claimed|done|blocked|dead|all]
  jobs.py show <id>
  jobs.py claim <id> [--room R] [--lease SECS] [--force]

  --req persists the job's required capabilities and --files the files
  it owns. --files is enforced: claiming a job whose declared files
  overlap another LIVE job's is refused (--force overrides), because two
  workers editing one file can only end in a merge nobody asked for.
  --req is what lets a worker that missed the DM (or joined late) still
  tell whether an open job is its own (see mos/worker.py scan_open).

  --depends-on makes a job wait: it cannot be claimed until every job it
  names is done, so one package is never built on another that does not
  exist yet. A dependency that is blocked or dead-lettered is reported
  as the reason instead of being waited on forever.

  --max-attempts bounds the retry loop. Every claim is an attempt, and a
  job that fails acceptance (or loses its worker) is handed back for
  another one; once it has used its attempts it is dead-lettered instead
  of being reopened forever — one bad spec must not keep the whole crew
  turning the crank. `jobs.py reset <id>` reopens it deliberately with a
  fresh budget, which is what a human does after fixing the spec.

  A spec is stored in plaintext and announced on the bus, so post
  refuses one that looks like it contains a live credential (see
  scan_for_secrets). --allow-secrets overrides that when it is a false
  positive.

  jobs.py heartbeat <id> [--lease SECS]
  jobs.py progress <id> --note TEXT [--room R]
  jobs.py done <id> --commit HASH [--note TEXT] [--room R]
  jobs.py blocked <id> --reason TEXT [--room R]
  jobs.py requeue <id> [--room R]
  jobs.py reset <id> [--room R]
  jobs.py sweep [--room R]
  jobs.py verify <id> --secret-file PATH

Signed jobs (defense against spoofed overseers):
  jobs.py post ... --secret-file ~/.config/muse-relay/job-secret
  jobs.py claim <id> --secret-file ~/.config/muse-relay/job-secret

  Signature versions: v3 is current and covers the job's semantic
  content including --req and --files; v2 (no req/files) and v1 (no job
  identity) still verify for jobs posted before the upgrade.

  The secret is a shared per-crew value distributed OUT OF BAND (your user
  tells both sides; never post it to the bus). The secret file is a keyring:
  one "kid:secret" per line (chmod 600); the first key signs, all keys
  verify, so keys rotate by adding a new kid line. A legacy single-token
  file still works as kid "default". post stores an HMAC-SHA256 over the
  canonical job content bound to the exact job id (v2 signatures); verify
  recomputes it and reports OK/BAD/UNSIGNED/EXPIRED (exit 0 only on OK).
  claim with --secret-file refuses BAD or EXPIRED jobs and posts a loud
  REJECTED alert to the bus so spoof attempts are visible. --expires SECONDS
  on post bounds how long a signature stays valid; --kid picks the signing
  key. The secret file is never printed.

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
reopens it — unless the job is out of attempts, in which case it is
dead-lettered (status "dead") instead of being retried again.

A job is one of: open, claimed, done, blocked, dead. Wire messages
(JOB:, CLAIM:, ...) are best-effort chat announcements; the Redis state
is the source of truth.

Never prints the token.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relay_common import (  # noqa: E402
    INSTANCE_ID, NICK, api_get, api_post, bus_key, bus_push, bus_trim,
    clean_room, nick_holder, presence_beat)

NS = os.environ.get("MUSE_RELAY_JOBNS", "muse-bus:job")
LEASE_DEFAULT = 1800  # 30 minutes
# Claims a job gets before it is dead-lettered (override with
# MUSE_RELAY_MAX_ATTEMPTS or --max-attempts at post time).
MAX_ATTEMPTS_DEFAULT = int(os.environ.get("MUSE_RELAY_MAX_ATTEMPTS", "3"))


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


def _all_ids():
    """Every job id, oldest first. Raises on transport failure."""
    return json.loads(
        api_get(f"zrange/{_index_key()}/0/-1"))["result"] or []


def reqs_of(h):
    """Capabilities a job requires (empty set when it names none).

    Persisted at post time so a worker that never saw the DM — or joined
    the crew after the dispatch — can still tell whether the job is
    theirs to take.
    """
    return {r.strip().lower() for r in (h.get("req") or "").split(",") if r.strip()}


def _norm_path(p):
    """Normalize a declared file path so two spellings compare equal."""
    return os.path.normpath(p.strip().lstrip("./")) if p.strip() else ""


def files_of(h):
    """Files a job declares as its own (empty set when it declares none)."""
    return {p for p in (_norm_path(x) for x in (h.get("files") or "").split(","))
            if p}


def roles_of(h):
    """Roles the job asks its worker to hold (empty set when none).

    Roles name the role skills in roles/*.md, so a job can ask for a
    reviewer rather than merely for someone with the right caps.
    """
    return {r.strip().lower() for r in (h.get("role") or "").split(",") if r.strip()}


def attempts_of(h):
    """How many times this job has been claimed (0 when never claimed)."""
    try:
        return int(h.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def max_attempts_of(h):
    """How many claims a job gets before it is dead-lettered.

    Read from the record (written at post time) rather than from the
    environment, so a worker and the overseer with different settings
    still agree on when a job is out of attempts.
    """
    try:
        return max(1, int(h.get("max_attempts") or MAX_ATTEMPTS_DEFAULT))
    except (TypeError, ValueError):
        return MAX_ATTEMPTS_DEFAULT


def exhausted(h):
    """True when a job has used up its attempts."""
    return attempts_of(h) >= max_attempts_of(h)


def deps_of(h):
    """Job ids this job waits for, in declared order ([] when none)."""
    return [d.strip() for d in (h.get("deps") or "").split(",") if d.strip()]


def deps_ready(jid, h):
    """(ready, reason) — whether every job this one waits for is done.

    A dependency counts as satisfied only when it is `done`. A
    dependency that is blocked or dead-lettered will never become done
    on its own, so it is reported as the reason instead of being waited
    on in silence.
    """
    ids = deps_of(h)
    if not ids:
        return True, ""
    for dep in ids:
        dh, _ = job_get(dep)
        if dh is None:
            return False, f"dependency {dep} no longer exists"
        status = dh.get("status", "?")
        if status != "done":
            why = dh.get("dead_reason") or dh.get("note") or ""
            return False, (f"dependency {dep} is {status}"
                           + (f": {why.strip()[:120]}" if why.strip() else ""))
    return True, ""


def _file_conflicts(jid, h):
    """Other LIVE jobs whose declared files overlap this job's.

    Two workers editing the same file cannot both be right, and the
    overseer merges branches — so a claim on overlapping files is
    refused instead of producing a conflict nobody asked for. A job only
    conflicts while it is claimed AND its lease mutex is alive; a dead
    lease is sweep's problem, not a blocker here.

    Returns [(other_jid, [shared paths])].
    """
    mine = files_of(h)
    if not mine:
        return []
    out = []
    try:
        ids = _all_ids()
    except Exception:
        return []  # unreadable index: don't block work on a lookup failure
    for other in ids:
        if str(other) == str(jid):
            continue
        oh = _hgetall(_job_key(other))
        if oh.get("status") != "claimed" or _mutex_holder(other) is None:
            continue
        shared = sorted(mine & files_of(oh))
        if shared:
            out.append((str(other), shared))
    return out


_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "OpenAI-style API key"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "Google API key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
                r"[A-Za-z0-9_-]{10,}\b"), "JWT"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b"
                r"\s*[:=]\s*[^\s'\"]{12,}"), "credential assignment"),
)


def scan_for_secrets(text):
    """Label anything in text that looks like a live credential.

    A job spec is stored in plaintext and its title is announced on the
    bus, so a credential pasted into one is a credential handed to
    everyone holding the bus token. This is a tripwire, not a proof —
    it names the KIND of match and the line, and never echoes the
    matched text, because printing the secret would undo the point.

    Returns [(lineno, label)].
    """
    hits = []
    for lineno, line in enumerate((text or "").splitlines(), 1):
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label))
                break
    return hits


# --- Signed jobs: HMAC-SHA256 over canonical job content -------------------
#
# Bus nicks are unauthenticated, so anyone can post JOB: as the overseer.
# A worker that executes a spoofed spec hands RCE to the spoofer. Signing
# closes that hole for crews that share a secret: the overseer signs at
# post time, the worker verifies before claiming.
#
# The secret is distributed OUT OF BAND — the user tells both sides
# directly (a file both machines already have, a DM on another channel,
# etc.). It must NEVER appear on the bus, in a spec, or in a commit.
#
# Keyring file format (chmod 600): one key per line as "kid:secret".
# The FIRST line's key signs; every line's key verifies (so two kids can
# be active during rotation). A legacy single-token file (no colon) still
# works and is treated as kid "default". Secrets must not contain
# whitespace, colons, or newlines.
#
# Signature versions: v1 is the original payload (title/spec/accept/
# branch/base/room, single secret). v2 binds the signature to the exact
# job instance (jid, kid, created_at, expires_at, nonce) so a captured
# signature cannot be transplanted onto another job. New posts are v4,
# which adds req/files/role and deps/max_attempts; v1–v3 signatures
# still verify for jobs posted before each upgrade.

SIG_VERSION = "4"


def _read_keyring(path):
    """Read crew secret(s). Returns (dict {kid: bytes}, default_kid, None)
    or (None, None, error string)."""
    try:
        with open(os.path.expanduser(path), "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, None, f"cannot read secret file {path!r}: {e}"
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()
             if ln.strip()]
    if not lines:
        return None, None, f"secret file {path!r} is empty"
    keys = {}
    if any(":" in ln for ln in lines):
        for ln in lines:
            if ln.count(":") != 1:
                return None, None, \
                    f"bad keyring line (want kid:secret): {ln!r}"
            kid, sec = (p.strip() for p in ln.split(":"))
            if not kid or not sec or any(c.isspace() for c in kid):
                return None, None, f"bad keyring line: {ln!r}"
            keys[kid] = sec.encode("utf-8")
    else:
        if len(lines) > 1:
            return None, None, \
                f"{path!r}: multiple secrets need kid:secret lines"
        keys["default"] = lines[0].encode("utf-8")
    return keys, next(iter(keys)), None


def _canonical_payload_v1(fields, spec):
    """Original payload: what v1 signatures cover."""
    payload = {
        "title": fields.get("title", ""),
        "spec": spec,
        "accept": fields.get("accept", ""),
        "branch": fields.get("branch", ""),
        "base": fields.get("base", ""),
        "room": fields.get("room", ""),
    }
    return json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _canonical_payload_v2(jid, kid, fields, spec):
    """v2 payload: v1 fields plus the exact job instance identity.

    Binding jid/kid/created_at/nonce means a signature lifted from one job
    does not verify on any other job, even with identical content.
    """
    payload = {
        "jid": str(jid),
        "kid": str(kid),
        "title": fields.get("title", ""),
        "spec": spec,
        "accept": fields.get("accept", ""),
        "branch": fields.get("branch", ""),
        "base": fields.get("base", ""),
        "room": fields.get("room", ""),
        # Redis stores hash values as strings; stringify here so the
        # post-time computation matches the verify-time recomputation.
        "created_at": str(fields.get("created_at", "")),
        "expires_at": str(fields.get("expires_at", "")),
        "nonce": str(fields.get("nonce", "")),
    }
    return json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign_job_v1(secret, fields, spec):
    return hmac.new(secret, _canonical_payload_v1(fields, spec),
                    hashlib.sha256).hexdigest()


def _v3_payload(jid, kid, fields, spec):
    """Payload shared by v3 and v4: v2 plus requirements and files.

    Those two fields decide who may take the job and which files are
    theirs, so a signature that ignored them would leave the ownership
    declaration editable. v2 signatures still verify (see verify_job).
    """
    return {
        "jid": str(jid),
        "kid": str(kid),
        "title": fields.get("title", ""),
        "spec": spec,
        "accept": fields.get("accept", ""),
        "branch": fields.get("branch", ""),
        "base": fields.get("base", ""),
        "room": fields.get("room", ""),
        "req": ",".join(sorted(reqs_of(fields))),
        "files": ",".join(sorted(files_of(fields))),
        "role": ",".join(sorted(roles_of(fields))),
        "created_at": str(fields.get("created_at", "")),
        "expires_at": str(fields.get("expires_at", "")),
        "nonce": str(fields.get("nonce", "")),
    }


def _canonical_payload_v3(jid, kid, fields, spec):
    return json.dumps(_v3_payload(jid, kid, fields, spec), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _canonical_payload_v4(jid, kid, fields, spec):
    """v4 payload: v3 plus the job's dependencies and attempt budget.

    deps decide WHEN a job may be claimed and max_attempts decides
    whether a failure reopens it or dead-letters it, so a signature
    that ignored them would leave the queue's flow editable. `attempts`
    is deliberately NOT signed: it moves on every claim, so a signature
    over it could never be verified after the first one.
    """
    payload = _v3_payload(jid, kid, fields, spec)
    payload["deps"] = ",".join(deps_of(fields))
    payload["max_attempts"] = str(fields.get("max_attempts", ""))
    return json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign_job_v2(secret, jid, kid, fields, spec):
    return hmac.new(secret, _canonical_payload_v2(jid, kid, fields, spec),
                    hashlib.sha256).hexdigest()


def _sign_job_v3(secret, jid, kid, fields, spec):
    return hmac.new(secret, _canonical_payload_v3(jid, kid, fields, spec),
                    hashlib.sha256).hexdigest()


def _sign_job_v4(secret, jid, kid, fields, spec):
    return hmac.new(secret, _canonical_payload_v4(jid, kid, fields, spec),
                    hashlib.sha256).hexdigest()


def verify_job(jid, keyring):
    """Check a job's signature.

    Returns one of 'OK', 'BAD', 'UNSIGNED', 'EXPIRED'.
    Raises ValueError for unknown job ids; other exceptions are transport
    failures from the state reads.
    """
    h, spec = job_get(jid)
    if h is None:
        raise ValueError(f"no such job: {jid}")
    sig = h.get("sig")
    if not sig:
        return "UNSIGNED"
    version = h.get("sig_v", "1")
    if version == "1":
        secret = keyring.get("default")
        if secret is None:
            return "BAD"
        good = hmac.compare_digest(
            _sign_job_v1(secret, h, spec or ""), sig)
        return "OK" if good else "BAD"
    if version not in ("2", "3", "4"):
        return "BAD"
    kid = h.get("sig_kid", "")
    secret = keyring.get(kid)
    if secret is None:
        return "BAD"  # signed with an unknown/retired key
    exp = h.get("expires_at", "")
    if exp and int(time.time()) > int(exp):
        return "EXPIRED"
    sign = {"2": _sign_job_v2, "3": _sign_job_v3,
            "4": _sign_job_v4}[version]
    good = hmac.compare_digest(sign(secret, jid, kid, h, spec or ""), sig)
    return "OK" if good else "BAD"


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


def settle_failed(jid, h, room, reason="lease expired"):
    """Hand a failed or abandoned claim back, or dead-letter it.

    Used by requeue, sweep and the overseer's acceptance failure: all
    three mean "this attempt did not work". A job with attempts left is
    reopened for another worker; a job with none is dead-lettered so a
    spec nobody can satisfy stops consuming the crew every cycle and
    becomes visible instead. Reopening a dead job is a deliberate human
    act (`jobs.py reset <id>`).

    Returns the job's new status: "open" or "dead".
    """
    _hdel(_job_key(jid), "claim", "claim_host", "claimed_at", "lease_until",
          "result_commit", "verified", "verified_at", "verify_report")
    try:
        api_get(f"del/{_claim_key(jid)}")
    except Exception:
        pass
    used = attempts_of(h)
    if exhausted(h):
        _hset(_job_key(jid), {
            "status": "dead", "dead_at": int(time.time()),
            "dead_reason": f"{reason} after {used} attempt(s)",
        })
        _announce(room, f"DEAD:{jid} {reason} (after {used} attempt(s))")
        return "dead"
    _hset(_job_key(jid), {"status": "open"})
    _announce(room, f"REQUEUE:{jid}")
    return "open"


def _claim_or_requeue(jid, h):
    """If h is claimed but its mutex is gone, the lease expired: settle it.

    Returns the (possibly refreshed) fields dict."""
    if h.get("status") == "claimed" and _mutex_holder(jid) is None:
        settle_failed(jid, h, h.get("room") or "")
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
    reqs = sorted({r.strip().lower()
                   for r in (args.req or "").split(",") if r.strip()})
    files = sorted({_norm_path(f)
                    for f in (args.files or "").split(",") if f.strip()})
    job_roles = sorted({r.strip().lower()
                        for r in (args.role or "").split(",") if r.strip()})
    hits = scan_for_secrets(spec)
    if hits and not args.allow_secrets:
        for lineno, label in hits:
            print(f"ERROR: spec line {lineno} looks like a {label}",
                  file=sys.stderr)
        print("ERROR: refusing to post — the spec is stored in plaintext "
              "and the job title is announced on the bus. Hand the "
              "credential over out of band, or re-post with "
              "--allow-secrets if this is a false positive",
              file=sys.stderr)
        return 2
    deps = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()]
    for dep in deps:
        if not _valid_id(dep):
            print(f"ERROR: --depends-on wants job ids, got {dep!r}",
                  file=sys.stderr)
            return 2
        dh, _ = job_get(dep)
        if dh is None:
            print(f"ERROR: no such job to depend on: {dep}", file=sys.stderr)
            return 2
    deps = sorted(set(deps))
    max_attempts = args.max_attempts
    if max_attempts < 1:
        print("ERROR: --max-attempts needs at least 1", file=sys.stderr)
        return 2
    try:
        jid = str(json.loads(api_get(f"incr/{_seq_key()}"))["result"])
        now = int(time.time())
        fields = {
            "title": args.title, "room": room, "branch": args.branch,
            "base": args.base, "accept": args.accept, "status": "open",
            "lease": lease, "created_by": NICK, "created_host": INSTANCE_ID,
            "created_at": now, "req": ",".join(reqs),
            "files": ",".join(files), "role": ",".join(job_roles),
            "deps": ",".join(deps), "max_attempts": str(max_attempts),
        }
        _hset(_job_key(jid), fields)
        api_post(f"set/{_spec_key(jid)}", spec)
        api_get(f"zadd/{_index_key()}/{now}/{jid}")
    except Exception as e:
        print(f"RELAY_ERROR: post failed ({e})", file=sys.stderr)
        return 2
    if args.secret_file:
        keyring, default_kid, kerr = _read_keyring(args.secret_file)
        if kerr:
            print(f"ERROR: {kerr}", file=sys.stderr)
            return 2
        kid = args.kid or default_kid
        secret = keyring.get(kid)
        if secret is None:
            print(f"ERROR: unknown kid {kid!r} "
                  f"(keyring has: {', '.join(sorted(keyring))})",
                  file=sys.stderr)
            return 2
        if args.expires is not None and args.expires <= 0:
            print("ERROR: --expires needs a positive number of seconds",
                  file=sys.stderr)
            return 2
        nonce = secrets.token_hex(8)
        expires_at = str(now + args.expires) if args.expires else ""
        sig_fields = dict(fields)
        sig_fields["nonce"] = nonce
        sig_fields["expires_at"] = expires_at
        try:
            _hset(_job_key(jid), {
                "sig": _sign_job_v4(secret, jid, kid, sig_fields, spec),
                "sig_v": SIG_VERSION,
                "sig_kid": kid,
                "signed_by": NICK,
                "nonce": nonce,
                "expires_at": expires_at,
            })
        except Exception as e:
            print(f"RELAY_ERROR: signing failed ({e})", file=sys.stderr)
            return 2
        print(f"SIGNED {jid} kid={kid}", file=sys.stderr)
    _announce(room, f"JOB:{jid} {args.title}")
    print(f"JOB {jid}")
    return 0


def cmd_list(args):
    try:
        ids = _all_ids()
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
              "req", "files", "role", "deps", "attempts", "max_attempts",
              "dead_at", "dead_reason", "claim", "claim_host", "claimed_at",
              "lease_until", "result_commit", "note", "verified",
              "verified_at", "verify_report", "created_by", "created_host",
              "created_at", "lease", "signed_by", "sig_v", "sig_kid",
              "expires_at", "nonce"):
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
    if args.secret_file and h.get("sig"):
        keyring, _, kerr = _read_keyring(args.secret_file)
        if kerr:
            print(f"ERROR: {kerr}", file=sys.stderr)
            return 2
        try:
            verdict = verify_job(args.id, keyring)
        except Exception as e:
            print(f"RELAY_ERROR: verify failed ({e})", file=sys.stderr)
            return 2
        if verdict in ("BAD", "EXPIRED"):
            # Loud refusal: a BAD/EXPIRED signature may be an active spoof
            # attempt, so the whole crew sees it. Best-effort; never fails
            # the refusal itself.
            _announce(args.room or h.get("room") or "",
                      f"REJECTED:{args.id} {verdict} "
                      f"signed_by={h.get('signed_by', '?')} "
                      f"rejected_by={NICK}")
            print(f"ERROR: job {args.id} signature {verdict} — not claiming",
                  file=sys.stderr)
            return 1
        # OK: fall through and claim. (Unsigned jobs have no sig to check;
        # the worker chose to verify, so an unsigned job is claimed as-is.)
    elif args.secret_file:
        print(f"WARNING: job {args.id} is unsigned — claiming without "
              f"verification", file=sys.stderr)
    h = _claim_or_requeue(args.id, h)
    if h.get("status") != "open":
        if h.get("status") == "dead":
            print(f"ERROR: job {args.id} is dead-lettered "
                  f"({h.get('dead_reason') or 'attempts exhausted'}) — "
                  f"fix the spec, then `jobs.py reset {args.id}` to give "
                  f"it a fresh budget", file=sys.stderr)
        else:
            print(f"ERROR: job {args.id} is {h.get('status')}"
                  + (f" (claimed by {h.get('claim')})"
                     if h.get("status") == "claimed" else ""),
                  file=sys.stderr)
        return 2
    ready, why = deps_ready(args.id, h)
    if not ready:
        print(f"ERROR: job {args.id} is waiting on its dependencies: "
              f"{why}", file=sys.stderr)
        return 2
    if exhausted(h):
        print(f"ERROR: job {args.id} has used all {max_attempts_of(h)} "
              f"attempt(s) and is exhausted — `jobs.py reset {args.id}` "
              f"reopens it deliberately", file=sys.stderr)
        return 2
    # File-ownership guard: a job that declares files owns them while it
    # is live, so two workers never get dispatched onto the same file.
    if not args.force:
        conflicts = _file_conflicts(args.id, h)
        if conflicts:
            for other, shared in conflicts:
                print(f"ERROR: job {args.id} declares file(s) already "
                      f"held by live job {other}: {', '.join(shared)}",
                      file=sys.stderr)
            print("ERROR: refusing the claim — wait for the other job to "
                  "finish, split the work, or re-claim with --force",
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
            # Every claim is an attempt: the counter is what stops a job
            # nobody can satisfy from being retried forever.
            "attempts": attempts_of(h) + 1,
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


def cmd_verify(args):
    keyring, _, kerr = _read_keyring(args.secret_file)
    if kerr:
        print(f"ERROR: {kerr}", file=sys.stderr)
        return 2
    try:
        verdict = verify_job(args.id, keyring)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"RELAY_ERROR: verify failed ({e})", file=sys.stderr)
        return 2
    print(verdict)
    return 0 if verdict == "OK" else 1


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
        state = settle_failed(args.id, h,
                              args.room or h.get("room") or "")
    except Exception as e:
        print(f"RELAY_ERROR: requeue failed ({e})", file=sys.stderr)
        return 2
    if state == "dead":
        print(f"DEAD_LETTERED {args.id} (attempts exhausted)")
        return 0
    print(f"REQUEUED {args.id}")
    return 0


def cmd_reset(args):
    """Reopen a dead-lettered (or exhausted) job with a fresh budget.

    The deliberate human step the dead-letter rule hands to: after
    fixing the spec, give the job its attempts back. Refused while a
    live claim holds it, so a reset can never yank work in progress.
    """
    h, _ = job_get(args.id)
    if h is None:
        print(f"ERROR: no such job: {args.id}", file=sys.stderr)
        return 2
    if h.get("status") == "claimed" and _mutex_holder(args.id) is not None:
        print(f"ERROR: job {args.id} is still claimed by "
              f"'{h.get('claim')}' — requeue or wait for it to finish",
              file=sys.stderr)
        return 2
    try:
        _hset(_job_key(args.id), {"status": "open", "attempts": "0"})
        _hdel(_job_key(args.id), "dead_at", "dead_reason", "claim",
              "claim_host", "claimed_at", "lease_until", "result_commit",
              "verified", "verified_at", "verify_report",
              "stuck_alerted_at")
        api_get(f"del/{_claim_key(args.id)}")
    except Exception as e:
        print(f"RELAY_ERROR: reset failed ({e})", file=sys.stderr)
        return 2
    _announce(args.room or h.get("room") or "", f"RESET:{args.id}")
    print(f"RESET {args.id}")
    return 0


def cmd_sweep(args):
    try:
        ids = _all_ids()
    except Exception as e:
        print(f"RELAY_ERROR: sweep failed ({e})", file=sys.stderr)
        return 2
    n = 0
    for jid in ids:
        h, _ = job_get(jid)
        if h and h.get("status") == "claimed" and \
                _mutex_holder(jid) is None:
            try:
                state = settle_failed(jid, h,
                                      args.room or h.get("room") or "")
                if state == "dead":
                    print(f"DEAD_LETTERED {jid} (attempts exhausted)")
                else:
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
    p.add_argument("--req", default="",
                   help="required caps, comma-separated (persisted so a "
                        "worker that missed the DM can still match)")
    p.add_argument("--files", default="",
                   help="files this job owns, comma-separated; a second "
                        "live job claiming any of them is refused")
    p.add_argument("--role", default="",
                   help="role skills the worker should hold "
                        "(see roles/), comma-separated")
    p.add_argument("--depends-on", default="",
                   help="job ids this job waits for, comma-separated; it "
                        "cannot be claimed until they are done")
    p.add_argument("--max-attempts", type=int,
                   default=MAX_ATTEMPTS_DEFAULT,
                   help="claims allowed before the job is dead-lettered "
                        f"(default: {MAX_ATTEMPTS_DEFAULT})")
    p.add_argument("--allow-secrets", action="store_true",
                   help="post even when the spec looks like it contains "
                        "a credential")
    p.add_argument("--lease", type=int, default=LEASE_DEFAULT)
    p.add_argument("--secret-file", default="",
                   help="sign the job with this crew secret (HMAC-SHA256)")
    p.add_argument("--kid", default="",
                   help="key id to sign with (default: first key in the file)")
    p.add_argument("--expires", type=int, default=None,
                   help="signature validity in seconds from post time")
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--status", default="all",
                   choices=["all", "open", "claimed", "done", "blocked",
                            "dead"])
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show a job and its spec")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomically claim a job")
    p.add_argument("id")
    p.add_argument("--room", default="", help="announce room override")
    p.add_argument("--lease", type=int, default=LEASE_DEFAULT)
    p.add_argument("--secret-file", default="",
                   help="verify a signed job's HMAC before claiming; "
                        "refuses on BAD signature")
    p.add_argument("--force", action="store_true",
                   help="claim even when another live job owns one of "
                        "this job's declared files")
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

    p = sub.add_parser("reset",
                       help="reopen a dead-lettered job with fresh attempts")
    p.add_argument("id")
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("sweep",
                       help="requeue all lease-expired claims")
    p.add_argument("--room", default="", help="announce room override")
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("verify", help="verify a signed job's HMAC")
    p.add_argument("id")
    p.add_argument("--secret-file", required=True,
                   help="crew secret file to verify against")
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
