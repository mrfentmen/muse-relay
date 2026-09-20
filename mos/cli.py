#!/usr/bin/env python3
"""MOS command line: the overseer's and worker's hands.

Usage:
  mos.py register --caps python,testing --dm SECRET [--roles builder]
  mos.py caps
  mos.py dispatch --title T --spec SPEC [--accept ACCEPT] [--req python]
                  [--room R] [--lease SECS] [--branch B]
  mos.py tick [--repo PATH --workdir-base PATH]
  mos.py verify <jid> [--repo PATH --workdir-base PATH]
  mos.py worker --dm SECRET --caps python [--once]
  mos.py reconcile
  mos.py review --repo PATH --branch B [--base main]
  mos.py merge --repo PATH --branch B [--base main]

Spec/accept may be given inline or via @file. Never prints the token.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import relay_common as rc  # noqa: E402
from mos import capabilities, gitops, overseer, reconcile, worker  # noqa: E402


def _text(v):
    if v and v.startswith("@"):
        with open(os.path.expanduser(v[1:])) as f:
            return f.read()
    return v or ""


def cmd_register(a):
    caps = [c for c in (a.caps or "").split(",") if c.strip()]
    capabilities.register(rc.NICK, caps, roles=a.roles or "",
                          max_jobs=a.max_jobs, dm_secret=a.dm or "")
    print(f"REGISTERED {rc.NICK}: {','.join(sorted(caps))}")
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


def cmd_dispatch(a):
    reqs = [r for r in (a.req or "").split(",") if r.strip()]
    jid, dms = overseer.dispatch(
        a.title, _text(a.spec), accept=_text(a.accept), requirements=reqs,
        room=a.room or "", lease=a.lease, branch=a.branch or "",
        base=a.base or "")
    print(f"DISPATCHED {jid}")
    for nick, ok, detail in dms:
        print(f"  DM {nick}: {'OK' if ok else 'FAIL'} {detail}")
    if not dms:
        print("  (no capable workers registered)")
    return 0


def cmd_tick(a):
    rep = overseer.tick(repo=a.repo, workdir_base=a.workdir_base)
    for jid in rep["swept"]:
        print(f"SWEPT {jid}")
    for nick in rep["pruned"]:
        print(f"PRUNED {nick}")
    for jid in rep["verified"]:
        print(f"VERIFIED {jid}")
    for jid in rep["failed"]:
        print(f"ACCEPTANCE_FAILED {jid}")
    for e in rep["errors"]:
        print(f"ERROR {e}", file=sys.stderr)
    if not any([rep["swept"], rep["pruned"], rep["verified"],
                rep["failed"], rep["errors"]]):
        print("TICK_CLEAN")
    return 0 if not rep["errors"] else 2


def cmd_verify(a):
    ok, report = overseer.verify_done(a.jid, repo=a.repo,
                                      workdir_base=a.workdir_base)
    print(report)
    print("VERIFIED" if ok else "NOT_VERIFIED")
    return 0 if ok else 1


def cmd_worker(a):
    caps = [c for c in (a.caps or "").split(",") if c.strip()]
    w = worker.Worker(dm_secret=a.dm or "", caps=caps,
                      roles=a.roles or "", workdir=a.workdir or ".",
                      lease=a.lease)
    w.register()
    w.heartbeat()
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
    # loop mode: the caller is the handler (an agent driving this CLI)
    print("worker loop: agent-driven; use --once per cycle", file=sys.stderr)
    return 2


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

    p = sub.add_parser("dispatch", help="post a job + DM capable workers")
    p.add_argument("--title", required=True)
    p.add_argument("--spec", required=True, help="text or @file")
    p.add_argument("--accept", default="", help="text or @file")
    p.add_argument("--req", default="", help="required caps, comma-sep")
    p.add_argument("--room", default="")
    p.add_argument("--lease", type=int, default=1800)
    p.add_argument("--branch", default="")
    p.add_argument("--base", default="")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("tick", help="one overseer pass")
    p.add_argument("--repo", default=None)
    p.add_argument("--workdir-base", default=None)
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("verify", help="verify a done job's acceptance")
    p.add_argument("jid")
    p.add_argument("--repo", default=None)
    p.add_argument("--workdir-base", default=None)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("worker", help="worker poll/claim cycle")
    p.add_argument("--dm", default="", help="DM dead-drop secret")
    p.add_argument("--caps", default="", help="comma-separated")
    p.add_argument("--roles", default="")
    p.add_argument("--workdir", default=".")
    p.add_argument("--lease", type=int, default=1800)
    p.add_argument("--once", action="store_true",
                   help="single poll+claim cycle, print spec")
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
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
