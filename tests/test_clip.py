"""Unit tests for the cross-machine clipboard (send.py --clip, paste.py)."""
import base64
import io
import os
import subprocess
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase  # noqa: E402
import relay_common as rc  # noqa: E402
import send  # noqa: E402
import paste  # noqa: E402
import clipboard  # noqa: E402


class FakeTool:
    """A stand-in for a clipboard tool command list (argv[0] = name)."""

    def __init__(self, name, *, fail=False, payload=b""):
        self.name = name
        self.fail = fail
        self.payload = payload
        self.received = []  # stdin bytes handed to the fake writer

    def __iter__(self):
        return iter([self.name])

    def __getitem__(self, i):
        return self.name


class FakeToolbox:
    """Registry of fake clipboard tools for one test.

    Patches clipboard.py once so every tool added to the box exists on
    PATH and runs in-memory (no real binaries, no real clipboard).
    """

    def __init__(self, testcase):
        self.lookup = {}
        self._patch(testcase)

    def _patch(self, testcase):
        def _which(name):
            return "/fake/bin/" + name if name in self.lookup else None

        def _run(cmd, **kwargs):
            tool = self.lookup.get(cmd[0])
            if tool is None:
                raise FileNotFoundError(2, "No such file", cmd[0])
            if tool.fail:
                return subprocess.CompletedProcess(
                    cmd, 1, b"", b"simulated tool failure")
            data = kwargs.get("input")
            if data is not None:
                tool.received.append(data)
            return subprocess.CompletedProcess(cmd, 0, tool.payload, b"")

        p1 = mock.patch.object(clipboard.shutil, "which", _which)
        p2 = mock.patch.object(clipboard.subprocess, "run", _run)
        p1.start()
        p2.start()
        testcase.addCleanup(p1.stop)
        testcase.addCleanup(p2.stop)

    def add(self, name, *, fail=False, payload=b""):
        tool = FakeTool(name, fail=fail, payload=payload)
        self.lookup[name] = tool
        return tool


class ClipboardHelperTest(RelayTestCase):
    """clipboard.py: tool fallbacks and loud failures."""

    def test_read_uses_first_working_tool(self):
        box = FakeToolbox(self)
        t1 = box.add("first", payload=b"a")
        t2 = box.add("second", payload=b"b")
        self.assertEqual(clipboard.clipboard_read([t1, t2]), b"a")

    def test_read_falls_through_failing_tool(self):
        box = FakeToolbox(self)
        bad = box.add("bad", fail=True)
        good = box.add("good", payload=b"ok")
        self.assertEqual(clipboard.clipboard_read([bad, good]), b"ok")

    def test_read_no_tools_installed_is_loud(self):
        with self.assertRaises(clipboard.ClipboardError) as cm:
            clipboard.clipboard_read([["nothing-here"], ["also-missing"]])
        self.assertIn("no clipboard tool found", str(cm.exception))
        self.assertIn("nothing-here", str(cm.exception))
        self.assertIn("install", str(cm.exception))

    def test_read_all_tools_fail_is_loud(self):
        box = FakeToolbox(self)
        bad = box.add("xclip", fail=True)
        bad2 = box.add("xsel", fail=True)
        with self.assertRaises(clipboard.ClipboardError) as cm:
            clipboard.clipboard_read([bad, bad2])
        self.assertIn("clipboard read failed", str(cm.exception))
        self.assertIn("xclip", str(cm.exception))
        self.assertIn("simulated tool failure", str(cm.exception))

    def test_write_returns_tool_name_and_sends_data(self):
        box = FakeToolbox(self)
        t = box.add("writer")
        self.assertEqual(clipboard.clipboard_write(b"hello", [t]), "writer")
        self.assertEqual(t.received, [b"hello"])

    def test_write_no_tools_is_loud(self):
        with self.assertRaises(clipboard.ClipboardError) as cm:
            clipboard.clipboard_write(b"x", [["missing-tool"]])
        self.assertIn("no clipboard tool found", str(cm.exception))


class ClipSendTest(RelayTestCase):
    """send.py --clip: store bytes as blob + clip envelope."""

    def setUp(self):
        super().setUp()
        self.box = FakeToolbox(self)
        self.clip_tool = self.box.add("fakeclip", payload=b"clip payload 123")
        self.set_clip_tools(read=[self.clip_tool])

    def test_clip_stores_blob_and_envelope(self):
        rc_, out, err = self.run_cli(send.main, ["--clip"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("CLIP_STORED", out)
        env = rc.clip_latest("tester")
        self.assertIsNotNone(env)
        self.assertEqual(env["host"], "host-a")
        data = rc.blob_get(env["hash"])
        self.assertEqual(data, b"clip payload 123")
        self.assertEqual(env["size"], len(data))
        # nothing was posted to any room: the clip key is the transport
        self.assertEqual(self.fake.pushes, [])

    def test_clip_latest_wins(self):
        self.run_cli(send.main, ["--clip"])
        self.clip_tool.payload = b"second clip"
        self.run_cli(send.main, ["--clip"])
        env = rc.clip_latest("tester")
        self.assertEqual(rc.blob_get(env["hash"]), b"second clip")
        self.assertEqual(len(rc.clip_history("tester")), 2)

    def test_clip_history_capped_at_20(self):
        for i in range(25):
            self.clip_tool.payload = f"clip {i}".encode()
            rc_, _, err = self.run_cli(send.main, ["--clip"])
            self.assertEqual(rc_, 0, err)
        hist = rc.clip_history("tester")
        self.assertEqual(len(hist), rc.CLIP_LOG_MAX)
        self.assertEqual(hist[0]["size"], len(b"clip 24"))  # newest first
        self.assertEqual(hist[-1]["size"], len(b"clip 5"))  # oldest kept

    def test_clip_empty_clipboard_rejected(self):
        self.clip_tool.payload = b""
        rc_, out, err = self.run_cli(send.main, ["--clip"])
        self.assertEqual(rc_, 2)
        self.assertIn("clipboard is empty", err)
        self.assertIsNone(rc.clip_latest("tester"))

    def test_clip_no_tool_is_loud(self):
        self.set_clip_tools(read=[["not-installed-xyz"]])
        rc_, out, err = self.run_cli(send.main, ["--clip"])
        self.assertEqual(rc_, 2)
        self.assertIn("no clipboard tool found", err)
        self.assertIn("not-installed-xyz", err)
        self.assertIsNone(rc.clip_latest("tester"))

    def test_clip_rejects_combinations(self):
        for argv in (["--clip", "hi"], ["--clip", "--blob", "f"],
                     ["--clip", "--at", "10m"], ["--clip", "--typing"],
                     ["--clip", "--img", "x.png"]):
            rc_, _, err = self.run_cli(send.main, argv)
            self.assertEqual(rc_, 2, argv)
            self.assertIn("doesn't combine", err)


class ClipPullTest(RelayTestCase):
    """paste.py: pull the latest clip, verify, restore locally."""

    def setUp(self):
        super().setUp()
        self.box = FakeToolbox(self)
        self.clip_tool = self.box.add("fakeclip", payload=b"cross-machine!")
        self.set_clip_tools(read=[self.clip_tool])
        self.paste_tool = self.box.add("fakepaste")

    def test_round_trip_between_machines(self):
        self.run_cli(send.main, ["--clip"])
        # A different machine, same nick: clip comes back via the bus.
        self.set_nick("tester", "host-b")
        self.set_clip_tools(write=[self.paste_tool])
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("PASTED 14 bytes", out)
        self.assertIn("host-a", out)
        self.assertEqual(self.paste_tool.received, [b"cross-machine!"])

    def test_stdout_dumps_raw_bytes(self):
        self.run_cli(send.main, ["--clip"])
        buf = io.BytesIO()
        with mock.patch.object(sys, "stdout", _BytesStdout(buf)):
            rc_ = paste.main(["--stdout"])
        self.assertEqual(rc_, 0)
        self.assertEqual(buf.getvalue(), b"cross-machine!")

    def test_no_clip_prints_marker(self):
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("NO_CLIP", out)
        self.assertEqual(self.paste_tool.received, [])

    def test_corrupt_hash_rejected(self):
        self.run_cli(send.main, ["--clip"])
        env = rc.clip_latest("tester")
        key = f"muse-bus:blob:{env['hash']}:0"
        self.fake.strings[key] = (
            base64.b64encode(b"tampered!!").decode("ascii"), None)
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 2)
        self.assertIn("don't match their hash", err)
        self.assertEqual(self.paste_tool.received, [])  # nothing written

    def test_missing_blob_rejected(self):
        self.run_cli(send.main, ["--clip"])
        env = rc.clip_latest("tester")
        for i in range(env["n"]):
            del self.fake.strings[f"muse-bus:blob:{env['hash']}:{i}"]
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 2)
        self.assertIn("reassembly failed", err)

    def test_no_clipboard_tool_on_paste_points_at_stdout(self):
        self.run_cli(send.main, ["--clip"])
        self.set_clip_tools(write=[["not-installed-xyz"]])
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 2)
        self.assertIn("no clipboard tool found", err)
        self.assertIn("--stdout", err)  # the escape hatch

    def test_per_nick_isolation(self):
        self.run_cli(send.main, ["--clip"])
        self.set_nick("other", "host-b")
        rc_, out, err = self.run_cli(paste.main, [])
        self.assertEqual(rc_, 0, err)
        self.assertIn("NO_CLIP", out)


class _BytesStdout:
    """sys.stdout stand-in exposing a BytesIO .buffer (for --stdout)."""

    def __init__(self, buf):
        self.buffer = buf

    def write(self, s):
        return len(s)

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
