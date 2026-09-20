"""Unit tests for mos/acceptance.py — acceptance criteria parsing + running."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
from mos import acceptance


class ParseTest(unittest.TestCase):
    def test_parse_all_kinds(self):
        checks = acceptance.parse_accept(
            "file-exists: a/b.py\n"
            "tests-pass: tests.test_x\n"
            "command-ok: echo hi\n"
            "contains: a/b.py :: needle\n")
        self.assertEqual(checks, [
            ("file-exists", "a/b.py"),
            ("tests-pass", "tests.test_x"),
            ("command-ok", "echo hi"),
            ("contains", "a/b.py :: needle"),
        ])

    def test_comments_and_blanks_ignored(self):
        self.assertEqual(
            acceptance.parse_accept("# comment\n\n  \nfile-exists: x\n"), [
                ("file-exists", "x")])

    def test_bad_lines_rejected(self):
        for bad in ["no separator here", "bogus-check: x",
                    "file-exists:   ", "contains: path-only"]:
            with self.assertRaises(ValueError, msg=bad):
                acceptance.parse_accept(bad)

    def test_empty_is_no_checks(self):
        self.assertEqual(acceptance.parse_accept(""), [])
        self.assertEqual(acceptance.parse_accept("  \n"), [])


class VerifyTest(RelayTestCase):
    def test_empty_accept_passes(self):
        ok, results = acceptance.verify("", self.tmp)
        self.assertTrue(ok)

    def test_parse_error_fails(self):
        ok, results = acceptance.verify("bogus-check: x", self.tmp)
        self.assertFalse(ok)
        self.assertIn("unknown check", results[0]["detail"])

    def test_file_exists(self):
        p = os.path.join(self.tmp, "out.txt")
        open(p, "w").write("hi")
        ok, _ = acceptance.verify("file-exists: out.txt", self.tmp)
        self.assertTrue(ok)
        ok, results = acceptance.verify("file-exists: nope.txt", self.tmp)
        self.assertFalse(ok)
        self.assertIn("missing", results[0]["detail"])

    def test_contains(self):
        p = os.path.join(self.tmp, "f.py")
        open(p, "w").write("def hello(): pass\n")
        ok, _ = acceptance.verify("contains: f.py :: def hello", self.tmp)
        self.assertTrue(ok)
        ok, _ = acceptance.verify("contains: f.py :: def goodbye", self.tmp)
        self.assertFalse(ok)

    def test_command_ok(self):
        ok, _ = acceptance.verify("command-ok: echo hello", self.tmp)
        self.assertTrue(ok)
        ok, results = acceptance.verify("command-ok: exit 3", self.tmp)
        self.assertFalse(ok)
        self.assertIn("exit=3", results[0]["detail"])

    def test_tests_pass_with_real_module(self):
        samp = os.path.join(self.tmp, "samp")
        os.makedirs(os.path.join(samp, "tests"))
        open(os.path.join(samp, "__init__.py"), "w").write("")
        open(os.path.join(samp, "tests", "__init__.py"), "w").write("")
        open(os.path.join(samp, "tests", "test_ok.py"), "w").write(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_yes(self):\n"
            "        self.assertTrue(True)\n")
        ok, results = acceptance.verify(
            "tests-pass: samp.tests.test_ok", self.tmp)
        self.assertTrue(ok, results)
        open(os.path.join(samp, "tests", "test_ok.py"), "w").write(
            "import unittest\n"
            "# now failing on purpose\n"
            "class T(unittest.TestCase):\n"
            "    def test_no(self):\n"
            "        self.assertTrue(False)\n")
        ok, _ = acceptance.verify("tests-pass: samp/tests/test_ok.py",
                                  self.tmp)
        self.assertFalse(ok)

    def test_format_report(self):
        ok, results = acceptance.verify(
            "file-exists: a\nfile-exists: b", self.tmp)
        self.assertFalse(ok)
        report = acceptance.format_report(results)
        self.assertIn("[FAIL] file-exists: a", report)


if __name__ == "__main__":
    unittest.main()
