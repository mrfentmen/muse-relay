"""webhook.py: bridge room messages to an external webhook."""
import io
import json
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import webhook  # noqa: E402


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Injectable urlopen: records requests, scripted failures."""

    def __init__(self, fail_times=0, status=200):
        self.requests = []
        self.fail_times = fail_times
        self.status = status
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        body = req.data.decode("utf-8") if req.data else ""
        self.requests.append((req.full_url, dict(req.headers),
                              json.loads(body)))
        if self.calls <= self.fail_times:
            raise ConnectionError("boom")
        return FakeResponse(self.status)


class WebhookTest(RelayTestCase):
    URL = "https://example.com/hook"

    def test_resolve_literal_url(self):
        self.assertEqual(webhook.resolve_url(self.URL), self.URL)

    def test_resolve_env_var(self):
        os.environ["RELAY_TEST_HOOK"] = self.URL
        self.addCleanup(os.environ.pop, "RELAY_TEST_HOOK")
        self.assertEqual(webhook.resolve_url("RELAY_TEST_HOOK"), self.URL)

    def test_resolve_missing_env(self):
        os.environ.pop("RELAY_TEST_HOOK_MISSING", None)
        with self.assertRaises(ValueError):
            webhook.resolve_url("RELAY_TEST_HOOK_MISSING")

    def test_resolve_non_url_env(self):
        os.environ["RELAY_TEST_HOOK_BAD"] = "not a url"
        self.addCleanup(os.environ.pop, "RELAY_TEST_HOOK_BAD")
        with self.assertRaises(ValueError):
            webhook.resolve_url("RELAY_TEST_HOOK_BAD")

    def test_bridge_once_posts_new(self):
        self.fake.bus_push("alice: hello", key="muse-bus")
        self.fake.bus_push("bob: hi", key="muse-bus")
        opener = FakeOpener()
        posted, failed = webhook.bridge_once(self.URL, "", opener=opener)
        self.assertEqual((posted, failed), (2, 0))
        payloads = [r[2] for r in opener.requests]
        self.assertEqual(payloads[0]["nick"], "alice")
        self.assertEqual(payloads[0]["text"], "hello")
        self.assertEqual(payloads[0]["room"], "main")
        self.assertIsInstance(payloads[0]["ts"], int)
        self.assertEqual(payloads[1]["nick"], "bob")

    def test_once_only_new_since_last_run(self):
        self.fake.bus_push("alice: first", key="muse-bus")
        opener = FakeOpener()
        webhook.bridge_once(self.URL, "", opener=opener)
        self.fake.bus_push("bob: second", key="muse-bus")
        opener2 = FakeOpener()
        posted, failed = webhook.bridge_once(self.URL, "", opener=opener2)
        self.assertEqual((posted, failed), (1, 0))
        self.assertEqual(opener2.requests[0][2]["text"], "second")

    def test_retry_then_success(self):
        self.fake.bus_push("alice: hello", key="muse-bus")
        opener = FakeOpener(fail_times=4)
        posted, failed = webhook.bridge_once(
            self.URL, "", opener=opener, sleep=lambda s: None)
        self.assertEqual((posted, failed), (1, 0))
        self.assertEqual(opener.calls, 5)

    def test_gives_up_after_five(self):
        self.fake.bus_push("alice: hello", key="muse-bus")
        opener = FakeOpener(fail_times=99)
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            posted, failed = webhook.bridge_once(
                self.URL, "", opener=opener, sleep=lambda s: None)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual((posted, failed), (0, 1))
        self.assertEqual(opener.calls, 5)
        # fails loudly, and the URL never appears in the noise
        self.assertIn("RELAY_ERROR", err.getvalue())
        self.assertNotIn("example.com", err.getvalue())
        self.assertNotIn("example.com", out.getvalue())
        # offset not advanced: next run retries the same message
        opener2 = FakeOpener()
        posted2, _ = webhook.bridge_once(self.URL, "", opener=opener2)
        self.assertEqual(posted2, 1)
        self.assertEqual(opener2.requests[0][2]["text"], "hello")

    def test_room_scoping(self):
        self.fake.bus_push("alice: news item", key="muse-bus:room:news")
        self.fake.bus_push("bob: main item", key="muse-bus")
        opener = FakeOpener()
        posted, failed = webhook.bridge_once(
            self.URL, "news", opener=opener)
        self.assertEqual((posted, failed), (1, 0))
        self.assertEqual(opener.requests[0][2]["room"], "news")
        self.assertEqual(opener.requests[0][2]["text"], "news item")

    def test_unparseable_lines_skipped(self):
        self.fake.bus_push("not a proper line", key="muse-bus")
        self.fake.bus_push("alice: ok", key="muse-bus")
        opener = FakeOpener()
        posted, failed = webhook.bridge_once(self.URL, "", opener=opener)
        self.assertEqual((posted, failed), (1, 0))

    def test_main_once(self):
        self.fake.bus_push("alice: hello", key="muse-bus")
        fake_opener = FakeOpener()
        orig = webhook.bridge_once
        seen = {}

        def spy(url, room, opener=None, sleep=None):
            seen["url"] = url
            # main() passes no opener: substitute the fake so no real
            # HTTP ever happens in this test.
            return orig(url, room, opener=fake_opener,
                        sleep=lambda s: None)

        webhook.bridge_once = spy
        try:
            rc_, out, err = self.run_cli(
                webhook.main, ["--url", self.URL, "--once"])
        finally:
            webhook.bridge_once = orig
        self.assertEqual(rc_, 0, err)
        self.assertEqual(seen["url"], self.URL)
        self.assertIn("WEBHOOK_POSTED 1", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
