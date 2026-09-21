#!/usr/bin/env python3
"""The web viewer's read cursor must be trim-aware.

bus.html polls the room list with an index, and LTRIM shifts every index
down — the same silent-loss bug relay_common.bus_read fixes for the
Python tools. relay_common's fix is covered by unit tests; this one
covers the browser copy by extracting the REAL readWindow() out of
bus.html and running it under node against a stubbed Upstash API. No
re-implementation, so the test cannot drift from what ships.

Needs node; skipped (not failed) where it is absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUS_HTML = os.path.join(REPO, "bus.html")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/async function readWindow\(\)[\s\S]*?\n\}/);
if (!m) { console.log(JSON.stringify({error: 'readWindow not found'})); process.exit(0); }
let seen = 0, store = {llen: 0, total: null, msgs: []};
function busKey() { return 'muse-bus'; }
async function api(p) {
  if (p.startsWith('/get/')) return {result: store.total == null ? null : String(store.total)};
  if (p.startsWith('/llen/')) return {result: store.llen};
  const mm = p.match(/^\/lrange\/[^/]+\/(\d+)\/-1$/);
  if (!mm) throw new Error('unexpected path ' + p);
  // Real Redis LRANGE: a position within the list, clamped at the end.
  return {result: store.msgs.slice(parseInt(mm[1], 10))};
}
eval(m[0]);

const cases = [
  // name, store, seen
  ['fresh tab', {llen: 3, total: 3, msgs: ['a','b','c']}, 0],
  ['caught up', {llen: 3, total: 3, msgs: ['a','b','c']}, 3],
  ['outrun by a trim', {llen: 3, total: 10, msgs: ['h','i','j']}, 2],
  ['inside the window', {llen: 3, total: 10, msgs: ['h','i','j']}, 8],
  ['at the very end', {llen: 3, total: 10, msgs: ['h','i','j']}, 10],
  ['no counter', {llen: 2, total: null, msgs: ['x','y']}, 0],
  ['stale counter', {llen: 2, total: 1, msgs: ['x','y']}, 0],
  ['empty bus', {llen: 0, total: 0, msgs: []}, 0],
  ['cursor ahead of total', {llen: 3, total: 10, msgs: ['h','i','j']}, 12]
];

(async () => {
  const out = {};
  for (const [name, st, s] of cases) {
    store = st; seen = s;
    try {
      const w = await readWindow();
      out[name] = {msgs: w.msgs, start: w.start, cursor: w.cursor, lost: w.lost};
    } catch (e) { out[name] = {error: String(e)}; }
  }
  console.log(JSON.stringify(out));
})();
"""

EXPECTED = {
    "fresh tab": {"msgs": ["a", "b", "c"], "start": 0, "cursor": 3, "lost": 0},
    "caught up": {"msgs": [], "start": 3, "cursor": 3, "lost": 0},
    # read 2 of 10, cap 3: 5 messages were trimmed before this read; the
    # three that survived are still delivered, and the cursor jumps to 10
    # so the tab never replays or re-skips them.
    "outrun by a trim": {"msgs": ["h", "i", "j"], "start": 7,
                         "cursor": 10, "lost": 5},
    "inside the window": {"msgs": ["i", "j"], "start": 8, "cursor": 10,
                          "lost": 0},
    "at the very end": {"msgs": [], "start": 10, "cursor": 10, "lost": 0},
    "no counter": {"msgs": ["x", "y"], "start": 0, "cursor": 2, "lost": 0},
    "stale counter": {"msgs": ["x", "y"], "start": 0, "cursor": 2, "lost": 0},
    "empty bus": {"msgs": [], "start": 0, "cursor": 0, "lost": 0},
    # never rewinds a cursor, even if the counter is nonsense
    "cursor ahead of total": {"msgs": [], "start": 12, "cursor": 12,
                              "lost": 0},
}


def _run_harness():
    tmp = tempfile.mkdtemp(prefix="bus-html-")
    path = os.path.join(tmp, "harness.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HARNESS)
    p = subprocess.run([NODE, path, BUS_HTML], capture_output=True,
                       text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"node harness failed: {p.stderr[-800:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node not installed (bus.html check needs it)")
class ViewerCursorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = _run_harness()

    def test_readwindow_is_present_and_runnable(self):
        self.assertNotIn("error", self.results,
                         f"could not extract readWindow: {self.results}")

    def test_every_case_matches(self):
        for name, want in EXPECTED.items():
            with self.subTest(case=name):
                self.assertEqual(self.results.get(name), want)

    def test_a_trim_is_never_silent(self):
        # The whole point: the outrun case must both deliver the
        # survivors and report the loss, rather than return nothing.
        got = self.results["outrun by a trim"]
        self.assertEqual(got["msgs"], ["h", "i", "j"])
        self.assertGreater(got["lost"], 0)


if __name__ == "__main__":
    unittest.main()
