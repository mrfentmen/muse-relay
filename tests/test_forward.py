"""Unit tests for the FWD protocol (forward.py) and incoming recording."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import store
import edits
import send
import poll
import watch
import forward


class RecordIncomingTest(RelayTestCase):
    """edits.record_incoming: poll/watch give other nicks' lines ids."""

    def test_poll_records_incoming_messages_with_ids(self):
        self.run_cli(send.main, ["--room", "build-x", "mine first"])
        self.set_nick("other", "host-b")
        self.fake.bus_push("alice: design doc ready", key="muse-bus:room:build-x")
        rc_, out, err = self.run_cli(poll.main, ["--room", "build-x"])
        self.assertEqual(rc_, 0, err)
        st = store.MessageStore(store.store_path("build-x"), room="build-x")
        self.assertEqual(st.get("build-x-1")["text"], "mine first")
        entry = st.get("build-x-2")
        self.assertEqual((entry["nick"], entry["text"]),
                         ("alice", "design doc ready"))

    def test_watch_records_incoming_messages_with_ids(self):
        self.fake.bus_push("bob: ping from the void", key="muse-bus")
        self.run_cli(send.main, ["wakeup"])  # own line: stored by send,
        # skipped by record_incoming on re-read (no duplicate id)
        st = store.MessageStore(store.store_path(""), room="")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            watch.watch_once([["main bus", "muse-bus",
                               os.path.join(self.tmp, "wseen"), 0]])
        entries = st.entries()
        bobs = [e for e in entries
                if (e["nick"], e["text"]) == ("bob", "ping from the void")]
        self.assertEqual(len(bobs), 1)

    def test_record_skips_own_lines_edit_lines_and_garbage(self):
        st = store.MessageStore(store.store_path(""), room="")
        self.assertIsNone(edits.record_incoming(st, "tester: mine", "tester"))
        self.assertIsNone(edits.record_incoming(
            st, "alice: EDIT 1 rewritten", "tester"))
        self.assertIsNone(edits.record_incoming(st, "no colon here", "tester"))
        self.assertIsNone(edits.record_incoming(st, ": leadless", "tester"))
        self.assertEqual(st.entries(), [])

    def test_record_skips_existing_texts_idempotently(self):
        st = store.MessageStore(store.store_path(""), room="")
        e1 = edits.record_incoming(st, "alice: hello", "tester")
        e2 = edits.record_incoming(st, "alice: hello", "tester")
        self.assertIsNotNone(e1)
        self.assertIsNone(e2)  # duplicate of the tail
        self.assertEqual(len(st.entries()), 1)


class ForwardTest(RelayTestCase):
    """forward.py: lookup, (via) wire format, hop preservation, errors."""

    def test_forward_own_sent_message(self):
        self.run_cli(send.main, ["--room", "build-x", "look at this"])
        rc_, out, err = self.run_cli(
            forward.main, ["--room", "build-x", "build-x-1", "#announce"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("FORWARDED build-x-1", out)
        self.assertTrue(any("tester (via tester): look at this" in b
                          for b in self.pushes_to("muse-bus:room:announce")))

    def test_forward_other_nicks_message_uses_their_nick(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("alice", "big news")
        rc_, out, err = self.run_cli(forward.main, ["1", "#news"])
        self.assertEqual(rc_, 0, err)
        self.assertTrue(any("alice (via tester): big news" in b
                          for b in self.pushes_to("muse-bus:room:news")))

    def test_forward_unknown_id_is_clear_error(self):
        rc_, out, err = self.run_cli(forward.main, ["99", "#news"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such message: 99", err)
        self.assertEqual(self.fake.pushes, [])

    def test_forward_preserves_single_hop(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("alice", "first post")
        self.run_cli(forward.main, ["1", "#news"])  # -> alice (via tester): ...
        # poll.py records the forwarded line arriving in #news, so it has
        # an id there; forwarding THAT keeps a single hop.
        self.set_nick("other", "host-b")
        self.run_cli(poll.main, ["--room", "news"])
        self.set_nick("tester", "host-a")  # back to tester for the re-forward
        forwarded = store.MessageStore(
            store.store_path("news"), room="news").get("news-1")
        self.assertIsNotNone(forwarded)
        self.assertEqual(
            forward.forward_text(forwarded),
            "alice (via other): first post")  # hop count stays at one

    def test_forward_text_never_stacks_via(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("bob", "x (via y): already hopped")
        self.run_cli(forward.main, ["1", "#news"])
        body = self.pushes_to("muse-bus:room:news")[0]
        self.assertEqual(body, "tester: bob (via tester): already hopped")
        self.assertNotIn("(via", body[8:].replace(" (via tester): ", ""))

    def test_forward_target_room_sanitized(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("alice", "hello")
        rc_, out, err = self.run_cli(forward.main, ["1", "NEWS"])
        self.assertEqual(rc_, 0, err)
        self.assertTrue(any("alice (via tester): hello" in b
                          for b in self.pushes_to("muse-bus:room:news")))

    def test_forward_from_dm_room(self):
        secret_room = rc.dm_room("s3cr3t")
        self.run_cli(send.main, ["--dm", "s3cr3t", "secret plan"])
        mid = f"{secret_room}-1"
        rc_, out, err = self.run_cli(
            forward.main, ["--dm", "s3cr3t", mid, "#crew"])
        self.assertEqual(rc_, 0, err)
        self.assertTrue(any("tester (via tester): secret plan" in b
                          for b in self.pushes_to("muse-bus:room:crew")))

    def test_parse_fwd_text(self):
        self.assertEqual(forward.parse_fwd_text("FWD build-x-3 #news"),
                         ("build-x-3", "news"))
        self.assertEqual(forward.parse_fwd_text("FWD 7 news"),
                         ("7", "news"))
        self.assertIsNone(forward.parse_fwd_text("FORWARD 7 nope"))
        self.assertIsNone(forward.parse_fwd_text("FWD 7 #bad room!"))

    def test_via_parts(self):
        self.assertEqual(
            forward.via_parts("alice (via tester): big news"),
            ("alice", "tester", "big news"))
        self.assertIsNone(forward.via_parts("alice: plain message"))
        # multiline text survives
        self.assertEqual(
            forward.via_parts("alice (via t): line1\nline2"),
            ("alice", "t", "line1\nline2"))


class ForwardBusProtocolTest(RelayTestCase):
    """A FWD line arriving on the bus re-forwardable via parse (docs)."""

    def test_poll_renders_fwd_lines_as_normal_messages(self):
        # FWD lines are ordinary "<nick>: FWD ..." chatter; the dedicated
        # CLI does the repost. They render as-is and get recorded.
        self.fake.bus_push("milo: FWD 7 #news", key="muse-bus")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("milo: FWD 7 #news", out)


if __name__ == "__main__":
    unittest.main()
