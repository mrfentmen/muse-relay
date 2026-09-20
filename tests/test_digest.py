"""digest.py: per-room activity digest."""
import json
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import relay_common as rc  # noqa: E402
import store  # noqa: E402
import digest  # noqa: E402


class DigestTest(RelayTestCase):
    def _seed_bus(self, key, lines):
        for line in lines:
            self.fake.bus_push(line, key=key)

    def _seed_roomdir(self, *keys):
        for i, k in enumerate(keys):
            rc.api_get(f"zadd/{rc.ROOMDIR_KEY}/{i + 1}/{k}")

    def test_room_digest(self):
        self._seed_bus("muse-bus:room:news",
                       ["alice: a", "bob: b", "alice: c", "alice: d"])
        rc_, out, err = self.run_cli(digest.main, ["--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("#news", out)
        self.assertIn("4 on bus", out)
        self.assertIn("alice (3)", out)
        self.assertIn("bob (1)", out)

    def test_json_output(self):
        self._seed_bus("muse-bus:room:news", ["alice: a", "bob: b"])
        rc_, out, err = self.run_cli(
            digest.main, ["--room", "news", "--json"])
        self.assertEqual(rc_, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        d = data[0]
        self.assertEqual(d["room"], "news")
        self.assertEqual(d["bus_messages"], 2)
        self.assertEqual(d["top_talkers"][0]["nick"], "alice")

    def test_busiest_hour_from_local_store(self):
        now = int(time.time())
        st = store.MessageStore(store.store_path("news"), room="news")
        # 3 messages this hour, 1 message 5 hours ago
        for i in range(3):
            st.append("alice", f"recent {i}", ts=now - 60)
        st.append("bob", "older", ts=now - 5 * 3600)
        self._seed_bus("muse-bus:room:news", ["x: y"])
        rc_, out, err = self.run_cli(
            digest.main, ["--room", "news", "--hours", "24", "--json"])
        self.assertEqual(rc_, 0, err)
        d = json.loads(out)[0]
        self.assertEqual(d["recent_messages"], 4)
        self.assertIsNotNone(d["busiest_hour"])
        this_hour = time.localtime(now).tm_hour
        self.assertEqual(d["busiest_hour"]["hour"], this_hour)
        self.assertEqual(d["busiest_hour"]["count"], 3)

    def test_hours_window_filters_store(self):
        now = int(time.time())
        st = store.MessageStore(store.store_path("news"), room="news")
        st.append("alice", "recent", ts=now - 60)
        st.append("bob", "old", ts=now - 5 * 3600)
        rc_, out, err = self.run_cli(
            digest.main, ["--room", "news", "--hours", "1", "--json"])
        d = json.loads(out)[0]
        self.assertEqual(d["recent_messages"], 1)

    def test_no_store_data_reports_na(self):
        self._seed_bus("muse-bus:room:quiet", ["alice: hi"])
        rc_, out, err = self.run_cli(
            digest.main, ["--room", "quiet", "--json"])
        self.assertEqual(rc_, 0, err)
        d = json.loads(out)[0]
        # tmp store file exists but is empty -> 0 recent, no busiest hour
        self.assertEqual(d["recent_messages"], 0)
        self.assertIsNone(d["busiest_hour"])

    def test_all_rooms_from_roomdir(self):
        self._seed_bus("muse-bus", ["alice: main msg"])
        self._seed_bus("muse-bus:room:news", ["bob: news msg"])
        self._seed_roomdir("muse-bus:room:news")
        rc_, out, err = self.run_cli(digest.main, ["--json"])
        self.assertEqual(rc_, 0, err)
        data = json.loads(out)
        rooms = {d["room"] for d in data}
        self.assertEqual(rooms, {"main", "news"})

    def test_bad_hours_rejected(self):
        rc_, out, err = self.run_cli(digest.main, ["--hours", "0"])
        self.assertEqual(rc_, 2)
        self.assertIn("--hours must be positive", err)

    def test_pages_big_room(self):
        lines = [f"u{i % 3}: msg {i}" for i in range(2500)]
        self._seed_bus("muse-bus:room:big", lines)
        rc_, out, err = self.run_cli(
            digest.main, ["--room", "big", "--json"])
        self.assertEqual(rc_, 0, err)
        d = json.loads(out)[0]
        self.assertEqual(d["bus_messages"], 2500)
        total = sum(t["count"] for t in d["top_talkers"])
        self.assertEqual(total, 2500)


if __name__ == "__main__":
    import unittest
    unittest.main()
