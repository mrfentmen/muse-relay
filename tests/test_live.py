#!/usr/bin/env python3
"""Tests for listen.py — the live doorbell and the wake-on-message loop.

Two halves. The poll-fallback and filter tests drive `listen.main` in the
test process over the fake REST store. The push tests stand up a real
RESP server (fake_resp) and a real socket, and run the listener in a
thread, because "a message posted now wakes a sleeping listener" is the
entire feature and a mocked socket would prove nothing about it.
"""
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase  # noqa: E402
from fake_resp import FakeRespServer  # noqa: E402
import listen  # noqa: E402
import relay_common as rc  # noqa: E402

CHANNEL = "muse-bus:live"


def envelope(body, room="muse-bus", kind="msg"):
    return json.dumps({"room": room, "kind": kind, "body": body, "ts": 1})


class HelpersTest(RelayTestCase):
    def test_mention_nicks(self):
        self.assertEqual(listen.mention_nicks("hey @milo and @Pax look"),
                         ["milo", "pax"])
        self.assertEqual(listen.mention_nicks("no mentions here"), [])

    def test_an_email_is_not_a_mention(self):
        # Same rule as bus.html: a mention needs a word boundary before
        # the @, so a mail address never wakes anybody.
        self.assertEqual(listen.mention_nicks("mail me at a@b.com"), [])
        self.assertEqual(listen.mention_nicks("mail @bob instead"), ["bob"])

    def test_mentions_me(self):
        self.assertTrue(listen.mentions_me("hello @milo", {"milo"}))
        self.assertTrue(listen.mentions_me("hello @all", {"milo"}))
        self.assertFalse(listen.mentions_me("hello @pax", {"milo"}))
        self.assertFalse(listen.mentions_me("milestone reached", {"milo"}))

    def test_sender_of(self):
        self.assertEqual(listen.sender_of("alice: hi"), "alice")
        self.assertEqual(listen.sender_of("no prefix"), "")
        self.assertEqual(listen.sender_of(""), "")

    def test_room_label(self):
        self.assertEqual(listen.room_label("muse-bus"), "main")
        self.assertEqual(listen.room_label("muse-bus:room:eng"), "eng")
        self.assertEqual(listen.room_label("other:thing"), "other:thing")

    def test_parse_envelope_accepts_the_real_shape(self):
        ev = listen.parse_envelope(envelope("alice: hi", "muse-bus:room:eng"),
                                   CHANNEL)
        self.assertEqual(ev["room"], "eng")
        self.assertEqual(ev["from"], "alice")
        self.assertEqual(ev["key"], "muse-bus:room:eng")

    def test_parse_envelope_rejects_junk(self):
        for raw in ("not json", "[]", "{}", '{"room": 1, "body": "x"}',
                    '{"body": "x"}'):
            self.assertIsNone(listen.parse_envelope(raw, CHANNEL))

    def test_format_line_marks_replays(self):
        ev = listen.make_event("muse-bus", "alice: hi", replay=True)
        self.assertIn("REPLAY [main] alice: hi", listen.format_line(ev))
        ev = listen.make_event("muse-bus", "alice: hi")
        self.assertIn("LIVE [main] alice: hi", listen.format_line(ev))


class PollFallbackTest(RelayTestCase):
    """listen.py without a TCP URL: same deliveries, list on a timer."""

    def run_listen(self, *args, **env):
        environ = {"MUSE_RELAY_TCP_URL": ""}
        environ.update(env)
        with mock.patch.dict(os.environ, environ):
            return self.run_cli(listen.main, list(args))

    def test_delivers_what_is_on_the_bus_and_advances_the_cursor(self):
        self.fake.bus_push("alice: hello")
        code, out, _ = self.run_listen("--once")
        self.assertEqual(code, 0)
        self.assertIn("LIVE [main] alice: hello", out)
        # second pass: nothing new, so nothing delivered
        code, out2, _ = self.run_listen("--once")
        self.assertEqual(code, 0)
        self.assertNotIn("alice: hello", out2)

    def test_own_messages_are_still_delivered(self):
        # listen.py is a feed, not poll.py: it hands the hook everything,
        # including lines from this nick (an agent may post as itself and
        # still need to see the reply arrive).
        self.fake.bus_push("tester: mine")
        _, out, _ = self.run_listen("--once")
        self.assertIn("tester: mine", out)

    def test_mentions_filter(self):
        self.fake.bus_push("alice: general chatter")
        self.fake.bus_push("bob: @tester do the thing")
        _, out, _ = self.run_listen("--once", "--mentions")
        self.assertIn("@tester do the thing", out)
        self.assertNotIn("general chatter", out)

    def test_mentions_filter_honours_broadcast(self):
        self.fake.bus_push("bob: @all standup in 5")
        _, out, _ = self.run_listen("--once", "--mentions")
        self.assertIn("standup in 5", out)

    def test_watch_adds_another_name_for_mentions(self):
        self.fake.bus_push("bob: @milo ping")
        _, out, _ = self.run_listen("--once", "--mentions", "--watch",
                                    "milo")
        self.assertIn("@milo ping", out)

    def test_room_selection(self):
        self.fake.bus_push("alice: main room", key="muse-bus")
        self.fake.bus_push("bob: eng room", key="muse-bus:room:eng")
        _, out, _ = self.run_listen("--once", "--room", "eng")
        self.assertIn("eng room", out)
        self.assertNotIn("main room", out)

    def test_json_output_is_one_object_per_message(self):
        self.fake.bus_push("alice: hello")
        _, out, _ = self.run_listen("--once", "--json")
        rows = [json.loads(line) for line in out.strip().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["from"], "alice")
        self.assertEqual(rows[0]["room"], "main")
        self.assertEqual(rows[0]["event"], "live")
        self.assertFalse(rows[0]["replay"])

    def test_count_stops_early(self):
        for i in range(5):
            self.fake.bus_push(f"alice: m{i}")
        _, out, _ = self.run_listen("--once", "--count", "2")
        self.assertEqual(len([ln for ln in out.splitlines()
                              if ln.startswith("LIVE")]), 2)

    def test_trim_loss_is_reported_not_hidden(self):
        key = "muse-bus"
        for i in range(10):
            self.fake.bus_push(f"alice: old{i}", key=key)
        rc.bus_trim(key=key, keep=3)
        # a reader that never read anything is now 7 messages behind
        _, _, err = self.run_listen("--once")
        self.assertIn("7 message(s) in main were trimmed", err)

    def test_bad_interval_is_rejected(self):
        code, _, err = self.run_listen("--once", "--interval", "0")
        self.assertEqual(code, 2)
        self.assertIn("--interval must be positive", err)

    def test_bad_room_is_rejected(self):
        code, _, err = self.run_listen("--once", "--room", "!!!")
        self.assertEqual(code, 2)
        self.assertIn("invalid room", err)


class LivePushTest(RelayTestCase):
    """The real thing: a socket, a published message, a woken listener."""

    def setUp(self):
        super().setUp()
        self.server = FakeRespServer()
        self.addCleanup(self.server.close)

    def run_listen(self, *args, wait_for=None, **env):
        """Run listen.main in a thread; returns (thread, result, out, err).

        `wait_for` is a callable the main thread blocks on before driving
        the subscriber, so the test never races the handshake.
        """
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(list(args))

        environ = {"MUSE_RELAY_TCP_URL": ""}
        environ.update(env)
        patches = mock.patch.dict(os.environ, environ)
        patches.start()
        self.addCleanup(patches.stop)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, result, out, err

    def finish(self, t, timeout=15):
        t.join(timeout)
        self.assertFalse(t.is_alive(), "listener did not exit")

    def test_a_published_message_wakes_the_listener(self):
        t, result, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: hello there"))
        self.finish(t)
        self.assertEqual(result.get("rc"), 0)
        self.assertIn("LIVE [main] alice: hello there", out.getvalue())

    def test_room_is_parsed_from_the_envelope(self):
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL,
                            envelope("bob: eng stuff", "muse-bus:room:eng"))
        self.finish(t)
        self.assertIn("LIVE [eng] bob: eng stuff", out.getvalue())

    def test_room_filter_drops_other_rooms(self):
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1",
            "--room", "eng")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("a: main only"))
        self.server.publish(CHANNEL, envelope("b: eng only",
                                              "muse-bus:room:eng"))
        self.finish(t)
        text = out.getvalue()
        self.assertIn("eng only", text)
        self.assertNotIn("main only", text)

    def test_mentions_filter_on_the_live_path(self):
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1",
            "--mentions")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("a: nothing for you"))
        self.server.publish(CHANNEL, envelope("b: @tester ping"))
        self.finish(t)
        text = out.getvalue()
        self.assertIn("@tester ping", text)
        self.assertNotIn("nothing for you", text)

    def test_junk_on_the_channel_is_skipped(self):
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, "not an envelope")
        self.server.publish(CHANNEL, envelope("alice: real one"))
        self.finish(t)
        text = out.getvalue()
        self.assertIn("real one", text)
        self.assertNotIn("not an envelope", text)

    def test_count_stops_a_running_listener(self):
        t, result, out, _ = self.run_listen(
            "--url", self.server.url, "--count", "1")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: one"))
        self.server.publish(CHANNEL, envelope("alice: two"))
        self.finish(t)
        self.assertEqual(result.get("rc"), 0)
        self.assertIn("alice: one", out.getvalue())
        self.assertNotIn("alice: two", out.getvalue())

    def test_catch_up_replays_what_the_subscription_missed(self):
        # posted while the listener was down: only the list can supply it
        self.fake.bus_push("alice: while you were out")
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.finish(t)
        self.assertIn("REPLAY [main] alice: while you were out",
                      out.getvalue())

    def test_a_push_duplicating_a_replay_is_delivered_once(self):
        """The same message can arrive twice — once from the catch-up read
        of the list, once through the doorbell. It must reach the hook
        once, or every agent wakes up twice for every message."""
        body = "alice: only once please"
        self.fake.bus_push(body)
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--timeout", "1.5")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope(body))
        self.finish(t)
        delivered = [ln for ln in out.getvalue().splitlines()
                     if body in ln]
        self.assertEqual(len(delivered), 1, out.getvalue())

    def test_no_catch_up_skips_the_replay(self):
        self.fake.bus_push("alice: while you were out")
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--timeout", "2",
            "--no-catch-up")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.finish(t)
        self.assertNotIn("while you were out", out.getvalue())

    def test_dead_url_fails_once_instead_of_spinning(self):
        t, result, _, err = self.run_listen("--url", "redis://127.0.0.1:1",
                                            "--once", "--timeout", "1")
        self.finish(t, timeout=20)
        self.assertEqual(result.get("rc"), 2)
        self.assertIn("LISTEN_FAILED", err.getvalue())

    def test_non_redis_url_is_rejected(self):
        t, result, _, err = self.run_listen("--url", "https://example.com",
                                            "--once", "--timeout", "1")
        self.finish(t, timeout=20)
        self.assertEqual(result.get("rc"), 2)
        self.assertIn("not a redis TCP URL", err.getvalue())

    def test_tcp_url_env_var_is_used(self):
        t, _, out, _ = self.run_listen("--once", "--count", "1",
                                       MUSE_RELAY_TCP_URL=self.server.url)
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: via env"))
        self.finish(t)
        self.assertIn("via env", out.getvalue())


class HookTest(LivePushTest):
    """--hook is how a ping becomes an action."""

    def test_hook_receives_the_event_on_stdin_and_in_the_env(self):
        seen = os.path.join(self.tmp, "seen.json")
        envfile = os.path.join(self.tmp, "from.txt")
        script = self.hook_script(
            f'cat > {seen}; echo "$MUSE_RELAY_FROM" > {envfile}; '
            f'echo "$MUSE_RELAY_ROOM" >> {envfile}')
        t, _, _, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1",
            "--hook", script)
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL,
                            envelope("alice: do the thing",
                                     "muse-bus:room:eng"))
        self.finish(t)
        with open(seen) as f:
            ev = json.loads(f.read())
        self.assertEqual(ev["text"], "alice: do the thing")
        self.assertEqual(ev["from"], "alice")
        self.assertEqual(ev["room"], "eng")
        with open(envfile) as f:
            self.assertEqual(f.read().strip().splitlines(),
                             ["alice", "eng"])

    def test_hook_output_is_forwarded(self):
        script = self.hook_script("echo HOOK_RAN")
        t, _, out, _ = self.run_listen(
            "--url", self.server.url, "--once", "--count", "1",
            "--hook", script)
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: hi"))
        self.finish(t)
        self.assertIn("HOOK_RAN", out.getvalue())

    def test_a_failing_hook_does_not_stop_the_listener(self):
        script = self.hook_script("exit 3")
        t, result, _, err = self.run_listen(
            "--url", self.server.url, "--count", "2", "--hook", script)
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: one"))
        self.server.publish(CHANNEL, envelope("alice: two"))
        self.finish(t)
        self.assertEqual(result.get("rc"), 0)
        self.assertIn("HOOK_EXIT 3", err.getvalue())

    def test_a_timing_out_hook_is_disabled_but_listening_continues(self):
        script = self.hook_script("sleep 5")
        t, result, out, err = self.run_listen(
            "--url", self.server.url, "--count", "2", "--hook", script,
            "--hook-timeout", "0.2")
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: one"))
        self.server.publish(CHANNEL, envelope("alice: two"))
        self.finish(t)
        self.assertEqual(result.get("rc"), 0)
        self.assertIn("HOOK_TIMEOUT", err.getvalue())
        self.assertIn("--hook disabled after a failure", err.getvalue())
        # both messages still came through the feed
        self.assertIn("alice: one", out.getvalue())
        self.assertIn("alice: two", out.getvalue())


class IdleCostTest(RelayTestCase):
    """What a connected listener costs while the bus is quiet.

    This is the claim that makes push worth a second credential: a
    subscriber is a held-open socket, not a timer, so sitting there
    costs no commands at all. A poll loop costs one per tick, forever.
    """

    def setUp(self):
        super().setUp()
        self.server = FakeRespServer()
        self.addCleanup(self.server.close)

    def count_rest_calls(self):
        """Record every REST call made through relay_common.api_get.

        relay_common.bus_read resolves api_get at call time, so every
        read path a listener uses — the catch-up replay and the poll
        fallback — lands in here.
        """
        calls = []
        real = self.fake.api_get

        def counting(path, *a, **k):
            calls.append(path)
            return real(path, *a, **k)

        p = mock.patch.object(rc, "api_get", counting)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_a_connected_listener_makes_no_rest_calls_while_idle(self):
        calls = self.count_rest_calls()
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                # --interval 0.1 is unused on the subscription path; it is
                # here so a regression back to polling shows up as calls
                # inside the measurement window instead of hiding outside
                # it.
                result["rc"] = listen.main(
                    ["--url", self.server.url, "--count", "1",
                     "--interval", "0.1"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        # the connect-time catch-up is allowed to read; everything after
        # it is what a quiet bus costs, and that is what we measure.
        # Seeing those calls first proves the counter is wired up, so a
        # zero afterwards means "nothing was spent" and not "the patch
        # never matched anything".
        self.assertTrue(calls, "counter never saw the catch-up read")
        calls.clear()
        time.sleep(1.0)
        self.assertEqual(calls, [], "an idle subscriber made REST calls")
        self.server.publish(CHANNEL, envelope("alice: hello"))
        t.join(15)
        self.assertFalse(t.is_alive(), "listener did not exit")
        self.assertEqual(result.get("rc"), 0)
        self.assertIn("alice: hello", out.getvalue())
        # a pushed message is delivered from the socket, not re-read
        self.assertEqual(calls, [], "the push path re-read the list")

    def test_a_push_reaches_the_hook_without_waiting_for_a_tick(self):
        """Delivery latency, measured on the delivery path only.

        `run_hook` is stubbed here on purpose: spawning a shell is the
        test machine's business, not the listener's, and timing that
        measures fork/exec under whatever load the suite happens to be
        carrying (it was 1-3s on a loaded machine, against 1-10ms on the
        actual path). What is asserted is that a published message
        reaches the hook in milliseconds while `--interval` is 30 — so
        nothing in the path waits for a tick. A timer-bound
        implementation cannot pass this test without taking 30s.
        """
        called = []

        def recorder(cmd, event, timeout):
            called.append((time.monotonic(), event["text"]))
            return 0

        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(
                    ["--url", self.server.url, "--count", "1",
                     "--hook", "/bin/true", "--interval", "30"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(listen, "run_hook", recorder):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
            start = time.monotonic()
            self.server.publish(CHANNEL, envelope("alice: wake up"))
            deadline = start + 25
            while not called and time.monotonic() < deadline:
                time.sleep(0.002)
            elapsed = time.monotonic() - start
            t.join(15)
        self.assertFalse(t.is_alive(), "listener did not exit")
        self.assertTrue(called, "the hook was never called")
        self.assertEqual(called[0][1], "alice: wake up")
        self.assertLess(elapsed, 5.0,
                        f"push took {elapsed:.3f}s with --interval 30 — "
                        f"that looks timer-bound")

    def test_a_pushed_message_reaches_a_real_hook(self):
        """No timing: just that the socket path feeds a real process."""
        fired = os.path.join(self.tmp, "fired.txt")
        script = self.hook_script(f'echo "$MUSE_RELAY_TEXT" > {fired}')
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(
                    ["--url", self.server.url, "--count", "1",
                     "--hook", script, "--interval", "30"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(self.server.wait_for_subscriber(CHANNEL))
        self.server.publish(CHANNEL, envelope("alice: wake up"))
        deadline = time.monotonic() + 20
        while not os.path.exists(fired) and time.monotonic() < deadline:
            time.sleep(0.005)
        t.join(15)
        self.assertTrue(os.path.exists(fired), "the hook never ran")
        with open(fired) as f:
            self.assertEqual(f.read().strip(), "alice: wake up")

    def test_the_poll_fallback_does_spend_commands(self):
        """The contrast that makes the number above mean something."""
        calls = self.count_rest_calls()
        with mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""}):
            self.run_cli(listen.main, ["--once"])
        self.assertTrue(calls, "a poll pass made no REST calls")
        self.assertTrue(any("llen" in c or "get" in c or "lrange" in c
                            or "total" in c for c in calls),
                        f"unexpected call shape: {calls}")


class OnceExitTest(RelayTestCase):
    """What `--once` reports when it could not look at the bus.

    A quiet bus and an unreadable bus must not exit the same way. The
    first is success; the second is a misconfiguration that would
    otherwise read as "nothing new" to whatever runs this on a timer.
    """

    def test_a_quiet_bus_exits_zero(self):
        with mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""}):
            code, _, _ = self.run_cli(listen.main, ["--once"])
        self.assertEqual(code, 0)

    def test_an_unreadable_list_exits_non_zero(self):
        with mock.patch.object(listen, "bus_read",
                               side_effect=RuntimeError("bus is down")):
            with mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""}):
                code, _, err = self.run_cli(listen.main, ["--once"])
        self.assertEqual(code, 1)
        self.assertIn("LISTEN_FAILED", err)

    def test_a_failed_catch_up_makes_the_one_shot_pass_incomplete(self):
        """TCP up, REST broken: two credentials, so a real case."""
        server = FakeRespServer()
        self.addCleanup(server.close)
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(
                    ["--url", server.url, "--once", "--timeout", "0.5"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(listen, "bus_read",
                               side_effect=RuntimeError("no REST token")):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            self.assertTrue(server.wait_for_subscriber(CHANNEL))
            t.join(15)
        self.assertFalse(t.is_alive(), "listener did not exit")
        self.assertIn("CATCHUP_FAILED", err.getvalue())
        self.assertEqual(result.get("rc"), 1)

    def test_a_delivered_message_still_exits_zero_with_a_broken_catch_up(self):
        """The pass did what it promised, even if the list was closed."""
        server = FakeRespServer()
        self.addCleanup(server.close)
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(
                    ["--url", server.url, "--count", "1"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(listen, "bus_read",
                               side_effect=RuntimeError("no REST token")):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            self.assertTrue(server.wait_for_subscriber(CHANNEL))
            server.publish(CHANNEL, envelope("alice: hello"))
            t.join(15)
        self.assertFalse(t.is_alive(), "listener did not exit")
        self.assertIn("alice: hello", out.getvalue())
        self.assertEqual(result.get("rc"), 0)

    def test_a_broken_link_mid_pass_exits_non_zero(self):
        server = FakeRespServer()
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def run():
            with mock.patch("sys.stdout", out), \
                    mock.patch("sys.stderr", err):
                result["rc"] = listen.main(
                    ["--url", server.url, "--once", "--timeout", "10"])

        env = mock.patch.dict(os.environ, {"MUSE_RELAY_TCP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(server.wait_for_subscriber(CHANNEL))
        server.close()
        t.join(15)
        self.assertFalse(t.is_alive(), "listener did not exit")
        self.assertIn("LISTEN_FAILED", err.getvalue())
        self.assertEqual(result.get("rc"), 1)


if __name__ == "__main__":
    unittest.main()
