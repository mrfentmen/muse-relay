#!/usr/bin/env python3
"""Machine-readable acceptance criteria for MOS jobs.

A job's `accept` field is a list of checks, one per line:

    file-exists: path/to/file.py
    tests-pass: tests.test_something        (or tests/test_something.py)
    command-ok: python3 -m py_compile mos/overseer.py
    contains: path/to/file.py :: expected substring

The overseer runs these against the worker's branch checkout before the
job counts as done. Anything unparseable is a failed check, never
silently skipped.

`verify(accept_text, workdir)` -> (ok: bool, results: list of dicts).
Each result: {"check": str, "ok": bool, "detail": str}.
"""
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
        if kind not in ("file-exists", "tests-pass", "command-ok", "contains"):
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


def run_checks(checks, workdir, timeout=300):
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
    return results


def verify(accept_text, workdir):
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
    results = run_checks(checks, workdir)
    return all(r["ok"] for r in results), results


def format_report(results):
    lines = []
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        lines.append(f"[{mark}] {r['check']} -- {r['detail']}")
    return "\n".join(lines)
