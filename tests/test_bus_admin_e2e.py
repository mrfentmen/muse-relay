#!/usr/bin/env python3
"""End-to-end tests for admin.py — no mocks.

Exercises the REAL admin.py against the REAL Upstash instance, isolated
under a random MUSE_RELAY_KEY_PREFIX. Every assertion is backed by an
actual HTTP round-trip. Nothing here is faked.

Requires: network access and the custom.upstash-muse-bus credential
(resolved in-process via dynamic_credentials, exactly like the bus-send
skill does). The REST token for the repo tooling is written to a temp
0600 file and deleted afterwards.

What remains unverified: concurrent admin writers racing init/promote
(single-writer here), and behavior when Upstash is unreachable (all
commands fail loudly; callers must handle that).
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://upright-mosquito-285786.upstash.io"
PREFIX = "muse-bus-admintest-" + secrets.token_hex(6)
BOSS = "testboss-" + secrets.token_hex(4)


def _upstash_token():
    entry = dc.dynamic_credential_entry("custom.upstash-muse-bus")
    return str(entry["surrogate"]).strip()


class AdminE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="busadmin-e2e-")
        cls.secret_file = os.path.join(cls.tmp, "admin_secret")
        with open(cls.secret_file, "wb") as f:
            f.write(secrets.token_bytes(32))
        os.chmod(cls.secret_file, 0o600)
        cls.wrong_secret = os.path.join(cls.tmp, "wrong_secret")
        with open(cls.wrong_secret, "wb") as f:
            f.write(secrets.token_bytes(32))
        os.chmod(cls.wrong_secret, 0o600)
        cls.token_file = os.path.join(cls.tmp, "token")
        with open(cls.token_file, "w") as f:
            f.write(_upstash_token())
        os.chmod(cls.token_file, 0o600)
        cls.env = dict(os.environ,
                       MUSE_RELAY_URL=URL,
                       MUSE_RELAY_TOKEN_FILE=cls.token_file,
                       MUSE_RELAY_NICK=BOSS,
                       MUSE_RELAY_INSTANCE_ID="e2e-admin",
                       MUSE_RELAY_KEY_PREFIX=PREFIX)

    @classmethod
    def tearDownClass(cls):
        # best-effort cleanup of every scratch key we may have created
        env = dict(cls.env)
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "import json;"
            "[rc.api_get('del/' + k) for k in "
            "[%r + s for s in (':admins', ':admin:secret_sha256',"
            " ':admin:log', ':names', ':names:history')]]"
        ) % (REPO, PREFIX)
        subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, timeout=60)
        for p in (cls.secret_file, cls.wrong_secret, cls.token_file):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(cls.tmp)
        except OSError:
            pass

    def run_admin(self, *argv, nick=None):
        env = dict(self.env)
        if nick is not None:
            env["MUSE_RELAY_NICK"] = nick
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "admin.py"),
             "--secret-file", self.secret_file] + list(argv),
            capture_output=True, text=True, env=env, timeout=60)

    def test_01_init(self):
        r = self.run_admin("init")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK admin initialized", r.stdout)
        r = self.run_admin("admins")
        self.assertIn(BOSS, r.stdout)
        # second init refuses
        r = self.run_admin("init")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already initialized", r.stdout)

    def test_02_verify_clean_log(self):
        r = self.run_admin("verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERIFY OK", r.stdout)

    def test_03_promote_and_demote(self):
        r = self.run_admin("promote", "alice")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_admin("admins")
        self.assertIn("alice", r.stdout)
        r = self.run_admin("demote", "alice")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_admin("admins")
        self.assertNotIn("alice", r.stdout)
        # cannot demote the last admin
        r = self.run_admin("demote", BOSS)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("last admin", r.stdout)

    def test_04_rename_and_whois(self):
        r = self.run_admin("rename", "alice", "Alice Cooper")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_admin("whois", "alice")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Alice Cooper", r.stdout)
        self.assertIn("history", r.stdout)
        r = self.run_admin("whois", "nobody-here")
        self.assertIn("no display name set", r.stdout)

    def test_05_rename_validation(self):
        for bad in ("has:colon", "x" * 33, "   "):
            r = self.run_admin("rename", "bob", bad)
            self.assertNotEqual(r.returncode, 0, f"accepted {bad!r}")
            self.assertIn("ERROR", r.stdout)

    def test_06_non_admin_rejected(self):
        r = self.run_admin("promote", "mallory", nick="intruder-xyz")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not an admin", r.stdout)

    def test_06b_rename_open_to_non_admin(self):
        # rename needs no admin secret: anyone with bus access can set names
        r = self.run_admin("rename", "carol", "Carol Singer", nick="intruder-xyz")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_admin("whois", "carol")
        self.assertIn("Carol Singer", r.stdout)

    def test_07_wrong_secret_rejected(self):
        env = dict(self.env)
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "admin.py"),
             "--secret-file", self.wrong_secret, "promote", "mallory"],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("secret does not match", r.stdout)

    def test_08_tampered_log_detected(self):
        # forge a log entry directly (bypassing the tool) with a bad sig
        code = (
            "import sys, json; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "env={'v':1,'cmd':'promote','args':{'nick':'mallory'},"
            "'actor':'%s','ts':1,'nonce':'00','sig':'forged'};"
            "rc.api_post('rpush/%s:admin:log', json.dumps(env).encode());"
            "print('forged')"
        ) % (REPO, BOSS, PREFIX)
        r = subprocess.run([sys.executable, "-c", code], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("forged", r.stdout)
        r = self.run_admin("verify")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BAD signatures", r.stdout)
        # remove the forged entry: rewrite the log without it
        code = (
            "import sys, json; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "es=json.loads(rc.api_get('lrange/%s:admin:log/0/-1'))['result'];"
            "es=[e for e in es if json.loads(e).get('sig')!='forged'];"
            "rc.api_get('del/%s:admin:log');"
            "[rc.api_post('rpush/%s:admin:log', e.encode()) for e in es];"
            "print('cleaned', len(es))"
        ) % (REPO, PREFIX, PREFIX, PREFIX)
        r = subprocess.run([sys.executable, "-c", code], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("cleaned", r.stdout)
        r = self.run_admin("verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERIFY OK", r.stdout)

    def test_09_display_name_lookup(self):
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "print(rc.display_name('alice')); print(rc.display_name('nobody-here'))"
        ) % REPO
        r = subprocess.run([sys.executable, "-c", code], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "Alice Cooper")
        self.assertEqual(lines[1], "nobody-here")

    def test_10_send_uses_display_name(self):
        # send.py posts with the display name as prefix (real bus, scratch key)
        store_dir = tempfile.mkdtemp(prefix="busadmin-store-")
        self.addCleanup(shutil.rmtree, store_dir, True)
        env = dict(self.env, MUSE_RELAY_NICK="alice",
                   MUSE_RELAY_BUS=f"{PREFIX}:bus",
                   MUSE_RELAY_STORE_DIR=store_dir)
        drv = (
            "import sys; sys.path.insert(0, %r);"
            "import send; send.post_message('hello display', %r)"
        ) % (REPO, f"{PREFIX}:bus")
        r = subprocess.run([sys.executable, "-c", drv], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        chk = (
            "import sys, json; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "print(json.dumps(rc.bus_get(-1, -1, key=%r)))"
        ) % (REPO, f"{PREFIX}:bus")
        r = subprocess.run([sys.executable, "-c", chk], env=env,
                           capture_output=True, text=True, timeout=60)
        tail = json.loads(r.stdout.strip())
        self.assertTrue(tail, "bus is empty")
        self.assertTrue(tail[-1].startswith("Alice Cooper: "),
                        f"unexpected prefix: {tail[-1]!r}")
        # cleanup: scratch bus key + real-namespace side effects of the send
        # (presence set entry, nick claim from warn_nick_conflict)
        cleanup = (
            "import sys; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "rc.api_get('del/%s:bus');"
            "rc.api_get('del/' + rc._nickclaim_key('alice'));"
            "rc.api_get('srem/muse-bus:nicks/alice');"
            "rc.api_get('del/muse-bus:presence:alice');"
            "print('cleaned')"
        ) % (REPO, PREFIX)
        r = subprocess.run([sys.executable, "-c", cleanup], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("cleaned", r.stdout)

    def test_11_rekey(self):
        import hashlib
        new_secret = secrets.token_bytes(32)
        digest = hashlib.sha256(new_secret).hexdigest()
        new_secret_file = os.path.join(self.tmp, "admin_secret_new")
        with open(new_secret_file, "wb") as f:
            f.write(new_secret)
        os.chmod(new_secret_file, 0o600)
        self.addCleanup(lambda: os.path.exists(new_secret_file)
                        and os.unlink(new_secret_file))
        # rotate with the CURRENT (old) secret
        r = self.run_admin("rekey", digest)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK admin secret rotated", r.stdout)
        # old secret is now rejected
        r = self.run_admin("promote", "mallory")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("secret does not match", r.stdout)
        # new secret works for a mutating command
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "admin.py"),
             "--secret-file", new_secret_file, "promote", "rekeyed-admin"],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK 'rekeyed-admin' promoted", r.stdout)
        # verify with the NEW secret: post-rekey entries check out, the
        # pre-rekey ones are reported sealed (not failures)
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "admin.py"),
             "--secret-file", new_secret_file, "verify"],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERIFY OK", r.stdout)
        self.assertIn("sealed under a previous secret", r.stdout)
        # a forged entry AFTER the rekey is still detected with the new secret
        code = (
            "import sys, json; sys.path.insert(0, %r);"
            "import relay_common as rc;"
            "env={'v':1,'cmd':'promote','args':{'nick':'mallory'},"
            "'actor':'%s','ts':1,'nonce':'00','sig':'forged'};"
            "rc.api_post('rpush/%s:admin:log', json.dumps(env).encode());"
            "print('forged')"
        ) % (REPO, BOSS, PREFIX)
        r = subprocess.run([sys.executable, "-c", code], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("forged", r.stdout)
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "admin.py"),
             "--secret-file", new_secret_file, "verify"],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("VERIFY FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
