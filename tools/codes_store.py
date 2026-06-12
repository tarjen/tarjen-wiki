#!/usr/bin/env python3
"""
tools/codes_store.py — QOJ 抓取代码的本地缓存管理

存储位置: ~/.local/share/wiki/codes/<cid>/<user>/<prob>.<ext>
索引: ~/.local/share/wiki/codes/<cid>/index.json

gitignored - 不进 repo。

只管"已经在本地的代码". 抓取逻辑（HTTP 请求）由 platform client 负责。

用法：
    store = CodesStore(Path("~/.local/share/wiki/codes").expanduser())
    store.save(2564, "alice", "A", "#include...", "cpp", submission_id=12345)
    code = store.read(2564, "alice", "A")
    files = store.list_files(2564)
    store.clean(2564)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


Source = Literal["mine", "watchlist", "sample", "other"]


@dataclass
class CodeFile:
    """一条缓存的代码记录."""
    user: str
    problem: str
    path: str                # 相对 contest_dir
    size: int
    mtime: str               # ISO
    language: str | None
    verdict: str | None
    submission_id: str | None
    source: Source
    contest_time: str | None


LANG_EXT = {
    "GNU C++17": "cpp",
    "GNU C++14": "cpp",
    "GNU C++11": "cpp",
    "GNU C++": "cpp",
    "C++17": "cpp",
    "C++": "cpp",
    "GNU C11": "c",
    "C": "c",
    "Python 3": "py",
    "Python 2": "py",
    "PyPy 3": "py",
    "Java 17": "java",
    "Java 11": "java",
    "Java 8": "java",
    "Rust": "rs",
    "Go": "go",
    "Kotlin": "kt",
    "JavaScript": "js",
    "TypeScript": "ts",
    "C#": "cs",
    "Ruby": "rb",
    "PHP": "php",
}


def lang_to_ext(lang: str | None) -> str:
    """平台语言标签 → 文件扩展名. 推断不出就 .txt."""
    if not lang:
        return "txt"
    if lang in LANG_EXT:
        return LANG_EXT[lang]
    # 模糊匹配: "GNU C++17" contains "C++"
    low = lang.lower()
    if "c++" in low or "cpp" in low or "g++" in low:
        return "cpp"
    if "python" in low or "pypy" in low:
        return "py"
    if "java" in low:
        return "java"
    if "rust" in low:
        return "rs"
    if " go " in f" {low} " or low.startswith("go "):
        return "go"
    if "ruby" in low:
        return "rb"
    if "php" in low:
        return "php"
    if "kotlin" in low:
        return "kt"
    if "javascript" in low:
        return "js"
    if "typescript" in low:
        return "ts"
    if "csharp" in low or "c#" in low:
        return "cs"
    if low.startswith("c"):
        return "c"
    return "txt"


class CodesStore:
    """代码缓存存储."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # === 路径 ===

    def contest_dir(self, cid: int | str) -> Path:
        return self.root / str(cid)

    def file_path(self, cid: int | str, user: str, problem: str,
                  ext: str = "txt") -> Path:
        # user 内不能含 .. 或 / (安全)
        safe_user = self._safe_path_component(user)
        return self.contest_dir(cid) / safe_user / f"{problem}.{ext}"

    def _safe_path_component(self, name: str) -> str:
        """防止 path traversal: 拒绝 .. / / 等."""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"unsafe path component: {name!r}")
        return name

    # === 单个文件 ===

    def exists(self, cid: int | str, user: str, problem: str) -> bool:
        """是否有缓存 (任意扩展名)."""
        d = self.contest_dir(cid) / self._safe_path_component(user)
        if not d.exists():
            return False
        for p in d.glob(f"{problem}.*"):
            if p.is_file():
                return True
        return False

    def save(self, cid: int | str, user: str, problem: str,
             code: str, language: str | None = None,
             *, verdict: str | None = None,
             submission_id: str | None = None,
             source: Source = "other",
             contest_time: str | None = None) -> Path:
        """保存代码. 返回写入的文件路径."""
        ext = lang_to_ext(language)
        target = self.file_path(cid, user, problem, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

        # 同步写 index
        self._update_index_entry(
            cid, user, problem, ext,
            language=language, verdict=verdict,
            submission_id=submission_id, source=source,
            contest_time=contest_time,
        )
        return target

    def read(self, cid: int | str, user: str, problem: str) -> str | None:
        """读代码. 没有返回 None."""
        d = self.contest_dir(cid) / self._safe_path_component(user)
        if not d.exists():
            return None
        matches = sorted(d.glob(f"{problem}.*"))
        for p in matches:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        return None

    def read_path(self, cid: int | str, user: str, problem: str) -> Path | None:
        """读代码文件路径. 没有返回 None."""
        d = self.contest_dir(cid) / self._safe_path_component(user)
        if not d.exists():
            return None
        matches = sorted(d.glob(f"{problem}.*"))
        for p in matches:
            if p.is_file():
                return p
        return None

    def delete(self, cid: int | str, user: str, problem: str) -> int:
        """删除指定 user/problem 的代码. 返回删了几个文件."""
        d = self.contest_dir(cid) / self._safe_path_component(user)
        if not d.exists():
            return 0
        count = 0
        for p in d.glob(f"{problem}.*"):
            if p.is_file():
                p.unlink()
                count += 1
        self._remove_index_entry(cid, user, problem)
        return count

    # === 整场 contest ===

    def list_files(self, cid: int | str, *,
                   problem: str | None = None,
                   user: str | None = None,
                   source: Source | None = None) -> list[CodeFile]:
        """列出缓存的所有 (或筛选后的) 代码文件."""
        d = self.contest_dir(cid)
        if not d.exists():
            return []

        files = []
        for user_dir in sorted(d.iterdir()):
            if not user_dir.is_dir() or user_dir.name == "_meta":
                continue
            if user and user_dir.name != user:
                continue
            for f in sorted(user_dir.iterdir()):
                if not f.is_file() or f.suffix == ".tmp":
                    continue
                # 文件名: <problem>.<ext>
                parts = f.stem, f.suffix.lstrip(".")
                prob_name = parts[0]
                if problem and prob_name != problem:
                    continue

                # 从 index 查元数据
                meta = self._lookup_index(cid, user_dir.name, prob_name) or {}
                files.append(CodeFile(
                    user=user_dir.name,
                    problem=prob_name,
                    path=str(f.relative_to(d)),
                    size=f.stat().st_size,
                    mtime=datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    language=meta.get("language"),
                    verdict=meta.get("verdict"),
                    submission_id=meta.get("submission_id"),
                    source=meta.get("source", "other"),
                    contest_time=meta.get("contest_time"),
                ))

        if source:
            files = [f for f in files if f.source == source]
        return files

    def clean(self, cid: int | str, *,
              user: str | None = None,
              problem: str | None = None) -> int:
        """清理代码. 整场/user/problem 范围. 返回删了几个文件."""
        d = self.contest_dir(cid)
        if not d.exists():
            return 0
        count = 0
        if user and problem:
            count += self.delete(cid, user, problem)
        elif user:
            user_dir = d / self._safe_path_component(user)
            if user_dir.exists():
                for f in user_dir.iterdir():
                    if f.is_file():
                        f.unlink()
                        count += 1
                # 删空的 user_dir (但保留 index.json)
                if not any(user_dir.iterdir()):
                    user_dir.rmdir()
        else:
            for f in d.rglob("*"):
                if f.is_file() and f.name != "index.json":
                    f.unlink()
                    count += 1
            # 删空目录
            for sub in sorted(d.iterdir(), reverse=True):
                if sub.is_dir():
                    if not any(sub.iterdir()):
                        sub.rmdir()
        return count

    def has_cache(self, cid: int | str) -> bool:
        return self.contest_dir(cid).exists()

    # === index.json 管理 ===

    def _index_path(self, cid: int | str) -> Path:
        return self.contest_dir(cid) / "index.json"

    def _read_index(self, cid: int | str) -> dict:
        p = self._index_path(cid)
        if not p.exists():
            return {"contest_id": str(cid), "files": []}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"contest_id": str(cid), "files": []}

    def _write_index(self, cid: int | str, data: dict) -> None:
        p = self._index_path(cid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _update_index_entry(self, cid, user, problem, ext, **meta) -> None:
        data = self._read_index(cid)
        # 找现有 entry
        for entry in data.get("files", []):
            if entry["user"] == user and entry["problem"] == problem:
                entry.update(meta)
                entry["filename"] = f"{problem}.{ext}"
                entry["mtime"] = datetime.now().isoformat()
                self._write_index(cid, data)
                return
        # 新 entry
        entry = {
            "user": user, "problem": problem,
            "filename": f"{problem}.{ext}",
            "mtime": datetime.now().isoformat(),
        }
        entry.update(meta)
        data.setdefault("files", []).append(entry)
        self._write_index(cid, data)

    def _remove_index_entry(self, cid, user, problem) -> None:
        data = self._read_index(cid)
        before = len(data.get("files", []))
        data["files"] = [
            e for e in data.get("files", [])
            if not (e["user"] == user and e["problem"] == problem)
        ]
        if len(data["files"]) != before:
            self._write_index(cid, data)

    def _lookup_index(self, cid, user, problem) -> dict | None:
        data = self._read_index(cid)
        for entry in data.get("files", []):
            if entry["user"] == user and entry["problem"] == problem:
                return entry
        return None

    def get_index(self, cid: int | str) -> dict:
        """返回完整 index (用于 'codes index' 命令)."""
        return self._read_index(cid)


def ensure_gitignore(codes_root: Path) -> None:
    """在 codes_root 创建 .gitignore (防止意外 git add)."""
    gi = codes_root / ".gitignore"
    if not gi.exists():
        codes_root.mkdir(parents=True, exist_ok=True)
        gi.write_text("*\n!.gitignore\n", encoding="utf-8")