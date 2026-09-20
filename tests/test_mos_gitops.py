"""Unit tests for mos/gitops.py — safe Git review/merge."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RelayTestCase
from mos import gitops


def _git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _make_repo(path):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("v1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    _git(path, "checkout", "-b", "worker/thing")
    with open(os.path.join(path, "b.txt"), "w") as f:
        f.write("new\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "worker commit")
    _git(path, "checkout", "main")
    return path


class GitopsTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.repo = _make_repo(os.path.join(self.tmp, "repo"))

    def test_review_branch(self):
        r = gitops.review_branch(self.repo, "worker/thing")
        self.assertEqual(r["branch"], "worker/thing")
        self.assertEqual(r["commit_count"], 1)
        self.assertIn("b.txt", r["files"])
        self.assertTrue(r["stat"])

    def test_review_rejects_self(self):
        with self.assertRaises(gitops.GitError):
            gitops.review_branch(self.repo, "main", base="main")

    def test_review_unknown_branch(self):
        with self.assertRaises(gitops.GitError):
            gitops.review_branch(self.repo, "nope")

    def test_approve_merge(self):
        h = gitops.approve_merge(self.repo, "worker/thing")
        self.assertRegex(h, r"^[0-9a-f]{40}$")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")))

    def test_merge_refuses_dirty_tree(self):
        with open(os.path.join(self.repo, "dirty.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(gitops.GitError):
            gitops.approve_merge(self.repo, "worker/thing")

    def test_merge_refuses_self(self):
        with self.assertRaises(gitops.GitError):
            gitops.approve_merge(self.repo, "main", base="main")

    def test_reject_branch(self):
        s = gitops.reject_branch("worker/thing", "tests fail")
        self.assertIn("REJECTED worker/thing", s)
        with self.assertRaises(ValueError):
            gitops.reject_branch("worker/thing", "  ")

    def test_worktree_add_remove(self):
        wt = os.path.join(self.tmp, "wt")
        gitops.worktree_add(self.repo, "worker/thing", wt)
        self.assertTrue(os.path.exists(os.path.join(wt, "b.txt")))
        gitops.worktree_remove(self.repo, wt)
        self.assertFalse(os.path.exists(wt))

    def test_not_a_repo(self):
        with self.assertRaises(gitops.GitError):
            gitops.review_branch(self.tmp, "worker/thing")


if __name__ == "__main__":
    unittest.main()
