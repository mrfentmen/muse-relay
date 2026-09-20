#!/usr/bin/env python3
"""Safe Git integration for MOS: review, approve/merge, reject.

Workers never push to main. They commit on their own branch; the
overseer reviews the diff and merges. Every mutating operation has
guards: refuse to merge main into itself, refuse on a dirty tree,
refuse unknown branches. Nothing here force-pushes, ever.
"""
import os
import subprocess


class GitError(RuntimeError):
    pass


def _git(repo, *args, timeout=120):
    try:
        p = subprocess.run(
            ["git", "-C", repo] + list(args), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise GitError("git not found on PATH")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out")
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: "
                       f"{p.stderr.strip()[-500:]}")
    return p.stdout


def is_repo(path):
    try:
        _git(path, "rev-parse", "--git-dir")
        return True
    except GitError:
        return False


def current_branch(repo):
    return _git(repo, "branch", "--show-current").strip()


def clean_tree(repo):
    return _git(repo, "status", "--porcelain").strip() == ""


def branch_exists(repo, branch):
    try:
        _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        return True
    except GitError:
        return False


def review_branch(repo, branch, base="main"):
    """Return a review summary dict for branch vs base."""
    if not is_repo(repo):
        raise GitError(f"not a git repo: {repo}")
    if branch == base:
        raise GitError("refusing to review base against itself")
    for b in (branch, base):
        if not branch_exists(repo, b):
            raise GitError(f"unknown branch: {b}")
    stat = _git(repo, "diff", "--stat", f"{base}...{branch}")
    files = _git(repo, "diff", "--name-only", f"{base}...{branch}").split()
    commits = _git(repo, "log", "--format=%h %s",
                   f"{base}..{branch}").splitlines()
    return {
        "branch": branch, "base": base,
        "files": files, "stat": stat.strip(),
        "commits": commits, "commit_count": len(commits),
    }


def approve_merge(repo, branch, base="main"):
    """Merge branch into base. Returns the merge commit hash.

    Guards: branch != base, both exist, tree clean, base checked out.
    Uses --no-ff so worker work is always visible as a merge.
    """
    if branch == base:
        raise GitError("refusing to merge base into itself")
    if not clean_tree(repo):
        raise GitError("working tree dirty — refusing to merge")
    for b in (branch, base):
        if not branch_exists(repo, b):
            raise GitError(f"unknown branch: {b}")
    if current_branch(repo) != base:
        _git(repo, "checkout", base)
    _git(repo, "merge", "--no-ff", branch,
         "-m", f"Merge {branch} (MOS approved)")
    return _git(repo, "rev-parse", "HEAD").strip()


def reject_branch(branch, reason):
    """Record a rejection. Does not delete anything — the branch stays
    for the worker to fix. Returns a human-readable summary."""
    if not reason or not reason.strip():
        raise ValueError("rejection needs a reason")
    return f"REJECTED {branch}: {reason.strip()}"


def worktree_add(repo, branch, path):
    """Check out branch into a detached worktree at path. Returns path."""
    if os.path.exists(path):
        raise GitError(f"worktree path already exists: {path}")
    if not branch_exists(repo, branch):
        raise GitError(f"unknown branch: {branch}")
    _git(repo, "worktree", "add", "--detach", path, branch)
    return path


def worktree_remove(repo, path):
    _git(repo, "worktree", "remove", "--force", path)
