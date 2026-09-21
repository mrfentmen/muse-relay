"""Shared fixtures for muse-relay unit tests."""
import io
import os
import subprocess
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
import webhook  # noqa: E402
import brief  # noqa: E402
import listen  # noqa: E402
from fake_redis import FakeUpstash  # noqa: E402


def make_git_repo(path, branch="worker/thing"):
    """Create a real git repo with a committed worker branch.

    mos/gitops.py shells out to git and tests its real behavior (worktree
    add/remove/reclaim), so there is nothing to fake here. `branch=None`
    gives a repo with only main. Returns path.
    """
    def git(*args):
        subprocess.run(["git", "-C", path] + list(args), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    os.makedirs(path, exist_ok=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("v1\n")
    git("add", ".")
    git("commit", "-m", "base")
    if branch:
        git("checkout", "-b", branch)
        with open(os.path.join(path, "b.txt"), "w") as f:
            f.write("new\n")
        git("add", ".")
        git("commit", "-m", "worker commit")
        git("checkout", "main")
    return path


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeUpstash()
        self._patches = [
            mock.patch.object(rc, "api_get", self.fake.api_get),
            mock.patch.object(rc, "api_post", self.fake.api_post),
            mock.patch.object(rc, "api_pipeline", self.fake.api_pipeline),
            mock.patch.object(rc, "bus_push", self.fake.bus_push),
            mock.patch("time.time", self.fake.now),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop)
        # consumers did `from relay_common import api_get/...`: patch the
        # names in their namespaces too, not just relay_common's.
        for mod in (jobs, send, poll, watch, edits, paste, forward,
                saved, digest, announce, timecapsule, webhook, brief,
                listen):
            for name in ("api_get", "api_post", "api_pipeline", "bus_push"):
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
            mock.patch.object(listen, "live_path",
                              lambda room: os.path.join(
                                  self.tmp, "live-" + (room or "main"))),
            # brief.py --consume writes the same cursor poll.py reads, so
            # the two share one path here too — that agreement is the
            # point of the flag.
            mock.patch.object(brief, "seen_path",
                              lambda room: os.path.join(
                                  self.tmp, "seen-" + (room or "main"))),
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
        brief.NICK = nick
        listen.NICK = nick
        rc._claim_state.update(checked=0.0, ok=None, holder=None)

    def run_cli(self, mod_main, argv, stdin=None):
        """Run a script main(); returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err), \
                mock.patch("sys.stdin", io.StringIO(stdin or "")):
            rc_code = mod_main(argv)
        return rc_code, out.getvalue(), err.getvalue()

    def hook_script(self, body):
        """Write an executable shell script in the test's tmp dir."""
        path = os.path.join(self.tmp, "hook.sh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

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
