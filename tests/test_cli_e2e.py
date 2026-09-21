#!/usr/bin/env python3
"""End-to-end: the real scripts, over HTTP, the way a user runs them.

Every other test imports the modules and patches the transport. This one
starts a small HTTP server that speaks the Upstash REST shape, points
`MUSE_RELAY_URL` at it, and then runs `jobs.py` and `mos.py` as
subprocesses — so argparse wiring, environment plumbing, exit codes and
process boundaries are all exercised for real. No network beyond
localhost, and nothing is written outside a temp dir.

It also proves the token file is read and sent (every request carries the
Bearer header) and that no command prints it.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_redis import FakeUpstash  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "e2e-super-secret-rest-token"


class _Handler(BaseHTTPRequestHandler):
    """Upstash-shaped REST surface over FakeUpstash."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the test output clean

    def _send(self, body):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.bearer.append(self.headers.get("Authorization", ""))
        path = self.path.lstrip("/")
        try:
            self._send(self.server.backend.api_get(path))
        except Exception as e:  # surface it the way the real API would
            self._send(json.dumps({"error": str(e)}))

    def do_POST(self):
        self.server.bearer.append(self.headers.get("Authorization", ""))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            self._send(self.server.backend.api_post(self.path.lstrip("/"),
                                                    body))
        except Exception as e:
            self._send(json.dumps({"error": str(e)}))


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.backend = FakeUpstash()
        self.bearer = []


class CliE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-e2e-")
        token_file = os.path.join(self.tmp, "token")
        with open(token_file, "w") as f:
            f.write(TOKEN + "\n")
        self.server = _Server()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.handler = os.path.join(self.tmp, "handler.sh")
        with open(self.handler, "w") as f:
            f.write("#!/bin/sh\necho COMMIT deadbee\n")
        self.base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.tmp,
            "MUSE_RELAY_URL":
                f"http://127.0.0.1:{self.server.server_port}",
            "MUSE_RELAY_TOKEN_FILE": token_file,
            "MUSE_RELAY_BUS": "muse-bus",
            "MUSE_RELAY_JOBNS": "muse-bus:job",
            "MUSE_RELAY_CAPNS": "muse-bus:cap",
            "MUSE_RELAY_INSTANCE_ID": "e2e-host",
        }

    # -- helpers -------------------------------------------------------
    def run_script(self, script, *args, nick="pax", stdin=None):
        env = dict(self.base_env, MUSE_RELAY_NICK=nick)
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, script)] + list(args),
            cwd=ROOT, env=env, input=stdin, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        self.assertNotIn(TOKEN, p.stdout)
        self.assertNotIn(TOKEN, p.stderr)
        return p

    def test_the_whole_crew_journey_over_http(self):
        # 1. Preflight: the setup is healthy as configured.
        d = self.run_script("mos.py", "doctor")
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        self.assertIn("OK redis:", d.stdout)
        self.assertIn("OK token:", d.stdout)

        # 2. Post a job with checkable acceptance criteria.
        p = self.run_script(
            "jobs.py", "post", "--title", "E2E build", "--spec", "do it",
            "--req", "python", "--max-attempts", "2",
            "--accept", "file-exists: README.md")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("JOB 1", p.stdout)

        # 3. The crew is visible, and the job is on the board.
        s = self.run_script("mos.py", "status")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("JOBS open=1", s.stdout)
        self.assertIn("TOTAL_JOBS 1", s.stdout)
        sj = self.run_script("mos.py", "status", "--json")
        self.assertEqual(json.loads(sj.stdout)["counts"]["open"], 1)

        # 4. A worker registers, the overseer dispatches to it.
        r = self.run_script("mos.py", "register", "--caps", "python",
                            "--dm", "s3cr3t", "--roles", "builder", nick="w1")
        self.assertEqual(r.returncode, 0, r.stderr)
        dsp = self.run_script("mos.py", "dispatch", "--title", "E2E build 2",
                              "--spec", "do it too", "--req", "python",
                              "--accept", "file-exists: README.md", nick="ov")
        self.assertEqual(dsp.returncode, 0, dsp.stderr)
        self.assertIn("DISPATCHED 2", dsp.stdout)
        self.assertIn("DM w1: OK", dsp.stdout)

        # 5. The worker runs its loop and finishes both jobs.
        w = self.run_script(
            "mos.py", "worker", "--dm", "s3cr3t", "--caps", "python",
            "--roles", "builder", "--workdir", ROOT, "--loop",
            "--handler", f"/bin/sh {self.handler}", "--interval", "0",
            "--max-cycles", "2", nick="w1")
        self.assertEqual(w.returncode, 0, w.stdout + w.stderr)
        self.assertIn("looping every", w.stdout)
        st = self.run_script("jobs.py", "list", "--status", "done")
        self.assertIn("E2E build", st.stdout)
        self.assertIn("E2E build 2", st.stdout)

        # 6. The overseer verifies both against their acceptance criteria.
        t = self.run_script("mos.py", "tick", "--repo", ROOT, nick="ov")
        self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
        self.assertIn("VERIFIED 1", t.stdout)
        self.assertIn("VERIFIED 2", t.stdout)
        show = self.run_script("jobs.py", "show", "1")
        self.assertIn("verified: 1", show.stdout)
        self.assertIn("attempts: 1", show.stdout)
        self.assertIn("max_attempts: 2", show.stdout)

        # 7. The token file was actually read and sent, and never leaked.
        self.assertTrue(self.server.bearer)
        self.assertTrue(all(b == f"Bearer {TOKEN}"
                            for b in self.server.bearer),
                        self.server.bearer[:3])

    def test_a_failing_job_dead_letters_then_resets(self):
        p = self.run_script(
            "jobs.py", "post", "--title", "Doomed", "--spec", "s",
            "--accept", "file-exists: nope.txt", "--max-attempts", "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        for argv in (["claim", "1"], ["done", "1", "--commit", "deadbee"]):
            r = self.run_script("jobs.py", *argv)
            self.assertEqual(r.returncode, 0, r.stderr)
        t = self.run_script("mos.py", "tick", "--repo", ROOT, nick="ov")
        self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
        self.assertIn("ACCEPTANCE_FAILED 1", t.stdout)

        dead = self.run_script("jobs.py", "list", "--status", "dead")
        self.assertIn("Doomed", dead.stdout)
        # claiming it says why and how to recover, and consumes nothing
        c = self.run_script("jobs.py", "claim", "1", nick="other")
        self.assertEqual(c.returncode, 2)
        self.assertIn("dead-lettered", c.stderr)
        self.assertIn("jobs.py reset 1", c.stderr)

        reset = self.run_script("jobs.py", "reset", "1")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertIn("RESET 1", reset.stdout)
        again = self.run_script("jobs.py", "claim", "1", nick="other")
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_dependencies_gate_the_claim_over_the_cli(self):
        first = self.run_script("jobs.py", "post", "--title", "A",
                                "--spec", "s")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script("jobs.py", "post", "--title", "B",
                                 "--spec", "s", "--depends-on", "1")
        self.assertEqual(second.returncode, 0, second.stderr)
        blocked = self.run_script("jobs.py", "claim", "2")
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("waiting on its dependencies", blocked.stderr)
        self.assertIn("dependency 1 is open", blocked.stderr)
        for argv in (["claim", "1"], ["done", "1", "--commit", "abc1234"]):
            self.assertEqual(self.run_script("jobs.py", *argv).returncode, 0)
        ok = self.run_script("jobs.py", "claim", "2")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_the_secret_tripwire_refuses_a_spec_over_the_cli(self):
        bad = self.run_script(
            "jobs.py", "post", "--title", "Leak", "--spec",
            "use sk-abcdefghijklmnopqrstuvwx to authenticate")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("looks like a", bad.stderr)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", bad.stderr)
        self.assertEqual(json.loads(self.server.backend.api_get(
            "zrange/muse-bus:jobindex/0/-1"))["result"], [])
        good = self.run_script(
            "jobs.py", "post", "--title", "Fixture", "--spec",
            "the fixture key sk-abcdefghijklmnopqrstuvwx is fake",
            "--allow-secrets")
        self.assertEqual(good.returncode, 0, good.stderr)


if __name__ == "__main__":
    unittest.main()
