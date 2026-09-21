#!/usr/bin/env python3
"""The crew boards must have a column for every job status.

`render()` walks its status buckets and looks up `#col-<status>` for
each one. A status with a bucket but no column throws mid-render; a
status with neither silently drops the job off the board — and a
dead-lettered job vanishing from the board is precisely the silence the
attempt budget exists to break. This test pins the two lists together.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARDS = ("crew-board.html", "crew-board-qa.html", "crew-board-qa2.html")

_COLS_OBJ = re.compile(r"const cols = \{(.*?)\};", re.S)


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class BoardStatusTest(unittest.TestCase):
    def test_every_board_exists(self):
        for name in BOARDS:
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), name)

    def test_columns_and_buckets_match(self):
        for name in BOARDS:
            html = _read(name)
            columns = set(re.findall(r'id="col-([a-z]+)"', html))
            m = _COLS_OBJ.search(html)
            self.assertIsNotNone(m, f"{name}: no cols object found")
            buckets = set(re.findall(r"(\w+): \[\]", m.group(1)))
            self.assertEqual(columns, buckets,
                             f"{name}: columns {sorted(columns)} but buckets "
                             f"{sorted(buckets)}")

    def test_dead_lettered_jobs_have_a_home(self):
        for name in BOARDS:
            html = _read(name)
            self.assertIn('id="col-dead"', html, name)
            self.assertIn("dead: []", html, name)

    def test_the_render_fallback_still_covers_unknown_statuses(self):
        # An unknown status must not throw: it falls back to the open
        # column rather than crashing the whole board.
        for name in BOARDS:
            self.assertIn("cols[st] || cols.open", _read(name), name)


if __name__ == "__main__":
    unittest.main()
