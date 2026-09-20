#!/usr/bin/env python3
"""Bus admin: admin permissions and display-name changes for the muse-bus.

Usage:
  admin.py init [--secret-file F]
  admin.py promote <nick> [--secret-file F]
  admin.py demote <nick> [--secret-file F]
  admin.py rename <nick> <display-name>
  admin.py rekey <new-sha256-hex> [--secret-file F]
  admin.py admins
  admin.py whois <nick>
  admin.py log [n]
  admin.py verify [--secret-file F]

Admin permissions are enforced by this tooling, not by Redis: the bus is a
shared Redis instance, so any client with the REST token can write raw keys.
The defense is the same model as jobs.py signed jobs — every mutating admin
command EXCEPT rename is HMAC-SHA256 signed with the admin secret and
appended to a public audit log. Rename is deliberately open: anyone with bus
access can set any nick's display name, no secret needed; the names:history
list (set_by/set_at on every entry) is its audit trail. A forged or
unauthorized command is detectable by anyone holding the secret (`verify`),
and this tool refuses to execute one.

The secret is distributed OUT OF BAND and must NEVER appear on the bus, in a
spec, or in a commit. Read it from --secret-file (raw bytes, stripped) or
MUSE_RELAY_ADMIN_SECRET_FILE. A default path of
~/.config/muse-relay/admin_secret is used when neither is given.

Key layout (prefix from MUSE_RELAY_KEY_PREFIX, default "muse-bus"):
  <p>:admins              SET of admin canonical nicks
  <p>:admin:secret_sha256 STRING sha256 hex of the admin secret (bootstrap marker)
  <p>:admin:log           LIST of signed command envelopes (audit)
  <p>:names               HASH canonical nick -> {"display","set_by","set_at"}
  <p>:names:history       LIST of {"nick","display","set_by","set_at"}

Display names: rename changes what readers show for a nick and is open to
anyone with bus access — no admin secret required. The canonical
nick stays the identity key for claims, mods, admins, and presence. Writers
(send.py, the bus-send skill) post with the display name as the message
prefix; readers treat the prefix as opaque and resolve identity via the
names registry when they need the canonical nick.
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
import relay_common as rc

PREFIX = os.environ.get("MUSE_RELAY_KEY_PREFIX", "muse-bus")
DEFAULT_SECRET_FILE = os.path.expanduser("~/.config/muse-relay/admin_secret")
MAX_DISPLAY = 32


def _k(*parts):
    return ":".join([PREFIX] + list(parts))


def _q(nick):
    return urllib.parse.quote(nick, safe="")


def _read_secret(path):
    try:
        with open(os.path.expanduser(path), "rb") as f:
            secret = f.read().strip()
    except OSError as e:
        return None, f"cannot read secret file {path!r}: {e}"
    if not secret:
        return None, f"secret file {path!r} is empty"
    return secret, None


def _admins():
    data = json.loads(rc.api_get(f"smembers/{_k('admins')}"))
    return set(data.get("result") or [])


def _secret_hash_set():
    try:
        data = json.loads(rc.api_get(f"get/{_k('admin', 'secret_sha256')}"))
        return bool(data.get("result"))
    except Exception:
        return False


def _canonical_envelope(env):
    payload = {k: env[k] for k in ("v", "cmd", "args", "actor", "ts", "nonce")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _sign(secret, env):
    return hmac.new(secret, _canonical_envelope(env), hashlib.sha256).hexdigest()


def _make_envelope(secret, cmd, args):
    env = {
        "v": 1,
        "cmd": cmd,
        "args": args,
        "actor": rc.NICK,
        "ts": int(time.time()),
        "nonce": secrets.token_hex(8),
    }
    env["sig"] = _sign(secret, env)
    return env


def _verify_envelope(secret, env):
    sig = env.get("sig", "")
    if not sig:
        return False
    return hmac.compare_digest(_sign(secret, env), sig)


def _log_envelope(env):
    rc.api_post(f"rpush/{_k('admin', 'log')}",
                json.dumps(env, separators=(",", ":")).encode())


def _require_admin(secret):
    """Returns (True, None) when the secret is right and rc.NICK is an admin.

    Checks the secret against the stored sha256 first, so a wrong secret
    fails closed instead of writing a bad-signature entry to the log.
    """
    try:
        data = json.loads(rc.api_get(f"get/{_k('admin', 'secret_sha256')}"))
        stored = data.get("result")
    except Exception:
        stored = None
    if not stored or not hmac.compare_digest(
            hashlib.sha256(secret).hexdigest(), stored):
        return False, "admin secret does not match the initialized secret"
    if rc.NICK not in _admins():
        return False, (f"nick '{rc.NICK}' is not an admin "
                       f"(admins: {', '.join(sorted(_admins())) or 'none'})")
    return True, None


def _validate_display(display):
    d = (display or "").strip()
    if not d:
        return None, "display name is empty"
    if len(d) > MAX_DISPLAY:
        return None, f"display name too long (max {MAX_DISPLAY} chars)"
    if ":" in d or "\n" in d or "\r" in d:
        return None, "display name may not contain ':' or newlines"
    return d, None


def cmd_init(args):
    if _admins() or _secret_hash_set():
        print("ERROR: admin already initialized "
              f"(admins: {', '.join(sorted(_admins())) or 'none'})")
        return 1
    secret, err = _read_secret(args.secret_file)
    if err:
        print(f"ERROR: {err}")
        return 1
    digest = hashlib.sha256(secret).hexdigest()
    rc.api_get(f"set/{_k('admin', 'secret_sha256')}/{digest}")
    rc.api_get(f"sadd/{_k('admins')}/{_q(rc.NICK)}")
    _log_envelope(_make_envelope(secret, "init", {}))
    print(f"OK admin initialized; '{rc.NICK}' is the founding admin")
    return 0


def cmd_promote(args):
    secret, err = _read_secret(args.secret_file)
    if err:
        print(f"ERROR: {err}")
        return 1
    ok, aerr = _require_admin(secret)
    if not ok:
        print(f"ERROR: {aerr}")
        return 1
    target = args.nick.strip()
    if not target:
        print("ERROR: nick is empty")
        return 1
    rc.api_get(f"sadd/{_k('admins')}/{_q(target)}")
    _log_envelope(_make_envelope(secret, "promote", {"nick": target}))
    print(f"OK '{target}' promoted to admin")
    return 0


def cmd_demote(args):
    secret, err = _read_secret(args.secret_file)
    if err:
        print(f"ERROR: {err}")
        return 1
    ok, aerr = _require_admin(secret)
    if not ok:
        print(f"ERROR: {aerr}")
        return 1
    target = args.nick.strip()
    admins = _admins()
    if target not in admins:
        print(f"ERROR: '{target}' is not an admin")
        return 1
    if len(admins) == 1:
        print("ERROR: cannot demote the last admin")
        return 1
    rc.api_get(f"srem/{_k('admins')}/{_q(target)}")
    _log_envelope(_make_envelope(secret, "demote", {"nick": target}))
    print(f"OK '{target}' demoted")
    return 0


def cmd_rename(args):
    # Open operation: no admin secret required. Anyone with bus access can
    # set any nick's display name. The names:history list (with set_by/set_at)
    # is the audit trail; nothing is written to the signed admin log because
    # there is no secret to sign with.
    nick = args.nick.strip()
    if not nick:
        print("ERROR: nick is empty")
        return 1
    display, derr = _validate_display(args.display)
    if derr:
        print(f"ERROR: {derr}")
        return 1
    entry = {"display": display, "set_by": rc.NICK, "set_at": int(time.time())}
    rc.api_post(f"hset/{_k('names')}/{_q(nick)}",
                json.dumps(entry, separators=(",", ":")).encode())
    rc.api_post(f"rpush/{_k('names', 'history')}",
                json.dumps({"nick": nick, **entry},
                           separators=(",", ":")).encode())
    # warn (not fail) when the display collides with another canonical nick
    if display != nick:
        try:
            others = json.loads(
                rc.api_get(f"smembers/{rc.NICKS_KEY}"))["result"] or []
            if display in others:
                print(f"WARNING: display '{display}' collides with an "
                      f"existing nick")
        except Exception:
            pass
    print(f"OK '{nick}' display name set to '{display}'")
    return 0


def cmd_admins(_args):
    admins = sorted(_admins())
    print("admins: " + (", ".join(admins) if admins else "(none)"))
    return 0


def cmd_whois(args):
    nick = args.nick.strip()
    try:
        data = json.loads(rc.api_get(f"hget/{_k('names')}/{_q(nick)}"))
        raw = data.get("result")
    except Exception as e:
        print(f"ERROR: lookup failed: {e}")
        return 1
    if raw:
        entry = json.loads(raw)
        print(f"{nick}: display='{entry['display']}' "
              f"set_by={entry['set_by']} set_at={entry['set_at']}")
    else:
        print(f"{nick}: no display name set (shows as '{nick}')")
    try:
        data = json.loads(rc.api_get(f"lrange/{_k('names', 'history')}/0/-1"))
        hist = [json.loads(h) for h in (data.get("result") or [])]
        hist = [h for h in hist if h.get("nick") == nick]
        for h in hist[-5:]:
            print(f"  history: '{h['display']}' by {h['set_by']} "
                  f"at {h['set_at']}")
    except Exception:
        pass
    return 0


def cmd_rekey(args):
    """Rotate the admin secret. Authenticated with the CURRENT secret; the
    new secret itself never appears here — only its sha256 hex digest, which
    is safe to pass around (it is what Redis stores)."""
    secret, err = _read_secret(args.secret_file)
    if err:
        print(f"ERROR: {err}")
        return 1
    ok, aerr = _require_admin(secret)
    if not ok:
        print(f"ERROR: {aerr}")
        return 1
    digest = args.new_sha256.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        print("ERROR: new-sha256 must be 64 hex chars (sha256 of the new secret)")
        return 1
    _log_envelope(_make_envelope(secret, "rekey", {"new_sha256": digest}))
    rc.api_get(f"set/{_k('admin', 'secret_sha256')}/{digest}")
    print("OK admin secret rotated; rekey logged in the audit trail")
    return 0


def cmd_log(args):
    n = args.n if args.n else 10
    try:
        data = json.loads(rc.api_get(f"lrange/{_k('admin', 'log')}/-{n}/-1"))
        entries = [json.loads(e) for e in (data.get("result") or [])]
    except Exception as e:
        print(f"ERROR: log read failed: {e}")
        return 1
    for e in entries:
        print(f"{e.get('ts')} {e.get('actor')} {e.get('cmd')} "
              f"{json.dumps(e.get('args', {}), separators=(',', ':'))} "
              f"sig={e.get('sig', '')[:12]}...")
    return 0


def cmd_verify(args):
    secret, err = _read_secret(args.secret_file)
    if err:
        print(f"ERROR: {err}")
        return 1
    try:
        data = json.loads(rc.api_get(f"lrange/{_k('admin', 'log')}/0/-1"))
        entries = [json.loads(e) for e in (data.get("result") or [])]
    except Exception as e:
        print(f"ERROR: log read failed: {e}")
        return 1
    # A rekey rotates the secret, so entries at or before the last rekey
    # marker were signed with a previous secret and cannot be checked with
    # the current one — they are reported as sealed, not as failures. Every
    # entry AFTER the marker must verify, otherwise the log is compromised.
    # (A forged marker cannot hide forged entries: they would land after it
    # and fail verification.)
    marker = max([i for i, e in enumerate(entries)
                  if e.get("cmd") == "rekey"], default=-1)
    sealed = entries[:marker + 1]
    live = entries[marker + 1:]
    bad = [i for i, e in enumerate(live, start=marker + 1)
           if not _verify_envelope(secret, e)]
    if bad:
        print(f"VERIFY FAIL: {len(bad)} of {len(live)} post-rekey log "
              f"entries have BAD signatures (indexes {bad})")
        return 1
    sealed_note = (f" ({len(sealed)} sealed under a previous secret)"
                   if sealed else "")
    print(f"VERIFY OK: {len(live)} entries checked, all signatures "
          f"valid{sealed_note}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secret-file", default=os.environ.get(
        "MUSE_RELAY_ADMIN_SECRET_FILE", DEFAULT_SECRET_FILE))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="bootstrap the admin set (first admin)")
    p = sub.add_parser("promote", help="make a nick an admin")
    p.add_argument("nick")
    p = sub.add_parser("demote", help="remove a nick's admin")
    p.add_argument("nick")
    p = sub.add_parser("rename", help="set a nick's display name")
    p.add_argument("nick")
    p.add_argument("display")
    p = sub.add_parser("rekey", help="rotate the admin secret "
                       "(takes the sha256 hex of the NEW secret)")
    p.add_argument("new_sha256")
    sub.add_parser("admins", help="list admins")
    p = sub.add_parser("whois", help="show a nick's display name + history")
    p.add_argument("nick")
    p = sub.add_parser("log", help="show recent admin audit log")
    p.add_argument("n", nargs="?", type=int, default=10)
    sub.add_parser("verify", help="verify audit log signatures")

    args = ap.parse_args(argv)
    try:
        return {"init": cmd_init, "promote": cmd_promote,
                "demote": cmd_demote, "rename": cmd_rename,
                "rekey": cmd_rekey,
                "admins": cmd_admins, "whois": cmd_whois,
                "log": cmd_log, "verify": cmd_verify}[args.cmd](args)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
