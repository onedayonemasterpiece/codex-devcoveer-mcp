"""Stdlib regressions: real Git worktrees and the real comparison implementation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import direct_ops2_worktrees as wt


class StaleWorktreeRegression(unittest.TestCase):
    def git(self, *args):
        env = os.environ.copy()
        env.update(GIT_AUTHOR_NAME="Regression", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="Regression", GIT_COMMITTER_EMAIL="test@example.invalid")
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], env=env, stderr=subprocess.PIPE,
            text=True,
        ).strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("base\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.target = self.root / "target"
        self.stale = self.root / "stale"
        for path in (self.target, self.stale):
            self.git("worktree", "add", "-q", "--detach", str(path), "HEAD")
        # Deliberately retain Git registration to reproduce a prunable entry.
        shutil.rmtree(self.stale)
        self.registry_before = self.git("worktree", "list", "--porcelain")
        self.assertIn("prunable", self.registry_before)

    def test_existing_target_compares_with_unrelated_missing_worktree(self):
        (self.target / "a.txt").write_text("changed\n")
        (self.target / "new.txt").write_text("new\n")
        result = asyncio.run(wt.worktree_compare(self.repo, {
            "worktree": str(self.target), "target_ref": self.base,
            "include_untracked": True, "limit": 100,
        }))
        self.assertEqual(result["worktree"], str(self.target))
        entries = {entry["path"]: entry for entry in result["entries"]}
        self.assertEqual(entries["a.txt"]["classification"], "modified_local")
        self.assertIn("new.txt", entries)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.registry_before)
        self.assertEqual((self.target / "a.txt").read_text(), "changed\n")

    def test_missing_requested_target_remains_not_found(self):
        with self.assertRaises(wt.ProbeError) as caught:
            asyncio.run(wt.resolve_repo_worktree(self.repo, str(self.stale)))
        self.assertEqual(caught.exception.category, "not_found")

    def test_unregistered_existing_path_and_symlink_remain_denied(self):
        other = self.root / "other"
        other.mkdir()
        link = self.root / "other-link"
        link.symlink_to(other, target_is_directory=True)
        for requested in (other, link):
            with self.subTest(requested=requested):
                with self.assertRaises(wt.ProbeError) as caught:
                    asyncio.run(wt.resolve_repo_worktree(self.repo, str(requested)))
                self.assertEqual(caught.exception.category, "worktree_denied")

    def test_canonical_external_only_guard_remains(self):
        with self.assertRaises(wt.ProbeError) as caught:
            asyncio.run(wt.resolve_repo_worktree(self.repo, str(self.repo), external_only=True))
        self.assertEqual(caught.exception.category, "canonical_denied")

    def test_permission_error_is_not_swallowed_as_missing(self):
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == self.stale:
                raise PermissionError("simulated inaccessible registration")
            return original(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            with self.assertRaises(PermissionError):
                asyncio.run(wt.resolve_repo_worktree(self.repo, str(self.target)))


if __name__ == "__main__":
    unittest.main()
