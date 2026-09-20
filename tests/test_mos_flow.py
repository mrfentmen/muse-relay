"""End-to-end tests for the MOS loop.

overseer.dispatch -> worker.run_once -> jobs done -> overseer.tick/verify.
Uses the fake Redis backend via RelayTestCase; no network, no real bus.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import jobs
from mos import capabilities, overseer, reconcile, worker


def _handler_writes_file(jid, title, spec, workdir):
    with open(os.path.join(workdir, "out.txt"), "w") as f:
        f.write("work product")
    return "deadbee", "wrote out.txt"


def _handler_no_file(jid, title, spec, workdir):
    return "deadbee", "wrote nothing"


class DispatchTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def test_dispatch_posts_and_dms(self):
        capabilities.register("w1", ["python"], dm_secret="s3cret")
        capabilities.register("w2", ["docs"], dm_secret="other")
        jid, dms = overseer.dispatch(
            "Build thing", "do the thing", accept="file-exists: out.txt",
            requirements=["python"])
        self.assertEqual(jid, "1")
        h, spec = jobs.job_get("1")
        self.assertEqual(h["status"], "open")
        self.assertEqual(h["accept"], "file-exists: out.txt")
        self.assertEqual(spec, "do the thing")
        # only the python worker got a DM
        self.assertEqual(len(dms), 1)
        self.assertEqual(dms[0][0], "w1")
        self.assertTrue(dms[0][1])
        dmkey = rc.bus_key(rc.dm_room("s3cret"))
        bodies = self.pushes_to(dmkey)
        self.assertTrue(any("JOB:1 Build thing" in b for b in bodies),
                        bodies)
        # docs worker got nothing
        self.assertEqual(
            self.pushes_to(rc.bus_key(rc.dm_room("other"))), [])

    def test_dispatch_no_capable_workers(self):
        jid, dms = overseer.dispatch("T", "spec text here")
        self.assertEqual(jid, "1")
        self.assertEqual(dms, [])

    def test_dispatch_empty_spec_rejected(self):
        with self.assertRaises(ValueError):
            overseer.dispatch("T", "   ")


class WorkerFlowTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")
        capabilities.register("w1", ["python"], dm_secret="s3cret")

    def _dispatched(self, accept="file-exists: out.txt", reqs=("python",)):
        jid, _ = overseer.dispatch("Build thing", "do the thing",
                                   accept=accept, requirements=reqs)
        return jid

    def _as_w1(self):
        self.set_nick("w1", "host-w")
        return worker.Worker(dm_secret="s3cret", caps=["python"],
                             workdir=self.tmp)

    def test_happy_path_claim_work_done(self):
        jid = self._dispatched()
        w = self._as_w1()
        rep = w.run_once(_handler_writes_file)
        self.assertEqual(rep["claimed"], [jid])
        self.assertEqual(rep["done"], [jid])
        self.assertEqual(rep["blocked"], [])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "done")
        self.assertEqual(h["result_commit"], "deadbee")

    def test_verify_passes_and_stamps(self):
        jid = self._dispatched()
        self._as_w1().run_once(_handler_writes_file)
        self.set_nick("overseer", "host-o")
        ok, report = overseer.verify_done(jid, repo=self.tmp)
        self.assertTrue(ok, report)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["verified"], "1")

    def test_verify_failure_requeues(self):
        jid = self._dispatched()
        self._as_w1().run_once(_handler_no_file)
        self.set_nick("overseer", "host-o")
        ok, report = overseer.verify_done(jid, repo=self.tmp)
        self.assertFalse(ok)
        self.assertIn("FAIL", report)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")
        self.assertIn("acceptance failed", h["note"])

    def test_tick_verifies_done_jobs(self):
        jid = self._dispatched()
        self._as_w1().run_once(_handler_writes_file)
        self.set_nick("overseer", "host-o")
        rep = overseer.tick(repo=self.tmp)
        self.assertEqual(rep["verified"], [jid])
        self.assertEqual(rep["failed"], [])

    def test_tick_flags_failed_acceptance(self):
        jid = self._dispatched()
        self._as_w1().run_once(_handler_no_file)
        self.set_nick("overseer", "host-o")
        rep = overseer.tick(repo=self.tmp)
        self.assertEqual(rep["failed"], [jid])

    def test_worker_skips_nonmatching_reqs(self):
        # DM arrives for a job whose requirements this worker can't cover
        self.set_nick("overseer", "host-o")
        self.run_cli(jobs.main, ["post", "--title", "Rust thing",
                                 "--spec", "do rust stuff"])
        rc, _, _ = self.run_cli(
            __import__("send").main,
            ["--dm", "s3cret", "JOB:1 Rust thing req=rust"])
        self.assertEqual(rc, 0)
        w = self._as_w1()
        rep = w.run_once(_handler_writes_file)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["skipped"], ["1"])

    def test_blocked_handler_marks_blocked(self):
        def blocked(jid, title, spec, workdir):
            raise worker.WorkerBlocked("need API key")
        jid = self._dispatched()
        w = self._as_w1()
        rep = w.run_once(blocked)
        self.assertEqual(rep["blocked"], [jid])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "blocked")
        self.assertIn("need API key", h["note"])

    def test_crashing_handler_marks_blocked(self):
        def boom(jid, title, spec, workdir):
            raise RuntimeError("kaboom")
        jid = self._dispatched()
        rep = self._as_w1().run_once(boom)
        self.assertEqual(rep["blocked"], [jid])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "blocked")

    def test_heartbeat_renews_claims(self):
        jid = self._dispatched()
        w = self._as_w1()
        w.register()
        ok, _ = w._claim(jid)
        self.assertTrue(ok)
        self.assertEqual(w.heartbeat(), [jid])

    def test_dm_seen_offset_survives_repoll(self):
        self._dispatched()
        w = self._as_w1()
        first = w.poll_dms()
        self.assertEqual(len(first), 1)
        # second poll sees nothing new
        w2 = worker.Worker(dm_secret="s3cret", caps=["python"],
                           workdir=self.tmp)
        self.assertEqual(w2.poll_dms(), [])

    def test_reconcile_clean(self):
        rep = reconcile.reconcile()
        self.assertTrue(rep["redis"])
        self.assertEqual(rep["requeued"], [])
        self.assertEqual(rep["blocked"], [])


if __name__ == "__main__":
    unittest.main()
