#!/usr/bin/env python3
"""files-only: the branch stayed inside the file set it claimed.

--files stops two jobs claiming the same file; files-only is the other
half of the contract — it fails the job when the branch diff touches
anything outside the declared set. It measures <base>...HEAD, so it
needs a base commit, and when it cannot find one it FAILS with the
reason instead of passing by default.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase, make_git_repo
from mos import acceptance


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)
    return p.stdout


class ParseTest(unittest.TestCase):
    def test_files_only_parses(self):
        self.assertEqual(acceptance.parse_accept("files-only: a.py,b.py"),
                         [("files-only", "a.py,b.py")])

    def test_files_only_needs_a_file_list(self):
        with self.assertRaises(ValueError):
            acceptance.parse_accept("files-only:   ")


class FilesOnlyTest(RelayTestCase):
    """Real git repos: the check shells out to git, so it is tested for real."""

    def _repo_changing(self, changed, name="feature"):
        """A repo on main plus a branch that touched `changed`."""
        path = os.path.join(self.tmp, "repo-" + name)
        make_git_repo(path, branch=None)
        _git(path, "checkout", "-b", name)
        for rel in changed:
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("x\n")
        _git(path, "add", ".")
        _git(path, "commit", "-m", "worker change")
        return path

    def _base(self, repo):
        return _git(repo, "rev-parse", "main").strip()

    def test_declared_files_pass(self):
        repo = self._repo_changing(["a.py", "mos/b.py"])
        ok, results = acceptance.verify(
            "files-only: a.py,mos/b.py", repo, base=self._base(repo))
        self.assertTrue(ok, results)

    def test_one_undeclared_file_fails(self):
        repo = self._repo_changing(["a.py", "surprise.py"])
        ok, results = acceptance.verify(
            "files-only: a.py", repo, base=self._base(repo))
        self.assertFalse(ok)
        self.assertIn("surprise.py", results[0]["detail"])
        self.assertIn("outside the declared file set", results[0]["detail"])

    def test_globs_are_honoured(self):
        repo = self._repo_changing(["mos/one.py", "mos/two.py"])
        ok, results = acceptance.verify(
            "files-only: mos/*.py", repo, base=self._base(repo))
        self.assertTrue(ok, results)
        ok, _ = acceptance.verify(
            "files-only: mos/one.py", repo, base=self._base(repo))
        self.assertFalse(ok)

    def test_the_base_can_be_found_without_being_given(self):
        # No explicit base: main is present, so the merge-base is used.
        repo = self._repo_changing(["a.py"])
        ok, results = acceptance.verify("files-only: a.py", repo)
        self.assertTrue(ok, results)

    def test_a_bad_base_fails_loudly(self):
        repo = self._repo_changing(["a.py"])
        ok, results = acceptance.verify("files-only: a.py", repo,
                                        base="deadbeefdeadbeef")
        self.assertFalse(ok)
        self.assertIn("not found", results[0]["detail"])

    def test_no_base_at_all_fails_instead_of_passing(self):
        # A repo with no main/master/origin to measure against: the
        # check must say so rather than wave the job through.
        path = os.path.join(self.tmp, "solo")
        os.makedirs(path)
        _git(path, "init", "-b", "solo")
        _git(path, "config", "user.email", "t@t.t")
        _git(path, "config", "user.name", "t")
        with open(os.path.join(path, "a.py"), "w") as f:
            f.write("x\n")
        _git(path, "add", ".")
        _git(path, "commit", "-m", "only commit")
        ok, results = acceptance.verify("files-only: a.py", path)
        self.assertFalse(ok)
        self.assertIn("needs a base commit", results[0]["detail"])

    def test_it_fails_outside_a_git_repo(self):
        ok, results = acceptance.verify("files-only: a.py", self.tmp)
        self.assertFalse(ok)
        self.assertIn("needs a base commit", results[0]["detail"])

    def test_other_checks_still_run_alongside_it(self):
        repo = self._repo_changing(["a.py"])
        ok, results = acceptance.verify(
            "file-exists: a.py\nfiles-only: a.py", repo, base=self._base(repo))
        self.assertTrue(ok, results)
        self.assertEqual(len(results), 2)

    def test_the_report_names_the_check(self):
        repo = self._repo_changing(["extra.py"])
        ok, results = acceptance.verify("files-only: a.py", repo,
                                        base=self._base(repo))
        self.assertFalse(ok)
        report = acceptance.format_report(results)
        self.assertIn("[FAIL] files-only: a.py", report)


if __name__ == "__main__":
    unittest.main()
