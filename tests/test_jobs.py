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


class SignedJobTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        # crew secret file both sides share out-of-band
        self.secret_file = os.path.join(self.tmpdir(), "job-secret")
        with open(self.secret_file, "wb") as f:
            f.write(b"crew-secret-abc\n")
        self.other_secret = os.path.join(self.tmpdir(), "wrong-secret")
        with open(self.other_secret, "wb") as f:
            f.write(b"wrong-secret-xyz")

    def tmpdir(self):
        d = getattr(self, "_tmpdir", None)
        if d is None:
            import tempfile
            d = self._tmpdir = tempfile.mkdtemp()
            self.addCleanup(__import__("shutil").rmtree, d,
                            ignore_errors=True)
        return d

    def post_signed(self, **kw):
        args = ["post", "--title", kw.get("title", "T"),
                "--spec", kw.get("spec", "do the thing"),
                "--secret-file", self.secret_file]
        for k in ("accept", "branch", "base", "room"):
            if kw.get(k):
                args += ["--" + k, kw[k]]
        return self.run_cli(jobs.main, args)

    def test_post_signed_stores_sig(self):
        rc, out, err = self.post_signed()
        self.assertEqual(rc, 0)
        self.assertIn("JOB 1", out)
        h, _ = jobs.job_get("1")
        self.assertIn("sig", h)
        self.assertEqual(h["signed_by"], "tester")
        self.assertEqual(len(h["sig"]), 64)  # sha256 hex

    def test_post_unsigned_has_no_sig(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s"])
        self.assertEqual(rc, 0)
        h, _ = jobs.job_get("1")
        self.assertNotIn("sig", h)

    def test_post_bad_secret_file_errors(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--secret-file", "/nonexistent/secret"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot read secret file", err)

    def test_verify_ok(self):
        self.post_signed()
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")

    def test_verify_unsigned(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "UNSIGNED")

    def test_verify_wrong_secret(self):
        self.post_signed()
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.other_secret])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "BAD")

    def test_verify_tampered_spec(self):
        self.post_signed()
        # attacker rewrites the spec in Redis directly
        jobs.api_post("set/muse-bus:tjob:1:spec", "do the EVIL thing")
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "BAD")

    def test_verify_tampered_field(self):
        self.post_signed(title="Original title")
        jobs._hset("muse-bus:tjob:1", {"title": "PWNED title"})
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "BAD")

    def test_verify_unknown_job(self):
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "99", "--secret-file", self.secret_file])
        self.assertEqual(rc, 2)
        self.assertIn("no such job", err)

    def test_claim_signed_ok(self):
        self.post_signed()
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 0)
        self.assertIn("CLAIMED 1", out)

    def test_claim_refuses_tampered(self):
        self.post_signed()
        jobs.api_post("set/muse-bus:tjob:1:spec", "do the EVIL thing")
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 1)
        self.assertIn("signature BAD", err)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["status"], "open")  # still unclaimed

    def test_claim_unsigned_with_secret_still_works(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.secret_file])
        self.assertEqual(rc, 0)
        self.assertIn("CLAIMED 1", out)

    def test_show_displays_signed_by(self):
        self.post_signed()
        rc, out, err = self.run_cli(jobs.main, ["show", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("signed_by: tester", out)


if __name__ == "__main__":    unittest.main()


class HardeningTest(RelayTestCase):
    """v2 hardening: key ids, expiry, jid-bound signatures, loud rejections."""

    def setUp(self):
        super().setUp()
        self.keyring = os.path.join(self.tmp, "keyring")
        with open(self.keyring, "w") as f:
            f.write("crew-2026-09:crew-secret-abc\n")
            f.write("crew-2026-10:crew-secret-def\n")
        self.legacy = os.path.join(self.tmp, "legacy-secret")
        with open(self.legacy, "wb") as f:
            f.write(b"crew-secret-abc\n")

    def post_signed(self, *extra):
        argv = ["post", "--title", "T", "--spec", "s",
                "--secret-file", self.keyring] + list(extra)
        rc, out, err = self.run_cli(jobs.main, argv)
        self.assertEqual(rc, 0, err)
        return out

    def test_post_defaults_to_first_kid(self):
        self.post_signed()
        h, _ = jobs.job_get("1")
        self.assertEqual(h["sig_kid"], "crew-2026-09")
        self.assertEqual(h["sig_v"], "2")
        self.assertTrue(h["nonce"])
        rc, out, err = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")

    def test_post_explicit_kid(self):
        self.post_signed("--kid", "crew-2026-10")
        h, _ = jobs.job_get("1")
        self.assertEqual(h["sig_kid"], "crew-2026-10")
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")

    def test_post_unknown_kid_rejected(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--secret-file", self.keyring, "--kid", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown kid", err)

    def test_verify_unknown_kid_is_bad(self):
        self.post_signed("--kid", "crew-2026-10")
        other = os.path.join(self.tmp, "other-ring")
        with open(other, "w") as f:
            f.write("crew-2026-09:crew-secret-abc\n")
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", other])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "BAD")

    def test_legacy_single_token_file_still_works(self):
        rc, out, err = self.run_cli(
            jobs.main, ["post", "--title", "T", "--spec", "s",
                        "--secret-file", self.legacy])
        self.assertEqual(rc, 0, err)
        h, _ = jobs.job_get("1")
        self.assertEqual(h["sig_kid"], "default")
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.legacy])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")

    def test_v1_signature_still_verifies(self):
        # a job signed before the v2 upgrade (old payload, no sig_v)
        import hashlib
        import hmac as hmac_mod
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        payload = {"title": "T", "spec": "s", "accept": "",
                   "branch": "", "base": "", "room": ""}
        canon = json.dumps(payload, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
        sig = hmac_mod.new(b"crew-secret-abc", canon,
                           hashlib.sha256).hexdigest()
        jobs._hset(jobs._job_key("1"), {"sig": sig, "signed_by": "tester"})
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.legacy])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "OK")

    def test_expiry(self):
        self.post_signed("--expires", "3600")
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.keyring])
        self.assertEqual(out.strip(), "OK")
        self.fake.advance(3601)
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "EXPIRED")

    def test_claim_refuses_expired_and_alerts(self):
        self.post_signed("--expires", "3600")
        self.fake.advance(7200)
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 1)
        self.assertIn("EXPIRED", err)
        alerts = self.pushes_to("muse-bus")
        self.assertTrue(any("REJECTED:1 EXPIRED" in a for a in alerts),
                        alerts)

    def test_claim_refuses_tampered_and_alerts(self):
        self.post_signed()
        jobs._hset(jobs._job_key("1"), {"title": "TAMPERED"})
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 1)
        self.assertIn("BAD", err)
        alerts = self.pushes_to("muse-bus")
        self.assertTrue(any("REJECTED:1 BAD" in a for a in alerts), alerts)

    def test_transplanted_signature_fails(self):
        # a signature lifted from job 1 does not verify on job 2:
        # the signature is bound to the exact job id
        self.post_signed()
        self.post_signed()
        h1, _ = jobs.job_get("1")
        jobs._hset(jobs._job_key("2"), {
            "sig": h1["sig"], "sig_v": "2", "sig_kid": h1["sig_kid"],
            "signed_by": h1["signed_by"], "nonce": h1["nonce"],
            "expires_at": h1.get("expires_at", "")})
        rc, out, _ = self.run_cli(
            jobs.main, ["verify", "2", "--secret-file", self.keyring])
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "BAD")

    def test_show_displays_sig_fields(self):
        self.post_signed("--expires", "3600")
        rc, out, _ = self.run_cli(jobs.main, ["show", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("sig_kid: crew-2026-09", out)
        self.assertIn("sig_v: 2", out)
        self.assertIn("nonce: ", out)

    def test_unsigned_claim_still_allowed_with_warning(self):
        self.run_cli(jobs.main, ["post", "--title", "T", "--spec", "s"])
        rc, out, err = self.run_cli(
            jobs.main, ["claim", "1", "--secret-file", self.keyring])
        self.assertEqual(rc, 0)
        self.assertIn("unsigned", err)
