#!/usr/bin/env python3
"""
tools/md_store.py — docs/contests/<slug>.md 详情页读写

读取 / 写入 / 删除比赛详情页 markdown 文件。
生成占位模板（从 sync.py 的 CONTEST_TEMPLATE 迁过来）。

纯数据层，不依赖 FastAPI / CLI。

用法：
    store = MdStore(Path("docs/contests"))
    store.exists("2025-icpc-xxx")
    store.read("2025-icpc-xxx")
    store.write("2025-icpc-xxx", "# title\n...")
    store.delete("2025-icpc-xxx")
    store.placeholder(contest)  # 生成默认模板
"""
from __future__ import annotations

import sys
from pathlib import Path

# Contest dataclass from csv_store
from csv_store import Contest  # noqa: E402


CONTEST_TEMPLATE = """# {name}

!!! tip "快速编辑"
    - [📝 编辑此页](../../editor/?view=md&slug={slug}) — 改总结、复盘、题目笔记
    - [📊 改状态表](../../editor/?slug={slug}) — 改 O/Ø/! 状态

## 元信息

| 字段 | 值 |
|------|-----|
| 比赛日期 | {date_iso} |
| 平台 |  |
| 比赛链接 | {link} |
| 参赛 |  |
| 通过 | {solved} / {total} |
| 排名 |  |
| 标签 | {tags} |

## 总结

> 待补。

## 题目记录

> 待补。每题用 `### A — 题名` 开头（自动生成锚点 `#a-题名`）。

## 复盘

> 待补。

## 相关链接

- 待补
"""


class MdStore:
    """比赛详情页 markdown 文件管理."""

    def __init__(self, contests_dir: Path):
        self.dir = Path(contests_dir)

    def _path(self, slug: str) -> Path:
        return self.dir / f"{slug}.md"

    def exists(self, slug: str) -> bool:
        return self._path(slug).exists()

    def read(self, slug: str) -> str:
        """读取详情页内容. 不存在抛 FileNotFoundError."""
        return self._path(slug).read_text(encoding="utf-8")

    def write(self, slug: str, content: str) -> None:
        """写入详情页. 原子写 (.tmp + rename)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self._path(slug)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

    def delete(self, slug: str) -> bool:
        """删除详情页. 返回是否真的删了什么."""
        target = self._path(slug)
        if target.exists():
            target.unlink()
            return True
        return False

    def placeholder(self, contest: Contest) -> str:
        """生成默认占位 markdown (从 sync.py 迁过来)."""
        try:
            y, m, d = contest.date.split(".")
            date_iso = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except (ValueError, AttributeError):
            date_iso = contest.date  # 兜底

        return CONTEST_TEMPLATE.format(
            slug=contest.slug,
            name=contest.name,
            date_iso=date_iso,
            link=contest.link or "",
            solved=contest.solved,
            total=contest.total,
            tags=contest.tags,
        )