#!/usr/bin/env python3
"""End-to-end tests for the muse-bus relay scripts — no mocks.

Exercises the REAL bin/bus_reply.py and bin/bus_poll.py against the REAL
Upstash instance, isolated on a dedicated test list (MUSE_BUS_KEY) with a
throwaway HOME for state files. Nothing here is faked: every assertion is
backed by an actual HTTP round-trip to Upstash.

Requires: ~/workspace/muse-bus/upstash_url present, network access.

What remains unverified: behavior under concurrent writers (single-writer here),
and Upstash-side latency spikes beyond the 20s script timeouts.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc

BIN = os.path.expanduser("~/workspace/skills/upstash/bin")
REAL_STATE = os.path.expanduser("~/workspace/muse-bus")
TEST_KEY = "muse-bus-e2e"
CRED = "custom.upstash"


def _upstash():
    url = open(os.path.join(REAL_STATE, "upstash_url")).read().strip().rstrip("/")
    return url, urllib.parse.urlparse(url).hostname or ""


def api(path, data=None):
    url, host = _upstash()
    headers = {"User-Agent": "muse-bus-e2e/1.0"}
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else data.encode()
        headers["Content-Type"] = "text/plain"
    req = urllib.request.Request(url + path, data=body, headers=headers)
    dc.add_surrogate_to_request(req, CRED, allowed_hosts=[host])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return dc.read_json_response(resp)


def run(script, args, home):
    env = dict(os.environ, HOME=home, MUSE_BUS_KEY=TEST_KEY)
    return subprocess.run(
        [sys.executable, os.path.join(BIN, script)] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


FAILURES = []


def set_nick(state, nick):
    with open(os.path.join(state, "nick"), "w") as f:
        f.write(nick)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)
        if detail:
            print("     detail: " + detail)


def main():
    home = tempfile.mkdtemp(prefix="buse2e-")
    state = os.path.join(home, "workspace", "muse-bus")
    os.makedirs(state)
    shutil.copy(os.path.join(REAL_STATE, "upstash_url"), state)
    set_nick(state, "e2e")
    api(f"/del/{TEST_KEY}")  # start clean

    try:
        # 1. send works against the real list
        r = run("bus_reply.py", ["hello e2e"], home)
        check("send returns sent",
              r.returncode == 0 and r.stdout.strip() == "sent",
              f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")

        # 2. byte-identical resend is idempotent
        r = run("bus_reply.py", ["hello e2e"], home)
        check("resend is idempotent", r.stdout.strip() == "already_sent",
              repr(r.stdout))
        n = api(f"/llen/{TEST_KEY}")["result"]
        check("no duplicate line landed", n == 1, f"llen={n}")

        # 3. caller-supplied own nick is not doubled
        r = run("bus_reply.py", ["e2e: prefixed already"], home)
        tail = api(f"/lrange/{TEST_KEY}/-1/-1")["result"]
        check("no double nick prefix", tail == ["e2e: prefixed already"],
              repr(tail))

        # 4. poll sees exactly the new lines, in order
        # (poll as a different nick: the poller skips your own nick's lines)
        set_nick(state, "e2e_rx")
        r = run("bus_poll.py", [], home)
        lines = r.stdout.strip().splitlines()
        check("poll sees new lines",
              lines == ["e2e: hello e2e", "e2e: prefixed already"],
              repr(lines))

        # 5. second poll is quiet — cursor advanced past what we saw
        r = run("bus_poll.py", [], home)
        check("poll cursor advanced", r.stdout.strip() == "", repr(r.stdout))

        # 6. crash-safety: orphaned pending queue is flushed exactly once
        set_nick(state, "e2e")
        with open(os.path.join(state, "pending_reply.txt"), "w") as f:
            f.write("orphan me")
        r = run("bus_reply.py", ["--flush"], home)
        check("flush delivers orphan", r.stdout.strip() == "sent",
              repr(r.stdout))
        check("pending cleared after flush",
              not os.path.exists(os.path.join(state, "pending_reply.txt")))
        r = run("bus_reply.py", ["--flush"], home)
        check("flush is idempotent", r.stdout.strip() == "empty",
              repr(r.stdout))
        tail = api(f"/lrange/{TEST_KEY}/-1/-1")["result"]
        check("orphan landed exactly once", tail == ["e2e: orphan me"],
              repr(tail))
    finally:
        api(f"/del/{TEST_KEY}")  # leave no trace
        shutil.rmtree(home, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("\nall e2e checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
