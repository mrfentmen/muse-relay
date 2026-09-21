#!/usr/bin/env python3
"""mos.py status / mos.py doctor: see the crew, and see what is wrong.

status is the one-screen answer to "what is happening right now";
doctor is the preflight that answers "why is nothing happening" before
anyone starts guessing. Neither prints the token.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase, make_git_repo
import jobs
import relay_common as rc
from mos import capabilities, cli, inspect as inspect_mod


class SnapshotTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.set_nick("overseer", "host-o")

    def _post(self, title="T", *extra):
        rc_, out, err = self.run_cli(
            jobs.main, ["post", "--title", title, "--spec", "s"] + list(extra))
        self.assertEqual(rc_, 0, err)
        return out.split()[-1]

    def test_an_empty_crew_reports_empty(self):
        snap = inspect_mod.snapshot()
        self.assertEqual(snap["jobs"], [])
        self.assertEqual(snap["agents"], {})
        self.assertIsNone(snap["oldest_open"])
        self.assertIn("AGENTS none registered", inspect_mod.status_lines(snap))

    def test_it_counts_every_job_state(self):
        self._post("A")
        self._post("B")
        self.assertEqual(inspect_mod.snapshot()["counts"]["open"], 2)
        self.run_cli(jobs.main, ["claim", "1"])
        snap = inspect_mod.snapshot()
        self.assertEqual(snap["counts"]["open"], 1)
        self.assertEqual(snap["counts"]["claimed"], 1)
        self.assertEqual(snap["load"], {"overseer": 1})

    def test_it_reports_agents_with_their_load(self):
        capabilities.register("w1", ["python", "docs"], roles="builder",
                              max_jobs=2, dm_secret="s")
        self.assertEqual(
            inspect_mod.snapshot()["agents"]["w1"]["roles"], "builder")
        line = [ln for ln in inspect_mod.status_lines()
                if ln.startswith("AGENT w1")][0]
        self.assertIn("caps=docs,python", line)
        self.assertIn("roles=builder", line)
        self.assertIn("jobs=0/2", line)
        self.assertIn("dm=yes", line)

    def test_a_stale_agent_is_marked_stale(self):
        capabilities.register("w1", ["python"], dm_secret="s")
        self.fake.advance(capabilities.STALE_AFTER + 5)
        line = [ln for ln in inspect_mod.status_lines()
                if ln.startswith("AGENT w1")][0]
        self.assertIn("stale", line)

    def test_the_oldest_open_job_is_reported_with_its_age(self):
        jid = self._post("Slow thing")
        self.fake.advance(600)
        snap = inspect_mod.snapshot()
        self.assertEqual(snap["oldest_open"]["jid"], jid)
        self.assertEqual(snap["oldest_open"]["age"], 600)
        self.assertIn("OLDEST_OPEN 1 600s Slow thing",
                      inspect_mod.status_lines(snap))

    def test_the_oldest_open_job_ignores_claimed_and_done_ones(self):
        self._post("A")
        self.fake.advance(600)
        second = self._post("B")
        self.run_cli(jobs.main, ["claim", "1"])
        snap = inspect_mod.snapshot()
        self.assertEqual(snap["oldest_open"]["jid"], second)
        self.assertEqual(snap["oldest_open"]["age"], 0)

    def test_an_unreadable_queue_is_reported_not_raised(self):
        with mock.patch.object(jobs, "_all_ids",
                               side_effect=RuntimeError("bus down")):
            snap = inspect_mod.snapshot()
        self.assertIn("job index unreadable", snap["error"])


class StatusCliTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"

    def test_status_prints_lines(self):
        rc_, out, err = self.run_cli(cli.main, ["status"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("TOTAL_JOBS 0", out)

    def test_status_json_is_machine_readable(self):
        capabilities.register("w1", ["python"], dm_secret="s")
        rc_, out, err = self.run_cli(cli.main, ["status", "--json"])
        self.assertEqual(rc_, 0, err)
        snap = json.loads(out)
        self.assertIn("w1", snap["agents"])
        self.assertEqual(snap["counts"]["open"], 0)

    def test_status_exits_nonzero_when_the_queue_is_unreadable(self):
        with mock.patch.object(jobs, "_all_ids",
                               side_effect=RuntimeError("bus down")):
            rc_, out, err = self.run_cli(cli.main, ["status"])
        self.assertEqual(rc_, 2)
        self.assertIn("ERROR", out)


class DoctorTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"
        self.token = os.path.join(self.tmp, "token")
        with open(self.token, "w") as f:
            f.write("super-secret-rest-token\n")

    def _checks(self, **kw):
        with mock.patch.object(rc, "RELAY_URL", "https://example.invalid"), \
                mock.patch.object(rc, "TOKEN_FILE", self.token):
            return inspect_mod.doctor_checks(**kw)

    def _by_name(self, checks):
        return {name: (ok, detail) for name, ok, detail in checks}

    def test_a_healthy_setup_passes(self):
        checks = self._checks()
        self.assertTrue(inspect_mod.doctor_ok(checks), checks)

    def test_a_missing_url_fails(self):
        with mock.patch.object(rc, "RELAY_URL", ""), \
                mock.patch.object(rc, "TOKEN_FILE", self.token):
            checks = self._by_name(inspect_mod.doctor_checks())
        ok, detail = checks["url"]
        self.assertFalse(ok)
        self.assertIn("MUSE_RELAY_URL", detail)

    def test_a_missing_token_file_fails_without_leaking_anything(self):
        with mock.patch.object(rc, "RELAY_URL", "https://example.invalid"), \
                mock.patch.object(rc, "TOKEN_FILE",
                                  os.path.join(self.tmp, "nope")):
            checks = self._by_name(inspect_mod.doctor_checks())
        ok, detail = checks["token"]
        self.assertFalse(ok)
        self.assertIn("no token file", detail)

    def test_an_empty_token_file_fails(self):
        empty = os.path.join(self.tmp, "empty-token")
        with open(empty, "w"):
            pass
        with mock.patch.object(rc, "RELAY_URL", "https://example.invalid"), \
                mock.patch.object(rc, "TOKEN_FILE", empty):
            checks = self._by_name(inspect_mod.doctor_checks())
        self.assertFalse(checks["token"][0])
        self.assertIn("empty", checks["token"][1])

    def test_the_token_value_is_never_printed(self):
        checks = self._checks()
        text = "\n".join(inspect_mod.doctor_lines(checks))
        self.assertNotIn("super-secret-rest-token", text)
        self.assertIn("not shown", text)

    def test_an_unreachable_bus_fails(self):
        with mock.patch.object(rc, "RELAY_URL", "https://example.invalid"), \
                mock.patch.object(rc, "TOKEN_FILE", self.token), \
                mock.patch.object(rc, "api_get",
                                  side_effect=RuntimeError("boom")):
            checks = self._by_name(inspect_mod.doctor_checks())
        ok, detail = checks["redis"]
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    def test_a_repo_check_reports_branch_and_dirt(self):
        repo = make_git_repo(os.path.join(self.tmp, "repo"), branch=None)
        checks = self._by_name(self._checks(repo=repo))
        ok, detail = checks["repo"]
        self.assertTrue(ok, detail)
        self.assertIn("main", detail)
        ok, detail = self._by_name(
            self._checks(repo=os.path.join(self.tmp, "nope")))["repo"]
        self.assertFalse(ok)
        self.assertIn("not a git repo", detail)

    def test_a_secret_file_check_parses_a_keyring(self):
        ring = os.path.join(self.tmp, "ring")
        with open(ring, "w") as f:
            f.write("crew-1:abc\n")
        ok, detail = self._by_name(self._checks(secret_file=ring))["secret"]
        self.assertTrue(ok, detail)
        self.assertIn("crew-1", detail)
        self.assertNotIn("abc", detail)

    def test_a_broken_secret_file_fails(self):
        ok, detail = self._by_name(
            self._checks(secret_file=os.path.join(self.tmp, "nope")))["secret"]
        self.assertFalse(ok)
        self.assertIn("cannot read secret file", detail)

    def test_doctor_cli_exits_zero_when_healthy(self):
        with mock.patch.object(rc, "RELAY_URL", "https://example.invalid"), \
                mock.patch.object(rc, "TOKEN_FILE", self.token):
            rc_, out, err = self.run_cli(cli.main, ["doctor"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("OK url:", out)

    def test_doctor_cli_exits_two_when_something_is_wrong(self):
        with mock.patch.object(rc, "RELAY_URL", ""):
            rc_, out, err = self.run_cli(cli.main, ["doctor"])
        self.assertEqual(rc_, 2)
        self.assertIn("FAIL url:", out)


if __name__ == "__main__":
    unittest.main()
