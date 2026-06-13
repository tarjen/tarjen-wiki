#!/usr/bin/env python3
"""
tools/codes_store.py — OJ 抓取代码的本地缓存管理

存储位置 (按 OJ 平台 + 比赛 + 题 + 用户 划分):
  ~/.local/share/wiki/codes/<platform>/<cid>/<problem>/<user>.<ext>

  e.g.
    ~/.local/share/wiki/codes/qoj/2521/A/tarjen.cpp
    ~/.local/share/wiki/codes/qoj/2521/A/cyp063.cpp    ← 别人的 AC
    ~/.local/share/wiki/codes/qoj/2521/B/tarjen.cpp

索引: ~/.local/share/wiki/codes/<platform>/<cid>/index.json
     (放在 contest 目录, 含每个文件的元数据: user, problem, lang, verdict, sub_id, source, time)

gitignored - 不进 repo (整个 ~/.local/share/wiki/codes 都在 repo 外)。

只管"已经在本地的代码". 抓取逻辑（HTTP 请求）由 platform client 负责.

用法:
    store = CodesStore(Path("~/.local/share/wiki/codes").expanduser())
    store.save(platform="qoj", cid=2564, problem="A", user="alice",
               code="#include...", language="GNU C++17", submission_id=12345)
    code = store.read(platform="qoj", cid=2564, problem="A", user="alice")
    files = store.list_files(platform="qoj", cid=2564)
    store.clean(platform="qoj", cid=2564)
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
    platform: str
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
    """代码缓存存储.

    新结构 (2026-06): platform/cid/problem/user.ext
    旧结构 (2025 之前): cid/user/problem.ext (仅读兼容, 写都用新)
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    # === 路径 ===

    def _safe(self, name: str, kind: str) -> str:
        """防止 path traversal: 拒绝 .. / / \\\\ 等."""
        if not name:
            raise ValueError(f"empty {kind}: {name!r}")
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"unsafe {kind}: {name!r}")
        return name

    def _platform_dir(self, platform: str) -> Path:
        return self.root / self._safe(platform, "platform")

    def _contest_dir(self, platform: str, cid: int | str) -> Path:
        return self._platform_dir(platform) / str(cid)

    def _problem_dir(self, platform: str, cid: int | str, problem: str) -> Path:
        return self._contest_dir(platform, cid) / self._safe(problem, "problem")

    def file_path(self, platform: str, cid: int | str, problem: str, user: str,
                  ext: str = "txt") -> Path:
        return self._problem_dir(platform, cid, problem) / f"{self._safe(user, 'user')}.{ext}"

    # === 单个文件 ===

    def exists(self, platform: str, cid: int | str, problem: str, user: str) -> bool:
        d = self._problem_dir(platform, cid, problem)
        if not d.exists():
            return False
        return any(d.glob(f"{self._safe(user, 'user')}.*"))

    def save(self, platform: str, cid: int | str, problem: str, user: str,
             code: str, language: str | None = None,
             *, verdict: str | None = None,
             submission_id: str | None = None,
             source: Source = "other",
             contest_time: str | None = None) -> Path:
        ext = lang_to_ext(language)
        target = self.file_path(platform, cid, problem, user, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        self._update_index_entry(
            platform, cid, problem, user, ext,
            language=language, verdict=verdict,
            submission_id=submission_id, source=source,
            contest_time=contest_time,
        )
        return target

    def read(self, platform: str, cid: int | str, problem: str, user: str) -> str | None:
        d = self._problem_dir(platform, cid, problem)
        if not d.exists():
            return None
        safe_user = self._safe(user, "user")
        for p in sorted(d.glob(f"{safe_user}.*")):
            if p.is_file():
                return p.read_text(encoding="utf-8")
        return None

    def read_path(self, platform: str, cid: int | str, problem: str, user: str) -> Path | None:
        d = self._problem_dir(platform, cid, problem)
        if not d.exists():
            return None
        safe_user = self._safe(user, "user")
        for p in sorted(d.glob(f"{safe_user}.*")):
            if p.is_file():
                return p
        return None

    def delete(self, platform: str, cid: int | str, problem: str, user: str) -> int:
        d = self._problem_dir(platform, cid, problem)
        if not d.exists():
            return 0
        safe_user = self._safe(user, "user")
        count = 0
        for p in d.glob(f"{safe_user}.*"):
            if p.is_file():
                p.unlink()
                count += 1
        self._remove_index_entry(platform, cid, problem, user)
        return count

    # === 整场 contest ===

    def list_files(self, platform: str, cid: int | str, *,
                   problem: str | None = None,
                   user: str | None = None,
                   source: Source | None = None) -> list[CodeFile]:
        """列出缓存的所有 (或筛选后的) 代码文件.

        兼容旧结构 (<root>/<cid>/<user>/<prob>.<ext>) 读取.
        """
        d = self._contest_dir(platform, cid)
        files: list[CodeFile] = []
        if d.exists():
            for prob_dir in sorted(d.iterdir()):
                if not prob_dir.is_dir():
                    continue
                if problem and prob_dir.name != problem:
                    continue
                # prob_dir 是题目目录 (<platform>/<cid>/<problem>/<user>.<ext>)
                for f in sorted(prob_dir.iterdir()):
                    if not f.is_file() or f.suffix == ".tmp":
                        continue
                    user_name = f.stem
                    meta = self._lookup_index(platform, cid, prob_dir.name, user_name) or {}
                    files.append(CodeFile(
                        platform=platform,
                        user=user_name,
                        problem=prob_dir.name,
                        path=str(f.relative_to(d)),
                        size=f.stat().st_size,
                        mtime=datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                        language=meta.get("language"),
                        verdict=meta.get("verdict"),
                        submission_id=meta.get("submission_id"),
                        source=meta.get("source", "other"),
                        contest_time=meta.get("contest_time"),
                    ))
        # 兼容旧结构
        old_d = self.root / str(cid)
        if old_d.exists() and old_d != d:
            for user_dir in sorted(old_d.iterdir()):
                if not user_dir.is_dir() or user_dir.name == "_meta":
                    continue
                for f in sorted(user_dir.iterdir()):
                    if not f.is_file() or f.suffix == ".tmp":
                        continue
                    prob_name = f.stem
                    if problem and prob_name != problem:
                        continue
                    files.append(CodeFile(
                        platform=platform,
                        user=user_dir.name,
                        problem=prob_name,
                        path=str(f.relative_to(self.root)),
                        size=f.stat().st_size,
                        mtime=datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                        language=None, verdict=None, submission_id=None,
                        source="other", contest_time=None,
                    ))
        if user:
            files = [f for f in files if f.user == user]
        if source:
            files = [f for f in files if f.source == source]
        return files

    def clean(self, platform: str, cid: int | str, *,
              user: str | None = None,
              problem: str | None = None) -> int:
        """清理代码. 整场/user/problem 范围. 返回删了几个文件."""
        d = self._contest_dir(platform, cid)
        count = 0
        if user and problem:
            count = self.delete(platform, cid, problem, user)
        elif user:
            for prob_dir in d.iterdir() if d.exists() else []:
                if not prob_dir.is_dir():
                    continue
                f = prob_dir / f"{self._safe(user, 'user')}.cpp"
                if f.exists():
                    f.unlink()
                    count += 1
                for ext in ('py', 'java', 'rs', 'go', 'kt', 'js', 'ts', 'cs', 'rb', 'php', 'c', 'txt'):
                    f2 = prob_dir / f"{self._safe(user, 'user')}.{ext}"
                    if f2.exists():
                        f2.unlink()
                        count += 1
        else:
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file() and f.name != "index.json":
                        f.unlink()
                        count += 1
                for sub in sorted(d.iterdir(), reverse=True):
                    if sub.is_dir() and not any(sub.iterdir()):
                        sub.rmdir()
        return count

    def has_cache(self, platform: str, cid: int | str) -> bool:
        return self._contest_dir(platform, cid).exists()

    # === index.json 管理 ===

    def _index_path(self, platform: str, cid: int | str) -> Path:
        return self._contest_dir(platform, cid) / "index.json"

    def _read_index(self, platform: str, cid: int | str) -> dict:
        p = self._index_path(platform, cid)
        if not p.exists():
            return {"platform": platform, "contest_id": str(cid), "files": []}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"platform": platform, "contest_id": str(cid), "files": []}

    def _write_index(self, platform: str, cid: int | str, data: dict) -> None:
        p = self._index_path(platform, cid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _update_index_entry(self, platform, cid, problem, user, ext, **meta) -> None:
        data = self._read_index(platform, cid)
        for entry in data.get("files", []):
            if entry.get("user") == user and entry.get("problem") == problem:
                entry.update(meta)
                entry["filename"] = f"{user}.{ext}"
                entry["mtime"] = datetime.now().isoformat()
                self._write_index(platform, cid, data)
                return
        entry = {
            "user": user, "problem": problem,
            "filename": f"{user}.{ext}",
            "mtime": datetime.now().isoformat(),
        }
        entry.update(meta)
        data.setdefault("files", []).append(entry)
        self._write_index(platform, cid, data)

    def _remove_index_entry(self, platform, cid, problem, user) -> None:
        data = self._read_index(platform, cid)
        before = len(data.get("files", []))
        data["files"] = [
            e for e in data.get("files", [])
            if not (e.get("user") == user and e.get("problem") == problem)
        ]
        if len(data["files"]) != before:
            self._write_index(platform, cid, data)

    def _lookup_index(self, platform, cid, problem, user) -> dict | None:
        data = self._read_index(platform, cid)
        for entry in data.get("files", []):
            if entry.get("user") == user and entry.get("problem") == problem:
                return entry
        return None

    def get_index(self, platform: str, cid: int | str) -> dict:
        return self._read_index(platform, cid)


def ensure_gitignore(codes_root: Path) -> None:
    """在 codes_root 创建 .gitignore (防止意外 git add)."""
    gi = codes_root / ".gitignore"
    if not gi.exists():
        codes_root.mkdir(parents=True, exist_ok=True)
        gi.write_text("*\n!.gitignore\n", encoding="utf-8")
