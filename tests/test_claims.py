"""Unit tests for nick claims and DM-room watching."""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import send
import poll
import watch


class NickClaimTest(RelayTestCase):
    def test_first_claim_wins(self):
        ok, holder = rc.nick_claim()
        self.assertEqual((ok, holder), (True, "host-a"))
        # a second instance on another machine, same nick
        self.set_nick("tester", "host-b")
        ok, holder = rc.nick_claim()
        self.assertEqual(ok, False)
        self.assertEqual(holder, "host-a")
        self.assertEqual(rc.nick_conflict_holder(), "host-a")

    def test_winner_sees_no_conflict(self):
        rc.nick_claim()
        self.assertIsNone(rc.nick_conflict_holder())

    def test_claim_renewed_by_heartbeat(self):
        rc.nick_claim()
        self.fake.advance(200)  # within the 300s TTL
        rc.presence_beat()
        # another host still can't take it
        self.set_nick("tester", "host-b")
        ok, holder = rc.nick_claim()
        self.assertFalse(ok)

    def test_claim_expires(self):
        rc.nick_claim()
        self.fake.advance(400)  # past the 300s TTL, no renewal
        self.set_nick("tester", "host-b")
        ok, holder = rc.nick_claim(force=True)
        self.assertEqual((ok, holder), (True, "host-b"))

    def test_conflict_clears_when_holder_leaves(self):
        rc.nick_claim()  # host-a
        self.set_nick("tester", "host-b")
        self.assertFalse(rc.nick_claim()[0])
        self.fake.advance(400)
        ok, holder = rc.nick_claim(force=True)
        self.assertEqual((ok, holder), (True, "host-b"))
        self.assertIsNone(rc.nick_conflict_holder())

    def test_nick_holder_readonly(self):
        self.assertIsNone(rc.nick_holder("tester"))
        rc.nick_claim()
        self.assertEqual(rc.nick_holder("tester"), "host-a")

    def test_send_warns_on_conflict(self):
        # someone else holds our nick
        self.fake.api_get("set/muse-bus:nickclaim:tester/host-evil/NX/EX/300")
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0)  # still sends; warns
        self.assertIn("WARNING: nick 'tester' is claimed by instance "
                      "'host-evil'", err)

    def test_send_quiet_without_conflict(self):
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0)
        self.assertNotIn("claimed by", err)


class DmRoomTest(RelayTestCase):
    def test_dm_room_vector(self):
        expected = "dm-" + hashlib.sha1(b"s3cr3t").hexdigest()[:12]
        self.assertEqual(rc.dm_room("s3cr3t"), expected)
        self.assertIs(rc.dm_room, send.dm_room)  # single shared impl

    def test_send_dm_targets_dm_room(self):
        rc_, out, err = self.run_cli(send.main, ["--dm", "s3cr3t", "hi"])
        self.assertEqual(rc_, 0)
        key = "muse-bus:room:" + rc.dm_room("s3cr3t")
        self.assertIn("tester: hi", self.pushes_to(key))
        self.assertEqual(self.pushes_to("muse-bus"), [])

    def test_poll_dm(self):
        key = "muse-bus:room:" + rc.dm_room("s3cr3t")
        self.fake.bus_push("overseer: JOB:1 build it", key=key)
        rc_, out, err = self.run_cli(poll.main, ["--dm", "s3cr3t"])
        self.assertEqual(rc_, 0)
        self.assertIn("overseer: JOB:1 build it", out)
        # second poll: nothing new (per-room offset tracked)
        rc_, out, err = self.run_cli(poll.main, ["--dm", "s3cr3t"])
        self.assertNotIn("overseer:", out)

    def test_poll_single_room_unchanged(self):
        self.fake.bus_push("other: hi", key="muse-bus")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0)
        self.assertIn("NEW_MESSAGES:", out)
        self.assertNotIn("ROOM", out)
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertIn("NO_NEW_MESSAGES", out)

    def test_poll_multi_room_headers(self):
        rc_, out, err = self.run_cli(
            poll.main, ["--room", "build-x", "--dm", "s3cr3t"])
        self.assertEqual(rc_, 0)
        self.assertIn("ROOM room 'build-x': NO_NEW_MESSAGES", out)
        self.assertIn("ROOM room 'dm-", out)

    def test_poll_own_messages_filtered(self):
        self.fake.bus_push("tester: my own", key="muse-bus")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertIn("NO_NEW_MESSAGES", out)

    def test_watch_once_dm(self):
        key = "muse-bus:room:" + rc.dm_room("s3cr3t")
        self.fake.bus_push("overseer: ping", key=key)
        import tempfile
        seen = os.path.join(self.tmp, "w")
        targets = [["dm", key, seen, 0]]
        shown = watch.watch_once(targets)
        self.assertEqual(shown, 1)
        self.assertEqual(targets[0][3], 1)
        # second iteration: nothing new
        self.assertEqual(watch.watch_once(targets), 0)


if __name__ == "__main__":
    unittest.main()
