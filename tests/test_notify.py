"""notify.py: the relay-side notification CLI."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import notify  # noqa: E402


class NotifyCliTest(RelayTestCase):
    def test_posts_notify_line_to_main(self):
        rc_, out, err = self.run_cli(
            notify.main, ["Job", "12", "stalled", "--severity", "warning",
                          "--topic", "jobs"])
        self.assertEqual(rc_, 0, err)
        pushed = self.pushes_to("muse-bus")
        self.assertEqual(len(pushed), 1)
        body = pushed[0]
        self.assertTrue(body.startswith("tester: NOTIFY:ntf-"), body)
        self.assertIn("warning [jobs] Job 12 stalled", body)
        self.assertIn("NOTIFIED muse-bus NOTIFY:", out)

    def test_room_and_target(self):
        rc_, out, err = self.run_cli(
            notify.main, ["Bridge down", "--room", "1",
                          "--severity", "critical", "--target", "milo"])
        self.assertEqual(rc_, 0, err)
        pushed = self.pushes_to("muse-bus:room:1")
        self.assertEqual(len(pushed), 1)
        self.assertIn("critical [general] @milo Bridge down", pushed[0])

    def test_bad_severity_rejected(self):
        rc_, out, err = self.run_cli(
            notify.main, ["hello", "--severity", "loud"])
        self.assertEqual(rc_, 2)
        self.assertIn("severity", err)
        self.assertEqual(self.fake.pushes, [])

    def test_id_is_deterministic(self):
        a = notify.notify_id("pax", "jobs", "Job 12 stalled", 1000)
        b = notify.notify_id("pax", "jobs", "Job 12 stalled", 1000)
        c = notify.notify_id("pax", "jobs", "Job 12 stalled", 1001)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("ntf-"))

    def test_line_flattens_newlines(self):
        line = notify.format_line("pax", "info", "general",
                                  "one\ntwo", ts=1000)
        self.assertNotIn("\n", line)
