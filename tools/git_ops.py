#!/usr/bin/env python3
"""
tools/git_ops.py — git 操作封装

给 server.py / cli_main.py 用，统一封装：
  - status: clean/ahead/behind/modified
  - add + commit + push
  - pull (本地脏抛 Conflict)

后端进程对本地 git 仓库有完整权限。
不在这里做高阶合并逻辑 —— 假设只有一个分支 (main), linear history。

用法：
    git = GitOps(Path("/home/tarjen/wiki"))
    s = git.status()                 # RepoStatus(branch, clean, ahead, behind, last_commit)
    if not s.clean:
        raise ConflictError(...)
    git.commit_and_push("add(xxx)", ["contests.csv", "docs/contests/xxx.md"])
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class CommitInfo:
    sha: str
    message: str
    author: str
    time: str        # ISO


@dataclass
class RepoStatus:
    branch: str
    clean: bool
    ahead: int
    behind: int
    last_commit: CommitInfo | None
    staged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


class GitError(RuntimeError):
    """git 命令失败 (非零退出码)."""


class GitConflictError(GitError):
    """本地有未推送/未提交改动, 操作需要先解决."""


class GitOps:
    """git 操作封装. 默认 branch=main, remote=origin."""

    def __init__(self, repo_path: Path, remote: str = "origin", branch: str = "main"):
        self.repo_path = Path(repo_path)
        self.remote = remote
        self.branch = branch
        if not (self.repo_path / ".git").exists():
            raise ValueError(f"not a git repo: {self.repo_path}")

    def _run(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """运行 git 命令. cwd = repo_path."""
        cmd = ["git", "-C", str(self.repo_path), *args]
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
        )

    # === 查询 ===

    def status(self) -> RepoStatus:
        """获取仓库状态. 包括 ahead/behind, dirty files."""
        branch = self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        # ahead/behind (相对 upstream)
        ahead, behind = 0, 0
        upstream = f"{self.remote}/{branch}"
        try:
            r = self._run("rev-list", "--left-right", "--count", f"{upstream}...{branch}")
            left, right = r.stdout.strip().split()
            behind, ahead = int(left), int(right)
        except GitError:
            # 没设 upstream, 不报错
            pass

        # 工作区状态
        s = self._run("status", "--porcelain").stdout
        staged, modified, untracked = [], [], []
        for line in s.splitlines():
            if not line:
                continue
            x = line[0]  # index status
            y = line[1]  # worktree status
            path = line[3:]
            if x in ("A", "M", "D", "R", "C", "T"):
                staged.append(path)
            if y in ("M", "D"):
                modified.append(path)
            if x == "?" and y == "?":
                untracked.append(path)

        clean = not (staged or modified or untracked)

        # last commit
        last_commit = None
        try:
            r = self._run(
                "log", "-1", "--format=%H%n%s%n%an%n%aI",
            )
            sha, msg, author, time = r.stdout.strip().split("\n")
            last_commit = CommitInfo(sha=sha, message=msg, author=author, time=time)
        except GitError:
            pass

        return RepoStatus(
            branch=branch,
            clean=clean,
            ahead=ahead,
            behind=behind,
            last_commit=last_commit,
            staged=staged,
            modified=modified,
            untracked=untracked,
        )

    # === 写入 ===

    def add(self, paths: list[str]) -> None:
        """git add paths. 不存在的路径静默忽略."""
        if not paths:
            return
        # 过滤存在的路径 (git add -- nonexistent 会 fatal)
        existing = [p for p in paths if (self.repo_path / p).exists() or self._is_tracked(p)]
        if not existing:
            return
        self._run("add", "--", *existing)

    def _is_tracked(self, path: str) -> bool:
        """检查 path 是否已在 git 索引里 (即使工作区删了也算)."""
        r = self._run("ls-files", "--error-unmatch", "--", path, check=False)
        return r.returncode == 0

    def commit(self, message: str) -> str:
        """git commit -m. 返回 SHA."""
        if not message.strip():
            raise ValueError("commit message 不能为空")
        self._run("commit", "-m", message)
        return self._run("rev-parse", "HEAD").stdout.strip()

    def push(self) -> None:
        """git push <remote> <branch>. 无 upstream 时自动 -u."""
        # 先看有没有 upstream
        try:
            self._run("rev-parse", "--abbrev-ref", f"{self.remote}/{self.branch}",
                      check=False)
            has_upstream = self._run.returncode == 0
        except Exception:
            has_upstream = False

        if has_upstream:
            self._run("push", self.remote, self.branch)
        else:
            self._run("push", "-u", self.remote, self.branch)

    def pull(self) -> None:
        """git pull. 本地有未提交改动抛 GitConflictError."""
        s = self.status()
        if not s.clean:
            raise GitConflictError(
                f"本地有未提交改动 (modified: {s.modified}, staged: {s.staged}); "
                "请先 commit 或 stash"
            )
        try:
            self._run("pull", "--ff-only", self.remote, self.branch)
        except GitError as e:
            raise GitConflictError(f"pull 失败 (可能需要 merge): {e}") from e

    # === 组合 ===

    def commit_and_push(self, message: str, paths: list[str],
                        *, require_clean: bool = True) -> tuple[str, bool]:
        """组合 add + commit + push.

        Args:
            message: commit message
            paths: 要 add 的文件路径 (相对 repo root)
            require_clean: True 时若已有 staged/modified 且不在 paths 里, 报错

        Returns:
            (commit_sha, pushed: bool)
        """
        s = self.status()
        if not s.clean and require_clean:
            # 检查现有 staged/modified 是否都包含在 paths 里
            existing = set(s.staged) | set(s.modified)
            new_paths = set(paths)
            if not existing.issubset(new_paths):
                raise GitConflictError(
                    f"仓库有其他未提交改动: {sorted(existing - new_paths)}; "
                    "请先处理"
                )

        self.add(paths)
        # 如果没东西可 commit (add 后没有 staged), 不 commit
        s_after = self.status()
        if not s_after.staged:
            return ("", False)

        sha = self.commit(message)
        pushed = True
        try:
            self.push()
        except GitError as e:
            # commit 成功但 push 失败, 报告但不回滚
            pushed = False
            raise GitPushError(f"commit {sha[:8]} 成功但 push 失败: {e}") from e
        return (sha, pushed)


class GitPushError(GitError):
    """commit 成功但 push 失败."""
    pass