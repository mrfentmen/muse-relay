"""Unit tests for mos/capabilities.py — the capability registry."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
from mos import capabilities


class RegistryTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        capabilities.CAPNS = "muse-bus:tcap"

    def test_register_and_get(self):
        capabilities.register("w1", ["Python", " testing "], roles="builder",
                              max_jobs=2, dm_secret="s3cret", info="x")
        rec = capabilities.get("w1")
        self.assertEqual(rec["caps"], "python,testing")
        self.assertEqual(rec["roles"], "builder")
        self.assertEqual(rec["max_jobs"], "2")
        self.assertEqual(rec["dm"], "s3cret")
        self.assertEqual(rec["host"], "host-a")

    def test_register_requires_caps(self):
        with self.assertRaises(ValueError):
            capabilities.register("w1", [])
        with self.assertRaises(ValueError):
            capabilities.register("", ["python"])

    def test_touch_unknown_returns_false(self):
        self.assertFalse(capabilities.touch("ghost"))

    def test_touch_refreshes(self):
        capabilities.register("w1", ["python"])
        self.assertTrue(capabilities.touch("w1"))

    def test_unregister(self):
        capabilities.register("w1", ["python"])
        capabilities.unregister("w1")
        self.assertIsNone(capabilities.get("w1"))
        self.assertEqual(capabilities.agents(), {})

    def test_find_for_matches_superset(self):
        capabilities.register("w1", ["python", "testing"])
        capabilities.register("w2", ["python"])
        capabilities.register("w3", ["docs"])
        self.assertEqual(capabilities.find_for(["python"]), ["w1", "w2"])
        self.assertEqual(capabilities.find_for(["python", "testing"]), ["w1"])
        self.assertEqual(capabilities.find_for(["rust"]), [])
        self.assertEqual(
            sorted(capabilities.find_for([])), ["w1", "w2", "w3"])

    def test_find_for_respects_capacity(self):
        capabilities.register("w1", ["python"], max_jobs=1)
        self.set_nick("overseer", "host-o")
        self.run_cli(__import__("jobs").main,
                     ["post", "--title", "T", "--spec", "s"])
        self.set_nick("w1", "host-w")
        rc, _, _ = self.run_cli(__import__("jobs").main, ["claim", "1"])
        self.assertEqual(rc, 0)
        # w1 is at capacity: no longer assignable
        self.assertEqual(capabilities.find_for(["python"]), [])

    def test_find_for_skips_stale(self):
        capabilities.register("w1", ["python"])
        # age the heartbeat past STALE_AFTER
        import relay_common as rc
        rc.api_get("hset/muse-bus:tcap:w1/updated/1")
        self.assertEqual(capabilities.find_for(["python"]), [])
        nicks = capabilities.prune_stale()
        self.assertEqual(nicks, ["w1"])
        self.assertIsNone(capabilities.get("w1"))

    def test_prune_keeps_fresh(self):
        capabilities.register("w1", ["python"])
        capabilities.register("w2", ["python"])
        self.assertEqual(capabilities.prune_stale(), [])
        self.assertEqual(len(capabilities.agents()), 2)


if __name__ == "__main__":
    unittest.main()
