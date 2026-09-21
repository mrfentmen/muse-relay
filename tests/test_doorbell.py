#!/usr/bin/env python3
"""The doorbell: what a send costs, and what happens when the ring fails.

`listen.py` wakes on a message only if something rang the channel, so the
ring matters as much as the write. This covers the sending half — the
half `test_live.py` cannot see, since it fakes the wire itself:

  * one message = one round trip (RPUSH + INCR + PUBLISH in a pipeline)
  * the PUBLISH reaches the live channel, carrying the message
  * MUSE_RELAY_NO_PING=1 makes a silent bus — for imports, migrations,
    tests — without losing the message
  * a bus with no pipeline endpoint still sends and still rings, just in
    more requests
  * a failed ring is never fatal: the message is already stored
  * api_pipeline's own parsing (per-command error, malformed reply)
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import RelayTestCase  # noqa: E402
import relay_common as rc  # noqa: E402
# RelayTestCase patches rc.api_pipeline with the fake, so hold the real one
# from import time — the parse tests are about the real function.
from relay_common import api_pipeline as real_api_pipeline  # noqa: E402


class DoorbellTest(RelayTestCase):
    # -- the happy path ------------------------------------------------
    def test_one_message_is_one_round_trip(self):
        rc.bus_send("milo: hello")
        self.assertEqual(len(self.fake.pipeline_calls), 1)
        cmds = [c[0] for c in self.fake.pipeline_calls[0]]
        self.assertEqual(cmds, ["RPUSH", "INCR", "PUBLISH"])

    def test_the_ring_carries_the_message_on_the_live_channel(self):
        rc.bus_send("milo: hello")
        self.assertEqual(len(self.fake.published), 1)
        channel, envelope = self.fake.published[0]
        self.assertEqual(channel, rc.live_channel())
        payload = json.loads(envelope)
        self.assertEqual(payload["body"], "milo: hello")
        self.assertEqual(payload["room"], rc.BUS)
        self.assertEqual(payload["kind"], "msg")

    def test_the_message_is_stored_once_and_counted_once(self):
        _, notified = rc.bus_send("milo: one")
        self.assertEqual(self.fake.lists[rc.BUS], ["milo: one"])
        # exactly one counter bump: the pipeline's own INCR
        self.assertEqual(rc.bus_total(), 1)
        self.assertEqual(notified, self.fake.subscribers)

    def test_the_ring_reports_listener_count(self):
        self.fake.subscribers = 3
        _, notified = rc.bus_send("milo: hello")
        self.assertEqual(notified, 3)

    def test_a_custom_key_rings_with_that_room(self):
        rc.bus_send("milo: eng", key="muse-bus:eng")
        payload = json.loads(self.fake.published[0][1])
        self.assertEqual(payload["room"], "muse-bus:eng")
        self.assertEqual(self.fake.lists["muse-bus:eng"], ["milo: eng"])

    def test_the_channel_is_overridable(self):
        with mock.patch.dict(os.environ,
                             {"MUSE_RELAY_LIVE_CHANNEL": "elsewhere"}):
            rc.bus_send("milo: hello")
        self.assertEqual(self.fake.published[0][0], "elsewhere")

    def test_kind_is_carried_for_non_message_pushes(self):
        rc.bus_send("status: heads down", kind="status")
        payload = json.loads(self.fake.published[0][1])
        self.assertEqual(payload["kind"], "status")

    # -- a silent bus --------------------------------------------------
    def test_no_ping_silences_the_ring_but_keeps_the_message(self):
        with mock.patch.dict(os.environ, {"MUSE_RELAY_NO_PING": "1"}):
            _, notified = rc.bus_send("milo: quiet")
        self.assertEqual(self.fake.published, [])
        self.assertEqual(notified, 0)
        self.assertEqual(self.fake.lists[rc.BUS], ["milo: quiet"])
        self.assertEqual(rc.bus_total(), 1)

    def test_truthy_no_ping_spellings(self):
        for spelling in ("1", "true", "YES", "on", " On "):
            with self.subTest(spelling=spelling):
                self.fake.__init__()
                with mock.patch.dict(os.environ,
                                     {"MUSE_RELAY_NO_PING": spelling}):
                    rc.bus_send("milo: quiet")
                self.assertEqual(self.fake.published, [])

    def test_falsey_no_ping_still_rings(self):
        for spelling in ("0", "false", "no", "off", ""):
            with self.subTest(spelling=spelling):
                self.fake.__init__()
                with mock.patch.dict(os.environ,
                                     {"MUSE_RELAY_NO_PING": spelling}):
                    rc.bus_send("milo: loud")
                self.assertEqual(len(self.fake.published), 1)

    # -- degradation ---------------------------------------------------
    def test_no_pipeline_endpoint_still_sends_and_rings(self):
        """An older instance or a proxy: slower, never different."""
        self.fake.api_pipeline = mock.Mock(
            side_effect=RuntimeError("no /pipeline here"))
        _, notified = rc.bus_send("milo: fallback")
        self.assertEqual(self.fake.lists[rc.BUS], ["milo: fallback"])
        self.assertEqual(len(self.fake.published), 1)
        self.assertEqual(notified, self.fake.subscribers)
        self.assertEqual(rc.bus_total(), 1)   # bus_push bumped it once

    def test_a_failed_rpush_raises_rather_than_losing_the_message(self):
        boom = mock.Mock(return_value=[rc.PipelineError("WRONGTYPE"), 1, 0])
        with mock.patch.object(rc, "api_pipeline", boom):
            with self.assertRaises(RuntimeError) as cm:
                rc.bus_send("milo: doomed")
        self.assertIn("WRONGTYPE", str(cm.exception))

    def test_a_failed_ring_is_reported_as_zero_not_as_a_failure(self):
        self.fake.subscribers = 0
        resp, notified = rc.bus_send("milo: nobody listening")
        self.assertEqual(notified, 0)
        self.assertIn("result", resp)

    def test_publish_event_never_raises_when_the_bus_is_down(self):
        with mock.patch.object(rc, "api_get",
                               side_effect=RuntimeError("down")):
            self.assertEqual(rc.publish_event(rc.BUS, "milo: hi"), 0)

    def test_bus_send_returns_rpush_reply_text(self):
        resp, _ = rc.bus_send("milo: hello")
        self.assertEqual(json.loads(resp)["result"], 1)


class PipelineParseTest(RelayTestCase):
    """relay_common.api_pipeline's parsing, through the raw reply."""

    def _parse(self, commands):
        # Patch the transport, not the parser: real_api_pipeline is the
        # function under test, driving the fake's RESP-shaped reply text.
        with mock.patch.object(rc, "_post_raw", self.fake.pipeline_raw):
            return real_api_pipeline(commands)

    def test_results_come_back_in_order(self):
        out = self._parse([["RPUSH", "k", "a"], ["INCR", "k:total"],
                           ["PUBLISH", "ch", "msg"]])
        self.assertEqual(out[0], 1)
        self.assertEqual(out[1], 1)
        self.assertIsInstance(out[2], int)

    def test_one_command_failing_is_a_pipeline_error_not_a_batch_failure(self):
        out = self._parse([["RPUSH", "k", "a"], ["NOSUCHCMD", "x"],
                           ["INCR", "k:total"]])
        self.assertIsInstance(out[0], int)
        self.assertIsInstance(out[1], rc.PipelineError)
        self.assertEqual(out[2], 1)

    def test_a_pipeline_error_prints_as_the_server_complaint(self):
        out = self._parse([["LTRIM", "k", "nonsense"]])
        self.assertIsInstance(out[0], rc.PipelineError)
        self.assertIn("nonsense", str(out[0]))

    def test_an_empty_batch_makes_no_request(self):
        self.assertEqual(real_api_pipeline([]), [])
        self.assertEqual(self.fake.pipeline_calls, [])

    def test_a_non_json_reply_raises_so_callers_fall_back(self):
        with mock.patch.object(rc, "_post_raw", lambda *a, **k: "<html>oops"):
            with self.assertRaises(RuntimeError):
                real_api_pipeline([["PING"]])

    def test_a_short_reply_raises_so_callers_fall_back(self):
        with mock.patch.object(rc, "_post_raw",
                               lambda *a, **k: json.dumps([{"result": 1}])):
            with self.assertRaises(RuntimeError):
                real_api_pipeline([["RPUSH", "k", "a"], ["INCR", "k:total"]])

    def test_a_malformed_entry_becomes_a_pipeline_error(self):
        with mock.patch.object(rc, "_post_raw",
                               lambda *a, **k: json.dumps(["nonsense"])):
            out = real_api_pipeline([["PING"]])
        self.assertIsInstance(out[0], rc.PipelineError)


if __name__ == "__main__":
    unittest.main()
