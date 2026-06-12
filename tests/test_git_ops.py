"""
tests/test_git_ops.py

git_ops.py 单元测试。
用临时目录 + git init 创建独立 repo, 不会污染真实仓库。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from git_ops import GitConflictError, GitOps, GitPushError, RepoStatus  # noqa: E402


def make_temp_repo() -> tuple[Path, Path]:
    """创建一个临时 git repo (init + initial commit + remote 指向自身).
    返回 (repo_path, bare_remote_path).
    """
    tmp = tempfile.mkdtemp()
    tmp_path = Path(tmp)
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    remote.mkdir()

    # init bare remote
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    # init repo + initial commit
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"],
                   check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)
    return Path(tmp), repo


def cleanup_tmp(tmp: Path) -> None:
    """强制删临时目录 (Windows / git locks)."""
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestGitOpsInit(unittest.TestCase):
    def test_not_a_repo_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                GitOps(Path(d))

    def test_init_valid_repo(self):
        tmp, repo = make_temp_repo()
        try:
            g = GitOps(repo)
            self.assertEqual(g.repo_path, repo)
            self.assertEqual(g.branch, "main")
        finally:
            cleanup_tmp(tmp)


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo = make_temp_repo()
        self.g = GitOps(self.repo)

    def tearDown(self):
        cleanup_tmp(self.tmp)

    def test_clean_repo(self):
        s = self.g.status()
        self.assertTrue(s.clean)
        self.assertEqual(s.ahead, 0)
        self.assertEqual(s.behind, 0)
        self.assertEqual(s.branch, "main")
        self.assertIsNotNone(s.last_commit)
        self.assertEqual(s.last_commit.message, "init")

    def test_modified_file(self):
        (self.repo / "README.md").write_text("# Modified\n", encoding="utf-8")
        s = self.g.status()
        self.assertFalse(s.clean)
        self.assertIn("README.md", s.modified)
        self.assertEqual(s.staged, [])

    def test_untracked_file(self):
        (self.repo / "new.txt").write_text("x", encoding="utf-8")
        s = self.g.status()
        self.assertFalse(s.clean)
        self.assertIn("new.txt", s.untracked)

    def test_staged_file(self):
        (self.repo / "new.txt").write_text("x", encoding="utf-8")
        self.g.add(["new.txt"])
        s = self.g.status()
        self.assertIn("new.txt", s.staged)


class TestAddCommit(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo = make_temp_repo()
        self.g = GitOps(self.repo)

    def tearDown(self):
        cleanup_tmp(self.tmp)

    def test_add(self):
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        self.g.add(["a.txt"])
        s = self.g.status()
        self.assertIn("a.txt", s.staged)

    def test_add_nonexistent_ok(self):
        # git add -- 接受不存在路径, 但不报错 (没什么可 add)
        self.g.add(["nonexistent.txt"])

    def test_commit_returns_sha(self):
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        self.g.add(["a.txt"])
        sha = self.g.commit("add a.txt")
        self.assertEqual(len(sha), 40)  # full SHA
        self.assertTrue(sha.isalnum())

    def test_commit_empty_message_raises(self):
        with self.assertRaises(ValueError):
            self.g.commit("   ")


class TestPush(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo = make_temp_repo()
        self.g = GitOps(self.repo)

    def tearDown(self):
        cleanup_tmp(self.tmp)

    def test_push(self):
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        self.g.add(["a.txt"])
        self.g.commit("add a.txt")
        self.g.push()
        s = self.g.status()
        self.assertEqual(s.ahead, 0)


class TestPull(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo = make_temp_repo()
        self.g = GitOps(self.repo)

    def tearDown(self):
        cleanup_tmp(self.tmp)

    def test_pull_clean(self):
        # 不报错
        self.g.pull()

    def test_pull_dirty_raises(self):
        (self.repo / "dirty.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(GitConflictError):
            self.g.pull()


class TestCommitAndPush(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo = make_temp_repo()
        self.g = GitOps(self.repo)

    def tearDown(self):
        cleanup_tmp(self.tmp)

    def test_basic(self):
        (self.repo / "c.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        sha, pushed = self.g.commit_and_push("add csv", ["c.csv"])
        self.assertEqual(len(sha), 40)
        self.assertTrue(pushed)
        s = self.g.status()
        self.assertTrue(s.clean)
        self.assertEqual(s.ahead, 0)

    def test_no_changes_returns_empty(self):
        sha, pushed = self.g.commit_and_push("nothing", ["nope.txt"])
        self.assertEqual(sha, "")
        self.assertFalse(pushed)

    def test_conflict_with_other_changes(self):
        # 已有 modified 的 tracked 文件, 但 paths 不包含它
        (self.repo / "README.md").write_text("# Changed\n", encoding="utf-8")
        with self.assertRaises(GitConflictError):
            self.g.commit_and_push("test", ["other.txt"])

    def test_untracked_files_dont_block(self):
        """untracked 文件不应 block commit (跟 git 本身行为一致)."""
        (self.repo / "unrelated.txt").write_text("x", encoding="utf-8")
        (self.repo / "new.txt").write_text("y", encoding="utf-8")
        # 应该成功
        sha, pushed = self.g.commit_and_push("add new", ["new.txt"])
        self.assertEqual(len(sha), 40)
        self.assertTrue(pushed)

    def test_conflict_ignored_when_paths_include_existing(self):
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        (self.repo / "b.txt").write_text("b", encoding="utf-8")
        # 两个文件都传, 应该 OK
        sha, pushed = self.g.commit_and_push("add both", ["a.txt", "b.txt"])
        self.assertEqual(len(sha), 40)
        self.assertTrue(pushed)


if __name__ == "__main__":
    unittest.main()