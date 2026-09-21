#!/usr/bin/env python3
"""The secret tripwire on job posts.

A job spec lives in plaintext in Redis and the job title is announced on
the bus, so a credential pasted into one is a credential handed to
everyone holding the bus token. post refuses a spec that looks like it
contains one — unless --allow-secrets says the match is a false
positive. The scan reports the KIND of match and the line, never the
matched text itself.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import jobs

# One sample per pattern the tripwire knows, plus what it should say.
SECRET_SPECS = [
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n", "PEM private key"),
    ("use sk-abcdefghijklmnopqrstuvwx for the client", "OpenAI-style API key"),
    ("token = ghp_abcdefghijklmnopqrstuvwxyz012345", "GitHub token"),
    ("slack: xoxb-1234567890-abcdefghijkl", "Slack token"),
    ("key AKIAIOSFODNN7EXAMPLE here", "AWS access key id"),
    ("maps key AIzaSyA1234567890abcdefghijklmnopqrstu", "Google API key"),
    ("bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
     "JWT"),
    ("password = hunter2hunter2", "credential assignment"),
]


class ScanTest(unittest.TestCase):
    def test_detects_each_known_shape(self):
        for spec, label in SECRET_SPECS:
            hits = jobs.scan_for_secrets(spec)
            self.assertEqual(len(hits), 1, (spec, hits))
            self.assertEqual(hits[0][1], label, spec)

    def test_a_clean_spec_is_clean(self):
        clean = ("Refactor the polling helpers in poll.py and add tests "
                 "under tests/test_poll_helpers.py.\n"
                 "Acceptance: command-ok: python3 -m unittest discover -s "
                 "tests\nfiles-only: poll.py,tests/test_poll_helpers.py")
        self.assertEqual(jobs.scan_for_secrets(clean), [])

    def test_empty_text_is_clean(self):
        self.assertEqual(jobs.scan_for_secrets(""), [])
        self.assertEqual(jobs.scan_for_secrets(None), [])

    def test_it_reports_the_line_and_never_the_secret(self):
        spec = "\n".join(["fine so far", "secret = super-secret-value-1"])
        hits = jobs.scan_for_secrets(spec)
        self.assertEqual(hits, [(2, "credential assignment")])
        self.assertNotIn("super-secret-value-1", repr(hits))

    def test_one_hit_per_line_is_reported(self):
        spec = "password = abcdefghijkl\ntoken = ghp_abcdefghijklmnopqrst\n"
        self.assertEqual([n for n, _ in jobs.scan_for_secrets(spec)], [1, 2])


class PostRefusalTest(RelayTestCase):
    def _post(self, *extra, **kw):
        argv = ["post", "--title", "T", "--spec", kw.get("spec", "s")]
        return self.run_cli(jobs.main, argv + list(extra))

    def test_post_refuses_a_spec_with_a_credential(self):
        rc_, out, err = self._post(
            spec="set the key to sk-abcdefghijklmnopqrstuvwx and retry")
        self.assertEqual(rc_, 2)
        self.assertIn("looks like a", err)
        self.assertIn("--allow-secrets", err)

    def test_a_refused_post_creates_nothing(self):
        self._post(spec="key = AKIAIOSFODNN7EXAMPLE")
        self.assertEqual(jobs._all_ids(), [])
        self.assertEqual(self.pushes_to("muse-bus"), [])

    def test_allow_secrets_overrides_the_tripwire(self):
        rc_, out, err = self._post(
            "--allow-secrets",
            spec="the fixture key sk-abcdefghijklmnopqrstuvwx is fake")
        self.assertEqual(rc_, 0, err)
        self.assertIn("JOB 1", out)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "open")

    def test_the_refusal_is_loud_about_every_line(self):
        rc_, out, err = self._post(
            spec="password = hunter2hunter2\nok line\n"
                 "token = ghp_abcdefghijklmnopqrstuvwxyz0")
        self.assertEqual(rc_, 2)
        self.assertIn("line 1", err)
        self.assertIn("line 3", err)
        self.assertNotIn("hunter2hunter2", err)

    def test_clean_specs_still_post(self):
        rc_, out, err = self._post(spec="fix the flaky test in tests/")
        self.assertEqual(rc_, 0, err)


if __name__ == "__main__":
    unittest.main()
