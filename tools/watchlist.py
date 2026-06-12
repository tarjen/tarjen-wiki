#!/usr/bin/env python3
"""
tools/watchlist.py — watchlist 管理

存储关注用户列表（每行一个，# 开头是注释），用于代码抓取时优先抓这些人。
文件位置: ~/.config/wiki/watchlist.txt（默认）

用法：
    w = Watchlist(Path("~/.config/wiki/watchlist.txt").expanduser())
    w.users()                      # ['alice', 'bob']
    w.contains("alice")            # True
    w.add(["carol"])
    w.remove(["alice"])
"""
from __future__ import annotations

from pathlib import Path


def parse_watchlist(text: str) -> list[str]:
    """从文本解析关注用户列表. 每行一个, # 开头是注释, 跳过空行."""
    users = []
    for line in text.splitlines():
        # 去掉行尾注释 (# 后面的全是注释, 除非在引号里 — 简化: 整行按 # 切)
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        users.append(line)
    return users


def render_watchlist(users: list[str]) -> str:
    """生成 watchlist 文件内容. 包含一个 header 注释."""
    lines = [
        "# Wiki watchlist",
        "# 一行一个用户名, # 开头是注释, 空行忽略",
        "# 用于 wiki codes fetch 时优先抓这些人的提交",
        "",
    ]
    lines.extend(users)
    lines.append("")  # trailing newline
    return "\n".join(lines)


class Watchlist:
    """watchlist 文件读写 + 修改."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._users: list[str] = []
        self._loaded = False

    def load(self) -> None:
        """加载 watchlist. 文件不存在视为空列表."""
        self._users = []
        if not self.path.exists():
            self._loaded = True
            return
        self._users = parse_watchlist(self.path.read_text(encoding="utf-8"))
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        """写回磁盘."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(render_watchlist(self._users), encoding="utf-8")

    def users(self) -> list[str]:
        self._ensure_loaded()
        return list(self._users)

    def contains(self, username: str) -> bool:
        self._ensure_loaded()
        return username in self._users

    def add(self, users: list[str]) -> list[str]:
        """添加用户. 返回真正新加的 (去重 + 跳过空)."""
        self._ensure_loaded()
        added = []
        for u in users:
            u = (u or "").strip()
            if not u:
                continue
            if u in self._users:
                continue
            self._users.append(u)
            added.append(u)
        if added:
            self.save()
        return added

    def remove(self, users: list[str]) -> list[str]:
        """删除用户. 返回真正删掉的."""
        self._ensure_loaded()
        removed = []
        for u in users:
            u = (u or "").strip()
            if not u:
                continue
            if u in self._users:
                self._users.remove(u)
                removed.append(u)
        if removed:
            self.save()
        return removed

    def __contains__(self, username: str) -> bool:
        return self.contains(username)

    def __iter__(self):
        return iter(self.users())

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._users)