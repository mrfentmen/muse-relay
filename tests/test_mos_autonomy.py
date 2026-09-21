#!/usr/bin/env python3
"""Autonomy: the worker loop, its handler contract, roles, and the
continuous overseer.

The point of these tests is that a crew with nobody driving it keeps
making progress: mos.py worker --loop claims matching jobs, runs the
command handler, reports done/blocked, renews its own lease while the
handler works, and picks up jobs whose DM never arrived.
"""
import io
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase, make_git_repo
import jobs
from mos import capabilities, cli, gitops, overseer, roles, worker


def _ok_handler(jid, title, spec, workdir):
    return "deadbee", "did the work"


class CommandHandlerTest(RelayTestCase):
    def _script(self, body):
        """A real executable the handler can run — no mocking."""
        path = os.path.join(self.tmp, "handler.sh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def test_parses_commit_line(self):
        self.assertEqual(worker.parse_commit("noise\nCOMMIT abc1234\nmore"),
                         "abc1234")

    def test_parses_bare_hash_on_last_line(self):
        self.assertEqual(worker.parse_commit("did stuff\nabc1234\n"),
                         "abc1234")

    def test_returns_none_without_a_hash(self):
        self.assertIsNone(worker.parse_commit("all done, no commit here"))
        self.assertIsNone(worker.parse_commit(""))

    def test_runs_and_returns_the_commit(self):
        h = worker.CommandHandler(self._script("echo COMMIT deadbee"),
                                  workdir=self.tmp)
        commit, note = h("7", "title", "spec", self.tmp)
        self.assertEqual(commit, "deadbee")
        self.assertTrue(note)

    def test_handler_gets_spec_on_stdin_and_job_env(self):
        h = worker.CommandHandler(
            self._script("cat > in.txt\n"
                         "printf '%s' \"$MUSE_JOB_ID\" > jid.txt\n"
                         "printf '%s' \"$MUSE_JOB_TITLE\" > title.txt\n"
                         "printf '%s' \"$MUSE_JOB_ROLE\" > role.txt\n"
                         "echo COMMIT deadbee"),
            workdir=self.tmp, role="builder")
        h("42", "Fix the thing", "THE SPEC", self.tmp)
        with open(os.path.join(self.tmp, "in.txt")) as f:
            self.assertEqual(f.read(), "THE SPEC")
        with open(os.path.join(self.tmp, "jid.txt")) as f:
            self.assertEqual(f.read(), "42")
        with open(os.path.join(self.tmp, "title.txt")) as f:
            self.assertEqual(f.read(), "Fix the thing")
        with open(os.path.join(self.tmp, "role.txt")) as f:
            self.assertEqual(f.read(), "builder")

    def test_nonzero_exit_blocks_with_the_output_tail(self):
        h = worker.CommandHandler(
            self._script("echo 'boom reasons' >&2\nexit 3"), workdir=self.tmp)
        with self.assertRaises(worker.WorkerBlocked) as ctx:
            h("7", "t", "s", self.tmp)
        self.assertIn("exited 3", str(ctx.exception))
        self.assertIn("boom reasons", str(ctx.exception))

    def test_missing_commit_is_blocked_not_silently_done(self):
        h = worker.CommandHandler(self._script("echo hello"),
                                  workdir=self.tmp)
        with self.assertRaises(worker.WorkerBlocked) as ctx:
            h("7", "t", "s", self.tmp)
        self.assertIn("no commit hash", str(ctx.exception))

    def test_timeout_blocks(self):
        h = worker.CommandHandler(self._script("sleep 5\necho COMMIT deadbee"),
                                  workdir=self.tmp, timeout=0.5)
        with self.assertRaises(worker.WorkerBlocked) as ctx:
            h("7", "t", "s", self.tmp)
        self.assertIn("timed out", str(ctx.exception))

    def test_empty_command_is_a_valueerror(self):
        with self.assertRaises(ValueError):
            worker.CommandHandler("   ")


class WorkerLoopTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"

    def _post(self, title="Build thing", req="python", role=""):
        argv = ["post", "--title", title, "--spec", "do it"]
        if req:
            argv += ["--req", req]
        if role:
            argv += ["--role", role]
        rc_, out, err = self.run_cli(jobs.main, argv)
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def _worker(self, raw_caps=("python",), roles_="", scan_open=False,
                dm="s3cret", lease=1800, lease_beat=None, nick="w1"):
        self.set_nick(nick, "host-" + nick)
        return worker.Worker(caps=list(raw_caps), roles=roles_,
                             dm_secret=dm, workdir=self.tmp,
                             lease=lease, lease_beat=lease_beat,
                             scan_open=scan_open)

    def _dm(self, jid, title="Build thing", req="python"):
        """Send the DM a real dispatch would send, from the current nick.

        Call this BEFORE switching to the worker's nick, or poll_dms
        correctly drops it as the worker's own message.
        """
        text = f"JOB:{jid} {title}" + (f" req={req}" if req else "")
        rc_, _, err = self.run_cli(
            sys.modules["send"].main, ["--dm", "s3cret", text])
        self.assertEqual(rc_, 0, err)

    def test_loop_processes_a_queued_job_and_stops(self):
        jid = self._post()
        self._dm(jid)
        w = self._worker()
        out = io.StringIO()
        cycles = w.run_loop(_ok_handler, interval=0, max_cycles=2, out=out)
        self.assertEqual(cycles, 2)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "done")
        self.assertEqual(h["result_commit"], "deadbee")
        self.assertIn("cycle w1:", out.getvalue())

    def test_loop_survives_a_failing_cycle(self):
        # A bus read that blows up must not take the worker down.
        w = self._worker()
        calls = {"n": 0}

        def flaky(handler):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("bus unreachable")
            return {"claimed": [], "done": [], "blocked": [],
                    "skipped": [], "errors": []}

        with mock.patch.object(w, "run_once", flaky):
            cycles = w.run_loop(_ok_handler, interval=0, max_cycles=2,
                                out=io.StringIO())
        self.assertEqual(cycles, 2)
        self.assertEqual(calls["n"], 2)

    def test_keep_lease_renews_while_the_handler_runs(self):
        jid = self._post()
        w = self._worker(lease=60, lease_beat=0.05)
        ok, _ = w._claim(jid)
        self.assertTrue(ok)
        real = jobs.main
        seen = []

        def counting(argv):
            seen.append(list(argv))
            return real(argv)

        with mock.patch.object(jobs, "main", counting):
            with w._keep_lease(jid):
                time.sleep(0.3)
        self.assertGreaterEqual(len(seen), 2, seen)
        for argv in seen:
            self.assertEqual(argv[0], "heartbeat")
            self.assertEqual(argv[1], jid)

    def test_missing_role_skips_the_job(self):
        jid = self._post(role="reviewer")
        self._dm(jid)                    # the DM arrives...
        w = self._worker()               # ...but I hold no roles
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["skipped"], [jid])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")

    def test_held_role_takes_the_job(self):
        jid = self._post(role="reviewer")
        w = self._worker(roles_="reviewer", scan_open=True)
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [jid])
        self.assertEqual(rep["done"], [jid])

    def test_insufficient_caps_skips_the_job(self):
        jid = self._post(req="rust")
        self._dm(jid, req="rust")
        w = self._worker(raw_caps=("python",))
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["skipped"], [jid])

    def test_a_scan_just_ignores_jobs_that_are_not_mine(self):
        # The scan filters by match rather than reporting a skip: an
        # open job the worker cannot take was never its business.
        jid = self._post(req="rust")
        w = self._worker(raw_caps=("python",), scan_open=True)
        self.assertEqual(w.scan_open_jobs(), [])
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["skipped"], [])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")

    def test_scan_open_picks_up_a_job_whose_dm_never_arrived(self):
        jid = self._post()
        w = self._worker(scan_open=True)
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [jid])
        self.assertEqual(rep["done"], [jid])

    def test_scan_open_never_steals_a_live_claim(self):
        jid = self._post()
        self._worker(scan_open=True)._claim(jid)
        other = self._worker(scan_open=True, nick="w2")
        rep = other.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["claim"], "w1")

    def test_dm_candidates_come_first_and_are_not_duplicated(self):
        jid = self._post()
        self._dm(jid)
        w = self._worker(scan_open=True)  # would find it a second time
        cands = w.candidates()
        self.assertEqual([c[0] for c in cands], [jid])

    def test_bad_role_is_rejected_at_construction(self):
        with self.assertRaises(roles.RoleError):
            self._worker(roles_="not-a-role")


class RolesTest(RelayTestCase):
    def test_lists_the_shipped_roles(self):
        names = roles.list_roles()
        for expected in ("builder", "planner", "reviewer", "tester"):
            self.assertIn(expected, names)

    def test_loads_a_role_brief(self):
        text = roles.load_role("builder")
        self.assertTrue(text.strip())

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(roles.RoleError):
            roles.load_role("nope")

    def test_a_role_name_cannot_escape_the_directory(self):
        for bad in ("../README", "../../etc/passwd", "a/b", "..", ""):
            with self.assertRaises(roles.RoleError):
                roles.role_path(bad)

    def test_parse_roles_dedupes_and_lowercases(self):
        self.assertEqual(roles.parse_roles("Builder, builder , tester"),
                         ["builder", "tester"])
        self.assertEqual(roles.parse_roles(""), [])

    def test_cli_lists_and_prints_roles(self):
        rc_, out, err = self.run_cli(cli.main, ["roles"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("builder", out)
        rc_, out, err = self.run_cli(cli.main, ["roles", "builder"])
        self.assertEqual(rc_, 0, err)
        self.assertTrue(out.strip())
        rc_, out, err = self.run_cli(cli.main, ["roles", "nope"])
        self.assertEqual(rc_, 2)
        self.assertIn("unknown role", err)

    def test_cli_rejects_an_unknown_role_before_registering(self):
        rc_, out, err = self.run_cli(
            cli.main, ["register", "--caps", "python", "--roles", "nope"])
        self.assertEqual(rc_, 2)
        self.assertIn("unknown role", err)
        self.assertEqual(capabilities.agents(), {})


class DispatchRoleTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def test_role_narrows_the_dispatch(self):
        capabilities.register("w1", ["python"], roles="builder",
                              dm_secret="s-builder")
        capabilities.register("w2", ["python"], roles="reviewer",
                              dm_secret="s-reviewer")
        jid, dms = overseer.dispatch("T", "spec", requirements=["python"],
                                     roles=["reviewer"])
        self.assertEqual([d[0] for d in dms], ["w2"])
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.roles_of(h), {"reviewer"})

    def test_dispatch_persists_files_and_reqs(self):
        capabilities.register("w1", ["python"], dm_secret="s")
        jid, _ = overseer.dispatch("T", "spec", requirements=["python"],
                                   files="a.py,b.py")
        h, _ = jobs.job_get(jid)
        self.assertEqual(jobs.files_of(h), {"a.py", "b.py"})
        self.assertEqual(jobs.reqs_of(h), {"python"})

    def test_dispatch_with_no_role_matching_sends_no_dm(self):
        capabilities.register("w1", ["python"], roles="builder",
                              dm_secret="s")
        jid, dms = overseer.dispatch("T", "spec", requirements=["python"],
                                     roles=["reviewer"])
        self.assertEqual(dms, [])
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "open")  # posted, just not offered


class OverseerLoopTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def test_tick_lines_reports_clean_when_idle(self):
        self.assertEqual(
            overseer.tick_lines({"swept": [], "pruned": [], "verified": [],
                                 "failed": [], "errors": []}),
            ["TICK_CLEAN"])

    def test_tick_lines_reports_events(self):
        lines = overseer.tick_lines({"swept": ["3"], "pruned": ["dead"],
                                     "verified": ["4"], "failed": ["5"],
                                     "errors": []})
        self.assertIn("SWEPT 3", lines)
        self.assertIn("PRUNED dead", lines)
        self.assertIn("VERIFIED 4", lines)
        self.assertIn("ACCEPTANCE_FAILED 5", lines)

    def test_tick_loop_runs_the_requested_cycles(self):
        n = overseer.tick_loop(repo=self.tmp, interval=0, max_cycles=3,
                               out=io.StringIO())
        self.assertEqual(n, 3)

    def test_tick_loop_survives_a_failing_tick(self):
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("bus down")
            return {"swept": [], "pruned": [], "verified": [],
                    "failed": [], "errors": []}

        with mock.patch.object(overseer, "tick", flaky):
            n = overseer.tick_loop(interval=0, max_cycles=2,
                                   out=io.StringIO())
        self.assertEqual(n, 2)
        self.assertEqual(calls["n"], 2)

    def test_cli_tick_loop_is_wired_up(self):
        rc_, out, err = self.run_cli(
            cli.main, ["tick", "--loop", "--interval", "0",
                       "--max-cycles", "2", "--repo", self.tmp])
        self.assertEqual(rc_, 0, err)


class WorkerCliTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"

    def test_loop_without_a_handler_is_refused(self):
        rc_, out, err = self.run_cli(
            cli.main, ["worker", "--caps", "python", "--dm", "s", "--loop"])
        self.assertEqual(rc_, 2)
        self.assertIn("--loop needs --handler", err)

    def test_loop_drains_the_queue_through_the_cli(self):
        script = os.path.join(self.tmp, "handler.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho COMMIT deadbee\n")
        os.chmod(script, 0o755)
        capabilities.register("w1", ["python"], dm_secret="s3cret")
        self.set_nick("overseer", "host-o")
        jid, _ = overseer.dispatch("Build thing", "do it",
                                  requirements=["python"])
        self.set_nick("w1", "host-w")
        rc_, out, err = self.run_cli(
            cli.main, ["worker", "--dm", "s3cret", "--caps", "python",
                       "--workdir", self.tmp, "--loop", "--handler", script,
                       "--interval", "0", "--max-cycles", "2"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("looping every", out)
        h, _ = jobs.job_get(jid)
        self.assertEqual(h["status"], "done")

    def test_registered_without_once_or_loop_exits_zero(self):
        rc_, out, err = self.run_cli(
            cli.main, ["worker", "--caps", "python", "--dm", "s"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("--once", err)


class VerifyWorktreeRecoveryTest(RelayTestCase):
    """A verify run that died must not poison the job forever."""

    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.repo = make_git_repo(os.path.join(self.tmp, "repo"))
        self.base = os.path.join(self.tmp, "wts")
        os.makedirs(self.base, exist_ok=True)
        self.set_nick("overseer", "host-o")

    def _done_job(self, jid="1"):
        rc_, _, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--branch", "worker/thing",
                        "--accept", "file-exists: b.txt"])
        self.assertEqual(rc_, 0, err)
        self.set_nick("w1", "host-w")
        rc_, _, err = self.run_cli(jobs.main, ["claim", jid])
        self.assertEqual(rc_, 0, err)
        rc_, _, err = self.run_cli(
            jobs.main, ["done", jid, "--commit", "deadbee"])
        self.assertEqual(rc_, 0, err)
        self.set_nick("overseer", "host-o")

    def test_reclaim_removes_a_stale_worktree_directory(self):
        stale = os.path.join(self.base, "verify-9")
        os.makedirs(stale)
        with open(os.path.join(stale, "leftover"), "w") as f:
            f.write("x")
        self.assertTrue(gitops.worktree_reclaim(self.repo, stale))
        self.assertFalse(os.path.exists(stale))

    def test_verify_reclaims_a_crashed_run_and_passes(self):
        self._done_job()
        stale = os.path.join(self.base, "verify-1")
        os.makedirs(stale)
        with open(os.path.join(stale, "leftover"), "w") as f:
            f.write("junk from a dead run")
        ok, report = overseer.verify_done(
            "1", repo=self.repo, workdir_base=self.base)
        self.assertTrue(ok, report)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["verified"], "1")
        # and the worktree is cleaned up, not left for the next run
        self.assertFalse(os.path.exists(stale))

    def test_verify_still_cleans_up_on_failure(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s",
                                 "--branch", "worker/thing",
                                 "--accept", "file-exists: nope.txt"])
        self.set_nick("w1", "host-w")
        self.run_cli(jobs.main, ["claim", "1"])
        self.run_cli(jobs.main, ["done", "1", "--commit", "deadbee"])
        self.set_nick("overseer", "host-o")
        ok, report = overseer.verify_done(
            "1", repo=self.repo, workdir_base=self.base)
        self.assertFalse(ok)
        self.assertIn("FAIL", report)
        self.assertEqual(os.listdir(self.base), [])


class WorkerBudgetTest(RelayTestCase):
    """max_jobs binds locally, not only at dispatch.

    Without a local budget a worker claims every job it matches in one
    cycle and then works them one at a time, leaving the rest claimed,
    untouched, and looking "in progress" to the whole crew while their
    leases tick down towards a sweep.
    """

    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"

    def _post(self, title):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", title, "--spec", "do it",
                        "--req", "python"])
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def _worker(self, max_jobs=1, nick="w1"):
        self.set_nick(nick, "host-" + nick)
        return worker.Worker(caps=["python"], dm_secret="s",
                             workdir=self.tmp, max_jobs=max_jobs,
                             scan_open=True)

    def test_only_the_budget_is_claimed_in_a_cycle(self):
        first = self._post("A")
        second = self._post("B")
        rep = self._worker().run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [first])
        self.assertEqual(rep["deferred"], [second])
        h, _ = jobs.job_get(second)
        self.assertEqual(h["status"], "open")     # left for the crew
        self.assertEqual(jobs.attempts_of(h), 0)   # and not an attempt

    def test_a_wider_budget_takes_both(self):
        first = self._post("A")
        second = self._post("B")
        rep = self._worker(max_jobs=2).run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [first, second])
        self.assertEqual(rep["done"], [first, second])
        self.assertEqual(rep["deferred"], [])

    def test_a_claim_already_held_uses_up_the_budget(self):
        first = self._post("A")
        second = self._post("B")
        w = self._worker()
        self.assertTrue(w._claim(first)[0])
        rep = w.run_once(_ok_handler)
        self.assertEqual(rep["claimed"], [])
        self.assertEqual(rep["deferred"], [second])

    def test_the_next_cycle_picks_up_what_was_deferred(self):
        self._post("A")
        second = self._post("B")
        w = self._worker()
        w.run_once(_ok_handler)
        self.assertEqual(w.run_once(_ok_handler)["claimed"], [second])

    def test_the_cycle_line_reports_deferrals(self):
        self._post("A")
        self._post("B")
        out = io.StringIO()
        self._worker().run_loop(_ok_handler, interval=0, max_cycles=1,
                                out=out)
        self.assertIn("deferred=1", out.getvalue())

    def test_the_cli_passes_max_jobs_through(self):
        script = os.path.join(self.tmp, "handler.sh")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho COMMIT deadbee\n")
        os.chmod(script, 0o755)
        rc_, out, err = self.run_cli(
            cli.main, ["worker", "--caps", "python", "--dm", "s",
                       "--workdir", self.tmp, "--loop", "--handler", script,
                       "--max-jobs", "3", "--interval", "0",
                       "--max-cycles", "1"])
        self.assertEqual(rc_, 0, err)


if __name__ == "__main__":
    unittest.main()
