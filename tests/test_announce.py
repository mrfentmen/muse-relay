"""announce.py: announcement rooms + poll/watch --announce filter."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import relay_common as rc  # noqa: E402
import announce  # noqa: E402
import poll  # noqa: E402
import watch  # noqa: E402


class AnnounceTest(RelayTestCase):
    def test_set_show_clear(self):
        rc_, out, err = self.run_cli(announce.main, ["set", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("ANNOUNCE_SET", out)
        self.assertIn("client-side convention", err)

        rc_, out, err = self.run_cli(announce.main, ["show", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("announcer is tester", out)

        rc_, out, err = self.run_cli(announce.main, ["clear", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("ANNOUNCE_CLEARED", out)

        rc_, out, err = self.run_cli(announce.main, ["show", "--room", "news"])
        self.assertIn("not an announce room", out)

    def test_gate_rejects_claim_loss(self):
        rc._claim_state.update(ok=False, holder="host-a")
        rc_, out, err = self.run_cli(announce.main, ["set", "--room", "news"])
        self.assertEqual(rc_, 2)
        self.assertIn("don't hold the nick claim", err)

    def test_gate_honors_mods(self):
        key = "muse-bus:room:news"
        rc.api_get(f"sadd/muse-bus:mods:{key}/alice")
        # tester is not a mod -> refused
        rc_, out, err = self.run_cli(announce.main, ["set", "--room", "news"])
        self.assertEqual(rc_, 2)
        self.assertIn("only room mods", err)
        # alice is a mod -> allowed
        self.set_nick("alice", "host-a")
        rc_, out, err = self.run_cli(announce.main, ["set", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("ANNOUNCE_SET", out)

    def test_no_mods_anyone_may_set(self):
        rc_, out, err = self.run_cli(announce.main, ["set", "--room", "news"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("announcer is tester",
                      self.run_cli(announce.main,
                                   ["show", "--room", "news"])[1])

    def test_filter_keeps_announcer_only(self):
        rc.api_get("set/muse-bus:announce:muse-bus:room:news/tester")
        kept, skipped = announce.announce_filter(
            "muse-bus:room:news",
            ["tester: ship it", "bob: wait", "tester: monday"])
        self.assertEqual(kept, ["tester: ship it", "tester: monday"])
        self.assertEqual(skipped, 1)

    def test_filter_no_announcer_keeps_all(self):
        kept, skipped = announce.announce_filter(
            "muse-bus:room:news", ["bob: hi"])
        self.assertEqual((kept, skipped), (["bob: hi"], 0))

    def test_poll_announce_flag(self):
        key = "muse-bus:room:news"
        rc.api_get(f"set/muse-bus:announce:{key}/alice")
        self.fake.bus_push("alice: release notes", key=key)
        self.fake.bus_push("bob: chatter", key=key)
        rc_, out, err = self.run_cli(
            poll.main, ["--room", "news", "--announce"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("alice: release notes", out)
        self.assertNotIn("bob: chatter", out)
        self.assertIn("skipped 1 non-announcer", err)

    def test_poll_announce_without_setter_warns(self):
        key = "muse-bus:room:news"
        self.fake.bus_push("bob: chatter", key=key)
        rc_, out, err = self.run_cli(
            poll.main, ["--room", "news", "--announce"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("bob: chatter", out)
        self.assertIn("no announcer is set", err)

    def test_watch_announce_flag(self):
        key = "muse-bus:room:news"
        rc.api_get(f"set/muse-bus:announce:{key}/alice")
        self.fake.bus_push("alice: release notes", key=key)
        self.fake.bus_push("bob: chatter", key=key)
        import os
        seen_file = os.path.join(self.tmp, "wseen-news")
        targets = [["room 'news'", key, seen_file, 0]]
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            shown = watch.watch_once(targets, announce_only=True)
        self.assertEqual(shown, 1)
        self.assertIn("alice: release notes", out.getvalue())
        self.assertNotIn("bob: chatter", out.getvalue())
        self.assertIn("skipped 1 non-announcer", err.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
