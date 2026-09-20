#!/usr/bin/env python3
"""Interop test: the crew-board's JS entry builders must match admin.py.

Renames are keyless now (no admin secret): the board writes the name
registry entry, the history entry, and the audit-log entry directly.
This test extracts the pure builder block from crew-board.html (delimited by
/*__SIGNING_START__*/ / /*__SIGNING_END__*/), runs it in Node with fixed
vectors, and checks every output against Python's json.dumps and admin.py's
keyless verify contract.

No mocks: real Node JS vs real Python. If the two sides ever drift, a rename
written in the browser would not match what admin.py expects.
"""
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
const out = {};
out.pystr = %s.map(pyStr);
out.nonce = randHexBytes(8);
out.log = renameLogEntry("Mrfentmen", "alice", "Alice Cooper", 1758390000, "0123456789abcdef");
out.hash = namesHashEntry("Mrfentmen", "Alice Cooper", 1758390000);
out.hist = namesHistoryEntry("Mrfentmen", "alice", "Alice Cooper", 1758390000);
console.log(JSON.stringify(out));
"""


def _builder_block():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"/\*__SIGNING_START__\*/(.*?)/\*__SIGNING_END__\*/",
                  html, re.S)
    assert m, "builder block markers missing from crew-board.html"
    return m.group(1)


def _run_node(block):
    driver = block + "\n" + NODE_DRIVER % json.dumps(PYSTR_CASES)
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as f:
        f.write(driver)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           timeout=60)
    finally:
        os.unlink(path)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout)


class KeylessInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _run_node(_builder_block())

    def test_pystr_matches_json_dumps(self):
        for case, got in zip(PYSTR_CASES, self.js["pystr"]):
            self.assertEqual(got, json.dumps(case, ensure_ascii=True),
                             f"pyStr mismatch for {case!r}")

    def test_nonce_shape(self):
        self.assertRegex(self.js["nonce"], r"^[0-9a-f]{16}$")

    def test_log_entry_is_valid_json(self):
        env = json.loads(self.js["log"])
        self.assertEqual(env["v"], 1)
        self.assertEqual(env["cmd"], "rename")
        self.assertEqual(env["args"],
                         {"nick": "alice", "display": "Alice Cooper"})
        self.assertEqual(env["actor"], "Mrfentmen")

    def test_log_entry_is_keyless(self):
        env = json.loads(self.js["log"])
        self.assertIsNone(env.get("sig"))

    def test_keyless_entry_passes_admin_verify(self):
        env = json.loads(self.js["log"])
        # None = keyless entry, no signature to check (not a failure)
        self.assertIsNone(admin._verify_envelope(None, env))

    def test_names_hash_entry_shape(self):
        h = json.loads(self.js["hash"])
        self.assertEqual(h, {"display": "Alice Cooper",
                             "set_by": "Mrfentmen", "set_at": 1758390000})

    def test_history_entry_shape(self):
        h = json.loads(self.js["hist"])
        self.assertEqual(h, {"nick": "alice", "display": "Alice Cooper",
                             "set_by": "Mrfentmen", "set_at": 1758390000})


if __name__ == "__main__":
    unittest.main(verbosity=2)
