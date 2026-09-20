"""export.py: room history export to markdown / JSON."""
import json
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from helpers import RelayTestCase  # noqa: E402
import store  # noqa: E402
import export  # noqa: E402


class ExportTest(RelayTestCase):
    def _seed(self, room, entries):
        path = store.store_path(room)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _entries(self):
        return [
            {"id": "news-1", "room": "news", "nick": "alice",
             "text": "hello world", "ts": 1700000000,
             "edited": False, "edit_history": []},
            {"id": "news-2", "room": "news", "nick": "bob",
             "text": "line one\nline two", "ts": 1700000060,
             "edited": True, "edit_history": ["old text"]},
        ]

    def test_md_export(self):
        self._seed("news", self._entries())
        rc_, out, err = self.run_cli(
            export.main, ["--room", "news", "--format", "md"])
        self.assertEqual(rc_, 0, err)
        self.assertIn("## alice (", out)
        self.assertIn("hello world", out)
        self.assertIn("(edited)", out)
        self.assertIn("line one\nline two", out)

    def test_json_export(self):
        self._seed("news", self._entries())
        rc_, out, err = self.run_cli(
            export.main, ["--room", "news", "--format", "json"])
        self.assertEqual(rc_, 0, err)
        objs = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[0]["nick"], "alice")
        self.assertEqual(objs[0]["text"], "hello world")
        self.assertEqual(objs[0]["ts"], 1700000000)
        self.assertNotIn("edited", objs[0])
        self.assertTrue(objs[1]["edited"])

    def test_out_file(self):
        self._seed("news", self._entries())
        dest = os.path.join(self.tmp, "news.md")
        rc_, out, err = self.run_cli(
            export.main, ["--room", "news", "--out", dest])
        self.assertEqual(rc_, 0, err)
        self.assertEqual(out, "")
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("## alice (", content)

    def test_empty_room_exports_cleanly(self):
        rc_, out, err = self.run_cli(
            export.main, ["--room", "nope", "--format", "json"])
        self.assertEqual(rc_, 0, err)
        self.assertEqual(out.strip(), "")

    def test_bad_format_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(export.main, ["--room", "news", "--format", "xml"])
        self.assertEqual(cm.exception.code, 2)

    def test_skips_bad_lines(self):
        path = store.store_path("news")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._entries()[0]) + "\n")
            f.write("this is not json\n")
            f.write("\n")
        rc_, out, err = self.run_cli(
            export.main, ["--room", "news", "--format", "json"])
        self.assertEqual(rc_, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("skipping bad line", err)

    def test_large_room_streams(self):
        entries = [
            {"id": f"big-{i}", "room": "big", "nick": "u",
             "text": f"msg {i}", "ts": 1700000000 + i,
             "edited": False, "edit_history": []}
            for i in range(5000)
        ]
        self._seed("big", entries)
        rc_, out, err = self.run_cli(
            export.main, ["--room", "big", "--format", "json"])
        self.assertEqual(rc_, 0, err)
        self.assertEqual(len(out.splitlines()), 5000)


if __name__ == "__main__":
    import unittest
    unittest.main()
