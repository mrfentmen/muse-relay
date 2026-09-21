#!/usr/bin/env python3
"""Overseer guardrails: stuck alerts, auto files-only, dead-letter report.

Silence is the scariest crew failure. A job can sit open for hours
because its DM never landed, because every capable worker went stale, or
because nobody in the crew can take it at all — and nothing says so.
tick() now says so, once per alert window, and reports a job that ran
out of attempts instead of quietly requeueing it.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs
from mos import capabilities, overseer


class StuckAlertTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def _post(self, title="T", *extra):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", title, "--spec", "s"] + list(extra))
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def _tick(self):
        return overseer.tick(repo=self.tmp)

    def _stuck_texts(self):
        return [p for p in self.pushes_to("muse-bus") if "STUCK:" in p]

    def test_a_job_nobody_can_take_alerts(self):
        jid = self._post("Rust thing", "--req", "rust")
        self.fake.advance(overseer.STUCK_AFTER + 10)
        rep = self._tick()
        self.assertEqual(rep["stuck"], [jid])
        texts = self._stuck_texts()
        self.assertTrue(any(f"STUCK:{jid}" in t for t in texts), texts)
        self.assertTrue(any("no capable worker" in t for t in texts), texts)

    def test_a_capable_but_idle_crew_still_alerts(self):
        # The DM may simply never have arrived — that is just as silent.
        capabilities.register("w1", ["python"], dm_secret="s")
        jid = self._post("Build thing", "--req", "python")
        self.fake.advance(overseer.STUCK_AFTER + 10)
        with mock.patch.object(capabilities, "STALE_AFTER", 10 ** 6):
            rep = self._tick()
        self.assertEqual(rep["stuck"], [jid])
        self.assertTrue(any("no claim" in t for t in self._stuck_texts()),
                        self._stuck_texts())

    def test_a_young_job_is_not_stuck(self):
        self._post()
        self.assertEqual(self._tick()["stuck"], [])
        self.assertEqual(self._stuck_texts(), [])

    def test_the_alert_repeats_at_most_once_per_window(self):
        self._post()
        self.fake.advance(overseer.STUCK_AFTER + 10)
        self.assertEqual(len(self._tick()["stuck"]), 1)
        self.assertEqual(self._tick()["stuck"], [])
        self.fake.advance(overseer.STUCK_ALERT_EVERY + 10)
        self.assertEqual(len(self._tick()["stuck"]), 1)

    def test_a_job_waiting_on_a_dependency_is_not_stuck(self):
        # Its dependency is what should alarm, not every job behind it.
        self._post("A")
        waiting = self._post("B", "--depends-on", "1")
        self.fake.advance(overseer.STUCK_AFTER + 10)
        rep = self._tick()
        self.assertIn("1", rep["stuck"])
        self.assertNotIn(waiting, rep["stuck"])

    def test_a_job_waiting_on_a_doomed_dependency_alerts(self):
        # Its dependency can never finish, so waiting on it would be
        # silence forever — the exact thing this alert exists for.
        first = self._post("A", "--max-attempts", "1")
        waiting = self._post("B", "--depends-on", first)
        rc_, _, err = self.run_cli(
            jobs.main, ["claim", first, "--lease", "60"])
        self.assertEqual(rc_, 0, err)
        self.fake.advance(120)
        self.run_cli(jobs.main, ["sweep"])       # first -> dead
        self.fake.advance(overseer.STUCK_AFTER + 10)
        rep = self._tick()
        self.assertIn(waiting, rep["stuck"])
        self.assertTrue(
            any(f"STUCK:{waiting}" in t and "waiting on" in t
                for t in self._stuck_texts()), self._stuck_texts())

    def test_a_claimed_job_is_not_stuck(self):
        # A long lease on purpose: the point is a job that IS being
        # worked on, not one whose worker has already gone quiet.
        jid = self._post()
        rc_, _, err = self.run_cli(
            jobs.main, ["claim", jid, "--lease", "86400"])
        self.assertEqual(rc_, 0, err)
        self.fake.advance(overseer.STUCK_AFTER + 10)
        self.assertEqual(self._tick()["stuck"], [])

    def test_tick_lines_reports_stuck_and_dead(self):
        lines = overseer.tick_lines({"swept": [], "pruned": [], "verified": [],
                                     "failed": [], "dead": ["4"],
                                     "stuck": ["5"], "errors": []})
        self.assertIn("DEAD_LETTERED 4", lines)
        self.assertIn("STUCK 5", lines)


class AutoFilesOnlyTest(RelayTestCase):
    """Declaring files is half a contract; dispatch adds the other half."""

    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def _accept(self, **kw):
        jid, _ = overseer.dispatch("T", "spec", **kw)
        h, _ = jobs.job_get(jid)
        return h.get("accept", "")

    def test_files_and_a_base_add_a_files_only_check(self):
        accept = self._accept(files="a.py,b.py", base="HEAD")
        self.assertIn("files-only: a.py,b.py", accept)

    def test_it_is_added_after_existing_criteria(self):
        accept = self._accept(accept="file-exists: a.py", files="a.py",
                              base="HEAD")
        self.assertIn("file-exists: a.py", accept)
        self.assertIn("files-only: a.py", accept)
        self.assertLess(accept.index("file-exists"), accept.index("files-only"))

    def test_no_base_means_no_check(self):
        # files-only diffs against a base, so it cannot be added without
        # one — a check that cannot run would fail every dispatch.
        self.assertNotIn("files-only", self._accept(files="a.py"))

    def test_no_files_means_no_check(self):
        self.assertNotIn("files-only", self._accept(base="HEAD"))

    def test_an_existing_check_is_not_duplicated(self):
        accept = self._accept(accept="files-only: a.py", files="a.py",
                              base="HEAD")
        self.assertEqual(accept.count("files-only:"), 1)

    def test_declared_files_are_still_persisted(self):
        jid, _ = overseer.dispatch("T", "spec", files="a.py", base="HEAD")
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.files_of(h), {"a.py"})


class DeadLetterReportingTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def test_tick_reports_a_dead_lettered_claim(self):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--max-attempts", "1"])
        self.assertEqual(rc_, 0, err)
        jid = out.split()[-1]
        self.set_nick("w1", "host-w")
        rc_, _, err = self.run_cli(
            jobs.main, ["claim", jid, "--lease", "60"])
        self.assertEqual(rc_, 0, err)
        self.set_nick("overseer", "host-o")
        self.fake.advance(120)
        rep = overseer.tick(repo=self.tmp)
        self.assertEqual(rep["dead"], [jid])
        self.assertIn(f"DEAD_LETTERED {jid}", overseer.tick_lines(rep))
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "dead")

    def test_a_dead_job_is_no_longer_swept_forever(self):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--max-attempts", "1"])
        jid = out.split()[-1]
        self.run_cli(jobs.main, ["claim", jid, "--lease", "60"])
        self.fake.advance(120)
        self.assertEqual(overseer.tick(repo=self.tmp)["dead"], [jid])
        # a second pass has nothing left to settle
        rep = overseer.tick(repo=self.tmp)
        self.assertEqual(rep["dead"], [])
        self.assertEqual(rep["swept"], [])


if __name__ == "__main__":
    unittest.main()
