#!/usr/bin/env python3
"""Interop test: the crew-board's JS signing code must match admin.py exactly.

Extracts the pure signing block from crew-board.html (delimited by
/*__SIGNING_START__*/ / /*__SIGNING_END__*/), runs it in Node with fixed
vectors, and compares every output against admin.py's own
_canonical_envelope/_sign plus Python's json.dumps(ensure_ascii=True).

No mocks: real Node crypto.subtle HMAC-SHA256 vs real Python hmac. If the
two sides ever drift, a rename signed in the browser would fail admin.py's
verify — this test pins the contract on both sides.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import admin  # noqa: E402 — the contract owner

HTML = os.path.join(REPO, "crew-board.html")
SECRET = bytes(range(32))
SECRET_HEX = SECRET.hex()

PYSTR_CASES = [
    "plain",
    'quote " and backslash \\',
    "line1\nline2\ttab\rcarriage",
    "caf\u00e9",
    "emoji \U0001F600",
    "del\x7f",
    "",
    "a" * 32,
]

NODE_DRIVER = """\
const SECRET_HEX = process.env.FIXTURE_SECRET_HEX;
const out = {};
out.canonical = canonicalRenameEnvelope(
  "del", "alice", "Alice Cooper", 1758390000, "0123456789abcdef");
out.pystr = %s.map(pyStr);
(async () => {
  out.sig = await hmacHex(SECRET_HEX, out.canonical);
  out.sha = await sha256Hex(hexToBytes(SECRET_HEX));
  out.nonce = randHexBytes(8);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(String(e)); process.exit(1); });
"""


def _signing_block():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"/\*__SIGNING_START__\*/(.*?)/\*__SIGNING_END__\*/",
                  html, re.S)
    assert m, "signing block markers missing from crew-board.html"
    return m.group(1)


def _run_node(block):
    driver = block + "\n" + NODE_DRIVER % json.dumps(PYSTR_CASES)
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as f:
        f.write(driver)
        path = f.name
    try:
        env = dict(os.environ, FIXTURE_SECRET_HEX=SECRET_HEX)
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           env=env, timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


class SigningInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _run_node(_signing_block())

    def test_canonical_envelope_matches(self):
        env = {"v": 1, "cmd": "rename",
               "args": {"nick": "alice", "display": "Alice Cooper"},
               "actor": "del", "ts": 1758390000,
               "nonce": "0123456789abcdef"}
        expected = admin._canonical_envelope(env).decode()
        self.assertEqual(self.js["canonical"], expected)

    def test_hmac_signature_matches(self):
        env = {"v": 1, "cmd": "rename",
               "args": {"nick": "alice", "display": "Alice Cooper"},
               "actor": "del", "ts": 1758390000,
               "nonce": "0123456789abcdef"}
        self.assertEqual(self.js["sig"], admin._sign(SECRET, env))

    def test_pystr_matches_json_dumps(self):
        for case, got in zip(PYSTR_CASES, self.js["pystr"]):
            self.assertEqual(got, json.dumps(case, ensure_ascii=True),
                             f"pyStr mismatch for {case!r}")

    def test_sha256_matches(self):
        self.assertEqual(self.js["sha"], hashlib.sha256(SECRET).hexdigest())

    def test_nonce_shape(self):
        self.assertRegex(self.js["nonce"], r"^[0-9a-f]{16}$")

    def test_verify_round_trip(self):
        # the exact log entry the page would write must pass admin._verify_envelope
        env = {"v": 1, "cmd": "rename",
               "args": {"nick": "alice", "display": "Alice Cooper"},
               "actor": "del", "ts": 1758390000,
               "nonce": "0123456789abcdef", "sig": self.js["sig"]}
        self.assertTrue(admin._verify_envelope(SECRET, env))


if __name__ == "__main__":
    unittest.main(verbosity=2)
