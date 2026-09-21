#!/usr/bin/env python3
"""Job ownership: declared files and required capabilities.

Two workers editing one file can only end in a merge nobody asked for,
so a job that declares --files owns them while its claim is live and a
second claim on an overlapping set is refused. --req is persisted so a
worker that never saw the DM can still tell whether an open job is its
own (mos/worker.py scan_open_jobs).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs


class FileOwnershipTest(RelayTestCase):
    def _post(self, title, files="", req="", role=""):
        argv = ["post", "--title", title, "--spec", "s"]
        if files:
            argv += ["--files", files]
        if req:
            argv += ["--req", req]
        if role:
            argv += ["--role", role]
        rc, out, err = self.run_cli(jobs.main, argv)
        self.assertEqual(rc, 0, err)
        return out.split()[-1]

    def _claim(self, jid, *extra):
        return self.run_cli(jobs.main, ["claim", jid] + list(extra))

    def test_disjoint_files_are_both_claimable(self):
        a = self._post("A", files="a.py")
        b = self._post("B", files="b.py")
        rc, _, err = self._claim(a)
        self.assertEqual(rc, 0, err)
        self.set_nick("other", "host-b")
        rc, _, err = self._claim(b)
        self.assertEqual(rc, 0, err)

    def test_undeclared_files_never_conflict(self):
        # Jobs that declare nothing are not assumed to own anything.
        a = self._post("A")
        b = self._post("B", files="a.py")
        self.assertEqual(self._claim(a)[0], 0)
        self.set_nick("other", "host-b")
        self.assertEqual(self._claim(b)[0], 0)

    def test_overlapping_files_are_refused(self):
        a = self._post("A", files="shared.py,a.py")
        b = self._post("B", files="shared.py")
        self.assertEqual(self._claim(a)[0], 0)
        self.set_nick("other", "host-b")
        rc, _, err = self._claim(b)
        self.assertEqual(rc, 2)
        self.assertIn("already held by live job", err)
        self.assertIn("shared.py", err)
        h, _ = jobs.job_get(b)
        self.assertEqual(h["status"], "open")  # nothing was claimed

    def test_paths_are_normalized_before_comparing(self):
        self._post("A", files="./shared.py")
        b = self._post("B", files="shared.py")
        self.assertEqual(self._claim("1")[0], 0)
        self.set_nick("other", "host-b")
        self.assertEqual(self._claim(b)[0], 2)

    def test_force_overrides_the_guard(self):
        self._post("A", files="shared.py")
        b = self._post("B", files="shared.py")
        self.assertEqual(self._claim("1")[0], 0)
        self.set_nick("other", "host-b")
        rc, _, err = self._claim(b, "--force")
        self.assertEqual(rc, 0, err)

    def test_guard_releases_when_the_holder_finishes(self):
        self._post("A", files="shared.py")
        b = self._post("B", files="shared.py")
        self.assertEqual(self._claim("1")[0], 0)
        rc, _, err = self.run_cli(
            jobs.main, ["done", "1", "--commit", "deadbee"])
        self.assertEqual(rc, 0, err)
        self.set_nick("other", "host-b")
        self.assertEqual(self._claim(b)[0], 0)

    def test_an_expired_lease_is_not_a_blocker(self):
        # A claim whose lease lapsed is sweep's problem, not a reason to
        # block live work on the same file.
        self._post("A", files="shared.py")
        b = self._post("B", files="shared.py")
        rc, _, err = self._claim("1", "--lease", "60")
        self.assertEqual(rc, 0, err)
        self.fake.advance(61)
        self.set_nick("other", "host-b")
        rc, _, err = self._claim(b)
        self.assertEqual(rc, 0, err)


class JobMetadataTest(RelayTestCase):
    def test_reqs_and_files_and_role_are_persisted(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--req", "python,Testing", "--files", "a.py,b.py",
                        "--role", "builder"])
        self.assertEqual(rc, 0, err)
        h, _ = jobs.job_get("1")
        self.assertEqual(jobs.reqs_of(h), {"python", "testing"})
        self.assertEqual(jobs.files_of(h), {"a.py", "b.py"})
        self.assertEqual(jobs.roles_of(h), {"builder"})

    def test_reqs_default_to_empty_when_unset(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        h, _ = jobs.job_get("1")
        self.assertEqual(jobs.reqs_of(h), set())
        self.assertEqual(jobs.files_of(h), set())
        self.assertEqual(jobs.roles_of(h), set())

    def test_show_reports_ownership(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s",
                                 "--files", "a.py", "--req", "python",
                                 "--role", "builder"])
        rc, out, err = self.run_cli(jobs.main, ["show", "1"])
        self.assertEqual(rc, 0, err)
        self.assertIn("files: a.py", out)
        self.assertIn("req: python", out)
        self.assertIn("role: builder", out)


if __name__ == "__main__":
    unittest.main()
