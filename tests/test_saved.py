"""saved.py: bookmark messages by store id."""
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import store  # noqa: E402
import saved  # noqa: E402


class SavedTest(RelayTestCase):
    def _store_msg(self, room="", nick="alice", text="hello"):
        st = store.MessageStore(store.store_path(room), room=room)
        return st.append(nick, text)

    def test_save_and_list(self):
        e = self._store_msg(text="remember this")
        rc_, out, err = self.run_cli(saved.main, ["save", e["id"]])
        self.assertEqual(rc_, 0, err)
        self.assertIn("SAVED 1", out)
        rc_, out, err = self.run_cli(saved.main, ["list"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("[1] main alice: remember this", out)

    def test_save_unknown_id_is_clear_error(self):
        rc_, out, err = self.run_cli(saved.main, ["save", "99"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such message: 99", err)

    def test_save_with_room(self):
        e = self._store_msg(room="news", nick="bob", text="big story")
        rc_, out, err = self.run_cli(
            saved.main, ["save", e["id"], "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("SAVED news-1", out)
        rc_, out, err = self.run_cli(saved.main, ["list"])
        self.assertIn("[news-1] #news bob: big story", out)

    def test_list_room_filter(self):
        e1 = self._store_msg(text="main msg")
        e2 = self._store_msg(room="news", text="news msg")
        self.run_cli(saved.main, ["save", e1["id"]])
        self.run_cli(saved.main, ["save", e2["id"], "--room", "news"])
        rc_, out, err = self.run_cli(saved.main, ["list", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("news msg", out)
        self.assertNotIn("main msg", out)

    def test_list_json(self):
        e = self._store_msg(nick="carol", text="json me")
        self.run_cli(saved.main, ["save", e["id"]])
        rc_, out, err = self.run_cli(saved.main, ["list", "--json"])
        self.assertEqual(rc_, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(
            (data[0]["id"], data[0]["nick"], data[0]["text"]),
            ("1", "carol", "json me"))
        self.assertIn("saved_ts", data[0])

    def test_list_empty(self):
        rc_, out, err = self.run_cli(saved.main, ["list"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("no saved messages", out)

    def test_unsave_removes(self):
        e = self._store_msg(text="bye")
        self.run_cli(saved.main, ["save", e["id"]])
        rc_, out, err = self.run_cli(saved.main, ["unsave", e["id"]])
        self.assertEqual(rc_, 0, err)
        self.assertIn("UNSAVED 1", out)
        rc_, out, err = self.run_cli(saved.main, ["list"])
        self.assertIn("no saved messages", out)

    def test_unsave_unknown_id_is_clear_error(self):
        rc_, out, err = self.run_cli(saved.main, ["unsave", "99"])
        self.assertEqual(rc_, 2)
        self.assertIn("no such saved message: 99", err)

    def test_newest_first_and_cap(self):
        st = store.MessageStore(store.store_path(""), room="")
        for i in range(205):
            e = st.append("alice", f"msg {i}")
            self.run_cli(saved.main, ["save", e["id"]])
        rc_, out, err = self.run_cli(saved.main, ["list", "--json"])
        self.assertEqual(rc_, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data), 200)
        # newest first: last saved ("msg 204") on top, oldest trimmed
        self.assertEqual(data[0]["text"], "msg 204")
        self.assertEqual(data[-1]["text"], "msg 5")

    def test_saves_are_nick_scoped(self):
        e = self._store_msg(text="mine")
        self.run_cli(saved.main, ["save", e["id"]])
        self.set_nick("other", "host-b")
        rc_, out, err = self.run_cli(saved.main, ["list"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("no saved messages", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
