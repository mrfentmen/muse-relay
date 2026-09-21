#!/usr/bin/env python3
"""MOS command line: the overseer's and worker's hands.

Usage:
  mos.py register --caps python,testing --dm SECRET [--roles builder]
  mos.py caps
  mos.py roles [NAME]              # list role skills, or print one
  mos.py dispatch --title T --spec SPEC [--accept ACCEPT] [--req python]
                  [--role builder] [--files a.py,b.py]
                  [--room R] [--lease SECS] [--branch B]
  mos.py tick [--repo PATH --workdir-base PATH] [--loop --interval SECS]
  mos.py verify <jid> [--repo PATH --workdir-base PATH]
  mos.py worker --dm SECRET --caps python [--once]
  mos.py worker --dm SECRET --caps python --loop --handler 'CMD'
                [--interval SECS] [--max-cycles N] [--scan-open]
                [--max-jobs N]
  mos.py status                    # who is online, what the queue holds
  mos.py doctor [--secret-file F]  # preflight: config, bus, roles, git
  mos.py reconcile
  mos.py review --repo PATH --branch B [--base main]
  mos.py merge --repo PATH --branch B [--base main]

`worker --loop --handler CMD` is the autonomous mode: it polls, claims
jobs its caps and role cover, runs CMD for each one, and reports
done/blocked. See mos/worker.py CommandHandler for what CMD is handed.

Spec/accept may be given inline or via @file. Never prints the token.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
from mos import capabilities, gitops, inspect as inspect_mod, overseer  # noqa: E402
from mos import reconcile, worker  # noqa: E402
from mos import roles as roles_mod  # noqa: E402


def _text(v):
    if v and v.startswith("@"):
        with open(os.path.expanduser(v[1:])) as f:
            return f.read()
    return v or ""


def cmd_register(a):
    caps = [c for c in (a.caps or "").split(",") if c.strip()]
    try:
        capabilities.register(rc.NICK, caps, roles=a.roles or "",
                              max_jobs=a.max_jobs, dm_secret=a.dm or "")
    except ValueError as e:  # e.g. --caps with nothing usable in it
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"REGISTERED {rc.NICK}: {','.join(sorted(caps))}")
    return 0


def cmd_roles(a):
    names = roles_mod.list_roles()
    if not a.name:
        if not names:
            print("NO_ROLES")
            return 0
        for name in names:
            print(name)
        return 0
    try:
        print(roles_mod.load_role(a.name), end="")
    except roles_mod.RoleError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_caps(a):
    agents = capabilities.agents()
    if not agents:
        print("NO_AGENTS")
        return 0
    for nick in sorted(agents):
        r = agents[nick]
        print(f"{nick}: caps={r.get('caps', '')} roles={r.get('roles', '')} "
              f"host={r.get('host', '')} updated={r.get('updated', '')}")
    return 0


def cmd_status(a):
    snap = inspect_mod.snapshot()
    if a.json:
        print(json.dumps(snap, sort_keys=True))
        return 2 if snap.get("error") else 0
    for line in inspect_mod.status_lines(snap):
        print(line)
    return 2 if snap.get("error") else 0


def cmd_doctor(a):
    checks = inspect_mod.doctor_checks(secret_file=a.secret_file or "",
                                       repo=a.repo or None)
    for line in inspect_mod.doctor_lines(checks):
        print(line)
    return 0 if inspect_mod.doctor_ok(checks) else 2


def cmd_dispatch(a):
    reqs = [r for r in (a.req or "").split(",") if r.strip()]
    wants = [r for r in (a.role or "").split(",") if r.strip()]
    try:
        jid, dms = overseer.dispatch(
            a.title, _text(a.spec), accept=_text(a.accept),
            requirements=reqs, room=a.room or "", lease=a.lease,
            branch=a.branch or "", base=a.base or "", roles=wants,
            files=a.files or "")
    except ValueError as e:  # e.g. an empty --spec
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"DISPATCHED {jid}")
    for nick, ok, detail in dms:
        print(f"  DM {nick}: {'OK' if ok else 'FAIL'} {detail}")
    if not dms:
        print("  (no capable workers registered)")
    return 0


def cmd_tick(a):
    if a.loop:
        overseer.tick_loop(repo=a.repo, workdir_base=a.workdir_base,
                           interval=a.interval, max_cycles=a.max_cycles)
        return 0
    rep = overseer.tick(repo=a.repo, workdir_base=a.workdir_base)
    for line in overseer.tick_lines(rep):
        print(line)
    for e in rep["errors"]:
        print(f"ERROR {e}", file=sys.stderr)
    return 0 if not rep["errors"] else 2


def cmd_verify(a):
    ok, report = overseer.verify_done(a.jid, repo=a.repo,
                                      workdir_base=a.workdir_base)
    print(report)
    print("VERIFIED" if ok else "NOT_VERIFIED")
    return 0 if ok else 1


def cmd_worker(a):
    # Validate arguments before touching Redis: a missing --handler is a
    # usage error, not a reason to register anything.
    if a.loop and not (a.handler or "").strip():
        print("ERROR: --loop needs --handler CMD — the command that does "
              "the work for each claimed job", file=sys.stderr)
        return 2
    caps = [c for c in (a.caps or "").split(",") if c.strip()]
    scan = a.scan_open or (a.loop and not a.no_scan_open)
    try:
        w = worker.Worker(dm_secret=a.dm or "", caps=caps,
                          roles=a.roles or "", workdir=a.workdir or ".",
                          lease=a.lease, max_jobs=a.max_jobs, scan_open=scan)
    except roles_mod.RoleError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    w.register()
    w.heartbeat()
    if a.loop:
        # The blank-command case was rejected above, so this cannot raise;
        # the role comes from the worker so the handler runs the persona
        # the worker registered as (MUSE_JOB_ROLE).
        handler = worker.CommandHandler(
            a.handler, workdir=a.workdir or ".",
            role=next(iter(w.role_set), ""), timeout=a.timeout)
        print(f"worker {rc.NICK}: looping every {a.interval:g}s, "
              f"caps={','.join(sorted(w.caps))}"
              + (f", roles={w.roles}" if w.roles else "")
              + (", scanning open jobs" if w.scan_open else ""), flush=True)
        w.run_loop(handler, interval=a.interval, max_cycles=a.max_cycles)
        return 0
    if a.once:
        found = w.poll_dms()
        claimed = None
        for jid, title, reqs in found:
            if not w._matches(reqs):
                print(f"SKIP {jid} (req {','.join(sorted(reqs))})")
                continue
            ok, _ = w._claim(jid)
            if ok:
                claimed = jid
                _, spec = __import__("jobs").job_get(jid)
                print(f"CLAIMED {jid}: {title}")
                print("--- spec ---")
                print(spec)
                break
            print(f"CLAIM_FAILED {jid}")
        if not claimed and not found:
            print("NO_DISPATCHES")
        return 0
    # No --once and no --loop: the shared registration state (register +
    # heartbeat) is already written, so an agent driving this CLI cycle
    # by cycle can just call --once. Be explicit about it.
    print("registered + heartbeated; pass --once for one cycle, or "
          "--loop --handler CMD to run unattended", file=sys.stderr)
    return 0


def cmd_reconcile(a):
    return reconcile.main([])


def cmd_review(a):
    try:
        r = gitops.review_branch(a.repo, a.branch, base=a.base)
    except gitops.GitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"branch {r['branch']} vs {r['base']}: "
          f"{r['commit_count']} commits, {len(r['files'])} files")
    for c in r["commits"]:
        print(f"  {c}")
    if r["stat"]:
        print(r["stat"])
    return 0


def cmd_merge(a):
    try:
        h = gitops.approve_merge(a.repo, a.branch, base=a.base)
    except gitops.GitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"MERGED {a.branch} -> {a.base}: {h}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mos.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register", help="register this agent's capabilities")
    p.add_argument("--caps", required=True, help="comma-separated")
    p.add_argument("--dm", default="", help="DM dead-drop secret")
    p.add_argument("--roles", default="")
    p.add_argument("--max-jobs", type=int, default=1)
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("caps", help="list registered agents")
    p.set_defaults(fn=cmd_caps)

    p = sub.add_parser("status",
                       help="who is online and what the queue holds")
    p.add_argument("--json", action="store_true",
                   help="machine-readable snapshot")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("doctor", help="preflight check the crew's setup")
    p.add_argument("--secret-file", default="",
                   help="also check this crew secret file parses")
    p.add_argument("--repo", default=None,
                   help="also check this path is a usable git repo")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("roles", help="list role skills, or print one")
    p.add_argument("name", nargs="?", default=None,
                   help="role to print (see roles/*.md); omit to list")
    p.set_defaults(fn=cmd_roles)

    p = sub.add_parser("dispatch", help="post a job + DM capable workers")
    p.add_argument("--title", required=True)
    p.add_argument("--spec", required=True, help="text or @file")
    p.add_argument("--accept", default="", help="text or @file")
    p.add_argument("--req", default="", help="required caps, comma-sep")
    p.add_argument("--role", default="",
                   help="role skills the worker must hold (roles/*.md), "
                        "comma-sep; a worker must hold at least one")
    p.add_argument("--files", default="",
                   help="files this job owns, comma-sep; claimed by two "
                        "live jobs is refused")
    p.add_argument("--room", default="")
    p.add_argument("--lease", type=int, default=1800)
    p.add_argument("--branch", default="")
    p.add_argument("--base", default="")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("tick", help="one overseer pass (or a continuous loop)")
    p.add_argument("--repo", default=None)
    p.add_argument("--workdir-base", default=None)
    p.add_argument("--loop", action="store_true",
                   help="keep ticking (sweep leases, prune, verify) "
                        "until interrupted")
    p.add_argument("--interval", type=float, default=60,
                   help="seconds between ticks in --loop mode")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="stop after N cycles (mainly for tests)")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("verify", help="verify a done job's acceptance")
    p.add_argument("jid")
    p.add_argument("--repo", default=None)
    p.add_argument("--workdir-base", default=None)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("worker", help="worker poll/claim cycle")
    p.add_argument("--dm", default="", help="DM dead-drop secret")
    p.add_argument("--caps", default="", help="comma-separated")
    p.add_argument("--roles", default="", help="role skills to work as")
    p.add_argument("--workdir", default=".")
    p.add_argument("--lease", type=int, default=1800)
    p.add_argument("--max-jobs", type=int, default=1,
                   help="claims this worker may hold at once (default: 1); "
                        "jobs run one at a time, so this bounds a cycle")
    p.add_argument("--once", action="store_true",
                   help="single poll+claim cycle, print spec")
    p.add_argument("--loop", action="store_true",
                   help="run unattended until interrupted (needs --handler)")
    p.add_argument("--handler", default="",
                   help="command that does a job's work; gets the spec on "
                        "stdin and must print the commit hash")
    p.add_argument("--timeout", type=float, default=None,
                   help="seconds before a handler run is killed and the "
                        "job marked blocked")
    p.add_argument("--interval", type=float, default=60,
                   help="seconds between poll cycles (default: 60)")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="stop after N cycles (mainly for tests)")
    p.add_argument("--scan-open", action="store_true",
                   help="also take open jobs nobody DMed about")
    p.add_argument("--no-scan-open", action="store_true",
                   help="in --loop mode, do not scan open jobs")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("reconcile", help="restart recovery")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("review", help="review a worker branch")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base", default="main")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("merge", help="approve-merge a worker branch")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base", default="main")
    p.set_defaults(fn=cmd_merge)

    args = ap.parse_args(argv)
    # A role name that matches no role skill would register or dispatch a
    # worker that can never be picked. Fail loudly, before any state change.
    for attr in ("roles", "role"):
        if hasattr(args, attr) and getattr(args, attr):
            try:
                setattr(args, attr, ",".join(
                    roles_mod.parse_roles(getattr(args, attr))))
            except roles_mod.RoleError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
