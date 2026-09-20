"""Unit tests for jobs.py — the crew job queue."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs


class PostTest(RelayTestCase):
    def test_post_creates_job(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "Fix login",
                        "--spec", "make it work\nsecond line",
                        "--accept", "tests pass", "--branch", "w/login"])
        self.assertEqual(rc, 0)
        self.assertIn("JOB 1", out)
        h, spec = jobs.job_get("1")
        self.assertEqual(h["title"], "Fix login")
        self.assertEqual(h["status"], "open")
        self.assertEqual(h["accept"], "tests pass")
        self.assertEqual(h["branch"], "w/login")
        self.assertEqual(h["created_by"], "tester")
        self.assertEqual(h["created_host"], "host-a")
        self.assertEqual(spec, "make it work\nsecond line")
        # wire announcement on the main bus
        self.assertIn("tester: JOB:1 Fix login", self.pushes_to("muse-bus"))

    def test_post_spec_from_stdin(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "-"],
            stdin="stdin spec here")
        self.assertEqual(rc, 0)
        _, spec = jobs.job_get("1")
        self.assertEqual(spec, "stdin spec here")

    def test_post_empty_spec_rejected(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "   "])
        self.assertEqual(rc, 2)
        self.assertIn("empty", err)

    def test_post_announces_to_room(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--room", "build-x"])
        self.assertEqual(rc, 0)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["room"], "build-x")
        self.assertIn("tester: JOB:1 T",
                      self.pushes_to("muse-bus:room:build-x"))


class ClaimTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])

    def test_claim_lifecycle(self):
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("CLAIMED 1 by tester", out)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "claimed")
        self.assertEqual(h["claim"], "tester")
        self.assertIn("tester: CLAIM:1 by tester",
                      self.pushes_to("muse-bus"))

    def test_claim_missing_job(self):
        rc, out, err = self.run_cli(jobs.main, ["claim", "99"])
        self.assertEqual(rc, 2)
        self.assertIn("no such job", err)

    def test_claim_twice_fails(self):
        self.run_cli(jobs.main, ["claim", "1"])
        self.set_nick("worker2", "host-b")
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("claimed by tester", err)

    def test_lost_race(self):
        # someone else's mutex is already in place
        self.fake.api_get("set/muse-bus:tjob:1:claim/other/NX/EX/1800")
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("lost the race", err)

    def test_ids_increment(self):
        self.run_cli(jobs.main, ["post", "--title", "T2", "--spec", "s"])
        rc, out, err = self.run_cli(jobs.main, ["list"])
        self.assertIn("1", out)
        self.assertIn("2", out)

    def test_claim_rolls_back_mutex_when_hset_fails(self):
        # The mutex SET NX wins, then the claim-record write blows up:
        # cmd_claim must best-effort delete the mutex so the job stays
        # open instead of claimed-but-unworkable until the lease expires.
        with mock.patch.object(jobs, "_hset",
                               side_effect=RuntimeError("boom")):
            rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("claim record failed", err)
        # mutex rolled back...
        self.assertIsNone(self.fake._get("muse-bus:tjob:1:claim"))
        # ...and the job is still open
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "open")


class ProgressDoneTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        self.run_cli(jobs.main, ["claim", "1"])

    def test_progress_renews_and_notes(self):
        h0, _ = jobs.job_get("1")
        self.fake.advance(60)
        rc, out, err = self.run_cli(
            jobs.main, ["progress", "1", "--note", "half done"])
        self.assertEqual(rc, 0)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["note"], "half done")
        self.assertGreater(int(h["lease_until"]), int(h0["lease_until"]))
        self.assertIn("tester: PROGRESS:1 half done",
                      self.pushes_to("muse-bus"))

    def test_heartbeat_renews(self):
        h0, _ = jobs.job_get("1")
        self.fake.advance(60)
        rc, out, err = self.run_cli(jobs.main, ["heartbeat", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("LEASE_RENEWED 1", out)
        h, _ = jobs.job_get("1")
        self.assertGreater(int(h["lease_until"]), int(h0["lease_until"]))

    def test_done(self):
        rc, out, err = self.run_cli(
            jobs.main, ["done", "1", "--commit", "abc123", "--note", "ship"])
        self.assertEqual(rc, 0)
        self.assertIn("DONE 1 abc123", out)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "done")
        self.assertEqual(h["result_commit"], "abc123")
        # mutex released
        self.assertIsNone(self.fake._get("muse-bus:tjob:1:claim"))
        self.assertIn("tester: DONE:1 abc123", self.pushes_to("muse-bus"))

    def test_done_by_other_fails(self):
        self.set_nick("worker2", "host-b")
        rc, out, err = self.run_cli(
            jobs.main, ["done", "1", "--commit", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("claimed by 'tester'", err)

    def test_blocked_releases(self):
        rc, out, err = self.run_cli(
            jobs.main, ["blocked", "1", "--reason", "need API key"])
        self.assertEqual(rc, 0)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "blocked")
        self.assertIsNone(self.fake._get("muse-bus:tjob:1:claim"))
        self.assertIn("tester: BLOCKED:1 need API key",
                      self.pushes_to("muse-bus"))

    def test_claim_blocked_fails(self):
        self.run_cli(jobs.main, ["blocked", "1", "--reason", "x"])
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("is blocked", err)


class LeaseExpiryTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        self.run_cli(jobs.main, ["claim", "1", "--lease", "100"])

    def test_expired_claim_can_be_reclaimed(self):
        self.fake.advance(200)  # past the 100s lease
        self.set_nick("worker2", "host-b")
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 0, err)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["claim"], "worker2")
        self.assertIn("worker2: REQUEUE:1", self.pushes_to("muse-bus"))

    def test_requeue_refuses_active_lease(self):
        rc, out, err = self.run_cli(jobs.main, ["requeue", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("still active", err)

    def test_requeue_after_expiry(self):
        self.fake.advance(200)
        rc, out, err = self.run_cli(jobs.main, ["requeue", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("REQUEUED 1", out)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "open")

    def test_sweep(self):
        self.run_cli(jobs.main, ["post", "--title", "T2", "--spec", "s"])
        self.run_cli(jobs.main, ["claim", "2", "--lease", "100"])
        self.fake.advance(50)
        # renew job 1's lease so only job 2 expires
        self.run_cli(jobs.main, ["heartbeat", "1", "--lease", "500"])
        self.fake.advance(200)
        rc, out, err = self.run_cli(jobs.main, ["sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("REQUEUED 2", out)
        self.assertNotIn("REQUEUED 1", out)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "claimed")


class TrustWarningTest(RelayTestCase):
    def test_claim_warns_on_spoofed_poster(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        # someone else now holds the 'tester' nick claim
        self.fake.api_get("del/muse-bus:nickclaim:tester")
        self.fake.api_get("set/muse-bus:nickclaim:tester/host-evil/NX/EX/300")
        self.set_nick("worker2", "host-b")
        rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("now claimed by 'host-evil'", err)


class ListShowTest(RelayTestCase):
    def test_list_and_show(self):
        self.run_cli(jobs.main, ["post", "--title", "First", "--spec",
                                 "spec-one", "--accept", "it works"])
        self.run_cli(jobs.main, ["post", "--title", "Second", "--spec", "s2"])
        self.run_cli(jobs.main, ["claim", "2"])
        rc, out, err = self.run_cli(jobs.main, ["list", "--status", "open"])
        self.assertIn("First", out)
        self.assertNotIn("Second", out)
        rc, out, err = self.run_cli(jobs.main, ["show", "1"])
        self.assertIn("title: First", out)
        self.assertIn("accept: it works", out)
        self.assertIn("spec-one", out)


class EncodingTest(RelayTestCase):
    """Read path must invert the write path exactly once, end to end.

    _hset URL-quotes field/value for the REST path; the server (real
    Upstash, mimicked by the fake) decodes each path segment before
    storing. So _hgetall must NOT unquote again: a stored literal
    "%25"/"%2F" would otherwise come back as "%"/"/".
    """

    def test_hgetall_roundtrips_literal_percent(self):
        val = "pct %25 slash %2F end"
        jobs._hset("muse-bus:tjob:enc", {"note": val})
        # the fake decodes path segments on receipt, so what is stored
        # is the original value...
        self.assertEqual(self.fake.hashes["muse-bus:tjob:enc"]["note"],
                         val)
        # ...and the read path must return it byte-identical
        h = jobs._hgetall("muse-bus:tjob:enc")
        self.assertEqual(h["note"], val)


class ClaimRollbackTest(RelayTestCase):
    def test_claim_mutex_rolled_back_when_hset_fails(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        orig_hset = jobs._hset

        def boom(key, mapping):
            raise RuntimeError("injected hset failure")

        jobs._hset = boom
        try:
            rc, out, err = self.run_cli(jobs.main, ["claim", "1"])
        finally:
            jobs._hset = orig_hset
        self.assertNotEqual(rc, 0)
        # mutex key must be gone so the job doesn't look claimed-but-unworkable
        self.assertNotIn(jobs._claim_key("1"), self.fake.strings)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "open")


if __name__ == "__main__":    unittest.main()
