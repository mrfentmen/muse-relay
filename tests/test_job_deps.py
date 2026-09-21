#!/usr/bin/env python3
"""Job dependencies: a job is not claimable until its deps are done.

A real build is a sequence — package B cannot be written against package
A that does not exist yet. `--depends-on` makes that explicit and the
claim gate enforces it, so the crew never builds on missing work. A
dependency that is blocked or dead-lettered can never become done, so
it is reported as the reason instead of being waited on in silence.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs
from mos import capabilities, overseer, worker


class DependsOnTest(RelayTestCase):
    def _post(self, title="T", *extra):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", title, "--spec", "s"] + list(extra))
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def _claim(self, jid, *extra):
        return self.run_cli(jobs.main, ["claim", jid] + list(extra))

    def _done(self, jid):
        rc_, _, err = self.run_cli(
            jobs.main, ["done", jid, "--commit", "deadbee"])
        self.assertEqual(rc_, 0, err)

    def test_a_dependency_is_persisted_and_shown(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", a)
        h, _ = jobs.job_get(b)
        self.assertEqual(jobs.deps_of(h), [a])
        rc_, out, err = self.run_cli(jobs.main, ["show", b])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"deps: {a}", out)

    def test_cannot_claim_while_the_dependency_is_open(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", a)
        rc_, out, err = self._claim(b)
        self.assertEqual(rc_, 2)
        self.assertIn("waiting on its dependencies", err)
        self.assertIn(f"dependency {a} is open", err)
        h, _ = jobs.job_get(b)
        self.assertEqual(h["status"], "open")   # nothing was consumed
        self.assertEqual(jobs.attempts_of(h), 0)

    def test_claim_allowed_once_the_dependency_is_done(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", a)
        self.assertEqual(self._claim(a)[0], 0)
        self._done(a)
        rc_, out, err = self._claim(b)
        self.assertEqual(rc_, 0, err)

    def test_every_dependency_must_be_done(self):
        a = self._post("A")
        b = self._post("B")
        c = self._post("C", "--depends-on", f"{a},{b}")
        self.assertEqual(self._claim(a)[0], 0)
        self._done(a)
        rc_, out, err = self._claim(c)
        self.assertEqual(rc_, 2)
        self.assertIn(f"dependency {b} is open", err)
        self.assertEqual(self._claim(b)[0], 0)
        self._done(b)
        self.assertEqual(self._claim(c)[0], 0)

    def test_a_blocked_dependency_reports_its_reason(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", a)
        self.assertEqual(self._claim(a)[0], 0)
        rc_, _, err = self.run_cli(
            jobs.main, ["blocked", a, "--reason", "need an API key"])
        self.assertEqual(rc_, 0, err)
        rc_, out, err = self._claim(b)
        self.assertEqual(rc_, 2)
        self.assertIn(f"dependency {a} is blocked", err)
        self.assertIn("need an API key", err)

    def test_a_dead_dependency_is_reported_not_awaited(self):
        a = self._post("A", "--max-attempts", "1")
        b = self._post("B", "--depends-on", a)
        self.assertEqual(self._claim(a)[0], 0)
        self.fake.advance(2000)
        self.run_cli(jobs.main, ["sweep"])
        rc_, out, err = self._claim(b)
        self.assertEqual(rc_, 2)
        self.assertIn(f"dependency {a} is dead", err)

    def test_an_unknown_dependency_is_refused_before_posting(self):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--depends-on", "7"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such job to depend on: 7", err)
        self.assertEqual(jobs._all_ids(), [])  # nothing was written

    def test_a_non_numeric_dependency_is_refused(self):
        for bad in ("abc", "1x", "-1", "0"):
            rc_, out, err = self.run_cli(
                jobs.main, ["post", "--title", "T", "--spec", "s",
                            "--depends-on", bad])
            self.assertEqual(rc_, 2, bad)
            self.assertIn("wants job ids", err)

    def test_dependencies_are_deduped(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", f"{a},{a}")
        h, _ = jobs.job_get(b)
        self.assertEqual(jobs.deps_of(h), [a])

    def test_deps_ready_is_true_without_dependencies(self):
        a = self._post("A")
        h, _ = jobs.job_get(a)
        self.assertEqual(jobs.deps_ready(a, h), (True, ""))

    def test_a_missing_dependency_job_is_reported(self):
        a = self._post("A")
        b = self._post("B", "--depends-on", a)
        self.fake.api_get("del/" + jobs._job_key(a))
        h, _ = jobs.job_get(b)
        ready, why = jobs.deps_ready(b, h)
        self.assertFalse(ready)
        self.assertIn("no longer exists", why)


class DependencyDispatchTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def test_the_scan_skips_jobs_that_are_not_ready(self):
        # Offering an unready job would only produce a refused claim
        # every cycle, so it is not a candidate at all.
        capabilities.register("w1", ["python"], dm_secret="s")
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "A", "--spec", "s",
                        "--req", "python"])
        self.assertEqual(rc_, 0, err)
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "B", "--spec", "s",
                        "--req", "python", "--depends-on", "1"])
        self.assertEqual(rc_, 0, err)
        self.set_nick("w1", "host-w")
        w = worker.Worker(caps=["python"], dm_secret="s", workdir=self.tmp,
                          scan_open=True)
        self.assertEqual([c[0] for c in w.scan_open_jobs()], ["1"])

    def test_the_worker_runs_the_chain_in_order(self):
        capabilities.register("w1", ["python"], dm_secret="s")
        jid, _ = overseer.dispatch("A", "s1", requirements=["python"])
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", "B", "--spec", "s2",
                        "--req", "python", "--depends-on", jid])
        self.assertEqual(rc_, 0, err)
        second = out.split()[-1]
        self.set_nick("w1", "host-w")

        def handler(jid_, title, spec, workdir):
            return "deadbee", f"did {title}"

        w = worker.Worker(caps=["python"], dm_secret="s", workdir=self.tmp,
                          scan_open=True)
        first_cycle = w.run_once(handler)
        self.assertEqual(first_cycle["claimed"], [jid])
        self.assertEqual(first_cycle["deferred"], [])
        second_cycle = w.run_once(handler)
        self.assertEqual(second_cycle["claimed"], [second])
        h, _ = jobs.job_get(second)
        self.assertEqual(h["status"], "done")


if __name__ == "__main__":
    unittest.main()
