#!/usr/bin/env python3
"""bus-tools parity: the live skill scripts must be byte-identical to the
vendored copies in bus-tools/.

The repo is the versioned source of truth for bus operator tooling; the
skill bin holds the live runtime copies. If someone edits one side without
the other, this test fails loudly. Real file comparison — no mocks.
"""
import hashlib
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_BIN = os.path.expanduser("~/workspace/skills/muse-bus-relay/bin")
PAIRS = [
    ("bus-tools/bus-send", "bus-send"),
    ("bus-tools/bus-poll", "bus-poll"),
    ("bus-tools/bus-rooms", "bus-rooms"),
]


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ParityTest(unittest.TestCase):
    def test_live_matches_vendored(self):
        for repo_rel, skill_name in PAIRS:
            repo_p = os.path.join(REPO, repo_rel)
            skill_p = os.path.join(SKILL_BIN, skill_name)
            self.assertTrue(os.path.exists(skill_p),
                            f"live skill script missing: {skill_p}")
            self.assertEqual(
                _sha(repo_p), _sha(skill_p),
                f"drift: {repo_rel} != skill bin/{skill_name} "
                f"(edit one, copy to the other, commit)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
