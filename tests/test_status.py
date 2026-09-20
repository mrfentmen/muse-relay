"""Unit tests for status messages (send.py --status, presence.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import send
import presence


class StatusSetTest(RelayTestCase):
    """relay_common.status_set/get: keys, TTL, clearing, limits."""

    def test_status_set_and_get_round_trip(self):
        rc.status_set("heads down")
        self.assertEqual(rc.status_get("tester"), "heads down")
        self.assertIn("muse-bus:status:tester", self.fake.strings)

    def test_empty_status_clears_key(self):
        rc.status_set("heads down")
        rc.status_set("")
        self.assertEqual(rc.status_get("tester"), None)
        self.assertNotIn("muse-bus:status:tester", self.fake.strings)

    def test_status_expires_after_ttl(self):
        rc.status_set("brb")
        self.fake.advance(rc.STATUS_TTL + 1)
        self.assertEqual(rc.status_get("tester"), None)

    def test_status_too_long_rejected(self):
        with self.assertRaises(ValueError) as cm:
            rc.status_set("x" * (rc.MAX_TEXT + 1))
        self.assertIn("too long", str(cm.exception))
        self.assertIsNone(rc.status_get("tester"))

    def test_status_get_missing_is_none(self):
        self.assertIsNone(rc.status_get("nobody"))

    def test_status_get_many_bulk(self):
        rc.status_set("busy", nick="alice")
        rc.status_set("afk", nick="bob")
        got = rc.status_get_many(["alice", "bob", "carol"])
        self.assertEqual(got, {"alice": "busy", "bob": "afk"})
        self.assertEqual(rc.status_get_many([]), {})


class StatusRefreshTest(RelayTestCase):
    """presence_beat renews the status TTL like a heartbeat."""

    def test_beat_refreshes_status_ttl(self):
        rc.status_set("heads down")
        self.fake.advance(90)  # 30s left on the status...
        rc.presence_beat()
        ttl = rc.json.loads(self.fake.api_get(
            f"ttl/muse-bus:status:tester"))["result"]
        self.assertGreater(ttl, 100)  # ...back to a fresh STATUS_TTL

    def test_beat_without_status_is_a_noop(self):
        rc.presence_beat()
        self.assertNotIn("muse-bus:status:tester", self.fake.strings)

    def test_status_fades_when_nick_goes_quiet(self):
        rc.status_set("brb")
        self.fake.advance(rc.STATUS_TTL + 5)
        # No beat, no send: the status is gone.
        self.assertEqual(rc.status_get("tester"), None)


class StatusCliTest(RelayTestCase):
    """send.py --status and presence.py display."""

    def test_send_status_sets_key(self):
        rc_, out, err = self.run_cli(send.main, ["--status", "heads down"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("STATUS_SET tester: heads down", out)
        self.assertEqual(rc.status_get("tester"), "heads down")
        self.assertEqual(self.fake.pushes, [])  # posts nothing

    def test_send_status_empty_clears(self):
        self.run_cli(send.main, ["--status", "heads down"])
        rc_, out, err = self.run_cli(send.main, ["--status", ""])
        self.assertEqual(rc_, 0, err)
        self.assertIn("STATUS_CLEARED tester", out)
        self.assertIsNone(rc.status_get("tester"))

    def test_send_status_too_long_rejected(self):
        rc_, out, err = self.run_cli(
            send.main, ["--status", "x" * (rc.MAX_TEXT + 1)])
        self.assertEqual(rc_, 2)
        self.assertIn("too long", err)
        self.assertIsNone(rc.status_get("tester"))

    def test_send_status_rejects_combinations(self):
        for argv in (["--status", "hi", "and this"],
                     ["--status", "hi", "--typing"],
                     ["--status", "hi", "--desc", "d"]):
            rc_, _, err = self.run_cli(send.main, argv)
            self.assertEqual(rc_, 2, argv)

    def test_presence_shows_status_next_to_nick(self):
        self.run_cli(send.main, ["--status", "heads down"])
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester (heads down)", out)

    def test_presence_bare_nick_without_status(self):
        self.run_cli(send.main, ["--status", "brb"])
        self.run_cli(send.main, ["--status", ""])  # cleared
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester\n", out)
        self.assertNotIn("tester (", out)

    def test_presence_expired_status_shows_bare_nick(self):
        self.run_cli(send.main, ["--status", "brb"])
        self.fake.advance(rc.STATUS_TTL + 1)  # status and presence expire
        rc.presence_beat()  # nick comes back, no status left to renew
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester\n", out)
        self.assertNotIn("(", out)

    def test_status_survives_a_regular_send_beat(self):
        self.run_cli(send.main, ["--status", "heads down"])
        self.fake.advance(60)
        self.run_cli(send.main, ["regular chatter"])  # presence_beat fires
        self.assertEqual(rc.status_get("tester"), "heads down")


if __name__ == "__main__":
    unittest.main()
