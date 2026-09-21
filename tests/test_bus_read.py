#!/usr/bin/env python3
"""Bus read cursor: trimmed messages must never be lost in silence.

The bus list is capped with LTRIM (MUSE_RELAY_KEEP, default 500), which
shifts every index down. Reading with a raw list index therefore skips
everything trimmed out from under the reader, and any gap bigger than
the cap is total loss with no error. relay_common.bus_read replaces
that with an absolute cursor kept honest by a monotonic push counter.

Also the regression for watch.py --announce replaying the announcer's
lines every cycle (it advanced the cursor by the FILTERED count).
"""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
import relay_common as rc
import poll
import watch


class BusReadTest(RelayTestCase):
    def test_caught_up_reader_gets_nothing(self):
        for i in range(5):
            self.fake.bus_push(f"alice: m{i}")
        msgs, cursor, lost = rc.bus_read(0)
        self.assertEqual(len(msgs), 5)
        self.assertEqual(cursor, 5)
        self.assertEqual(lost, 0)
        msgs, cursor, lost = rc.bus_read(cursor)
        self.assertEqual(msgs, [])
        self.assertEqual(cursor, 5)
        self.assertEqual(lost, 0)

    def test_partial_read_advances_to_total(self):
        for i in range(3):
            self.fake.bus_push(f"alice: m{i}")
        _, cursor, _ = rc.bus_read(0)
        self.assertEqual(cursor, 3)
        self.fake.bus_push("bob: m3")
        msgs, cursor, lost = rc.bus_read(cursor)
        self.assertEqual(msgs, ["bob: m3"])
        self.assertEqual(cursor, 4)
        self.assertEqual(lost, 0)

    def test_trimmed_messages_are_counted_not_skipped(self):
        # A reader reads 10, then 600 arrive and the cap (500) trims the
        # oldest 110 messages. 100 of them were never read: report them,
        # and still deliver everything that survived.
        for i in range(10):
            self.fake.bus_push(f"alice: early{i}")
        _, cursor, _ = rc.bus_read(0)
        self.assertEqual(cursor, 10)
        for i in range(600):
            self.fake.bus_push(f"bob: late{i}")
        rc.bus_trim(key="muse-bus", keep=500)
        self.assertEqual(rc.bus_llen(), 500)
        self.assertEqual(rc.bus_total(), 610)
        msgs, cursor, lost = rc.bus_read(cursor)
        self.assertEqual(lost, 100)          # 110 dropped - 10 already read
        self.assertEqual(len(msgs), 500)     # the survivors still arrive
        self.assertEqual(cursor, 610)
        self.assertEqual(msgs[-1], "bob: late599")

    def test_reader_trimmed_out_entirely_reports_full_loss(self):
        for i in range(3):
            self.fake.bus_push(f"alice: m{i}")
        rc.bus_trim(key="muse-bus", keep=3)
        msgs, cursor, lost = rc.bus_read(0)
        self.assertEqual(msgs, ["alice: m0", "alice: m1", "alice: m2"])
        self.assertEqual(lost, 0)
        # A reader that stopped after m0: m1, m2 and n0 were trimmed
        # away, so it lost 3 — and none of them are silently skipped.
        for i in range(4):
            self.fake.bus_push(f"bob: n{i}")
        rc.bus_trim(key="muse-bus", keep=3)
        msgs, cursor, lost = rc.bus_read(1)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(lost, 3)
        self.assertEqual(cursor, 7)
        self.assertEqual(msgs, ["bob: n1", "bob: n2", "bob: n3"])

    def test_cursor_never_moves_backwards(self):
        # A reset/lost counter must not rewind a reader into replays.
        self.fake.bus_push("alice: m0")
        self.fake.strings["muse-bus:total"] = ("0", None)
        _, cursor, lost = rc.bus_read(9)
        self.assertEqual(cursor, 9)
        self.assertEqual(lost, 0)

    def test_falls_back_to_plain_index_without_a_counter(self):
        # Lists written before the counter existed: behave like before.
        self.fake.bus_push("alice: m0")
        del self.fake.strings["muse-bus:total"]
        msgs, cursor, lost = rc.bus_read(0)
        self.assertEqual(msgs, ["alice: m0"])
        self.assertEqual(cursor, 1)
        self.assertEqual(lost, 0)


class PollTrimWarningTest(RelayTestCase):
    def _catch_up_then_trim(self, room):
        """Read to the end, then push past the cap so a trim happens."""
        key = rc.bus_key(room)
        self.run_cli(poll.main, ["--room", room] if room else [])
        for i in range(600):
            self.fake.bus_push(f"bob: late{i}", key=key)
        rc.bus_trim(key=key, keep=500)

    def test_poll_warns_loudly_on_trim_loss(self):
        self.fake.bus_push("alice: first")
        self._catch_up_then_trim("")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0)
        self.assertIn("WARNING", err)
        self.assertIn("trimmed off the bus", err)
        # and the survivors are still delivered, not swallowed
        self.assertIn("NEW_MESSAGES:", out)
        self.assertIn("bob: late599", out)

    def test_poll_stays_quiet_when_nothing_was_trimmed(self):
        self.fake.bus_push("alice: hello")
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0)
        self.assertNotIn("trimmed off the bus", err)

    def test_poll_survives_the_old_total_loss_case(self):
        # The original bug: a caught-up reader + a full cap = 0 messages,
        # no warning, everything gone.
        self.fake.bus_push("alice: first")
        self._catch_up_then_trim("")
        _, out, _ = self.run_cli(poll.main, [])
        self.assertNotEqual(out.strip(), "NO_NEW_MESSAGES")


class WatchAnnounceReplayTest(RelayTestCase):
    """watch.py --announce must not reprint the announcer every cycle."""

    def _targets(self, key):
        return [["room 'news'", key, os.path.join(self.tmp, "wseen-news"), 0]]

    def _watch(self, targets):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            shown = watch.watch_once(targets, announce_only=True)
        return shown, out.getvalue(), err.getvalue()

    def test_announce_lines_print_once_not_every_cycle(self):
        key = rc.bus_key("news")
        rc.api_get(f"set/muse-bus:announce:{key}/alice")
        self.fake.bus_push("alice: release notes", key=key)
        self.fake.bus_push("bob: chatter", key=key)
        targets = self._targets(key)
        shown, out, err = self._watch(targets)
        self.assertEqual(shown, 1)
        self.assertIn("alice: release notes", out)
        self.assertNotIn("bob: chatter", out)
        self.assertEqual(targets[0][3], 2)  # real count, not the filtered 1
        # Cycle 2: nothing new arrived, so nothing is reprinted.
        shown2, out2, _ = self._watch(targets)
        self.assertEqual(shown2, 0)
        self.assertEqual(out2, "")
        # Cycle 3: a genuinely new announcer line prints exactly once.
        self.fake.bus_push("alice: second note", key=key)
        shown3, out3, _ = self._watch(targets)
        self.assertEqual(shown3, 1)
        self.assertIn("alice: second note", out3)
        self.assertNotIn("release notes", out3)

    def test_watch_warns_on_trim_loss(self):
        key = rc.bus_key("trimroom")
        self.fake.bus_push("alice: first", key=key)
        targets = self._targets(key)
        self._watch(targets)
        for i in range(600):
            self.fake.bus_push(f"bob: late{i}", key=key)
        rc.bus_trim(key=key, keep=500)
        _, out, err = self._watch(targets)
        self.assertIn("trimmed off the bus", err)
        self.assertIn("bob: late599", out)


if __name__ == "__main__":
    unittest.main()
