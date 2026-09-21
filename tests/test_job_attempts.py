#!/usr/bin/env python3
"""Attempt budgets: a job that keeps failing stops being retried.

Every claim is an attempt. A job that fails acceptance, or whose worker
disappears, is handed back while it has attempts left and dead-lettered
once it has none — otherwise one unsatisfiable spec keeps the whole crew
busy forever, which is exactly the failure that looks like progress.
`jobs.py reset` is the deliberate way back once a human fixes the spec.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs
from mos import overseer


class AttemptCountingTest(RelayTestCase):
    def _post(self, *extra):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s"] + list(extra))
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def test_a_claim_counts_an_attempt(self):
        jid = self._post()
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.attempts_of(h), 0)
        rc_, _, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 0, err)
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.attempts_of(h), 1)

    def test_the_default_budget_is_written_into_the_record(self):
        # Written at post time so every reader agrees on the budget even
        # when their environments disagree.
        jid = self._post()
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.max_attempts_of(h), jobs.MAX_ATTEMPTS_DEFAULT)

    def test_max_attempts_flag_is_persisted(self):
        jid = self._post("--max-attempts", "2")
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.max_attempts_of(h), 2)
        self.assertEqual(jobs.attempts_of(h), 0)

    def test_max_attempts_must_be_positive(self):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--max-attempts", "0"])
        self.assertEqual(rc_, 2)
        self.assertIn("--max-attempts", err)

    def test_show_reports_the_budget(self):
        jid = self._post("--max-attempts", "2")
        self.run_cli(jobs.main, ["claim", jid])
        rc_, out, err = self.run_cli(jobs.main, ["show", jid])
        self.assertEqual(rc_, 0, err)
        self.assertIn("attempts: 1", out)
        self.assertIn("max_attempts: 2", out)

    def test_exhausted_and_attempts_helpers_tolerate_junk(self):
        self.assertEqual(jobs.attempts_of({"attempts": "abc"}), 0)
        self.assertEqual(jobs.attempts_of({}), 0)
        self.assertEqual(jobs.max_attempts_of({"max_attempts": "x"}),
                         jobs.MAX_ATTEMPTS_DEFAULT)
        self.assertFalse(jobs.exhausted({}))
        self.assertTrue(jobs.exhausted({"attempts": "2", "max_attempts": "2"}))


class DeadLetterTest(RelayTestCase):
    """A job that burns its attempts is dead-lettered, not reopened."""

    def _post(self, *extra):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s"] + list(extra))
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def _expire_the_claim(self, jid):
        rc_, _, err = self.run_cli(
            jobs.main, ["claim", jid, "--lease", "60"])
        self.assertEqual(rc_, 0, err)
        self.fake.advance(120)

    def _dead_job(self):
        jid = self._post("--max-attempts", "1")
        self._expire_the_claim(jid)
        rc_, out, err = self.run_cli(jobs.main, ["sweep"])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"DEAD_LETTERED {jid}", out)
        return jid

    def test_a_job_out_of_attempts_dies_instead_of_being_requeued(self):
        jid = self._dead_job()
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "dead")
        self.assertIn("lease expired after 1 attempt(s)", h["dead_reason"])
        pushes = self.pushes_to("muse-bus")
        self.assertTrue(any("DEAD:" + jid in p for p in pushes), pushes)
        self.assertFalse(any("REQUEUE:" + jid in p for p in pushes), pushes)

    def test_a_job_with_attempts_left_is_still_requeued(self):
        jid = self._post("--max-attempts", "2")
        self._expire_the_claim(jid)
        rc_, out, err = self.run_cli(jobs.main, ["sweep"])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"REQUEUED {jid}", out)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")
        self.assertEqual(jobs.attempts_of(h), 1)

    def test_explicit_requeue_also_dead_letters(self):
        jid = self._post("--max-attempts", "1")
        self._expire_the_claim(jid)
        rc_, out, err = self.run_cli(jobs.main, ["requeue", jid])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"DEAD_LETTERED {jid}", out)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "dead")

    def test_a_dead_job_cannot_be_claimed_and_says_how_to_recover(self):
        jid = self._dead_job()
        self.set_nick("worker2", "host-b")
        rc_, out, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 2)
        self.assertIn("dead-lettered", err)
        self.assertIn(f"jobs.py reset {jid}", err)
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.attempts_of(h), 1)  # nothing was consumed

    def test_an_exhausted_but_still_open_job_is_not_claimable(self):
        # The claim gate has to hold even if a job is somehow open with
        # no attempts left, or the loop comes straight back.
        jid = self._post("--max-attempts", "1")
        jobs._hset(jobs._job_key(jid), {"attempts": "1"})
        rc_, out, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 2)
        self.assertIn("used all 1 attempt", err)

    def test_reset_reopens_with_a_fresh_budget(self):
        jid = self._dead_job()
        rc_, out, err = self.run_cli(jobs.main, ["reset", jid])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"RESET {jid}", out)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")
        self.assertEqual(jobs.attempts_of(h), 0)
        self.assertNotIn("dead_reason", h)
        self.set_nick("worker2", "host-b")
        rc_, out, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 0, err)

    def test_reset_is_announced_and_needs_a_real_job(self):
        jid = self._dead_job()
        self.run_cli(jobs.main, ["reset", jid])
        self.assertTrue(any(f"RESET:{jid}" in p
                            for p in self.pushes_to("muse-bus")))
        rc_, out, err = self.run_cli(jobs.main, ["reset", "99"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such job", err)

    def test_reset_refuses_while_a_live_claim_holds_it(self):
        jid = self._post("--max-attempts", "2")
        rc_, _, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 0, err)
        rc_, out, err = self.run_cli(jobs.main, ["reset", jid])
        self.assertEqual(rc_, 2)
        self.assertIn("still claimed", err)

    def test_list_can_filter_for_dead_jobs(self):
        self._dead_job()
        rc_, out, err = self.run_cli(jobs.main, ["list", "--status", "dead"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("dead", out)
        rc_, out, err = self.run_cli(jobs.main, ["list", "--status", "open"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("NO_JOBS", out)


class AcceptanceFailureTest(RelayTestCase):
    """The overseer's failure path goes through the same budget."""

    def _post(self, accept):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--accept", accept, "--max-attempts", "1"])
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def test_failing_acceptance_dead_letters_the_last_attempt(self):
        jid = self._post("file-exists: nope.txt")
        rc_, _, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 0, err)
        rc_, _, err = self.run_cli(
            jobs.main, ["done", jid, "--commit", "deadbee"])
        self.assertEqual(rc_, 0, err)
        ok, report = overseer.verify_done(jid)
        self.assertFalse(ok)
        self.assertIn("[DEAD-LETTERED]", report)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "dead")
        self.assertIn("acceptance failed", h["dead_reason"])
        # the evidence stays with the job even though it died
        self.assertIn("acceptance failed", h["note"])

    def test_a_job_with_attempts_left_is_requeued_not_killed(self):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--accept", "file-exists: nope.txt",
                        "--max-attempts", "2"])
        self.assertEqual(rc_, 0, err)
        jid = out.split()[-1]
        self.run_cli(jobs.main, ["claim", jid])
        self.run_cli(jobs.main, ["done", jid, "--commit", "deadbee"])
        ok, report = overseer.verify_done(jid)
        self.assertFalse(ok)
        self.assertNotIn("[DEAD-LETTERED]", report)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")


if __name__ == "__main__":
    unittest.main()
