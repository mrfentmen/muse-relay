#!/usr/bin/env python3
"""The vendored bus-tools, run for real.

`test_bustools_parity.py` only hashes these scripts against the live skill
bin — it never executes them, so nothing caught that `bus-send` posted
messages no live listener would hear and `bus-poll` read with a raw list
index. Both are the tooling a human actually uses to text the relay, so
they get the same treatment as the Python: a real subprocess, real HTTP,
real assertions.

The scripts are copied to a temp dir before running. They resolve their
cursor state relative to their own path (`state/` next to the script), so
running the copy proves they are self-contained and keeps the repo clean.
`dynamic_credentials` (the skill bin's credential vault) is stubbed on
PYTHONPATH — it is the one dependency that lives outside this repo.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_http import FakeUpstashHTTP  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "bus-tools")
SURROGATE = "surrogate-token-for-tests"
CHANNEL = "muse-bus:live"


class BusToolsRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay-tools-")
        # the scripts live in their own dir; state/ is created next to them
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("bus-send", "bus-poll"):
            shutil.copy(os.path.join(TOOLS, name),
                        os.path.join(self.bin, name))
        # the credential vault the scripts import, stubbed
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        with open(os.path.join(self.vault, "dynamic_credentials.py"), "w") as f:
            f.write("def dynamic_credential_entry(cred):\n"
                    "    return {'surrogate': %r}\n" % SURROGATE)
        self.http = FakeUpstashHTTP()
        self.addCleanup(self.http.close)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.tmp,
            "PYTHONPATH": self.vault,
            "MUSE_RELAY_URL": self.http.url,
            "MUSE_RELAY_BUS": "muse-bus",
            "MUSE_RELAY_NICK": "tester",
        }

    # -- helpers -------------------------------------------------------
    def run_tool(self, name, *args, **env):
        e = dict(self.env)
        e.update(env)
        p = subprocess.run(
            [sys.executable, os.path.join(self.bin, name)] + list(args),
            cwd=self.bin, env=e, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertNotIn(SURROGATE, p.stdout)
        self.assertNotIn(SURROGATE, p.stderr)
        return p

    def bus(self, key="muse-bus"):
        return self.http.backend.lists.get(key, [])

    def counter(self, key="muse-bus"):
        hit = self.http.backend.strings.get(f"{key}:total")
        return int(hit[0]) if hit else 0

    def published(self):
        return self.http.backend.published

    def seen_file(self, room=""):
        return os.path.join(self.bin, "state",
                            f"seen-{room}.txt" if room else "seen.txt")

    # -- bus-send ------------------------------------------------------
    def test_bus_send_posts_the_message_with_the_nick_prefix(self):
        p = self.run_tool("bus-send", "hello there")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("SENT", p.stdout)
        self.assertEqual(self.bus(), ["tester: hello there"])

    def test_bus_send_bumps_the_counter_that_makes_loss_detectable(self):
        self.run_tool("bus-send", "one")
        self.assertEqual(self.counter(), 1)
        self.run_tool("bus-send", "two")
        self.assertEqual(self.counter(), 2)

    def test_bus_send_rings_the_doorbell_with_a_real_envelope(self):
        """The whole point: a listener hears it now, not on reconnect."""
        self.run_tool("bus-send", "wake up")
        self.assertEqual(len(self.published()), 1)
        channel, envelope = self.published()[0]
        self.assertEqual(channel, CHANNEL)
        payload = json.loads(envelope)
        self.assertEqual(payload["room"], "muse-bus")
        self.assertEqual(payload["body"], "tester: wake up")
        self.assertEqual(payload["kind"], "msg")
        self.assertIsInstance(payload["ts"], int)

    def test_the_envelope_is_one_listen_py_accepts(self):
        sys.path.insert(0, ROOT)
        import listen  # noqa: E402  (after the path insert)
        self.run_tool("bus-send", "shape check")
        _, envelope = self.published()[0]
        ev = listen.parse_envelope(envelope, CHANNEL)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["from"], "tester")
        self.assertEqual(ev["text"], "tester: shape check")
        self.assertEqual(ev["room"], "main")

    def test_bus_send_reports_how_many_listeners_heard_it(self):
        self.http.backend.subscribers = 2
        p = self.run_tool("bus-send", "anyone there")
        self.assertIn("PINGED 2 listener(s)", p.stdout)

    def test_bus_send_says_so_when_nobody_is_listening(self):
        p = self.run_tool("bus-send", "echo")
        self.assertIn("PINGED 0 listeners", p.stdout)

    def test_no_ping_silences_the_bell_but_keeps_the_message(self):
        p = self.run_tool("bus-send", "quiet", MUSE_RELAY_NO_PING="1")
        self.assertEqual(self.published(), [])
        self.assertEqual(self.bus(), ["tester: quiet"])
        self.assertEqual(self.counter(), 1)
        self.assertNotIn("PINGED", p.stdout)

    def test_a_room_post_lands_in_the_room_and_rings_with_that_room(self):
        p = self.run_tool("bus-send", "eng talk", "--room", "Eng")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.bus("muse-bus:room:eng"),
                         ["tester: eng talk"])
        payload = json.loads(self.published()[0][1])
        self.assertEqual(payload["room"], "muse-bus:room:eng")

    def test_a_body_that_would_break_a_url_still_posts(self):
        tricky = "50% off / a&b=c?d #tag + plus"
        p = self.run_tool("bus-send", tricky)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.bus(), [f"tester: {tricky}"])
        payload = json.loads(self.published()[0][1])
        self.assertEqual(payload["body"], f"tester: {tricky}")

    def test_the_request_carries_the_vaulted_credential(self):
        self.run_tool("bus-send", "auth check")
        self.assertTrue(self.http.bearer)
        self.assertTrue(all(h == f"Bearer {SURROGATE}"
                            for h in self.http.bearer))

    # -- bus-poll ------------------------------------------------------
    def test_bus_poll_prints_new_messages_from_other_nicks(self):
        self.run_tool("bus-send", "mine")
        self.run_tool("bus-send", "from alice", MUSE_RELAY_NICK="alice")
        p = self.run_tool("bus-poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("NEW_MESSAGES:", p.stdout)
        self.assertIn("alice: from alice", p.stdout)
        self.assertNotIn("tester: mine", p.stdout)

    def test_bus_poll_advances_its_cursor(self):
        self.run_tool("bus-send", "one", MUSE_RELAY_NICK="alice")
        first = self.run_tool("bus-poll")
        self.assertIn("NEW_MESSAGES:", first.stdout)
        with open(self.seen_file()) as f:
            self.assertEqual(int(f.read().strip()), 1)
        second = self.run_tool("bus-poll")
        self.assertIn("NO_NEW_MESSAGES", second.stdout)

    def test_bus_poll_reports_messages_trimmed_off_before_it_read(self):
        """The raw-index bug, in the tool a human actually runs.

        A reader that read 10 messages, then 600 arrive and the cap (500)
        trims the oldest 110: 100 of them were never read. Before this,
        LRANGE with a stale index returned nothing and said nothing.
        """
        for i in range(10):
            self.http.backend.bus_push(f"alice: early{i}")
        self.run_tool("bus-poll")
        with open(self.seen_file()) as f:
            self.assertEqual(int(f.read().strip()), 10)
        for i in range(600):
            self.http.backend.bus_push(f"alice: late{i}")
        self.http.backend.api_get("ltrim/muse-bus/-500/-1")
        self.assertEqual(self.http.backend._run("llen", ["muse-bus"]), 500)
        self.assertEqual(self.counter(), 610)

        p = self.run_tool("bus-poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("WARNING: 100 message(s)", p.stderr)
        self.assertIn("trimmed off", p.stderr)
        # the survivors still arrive, newest last
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith("alice")]
        self.assertEqual(len(lines), 500)
        self.assertEqual(lines[-1], "alice: late599")
        with open(self.seen_file()) as f:
            self.assertEqual(int(f.read().strip()), 610)

    def test_bus_poll_is_quiet_when_it_is_caught_up(self):
        self.http.backend.bus_push("alice: one")
        self.run_tool("bus-poll")
        p = self.run_tool("bus-poll")
        self.assertNotIn("WARNING", p.stderr)
        self.assertIn("NO_NEW_MESSAGES", p.stdout)

    def test_bus_poll_still_handles_a_bus_with_no_counter(self):
        """Lists written before the counter existed."""
        self.http.backend.bus_push("alice: legacy")
        del self.http.backend.strings["muse-bus:total"]
        p = self.run_tool("bus-poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("alice: legacy", p.stdout)
        self.assertNotIn("WARNING", p.stderr)

    def test_bus_poll_does_not_write_into_the_repo(self):
        self.run_tool("bus-poll")
        self.assertFalse(os.path.exists(os.path.join(TOOLS, "state")),
                         "bus-poll wrote cursor state into the repo")


if __name__ == "__main__":
    unittest.main()
