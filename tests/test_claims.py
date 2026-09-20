"""Unit tests for nick claims and DM-room watching."""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import presence
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
        self.fake.advance(100)  # within the TTL (matched to presence)
        rc.presence_beat()
        # another host still can't take it
        self.set_nick("tester", "host-b")
        ok, holder = rc.nick_claim()
        self.assertFalse(ok)

    def test_claim_ttl_matches_presence_interval(self):
        # The reservation lapses exactly when presence does: both are
        # renewed by every heartbeat.
        self.assertEqual(rc.NICKCLAIM_TTL, rc.PRESENCE_TTL)
        rc.nick_claim()
        self.fake.advance(rc.PRESENCE_TTL + 1)  # no renewal: both lapse
        self.set_nick("tester", "host-b")
        ok, holder = rc.nick_claim(force=True)
        self.assertEqual((ok, holder), (True, "host-b"))

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

    def test_send_rejects_conflicting_nick(self):
        # someone else holds our nick: the send is rejected, nothing posted
        self.fake.api_get(
            f"set/muse-bus:nickclaim:tester/host-evil/NX/EX/{rc.NICKCLAIM_TTL}")
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 2)
        self.assertIn("reserved by instance 'host-evil'", err)
        self.assertIn("pick another nick", err)
        self.assertEqual(self.pushes_to("muse-bus"), [])

    def test_first_send_reserves_nick(self):
        # first use of a nick reserves it via the send path
        self.assertIsNone(rc.nick_holder("tester"))
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0, err)
        self.assertEqual(rc.nick_holder("tester"), "host-a")

    def test_send_rejected_after_claim_lapses(self):
        # holder goes quiet past the TTL: the nick frees up, send works
        rc.nick_claim()  # host-a
        self.set_nick("tester", "host-b")
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 2)  # rejected while the claim is live
        self.fake.advance(rc.NICKCLAIM_TTL + 1)
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester: hello", self.pushes_to("muse-bus"))

    def test_send_quiet_without_conflict(self):
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0)
        self.assertNotIn("claimed by", err)


class PresenceClaimDisplayTest(RelayTestCase):
    def test_presence_shows_claim_holders(self):
        rc.presence_beat()  # tester/host-a: heartbeat + nick claim
        self.set_nick("other", "host-b")
        rc.presence_beat()
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester [host-a]", out)
        self.assertIn("other [host-b]", out)

    def test_presence_bare_when_nick_unclaimed(self):
        # ghost is online (live heartbeat) but holds no reservation
        self.fake.api_get("setex/muse-bus:presence:ghost/120/1")
        self.fake.api_get("sadd/muse-bus:nicks/ghost")
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("ghost", out)
        self.assertNotIn("ghost [", out)

    def test_presence_holder_combines_with_status(self):
        rc.presence_beat()
        rc.status_set("heads down")
        rc_, out, err = self.run_cli(presence.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester [host-a] (heads down)", out)

    def test_nick_holders_batch(self):
        rc.presence_beat()  # claims tester for host-a
        self.assertEqual(rc.nick_holders(["tester", "nobody"]),
                         {"tester": "host-a"})
        self.assertEqual(rc.nick_holders([]), {})


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
