"""Unit tests for the message store and the EDIT protocol."""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import store
import edits
import send
import poll
import watch


class StoreTest(RelayTestCase):
    """store.py: ids, entries, ownership, persistence."""

    def setUp(self):
        super().setUp()
        self.room = "build-x"
        self.path = store.store_path(self.room)
        self.st = store.MessageStore(self.path, room=self.room)

    def test_append_assigns_room_scoped_ids(self):
        e1 = self.st.append("tester", "hello")
        e2 = self.st.append("tester", "again")
        self.assertEqual(e1["id"], "build-x-1")
        self.assertEqual(e2["id"], "build-x-2")
        self.assertEqual(e1["room"], "build-x")
        self.assertEqual(e1["nick"], "tester")
        self.assertEqual(e1["ts"], int(self.fake.now()))
        self.assertFalse(e1["edited"])
        self.assertEqual(e1["edit_history"], [])

    def test_append_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.st.append("tester", "")
        with self.assertRaises(ValueError):
            self.st.append("", "hi")
        self.assertEqual(self.st.entries(), [])

    def test_edit_own_message_updates_flag_and_history(self):
        e = self.st.append("tester", "hello")
        out = self.st.edit(e["id"], "tester", "hello fixed", ts=123)
        self.assertTrue(out["edited"])
        self.assertEqual(out["text"], "hello fixed")
        self.assertEqual(len(out["edit_history"]), 1)
        self.assertEqual(out["edit_history"][0],
                         {"text": "hello", "ts": 123})

    def test_edit_other_nicks_message_rejected(self):
        self.st.append("someone", "not yours")
        with self.assertRaises(store.EditError) as cm:
            self.st.edit("build-x-1", "tester", "hacked")
        self.assertIn("only the original nick may edit it", str(cm.exception))
        self.assertEqual(self.st.get("build-x-1")["text"], "not yours")

    def test_edit_unknown_id_rejected(self):
        self.st.append("tester", "hello")
        with self.assertRaises(store.EditError) as cm:
            self.st.edit("build-x-99", "tester", "boo")
        self.assertIn("no such message: build-x-99", str(cm.exception))

    def test_edit_empty_text_rejected(self):
        e = self.st.append("tester", "hello")
        with self.assertRaises(store.EditError):
            self.st.edit(e["id"], "tester", "")

    def test_restart_new_store_instance_preserves_everything(self):
        e = self.st.append("tester", "hello")
        self.st.edit(e["id"], "tester", "v2", ts=55)
        st2 = store.MessageStore(self.path, room=self.room)
        got = st2.get(e["id"])
        self.assertTrue(got["edited"])
        self.assertEqual(got["text"], "v2")
        self.assertEqual(got["edit_history"][0]["text"], "hello")
        # the counter was persisted too: no id reuse
        nxt = st2.append("tester", "after restart")
        self.assertEqual(nxt["id"], "build-x-2")

    def test_file_is_valid_jsonl(self):
        self.st.append("tester", "a")
        self.st.append("tester", "b")
        with open(self.path, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual([e["id"] for e in lines],
                         ["build-x-1", "build-x-2"])

    def test_bad_line_skipped_not_fatal(self):
        self.st.append("tester", "a")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        st2 = store.MessageStore(self.path, room=self.room)
        self.assertEqual([e["id"] for e in st2.entries()], ["build-x-1"])
        nxt = st2.append("tester", "b")
        self.assertEqual(nxt["id"], "build-x-2")


class EditProtocolTest(RelayTestCase):
    """edits.py protocol helpers: parse, length limit, render."""

    def test_parse_edit_text(self):
        self.assertEqual(edits.parse_edit_text("EDIT 7 new words"),
                         ("7", "new words"))
        self.assertEqual(edits.parse_edit_text("EDIT build-x-3 fixed"),
                         ("build-x-3", "fixed"))
        self.assertIsNone(edits.parse_edit_text("hello world"))
        self.assertIsNone(edits.parse_edit_text("EDITED 7 nope"))

    def test_apply_edit_enforces_max_text(self):
        st = store.MessageStore(store.store_path(""), room="")
        e = st.append("tester", "hi")
        with self.assertRaises(store.EditError) as cm:
            edits.apply_edit(st, e["id"], "tester",
                             "x" * (rc.MAX_TEXT + 1))
        self.assertIn("too long", str(cm.exception))
        self.assertFalse(st.get(e["id"])["edited"])

    def test_apply_edit_empty_rejected(self):
        st = store.MessageStore(store.store_path(""), room="")
        e = st.append("tester", "hi")
        with self.assertRaises(store.EditError):
            edits.apply_edit(st, e["id"], "tester", "   ")

    def test_render_applies_known_edit(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("tester", "hello")
        out = edits.render_incoming("tester: EDIT 1 hello fixed", "")
        self.assertEqual(out, "tester: hello fixed (edited)")
        self.assertTrue(st.get("1")["edited"])
        self.assertEqual(st.get("1")["text"], "hello fixed")

    def test_render_rejects_spoofed_edit(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("tester", "hello")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = edits.render_incoming("evil: EDIT 1 hacked", "")
        self.assertEqual(out, "evil: EDIT 1 hacked (edited)")
        self.assertFalse(st.get("1")["edited"])
        self.assertIn("only the original nick may edit it", err.getvalue())

    def test_render_unknown_id_keeps_marker(self):
        out = edits.render_incoming("someone: EDIT 99 mystery text", "")
        self.assertEqual(out, "someone: EDIT 99 mystery text (edited)")

    def test_render_plain_line_untouched(self):
        self.assertEqual(edits.render_incoming("milo: hello", ""),
                         "milo: hello")


class SendEditCliTest(RelayTestCase):
    """send.py records + prints ids; edits.py applies and announces."""

    def test_send_records_message_and_prints_id(self):
        rc_, out, err = self.run_cli(send.main, ["hello"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("MSG_ID 1", out)
        st = store.MessageStore(store.store_path(""), room="")
        e = st.get("1")
        self.assertEqual((e["nick"], e["text"], e["room"]),
                         ("tester", "hello", ""))

    def test_send_room_message_gets_room_prefixed_id(self):
        rc_, out, err = self.run_cli(send.main, ["--room", "build-x", "hi"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("MSG_ID build-x-1", out)

    def test_send_too_long_rejected(self):
        rc_, out, err = self.run_cli(send.main, ["x" * (rc.MAX_TEXT + 1)])
        self.assertEqual(rc_, 2)
        self.assertIn("too long", err)
        self.assertEqual(self.pushes_to("muse-bus"), [])

    def test_edit_own_message_end_to_end(self):
        self.run_cli(send.main, ["hello"])
        rc_, out, err = self.run_cli(edits.main, ["1", "hello fixed"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("EDITED 1", out)
        st = store.MessageStore(store.store_path(""), room="")
        e = st.get("1")
        self.assertTrue(e["edited"])
        self.assertEqual(e["text"], "hello fixed")
        self.assertEqual(len(e["edit_history"]), 1)
        self.assertEqual(e["edit_history"][0]["text"], "hello")
        # announced on the bus for other participants
        self.assertIn("tester: EDIT 1 hello fixed",
                      self.pushes_to("muse-bus"))

    def test_edit_nonexistent_id_rejected(self):
        self.run_cli(send.main, ["hello"])
        rc_, out, err = self.run_cli(edits.main, ["99", "boo"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such message: 99", err)

    def test_edit_other_nicks_message_rejected(self):
        st = store.MessageStore(store.store_path(""), room="")
        st.append("worker2", "theirs")
        rc_, out, err = self.run_cli(edits.main, ["1", "hacked"])
        self.assertEqual(rc_, 2)
        self.assertIn("only the original nick may edit it", err)
        self.assertEqual(st.get("1")["text"], "theirs")
        self.assertFalse(st.get("1")["edited"])
        self.assertEqual(self.pushes_to("muse-bus"), [])  # never announced

    def test_edit_too_long_rejected(self):
        self.run_cli(send.main, ["hello"])
        rc_, out, err = self.run_cli(edits.main, ["1", "x" * (rc.MAX_TEXT + 1)])
        self.assertEqual(rc_, 2)
        self.assertIn("too long", err)
        st = store.MessageStore(store.store_path(""), room="")
        self.assertEqual(st.get("1")["text"], "hello")

    def test_edit_room_scoped(self):
        self.run_cli(send.main, ["--room", "build-x", "roomy"])
        rc_, out, err = self.run_cli(
            edits.main, ["--room", "build-x", "build-x-1", "roomy v2"])
        self.assertEqual(rc_, 0, err)
        st = store.MessageStore(store.store_path("build-x"),
                                room="build-x")
        self.assertEqual(st.get("build-x-1")["text"], "roomy v2")
        self.assertIn("tester: EDIT build-x-1 roomy v2",
                      self.pushes_to("muse-bus:room:build-x"))


class DmIdTest(RelayTestCase):
    """DM derivation compatibility: ids deterministic, unique per room."""

    def test_dm_ids_deterministic_and_unique_across_rooms(self):
        expected = rc.dm_room("s3cr3t")
        rc_, out, err = self.run_cli(send.main, ["--dm", "s3cr3t", "hi"])
        self.assertEqual(rc_, 0, err)
        self.assertIn(f"MSG_ID {expected}-1", out)
        dm_st = store.MessageStore(store.store_path(expected),
                                   room=expected)
        self.assertEqual(dm_st.get(f"{expected}-1")["text"], "hi")
        # main-bus ids live in their own store: no collision with dm ids
        rc_, out, err = self.run_cli(send.main, ["main bus msg"])
        self.assertIn("MSG_ID 1", out)
        main_st = store.MessageStore(store.store_path(""), room="")
        self.assertEqual(main_st.get("1")["text"], "main bus msg")
        self.assertIsNone(dm_st.get("1"))
        self.assertIsNone(main_st.get(f"{expected}-1"))

    def test_edit_dm_message(self):
        expected = rc.dm_room("s3cr3t")
        self.run_cli(send.main, ["--dm", "s3cr3t", "secret-ish"])
        rc_, out, err = self.run_cli(
            edits.main, ["--dm", "s3cr3t", f"{expected}-1", "still secret"])
        self.assertEqual(rc_, 0, err)
        dm_st = store.MessageStore(store.store_path(expected),
                                   room=expected)
        self.assertEqual(dm_st.get(f"{expected}-1")["text"], "still secret")
        key = "muse-bus:room:" + expected
        self.assertIn(f"tester: EDIT {expected}-1 still secret",
                      self.pushes_to(key))


class PollEditedMarkerTest(RelayTestCase):
    """poll.py renders (edited) markers and applies known edits."""

    def test_poll_shows_edited_marker(self):
        self.run_cli(send.main, ["hello"])
        self.run_cli(edits.main, ["1", "hello fixed"])
        self.set_nick("other", "host-b")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("tester: hello", out)          # the original line
        self.assertIn("tester: hello fixed (edited)", out)
        # shared store dir: the edit was applied for real
        st = store.MessageStore(store.store_path(""), room="")
        self.assertTrue(st.get("1")["edited"])

    def test_poll_rejects_spoofed_edit(self):
        self.run_cli(send.main, ["hello"])
        self.fake.bus_push("evil: EDIT 1 hacked", key="muse-bus")
        self.set_nick("other", "host-b")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("evil: EDIT 1 hacked (edited)", out)
        self.assertIn("only the original nick may edit it", err)
        st = store.MessageStore(store.store_path(""), room="")
        self.assertEqual(st.get("1")["text"], "hello")
        self.assertFalse(st.get("1")["edited"])

    def test_poll_unknown_id_edit_marker(self):
        self.fake.bus_push("someone: EDIT 99 mystery text", key="muse-bus")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertIn("someone: EDIT 99 mystery text (edited)", out)

    def test_watch_renders_edited_marker(self):
        self.run_cli(send.main, ["hello"])
        self.run_cli(edits.main, ["1", "hello fixed"])
        self.set_nick("other", "host-b")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            shown = watch.watch_once(
                [["main bus", "muse-bus",
                  os.path.join(self.tmp, "wseen"), 0]])
        self.assertEqual(shown, 2)
        self.assertIn("tester: hello fixed (edited)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
