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
  jobs.py verify <id> --secret-file PATH

Signed jobs (defense against spoofed overseers):
  jobs.py post ... --secret-file ~/.config/muse-relay/job-secret
  jobs.py claim <id> --secret-file ~/.config/muse-relay/job-secret

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
reopens it. Wire messages (JOB:, CLAIM:, ...) are best-effort chat
announcements; the Redis state is the source of truth.

Never prints the token.
"""
import argparse
import hashlib
import hmac
import json
import os
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
# signature cannot be transplanted onto another job. New posts are v2;
# v1 signatures still verify for jobs posted before the upgrade.

SIG_VERSION = "2"


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


def _sign_job_v2(secret, jid, kid, fields, spec):
    return hmac.new(secret, _canonical_payload_v2(jid, kid, fields, spec),
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
    if version != "2":
        return "BAD"
    kid = h.get("sig_kid", "")
    secret = keyring.get(kid)
    if secret is None:
        return "BAD"  # signed with an unknown/retired key
    exp = h.get("expires_at", "")
    if exp and int(time.time()) > int(exp):
        return "EXPIRED"
    good = hmac.compare_digest(
        _sign_job_v2(secret, jid, kid, h, spec or ""), sig)
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
        fields = {
            "title": args.title, "room": room, "branch": args.branch,
            "base": args.base, "accept": args.accept, "status": "open",
            "lease": lease, "created_by": NICK, "created_host": INSTANCE_ID,
            "created_at": now,
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
                "sig": _sign_job_v2(secret, jid, kid, sig_fields, spec),
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
    p.add_argument("--secret-file", default="",
                   help="sign the job with this crew secret (HMAC-SHA256)")
    p.add_argument("--kid", default="",
                   help="key id to sign with (default: first key in the file)")
    p.add_argument("--expires", type=int, default=None,
                   help="signature validity in seconds from post time")
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
    p.add_argument("--secret-file", default="",
                   help="verify a signed job's HMAC before claiming; "
                        "refuses on BAD signature")
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

    p = sub.add_parser("verify", help="verify a signed job's HMAC")
    p.add_argument("id")
    p.add_argument("--secret-file", required=True,
                   help="crew secret file to verify against")
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
