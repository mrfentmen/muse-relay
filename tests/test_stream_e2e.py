#!/usr/bin/env python3
"""Real end-to-end tests for the stream-based muse-bus transport.

NO MOCKS: every test hits the live Upstash database, using an isolated
stream key (muse-bus-stream-e2e) that is deleted before and after each test.
Requires the custom.upstash credential (surrogate) like the runtime scripts.

Checks:
  1. send returns a stream entry id and the envelope round-trips
  2. idempotent resend of the same msg_id is a no-op (no duplicate entry)
  3. poll ordering is preserved and each consumer sees each message once
  4. crash recovery: an un-ACKed entry is reclaimed by XAUTOCLAIM
  5. presence: ping shows up in who(); an expired key does not

Run: python3 -m unittest discover -s tests -q   (pytest is not installed)
"""
import os
import sys
import time
import unittest
import uuid

# Isolated keys BEFORE importing bus_stream (it reads env at import time).
os.environ["MUSE_BUS_STREAM"] = "muse-bus-stream-e2e"
os.environ["MUSE_BUS_MSGIDS"] = "muse-bus-stream-e2e:msgids"
os.environ["MUSE_BUS_PRESENCE"] = "muse-bus-stream-e2e:presence"

# Repo root on sys.path so the test exercises the vendored stream modules.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import stream_common as bs


def xlen(key):
    return bs.api(f"/xlen/{key}").get("result")


def xgroup_destroy(key, group):
    try:
        bs.api(f"/xgroup/DESTROY/{key}/{group}")
    except Exception:
        pass


class StreamE2E(unittest.TestCase):
    def setUp(self):
        bs.delete(bs.STREAM, bs.MSGIDS)
        for k in bs.keys(f"{bs.PRESENCE_PREFIX}:*"):
            bs.delete(k)
        xgroup_destroy(bs.STREAM, bs.GROUP)
        bs.xgroup_create_mkstream(bs.STREAM, bs.GROUP)

    def tearDown(self):
        xgroup_destroy(bs.STREAM, bs.GROUP)
        bs.delete(bs.STREAM, bs.MSGIDS)
        for k in bs.keys(f"{bs.PRESENCE_PREFIX}:*"):
            bs.delete(k)

    def _send_as(self, nick, text, msg_id=None):
        bs.write_file("nick", nick)
        pass  # repo root already on path
        import stream_send as sender
        return sender.send(text, msg_id)

    def test_01_send_and_envelope(self):
        entry_id = self._send_as("alice", "hello stream")
        self.assertRegex(entry_id, r"^\d+-\d+$")
        entries = bs.xreadgroup(bs.STREAM, bs.GROUP, "probe", count=10)
        self.assertEqual(len(entries), 1)
        _eid, fields = entries[0]
        self.assertEqual(fields["nick"], "alice")
        self.assertEqual(fields["text"], "hello stream")
        self.assertIn("msg_id", fields)
        self.assertIn("ts", fields)
        bs.xack(bs.STREAM, bs.GROUP, _eid)

    def test_02_idempotent_resend(self):
        mid = uuid.uuid4().hex
        first = self._send_as("alice", "once only", msg_id=mid)
        self.assertNotEqual(first, "duplicate")
        self.assertEqual(xlen(bs.STREAM), 1)
        second = self._send_as("alice", "once only", msg_id=mid)
        self.assertEqual(second, "duplicate")
        self.assertEqual(xlen(bs.STREAM), 1)

    def test_03_poll_ordering_and_exactly_once(self):
        self._send_as("alice", "msg one")
        self._send_as("bob", "msg two")
        self._send_as("alice", "msg three")
        bs.write_file("nick", "carol")
        import stream_poll as poller
        lines = poller.poll("carol-test", count=10)
        self.assertEqual(lines, ["alice: msg one", "bob: msg two",
                                 "alice: msg three"])
        # second poll: nothing new (all ACKed)
        self.assertEqual(poller.poll("carol-test", count=10), [])
        bs.write_file("nick", "del")

    def test_04_crash_recovery_reclaims_pending(self):
        self._send_as("alice", "do not lose me")
        # a consumer reads but "crashes" before ACK
        got = bs.xreadgroup(bs.STREAM, bs.GROUP, "crasher", count=10)
        self.assertEqual(len(got), 1)
        entry_id, _fields = got[0]
        # fresh consumer sees nothing new...
        fresh = bs.xreadgroup(bs.STREAM, bs.GROUP, "rescuer", count=10)
        self.assertEqual(fresh, [])
        # ...but XAUTOCLAIM reclaims the idle pending entry
        time.sleep(0.05)
        reclaimed, _cursor = bs.xautoclaim(bs.STREAM, bs.GROUP, "rescuer",
                                           min_idle_ms=1, count=10)
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0][0], entry_id)
        bs.xack(bs.STREAM, bs.GROUP, entry_id)

    def test_05_presence_ping_and_expiry(self):
        self.assertTrue(bs.heartbeat("zoe"))
        live = bs.who()
        self.assertIn("zoe", live)
        # a short-lived ghost key must not appear after expiry
        bs.setex(bs.presence_key("ghost"), 1, f"{time.time():.3f}")
        time.sleep(2.2)
        live = bs.who()
        self.assertIn("zoe", live)
        self.assertNotIn("ghost", live)


if __name__ == "__main__":
    unittest.main()
