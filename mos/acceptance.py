#!/usr/bin/env python3
"""Machine-readable acceptance criteria for MOS jobs.

A job's `accept` field is a list of checks, one per line:

    file-exists: path/to/file.py
    tests-pass: tests.test_something        (or tests/test_something.py)
    command-ok: python3 -m py_compile mos/overseer.py
    contains: path/to/file.py :: expected substring
    files-only: a.py,mos/*.py

The overseer runs these against the worker's branch checkout before the
job counts as done. Anything unparseable is a failed check, never
silently skipped.

`files-only` closes the other half of the file-ownership contract:
--files stops two jobs claiming the same file, and files-only fails the
job when its branch diff touches anything outside the declared set. It
diffs <base>...HEAD, so it needs a base commit — the job's --base, or a
local main/master (or origin/main) to merge-base against. Without one
the check fails with the reason rather than passing by default.

`verify(accept_text, workdir, base="")` -> (ok, results: list of dicts).
Each result: {"check": str, "ok": bool, "detail": str}.
"""
import fnmatch
import os
import subprocess
import sys


def parse_accept(text):
    """Parse accept text into [(kind, arg)] — raises ValueError on bad lines."""
    checks = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"line {lineno}: no ':' separator: {line!r}")
        kind, _, arg = line.partition(":")
        kind = kind.strip().lower()
        arg = arg.strip()
        if kind not in ("file-exists", "tests-pass", "command-ok",
                        "contains", "files-only"):
            raise ValueError(f"line {lineno}: unknown check {kind!r}")
        if not arg:
            raise ValueError(f"line {lineno}: empty argument")
        if kind == "contains" and "::" not in arg:
            raise ValueError(
                f"line {lineno}: contains needs 'path :: substring'")
        checks.append((kind, arg))
    return checks


def _mod_name(target):
    """tests/test_x.py -> tests.test_x ; tests.test_x stays as-is."""
    t = target.strip()
    if t.endswith(".py"):
        t = t[:-3]
    return t.replace("/", ".").replace("\\", ".")


def _run(cmd, workdir, timeout, shell=False):
    try:
        p = subprocess.run(
            cmd, cwd=workdir, timeout=timeout, shell=shell,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p.returncode, p.stdout[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"command not found: {e}"
    except Exception as e:
        return 1, f"runner error: {e}"


def _resolve_base(workdir, base):
    """Pick a commit to diff the branch against.

    Returns (ref, "") or (None, why). An explicit base wins; otherwise
    the first local main/master (or their origin/ form) is used through
    its merge-base with HEAD, so a worker branch forked some time ago is
    still measured from where it left off.
    """
    if base:
        rc, _ = _run(["git", "rev-parse", "--verify",
                      f"{base}^{{commit}}"], workdir, 30)
        if rc == 0:
            return base, ""
        return None, f"base commit {base!r} not found in the checkout"
    for ref in ("origin/main", "origin/master", "main", "master"):
        rc, _ = _run(["git", "rev-parse", "--verify", ref], workdir, 30)
        if rc != 0:
            continue
        rc, out = _run(["git", "merge-base", "HEAD", ref], workdir, 30)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[-1], ""
        return None, f"no merge-base between HEAD and {ref}"
    return None, ("files-only needs a base commit — post the job with "
                  "--base, or keep a local main/master branch")


def _run_files_only(arg, workdir, base, timeout):
    """Fail unless the branch diff touched only the declared files."""
    allowed = [p.strip() for p in arg.split(",") if p.strip()]
    ref, why = _resolve_base(workdir, base)
    if ref is None:
        return False, why
    rc, out = _run(["git", "diff", "--name-only", f"{ref}...HEAD"],
                   workdir, timeout)
    if rc != 0:
        return False, f"git diff failed (exit={rc}): {out[-300:]}"
    changed = [ln.strip() for ln in out.splitlines() if ln.strip()]
    extra = [f for f in changed
             if not any(fnmatch.fnmatch(f, pat) for pat in allowed)]
    if extra:
        return False, ("changed outside the declared file set: "
                       + ", ".join(extra[:10])
                       + (f" (+{len(extra) - 10} more)" if len(extra) > 10
                          else ""))
    return True, f"{len(changed)} changed file(s), all declared"


def run_checks(checks, workdir, timeout=300, base=""):
    """Run parsed checks. Returns list of result dicts."""
    results = []
    for kind, arg in checks:
        if kind == "file-exists":
            path = os.path.join(workdir, arg)
            ok = os.path.exists(path)
            results.append({"check": f"file-exists: {arg}", "ok": ok,
                            "detail": "found" if ok else "missing"})
        elif kind == "tests-pass":
            mod = _mod_name(arg)
            rc, out = _run([sys.executable, "-m", "unittest", mod],
                           workdir, timeout)
            results.append({"check": f"tests-pass: {arg}", "ok": rc == 0,
                            "detail": f"exit={rc} :: {out[-500:]}"})
        elif kind == "command-ok":
            # shell=True for compound commands; arg comes from the
            # overseer's own accept text, never from workers.
            rc, out = _run(arg, workdir, timeout, shell=True)
            results.append({"check": f"command-ok: {arg}", "ok": rc == 0,
                            "detail": f"exit={rc} :: {out[-500:]}"})
        elif kind == "contains":
            path, _, needle = arg.partition("::")
            path, needle = path.strip(), needle.strip()
            full = os.path.join(workdir, path)
            try:
                with open(full, "r", encoding="utf-8",
                          errors="replace") as f:
                    content = f.read()
                ok = needle in content
                detail = "substring present" if ok else "substring absent"
            except OSError as e:
                ok, detail = False, f"read error: {e}"
            results.append({"check": f"contains: {arg}", "ok": ok,
                            "detail": detail})
        elif kind == "files-only":
            ok, detail = _run_files_only(arg, workdir, base, timeout)
            results.append({"check": f"files-only: {arg}", "ok": ok,
                            "detail": detail})
    return results


def verify(accept_text, workdir, base=""):
    """Returns (ok, results). Empty accept text -> (True, []) with a note."""
    if not (accept_text or "").strip():
        return True, [{"check": "(no acceptance criteria)",
                       "ok": True, "detail": "nothing to verify"}]
    try:
        checks = parse_accept(accept_text)
    except ValueError as e:
        return False, [{"check": "(parse)", "ok": False,
                        "detail": str(e)}]
    if not checks:
        return True, [{"check": "(no acceptance criteria)",
                       "ok": True, "detail": "nothing to verify"}]
    results = run_checks(checks, workdir, base=base)
    return all(r["ok"] for r in results), results


def format_report(results):
    lines = []
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        lines.append(f"[{mark}] {r['check']} -- {r['detail']}")
    return "\n".join(lines)
