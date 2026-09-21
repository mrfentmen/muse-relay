#!/usr/bin/env python3
"""Tests for resp.py — the stdlib RESP2 client behind listen.py.

Runs against a real socket (tests/fake_resp.py) rather than a mock, so the
wire format, the framing and the reconnect path are all exercised. The
streaming parser is also driven directly with deliberately split buffers,
because a network test cannot reliably split a TCP segment on demand.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resp  # noqa: E402
from fake_resp import FakeRespServer  # noqa: E402


class ParseUrlTest(unittest.TestCase):
    def test_plain_and_tls_urls(self):
        self.assertEqual(resp.parse_tcp_url("redis://h:6380"),
                         ("h", 6380, "", "", False))
        self.assertEqual(resp.parse_tcp_url("rediss://h:6380"),
                         ("h", 6380, "", "", True))

    def test_default_port(self):
        self.assertEqual(resp.parse_tcp_url("rediss://h")[1], 6379)

    def test_credentials_are_unescaped(self):
        host, port, user, pw, tls = resp.parse_tcp_url(
            "rediss://me:p%40ss@h:1234")
        self.assertEqual((host, port, user, pw, tls), ("h", 1234, "me",
                                                      "p@ss", True))

    def test_default_username_means_one_arg_auth(self):
        # Upstash prints 'default' in REDIS_URL; a one-argument AUTH means
        # the same thing, so it must not be sent as a username.
        self.assertEqual(resp.parse_tcp_url("rediss://default:pw@h:1")[2], "")

    def test_rejects_non_redis_urls(self):
        for bad in ("https://h", "h:6379", "", "redis://"):
            with self.assertRaises(resp.RespError):
                resp.parse_tcp_url(bad)


class EncodeTest(unittest.TestCase):
    def test_array_of_bulk_strings(self):
        self.assertEqual(resp.encode_command("PING"), b"*1\r\n$4\r\nPING\r\n")
        self.assertEqual(resp.encode_command("GET", "a"),
                         b"*2\r\n$3\r\nGET\r\n$1\r\na\r\n")

    def test_accepts_ints(self):
        self.assertEqual(resp.encode_command("EXPIRE", "k", 60),
                         b"*3\r\n$6\r\nEXPIRE\r\n$1\r\nk\r\n$2\r\n60\r\n")


class _BufSub(resp.Subscriber):
    """A Subscriber with no socket, for driving the parser directly."""

    def __init__(self, data):
        super().__init__("redis://127.0.0.1:1", ["c"])
        self._buf = bytearray(data)


class StreamParseTest(unittest.TestCase):
    def test_complete_frames_parse(self):
        sub = _BufSub(b"+OK\r\n:42\r\n$3\r\nfoo\r\n$-1\r\n")
        self.assertEqual(sub._parse(0), (5, "OK"))
        self.assertEqual(sub._parse(5), (10, 42))
        self.assertEqual(sub._parse(10), (19, "foo"))
        self.assertEqual(sub._parse(19), (24, None))

    def test_every_prefix_is_incomplete(self):
        # A frame split anywhere must read as "need more bytes", never as
        # a wrong value: this is the whole reason the parser reports a
        # consumed count instead of assuming one recv holds a frame.
        whole = b"*3\r\n$7\r\nmessage\r\n$1\r\nc\r\n$5\r\nhello\r\n"
        for cut in range(1, len(whole)):
            sub = _BufSub(whole[:cut])
            self.assertIs(sub._parse(0), resp._INCOMPLETE,
                          f"prefix of {cut} bytes should be incomplete")

    def test_nested_array_bulk_data_with_crlf_inside(self):
        payload = "line1\r\nline2"
        frame = (b"*3\r\n" + b"$7\r\nmessage\r\n" + b"$1\r\nc\r\n"
                 + b"$%d\r\n" % len(payload) + payload.encode() + b"\r\n")
        sub = _BufSub(frame)
        consumed, value = sub._parse(0)
        self.assertEqual(consumed, len(frame))
        self.assertEqual(value, ["message", "c", payload])

    def test_error_frame_raises(self):
        sub = _BufSub(b"-ERR nope\r\n")
        with self.assertRaises(resp.RespError):
            sub._parse(0)

    def test_unknown_frame_type_raises(self):
        sub = _BufSub(b"%weird\r\n")
        with self.assertRaises(resp.RespError):
            sub._parse(0)


class ClassifyTest(unittest.TestCase):
    def test_message_frame(self):
        self.assertEqual(resp.Subscriber._classify(["message", "c", "hi"]),
                         ("message", "c", "hi"))

    def test_subscribe_confirmation_carries_the_count(self):
        self.assertEqual(resp.Subscriber._classify(["subscribe", "c", 1]),
                         ("subscribe", "c", 1))

    def test_pong_is_bookkeeping(self):
        self.assertEqual(resp.Subscriber._classify(["pong", ""]),
                         ("pong", "", ""))
        self.assertEqual(resp.Subscriber._classify("PONG"), ("pong", "", ""))

    def test_junk_is_other(self):
        self.assertEqual(resp.Subscriber._classify({"a": 1}),
                         ("other", "", ""))


class SubscriberWireTest(unittest.TestCase):
    """The client against a real server, over a real socket."""

    def setUp(self):
        self.server = FakeRespServer()
        self.addCleanup(self.server.close)

    def test_connect_subscribes_and_confirms(self):
        sub = resp.Subscriber(self.server.url, ["chan-a"])
        self.addCleanup(sub.close)
        self.assertEqual(sub.connect(), 1)
        self.assertTrue(self.server.wait_for_subscriber("chan-a"))

    def test_multiple_channels_confirm_one_by_one(self):
        sub = resp.Subscriber(self.server.url, ["a", "b", "c"])
        self.addCleanup(sub.close)
        self.assertEqual(sub.connect(), 3)
        self.assertTrue(self.server.wait_for_subscriber("c"))

    def test_delivers_published_messages(self):
        sub = resp.Subscriber(self.server.url, ["chan"])
        self.addCleanup(sub.close)
        sub.connect()
        self.assertTrue(self.server.wait_for_subscriber("chan"))
        self.assertEqual(self.server.publish("chan", "hello world"), 1)
        kind, channel, payload = sub.next_event(5)
        self.assertEqual((kind, channel, payload),
                         ("message", "chan", "hello world"))

    def test_ignores_other_channels(self):
        sub = resp.Subscriber(self.server.url, ["mine"])
        self.addCleanup(sub.close)
        sub.connect()
        self.server.publish("theirs", "not for me")
        self.server.publish("mine", "for me")
        kind, _, payload = sub.next_event(5)
        self.assertEqual((kind, payload), ("message", "for me"))

    def test_ping_gets_a_pong(self):
        sub = resp.Subscriber(self.server.url, ["chan"])
        self.addCleanup(sub.close)
        sub.connect()
        sub.ping()
        deadline = time.time() + 5
        seen = []
        while time.time() < deadline:
            ev = sub.next_event(0.5)
            if ev is None:
                continue
            seen.append(ev[0])
            if ev[0] == "pong":
                break
        self.assertIn("pong", seen)

    def test_idle_returns_none_instead_of_raising(self):
        sub = resp.Subscriber(self.server.url, ["chan"])
        self.addCleanup(sub.close)
        sub.connect()
        started = time.time()
        self.assertIsNone(sub.next_event(0.3))
        self.assertLess(time.time() - started, 5)

    def test_auth_is_used_when_the_url_has_a_password(self):
        auth = FakeRespServer(password="s3cret")
        self.addCleanup(auth.close)
        sub = resp.Subscriber(auth.url, ["chan"])
        self.addCleanup(sub.close)
        self.assertEqual(sub.connect(), 1)
        self.assertTrue(auth.wait_for_subscriber("chan"))

    def test_wrong_password_raises(self):
        auth = FakeRespServer(password="right")
        self.addCleanup(auth.close)
        sub = resp.Subscriber(f"redis://user:wrong@127.0.0.1:{auth.port}",
                              ["chan"])
        with self.assertRaises(resp.RespError):
            sub.connect()

    def test_server_gone_reads_as_a_broken_link(self):
        server = FakeRespServer()
        sub = resp.Subscriber(server.url, ["chan"])
        sub.connect()
        server.close()
        with self.assertRaises(resp.RespError):
            for _ in range(20):
                sub.next_event(0.2)
        sub.close()

    def test_unreachable_port_raises(self):
        sub = resp.Subscriber("redis://127.0.0.1:1", ["chan"])
        with self.assertRaises(resp.RespError):
            sub.connect()

    def test_close_is_idempotent(self):
        sub = resp.Subscriber(self.server.url, ["chan"])
        sub.connect()
        sub.close()
        sub.close()

    def test_no_channels_is_rejected(self):
        with self.assertRaises(resp.RespError):
            resp.Subscriber(self.server.url, [])


class HandshakeRaceTest(unittest.TestCase):
    def test_a_message_read_during_the_handshake_is_not_lost(self):
        """connect() can read a publish that landed before the last
        confirmation. It has to be kept for the caller — next_event pops
        that pending queue first — because a dropped one is `a Muse that
        was told something and never heard it`.

        Driven at the queue level rather than through a real race: a
        network test cannot split a segment on demand, and the pending
        queue is exactly what connect() fills.
        """
        sub = resp.Subscriber("redis://127.0.0.1:1", ["a", "b"])
        sub._pending.append(("message", "a", "early bird"))
        self.assertEqual(sub.next_event(0), ("message", "a", "early bird"))
        self.assertIsNone(sub.next_event(0))

    def test_reconnect_returns_a_working_subscription(self):
        server = FakeRespServer()
        self.addCleanup(server.close)
        sub = resp.Subscriber(server.url, ["chan"])
        sub.connect()
        sub.close()
        sub.connect()
        self.assertEqual(server.publish("chan", "after reconnect"), 1)
        kind, _, payload = sub.next_event(5)
        self.assertEqual((kind, payload), ("message", "after reconnect"))
        sub.close()


class ThreadedSanityTest(unittest.TestCase):
    def test_two_clients_each_get_their_own_copies(self):
        server = FakeRespServer()
        self.addCleanup(server.close)
        subs = [resp.Subscriber(server.url, ["chan"]) for _ in range(2)]
        for s in subs:
            self.addCleanup(s.close)
            s.connect()
        self.assertTrue(server.wait_for_subscriber("chan"))
        self.assertEqual(server.publish("chan", "broadcast"), 2)
        for s in subs:
            self.assertEqual(s.next_event(5)[2], "broadcast")

    def test_publish_reports_zero_without_subscribers(self):
        server = FakeRespServer()
        self.addCleanup(server.close)
        self.assertEqual(server.publish("nobody", "hi"), 0)


class FakeServerItselfTest(unittest.TestCase):
    """The test double is load-bearing, so it gets checked too."""

    def test_unknown_command_is_an_error_not_a_crash(self):
        import socket as _socket
        server = FakeRespServer()
        self.addCleanup(server.close)
        s = _socket.create_connection((server.host, server.port),
                                      timeout=5)
        self.addCleanup(s.close)
        s.sendall(resp.encode_command("NOPE"))
        self.assertTrue(s.recv(256).startswith(b"-ERR"))

    def test_password_is_required_when_configured(self):
        server = FakeRespServer(password="pw")
        self.addCleanup(server.close)
        client = resp.Subscriber(f"redis://127.0.0.1:{server.port}", ["c"])
        with self.assertRaises(resp.RespError):
            client.connect()


if __name__ == "__main__":
    unittest.main()
