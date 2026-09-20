"""Pomodoro timers: send --pomodoro, timecapsule firing, poll render."""
import json
import sys
import time
import urllib.parse

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import relay_common as rc  # noqa: E402
import send  # noqa: E402
import poll  # noqa: E402
import timecapsule  # noqa: E402


class PomodoroTest(RelayTestCase):
    def test_parse_compound_durations(self):
        p = send.parse_pomodoro_duration
        self.assertEqual(p("25m"), 1500)
        self.assertEqual(p("1h30m"), 5400)
        self.assertEqual(p("90s"), 90)
        self.assertEqual(p("2h"), 7200)
        self.assertEqual(p("1d"), 86400)
        self.assertEqual(p("2h 15m"), 8100)

    def test_parse_rejects_garbage(self):
        for bad in ("", "abc", "10x", "1h 2x", "0m", "m", "-5m", "1.5h"):
            with self.assertRaises(ValueError, msg=bad):
                send.parse_pomodoro_duration(bad)

    def test_send_pomodoro_posts_and_schedules(self):
        before = int(time.time())
        rc_, out, err = self.run_cli(
            send.main, ["--pomodoro", "25m", "write docs"])
        self.assertEqual(rc_, 0, err)
        # start line on the bus
        pushes = self.pushes_to("muse-bus")
        self.assertTrue(
            any(b == "tester: POMODORO tester 1500 write docs"
                for b in pushes), pushes)
        # timer registered in the pomodoro zset
        data = json.loads(rc.api_get(f"zrange/{rc.POMO_KEY}/0/-1"))
        members = data.get("result") or []
        self.assertEqual(len(members), 1)
        item = json.loads(members[0])
        self.assertEqual(item["nick"], "tester")
        self.assertEqual(item["label"], "write docs")
        self.assertEqual(item["secs"], 1500)
        self.assertTrue(before + 1500 <= item["end"] <= before + 1500 + 5)
        # done capsule scheduled for the same end time
        data = json.loads(rc.api_get(
            f"zrangebyscore/{timecapsule.TIMECAPSULE_KEY}/0/+inf"))
        due = [json.loads(m) for m in (data.get("result") or [])]
        self.assertTrue(
            any(d["text"] == "POMODORO-DONE tester write docs"
                for d in due), due)

    def test_send_pomodoro_garbage_duration(self):
        rc_, out, err = self.run_cli(
            send.main, ["--pomodoro", "soon", "write docs"])
        self.assertEqual(rc_, 2)
        self.assertIn("not a pomodoro duration", err)

    def test_send_pomodoro_needs_label(self):
        rc_, out, err = self.run_cli(send.main, ["--pomodoro", "25m"])
        self.assertEqual(rc_, 2)
        self.assertIn("needs a label", err)

    def test_send_pomodoro_exclusive(self):
        rc_, out, err = self.run_cli(
            send.main, ["--pomodoro", "25m", "--every", "60s", "x"])
        self.assertEqual(rc_, 2)
        self.assertIn("doesn't combine", err)

    def test_timecapsule_fires_done(self):
        rc_, out, err = self.run_cli(
            send.main, ["--pomodoro", "2s", "quick break"])
        self.assertEqual(rc_, 0, err)
        delivered = timecapsule.pop_due(now=time.time() + 30)
        self.assertTrue(
            any(body == "tester: POMODORO-DONE tester quick break"
                for _, body in delivered), delivered)
        pushes = self.pushes_to("muse-bus")
        self.assertIn("tester: POMODORO-DONE tester quick break", pushes)

    def test_poll_renders_active_timers(self):
        now = int(time.time())
        rc.pomo_register("muse-bus", "alice", "write docs", 1500, now + 1500)
        rc_, out, err = self.run_cli(poll.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("TIMER alice: write docs", out)
        self.assertIn("left", out)

    def test_poll_prunes_expired_timers(self):
        now = int(time.time())
        rc.pomo_register("muse-bus", "alice", "old", 60, now - 10)
        timers = rc.pomo_active("muse-bus", now=now)
        self.assertEqual(timers, [])
        # pruned from the zset
        data = json.loads(rc.api_get(f"zrange/{rc.POMO_KEY}/0/-1"))
        self.assertEqual(data.get("result"), [])

    def test_poll_timer_room_scoped(self):
        now = int(time.time())
        rc.pomo_register("muse-bus:room:news", "alice", "news timer",
                         600, now + 600)
        self.assertEqual(rc.pomo_active("muse-bus", now=now), [])
        timers = rc.pomo_active("muse-bus:room:news", now=now)
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0]["label"], "news timer")
        self.assertTrue(590 <= timers[0]["remaining"] <= 600)

    def test_fmt_remaining(self):
        self.assertEqual(rc.fmt_remaining(45), "45s")
        self.assertEqual(rc.fmt_remaining(1500), "25m")
        self.assertEqual(rc.fmt_remaining(5430), "1h 30m 30s")
        self.assertEqual(rc.fmt_remaining(0), "0s")


if __name__ == "__main__":
    import unittest
    unittest.main()
