"""Shared fixtures for muse-relay unit tests."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay_common as rc  # noqa: E402
import jobs  # noqa: E402
import send  # noqa: E402
import poll  # noqa: E402
import watch  # noqa: E402
import edits  # noqa: E402
import store  # noqa: E402
import clipboard  # noqa: E402
import paste  # noqa: E402
import forward  # noqa: E402
import saved  # noqa: E402
import digest  # noqa: E402
import announce  # noqa: E402
import timecapsule  # noqa: E402
import export  # noqa: E402
import webhook  # noqa: E402
from fake_redis import FakeUpstash  # noqa: E402


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeUpstash()
        self._patches = [
            mock.patch.object(rc, "api_get", self.fake.api_get),
            mock.patch.object(rc, "api_post", self.fake.api_post),
            mock.patch.object(rc, "bus_push", self.fake.bus_push),
            mock.patch("time.time", self.fake.now),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop)
        # consumers did `from relay_common import api_get/...`: patch the
        # names in their namespaces too, not just relay_common's.
        for mod in (jobs, send, poll, watch, edits, paste, forward,
                saved, digest, announce, timecapsule, webhook):
            for name in ("api_get", "api_post", "bus_push"):
                if hasattr(mod, name):
                    p = mock.patch.object(mod, name,
                                          getattr(self.fake, name))
                    p.start()
                    self.addCleanup(p.stop)
        rc._claim_state.update(checked=0.0, ok=None, holder=None)
        self.tmp = tempfile.mkdtemp(prefix="relay-test-")
        # seen files and message stores go to tmp, not the repo dir
        self._seen_patches = [
            mock.patch.object(poll, "seen_path",
                              lambda room: os.path.join(
                                  self.tmp, "seen-" + (room or "main"))),
            mock.patch.object(watch, "seen_path",
                              lambda room: os.path.join(
                                  self.tmp, "wseen-" + (room or "main"))),
            mock.patch.object(webhook, "seen_path",
                              lambda room: os.path.join(
                                  self.tmp, "hkseen-" + (room or "main"))),
            mock.patch.object(
                store, "store_path",
                lambda room: os.path.join(
                    self.tmp, "messages.jsonl" if not room
                    else "messages-" + room + ".jsonl")),
        ]
        for p in self._seen_patches:
            p.start()
            self.addCleanup(p.stop)
        jobs.NS = "muse-bus:tjob"  # isolated job namespace
        self.set_nick("tester", "host-a")

    def _stop(self):
        for p in reversed(self._patches):
            p.stop()

    def set_nick(self, nick, host):
        """Act as a different agent: new nick and/or new machine."""
        rc.NICK = nick
        rc.INSTANCE_ID = host
        jobs.NICK = nick
        jobs.INSTANCE_ID = host
        send.NICK = nick
        poll.NICK = nick
        watch.NICK = nick
        edits.NICK = nick
        forward.NICK = nick
        saved.NICK = nick
        announce.NICK = nick
        rc._claim_state.update(checked=0.0, ok=None, holder=None)

    def run_cli(self, mod_main, argv, stdin=None):
        """Run a script main(); returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            rc_code = mod_main(argv)
        return rc_code, out.getvalue(), err.getvalue()

    def pushes_to(self, key):
        return [b for k, b in self.fake.pushes if k == key]

    def set_clip_tools(self, read=None, write=None):
        """Force clipboard helpers onto specific fake tools (or none)."""
        if read is not None:
            self._patches.append(
                mock.patch.object(clipboard, "READ_TOOLS", read))
            self._patches[-1].start()
        if write is not None:
            self._patches.append(
                mock.patch.object(clipboard, "WRITE_TOOLS", write))
            self._patches[-1].start()
