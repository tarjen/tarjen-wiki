#!/usr/bin/env python3
"""
tools/csv_store.py — contests.csv 读写层

纯数据层，不依赖 FastAPI / CLI。可被 server.py / cli_main.py / sync.py 复用。

CSV 列定义（与现有 contests.csv 兼容）：
  slug*, name*, date*, solved*, total*, problems*, link, tags

字段约束：
  - slug:     ^[a-z0-9][a-z0-9\-_.]*$，全局唯一
  - date:     YYYY.M.D / YYYY-MM-DD / YYYY/M/D（统一存为 YYYY.M.D）
  - total:    > 0
  - problems: 长度 == total, 字符 ∈ {O, Ø, !, .}
  - solved:   总是从 problems 重算 (O + Ø)，CSV 里写的值仅供参考 + warn

原子写：写到 .tmp 后 rename，不留临时文件。

用法：
    store = CsvStore(Path("contests.csv"))
    store.load()
    for c in store.all(): ...
    store.add(Contest(...))
    store.update("slug", name="...", problems=[...])
    store.save()  # 写回磁盘
"""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


# === 常量 (从 sync.py 迁移) ===

DATE_RE = re.compile(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]*$")
ALLOWED_PROBLEM_CHARS = set("OØ.!")  # O, Ø, !, .

HEADER = ["slug", "name", "date", "solved", "total", "problems", "link", "tags"]


# === Dataclass ===

@dataclass
class Contest:
    slug: str
    name: str
    date: str           # 已规范化为 YYYY.M.D
    solved: int         # 总是从 problems 重算
    total: int
    problems: list[str] # 长度 == total
    link: str
    tags: str

    @property
    def iso_date(self) -> str:
        """ISO 格式日期，用于排序: YYYY-MM-DD."""
        y, m, d = self.date.split(".")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

    @property
    def in_contest_solved(self) -> int:
        """赛中过的题数 (O 的数量)."""
        return sum(1 for p in self.problems if p == "O")

    @property
    def upsolved(self) -> int:
        """赛后补过的题数 (Ø 的数量)."""
        return sum(1 for p in self.problems if p == "Ø")

    @property
    def tried_unsolved(self) -> int:
        """尝试过但没过的题数 (! 的数量)."""
        return sum(1 for p in self.problems if p == "!")

    @property
    def untouched(self) -> int:
        """未做的题数 (. 的数量)."""
        return sum(1 for p in self.problems if p == ".")

    @property
    def tags_list(self) -> list[str]:
        """分割 tags 字符串为列表."""
        return [t for t in self.tags.split() if t.startswith("#")]

    def recompute_solved(self) -> int:
        """从 problems 重算 solved 并写回."""
        self.solved = self.in_contest_solved + self.upsolved
        return self.solved


# === 异常 ===

class CsvValidationError(ValueError):
    """CSV 校验错误，带行号/slug 上下文."""
    def __init__(self, message: str, row_num: int | None = None, slug: str | None = None):
        self.row_num = row_num
        self.slug = slug
        super().__init__(message)


# === 工具函数 ===

def parse_problems(raw: str) -> list[str]:
    """支持 'O;O;.;O' 和 'OO.OO' 两种写法.

    含分号/逗号/空格 → 按分号切；否则逐字符。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    if ";" not in raw and "," not in raw and " " not in raw:
        chars = list(raw)
    else:
        parts = [p.strip() for p in raw.split(";")]
        if any(not p for p in parts):
            raise ValueError(f"problems 含有空段: {raw!r}")
        chars = parts
    return chars


def normalize_date(s: str) -> str:
    """只把分隔符统一为 `.`, 保留用户原始的零填充格式.

    2024/5/1   → 2024.5.1
    2024-05-01 → 2024-05-01   (保留零填充)
    2024.05.01 → 2024.05.01   (不变)
    """
    return s.replace("/", ".").replace("-", ".")


def problems_to_string(problems: list[str]) -> str:
    """序列化 problems 列表为 'O;O;.;O' 格式."""
    return ";".join(problems)


# === 主类 ===

class CsvStore:
    """contests.csv 的读写接口.

    用法：
        store = CsvStore(path)
        store.load()                   # 一次性加载全部到内存
        store.all()                    # 列表 (按日期倒序)
        store.get(slug)                # 单条
        store.add(contest)             # 加
        store.update(slug, **fields)   # 改
        store.delete(slug)             # 删
        store.save()                   # 原子写回磁盘
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._contests: dict[str, Contest] = {}
        self._loaded = False

    # === 读 ===

    def load(self) -> list[str]:
        """加载并校验整个 CSV. 不存在或空文件不报错.

        Returns:
            warnings: 加载过程中的 warning 列表 (e.g. solved 列与 problems 不一致).
                      留给调用方决定怎么显示 (默认静默, --verbose 时打印).

        Raises:
            CsvValidationError: 任何字段非法
        """
        warnings: list[str] = []
        self._contests.clear()

        if not self.path.exists():
            self._loaded = True
            return warnings

        with self.path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            missing = set(HEADER) - set(reader.fieldnames or [])
            if missing:
                raise CsvValidationError(
                    f"CSV 缺少必填列: {', '.join(sorted(missing))}"
                )

            for i, row in enumerate(reader, start=2):
                try:
                    contest = self._parse_row(row, row_num=i, warnings=warnings)
                except CsvValidationError:
                    raise
                except Exception as e:
                    raise CsvValidationError(
                        f"第 {i} 行解析失败: {e}", row_num=i
                    ) from e
                self._contests[contest.slug] = contest

        self._loaded = True
        return warnings

    def _parse_row(self, row: dict, row_num: int, *, warnings: list[str]) -> Contest:
        # 必填字段检查
        for fname in ("slug", "name", "date", "total", "problems"):
            if not (row.get(fname) or "").strip():
                raise CsvValidationError(
                    f"第 {row_num} 行 `{fname}` 为空",
                    row_num=row_num,
                )

        # slug
        slug = row["slug"].strip()
        if not SLUG_RE.match(slug):
            raise CsvValidationError(
                f"第 {row_num} 行 slug 非法: {slug!r}",
                row_num=row_num, slug=slug,
            )

        # date
        date_raw = row["date"].strip()
        if not DATE_RE.match(date_raw):
            raise CsvValidationError(
                f"第 {row_num} 行 date 格式不对: {date_raw!r}",
                row_num=row_num, slug=slug,
            )
        date = normalize_date(date_raw)

        # total / solved (input)
        try:
            total = int(row["total"])
            solved_input = int(row.get("solved") or 0)
        except ValueError as e:
            raise CsvValidationError(
                f"第 {row_num} 行 solved/total 不是整数: "
                f"solved={row.get('solved')!r} total={row['total']!r}",
                row_num=row_num, slug=slug,
            ) from e

        if total <= 0:
            raise CsvValidationError(
                f"第 {row_num} 行 total 必须 > 0: {total}",
                row_num=row_num, slug=slug,
            )

        # problems
        problems = parse_problems(row["problems"].strip())
        if len(problems) != total:
            raise CsvValidationError(
                f"第 {row_num} 行 problems 长度 {len(problems)} ≠ total {total}: "
                f"{row['problems']!r}",
                row_num=row_num, slug=slug,
            )
        for j, p in enumerate(problems, start=1):
            if p not in ALLOWED_PROBLEM_CHARS:
                raise CsvValidationError(
                    f"第 {row_num} 行 problems 第 {j} 个字符非法: {p!r} "
                    f"(只允许 {sorted(ALLOWED_PROBLEM_CHARS)})",
                    row_num=row_num, slug=slug,
                )

        # solved 总是从 problems 重算
        contest = Contest(
            slug=slug,
            name=row["name"].strip(),
            date=date,
            solved=0,  # 临时, 下面计算
            total=total,
            problems=problems,
            link=(row.get("link") or "").strip(),
            tags=(row.get("tags") or "").strip(),
        )
        contest.recompute_solved()

        # 与 CSV 写入的 solved 不一致时记 warning (不直接 print, 留给调用方)
        if contest.solved != solved_input:
            warnings.append(
                f"第 {row_num} 行 {slug}: CSV solved={solved_input} "
                f"与 problems 算出 {contest.solved} 不一致, 以 problems 为准"
            )

        return contest

    # === 查询 ===

    def all(self) -> list[Contest]:
        if not self._loaded:
            self.load()
        # 按日期倒序
        return sorted(self._contests.values(), key=lambda c: c.iso_date, reverse=True)

    def get(self, slug: str) -> Contest | None:
        if not self._loaded:
            self.load()
        return self._contests.get(slug)

    def exists(self, slug: str) -> bool:
        if not self._loaded:
            self.load()
        return slug in self._contests

    def __iter__(self) -> Iterator[Contest]:
        return iter(self.all())

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._contests)

    def __contains__(self, slug: str) -> bool:
        return self.exists(slug)

    # === 增删改 (in-memory, 需 save() 落盘) ===

    def add(self, contest: Contest) -> None:
        """添加新比赛. 已存在或字段非法抛 CsvValidationError."""
        self._validate_for_write(contest)
        if contest.slug in self._contests:
            raise CsvValidationError(
                f"slug 已存在: {contest.slug}", slug=contest.slug,
            )
        contest.recompute_solved()
        self._contests[contest.slug] = contest

    def update(self, slug: str, **fields) -> Contest:
        """更新比赛任意字段. 返回更新后的 contest.

        Raises:
            CsvValidationError: slug 不存在 / 字段名非法 / 字段值非法
        """
        if slug not in self._contests:
            raise CsvValidationError(f"slug 不存在: {slug}", slug=slug)

        c = self._contests[slug]
        for k, v in fields.items():
            if not hasattr(c, k):
                raise CsvValidationError(
                    f"未知字段: {k}", slug=slug,
                )
            setattr(c, k, v)

        # 重算 solved
        if "problems" in fields:
            c.recompute_solved()

        # 校验 (允许修改 slug 到新值, 但要检查冲突)
        self._validate_for_write(c)
        if c.slug != slug:
            # slug 改了
            if c.slug in self._contests:
                raise CsvValidationError(
                    f"目标 slug 已存在: {c.slug}", slug=slug,
                )
            del self._contests[slug]
            self._contests[c.slug] = c

        return c

    def delete(self, slug: str) -> Contest:
        """删除比赛. 返回被删除的 contest."""
        if slug not in self._contests:
            raise CsvValidationError(f"slug 不存在: {slug}", slug=slug)
        return self._contests.pop(slug)

    def _validate_for_write(self, c: Contest) -> None:
        """校验一个 contest 对象用于 add/update."""
        if not SLUG_RE.match(c.slug):
            raise CsvValidationError(f"slug 非法: {c.slug!r}", slug=c.slug)
        if not c.name.strip():
            raise CsvValidationError("name 不能为空", slug=c.slug)
        if not DATE_RE.match(c.date):
            raise CsvValidationError(f"date 格式不对: {c.date!r}", slug=c.slug)
        if c.total <= 0:
            raise CsvValidationError(f"total 必须 > 0: {c.total}", slug=c.slug)
        if len(c.problems) != c.total:
            raise CsvValidationError(
                f"problems 长度 {len(c.problems)} ≠ total {c.total}",
                slug=c.slug,
            )
        for j, p in enumerate(c.problems, start=1):
            if p not in ALLOWED_PROBLEM_CHARS:
                raise CsvValidationError(
                    f"problems 第 {j} 个字符非法: {p!r}",
                    slug=c.slug,
                )

    # === 持久化 ===

    def save(self) -> None:
        """原子写回磁盘: 写 .tmp, rename, 不留临时文件."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")

        contests = self.all()  # 按日期倒序
        with tmp_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(HEADER)
            for c in contests:
                writer.writerow([
                    c.slug,
                    c.name,
                    c.date,
                    c.solved,
                    c.total,
                    problems_to_string(c.problems),
                    c.link,
                    c.tags,
                ])

        # 原子 rename (POSIX 保证)
        tmp_path.replace(self.path)

    # === 上下文 ===

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 不自动 save, 让调用方显式决定
        return False