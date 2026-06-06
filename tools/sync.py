#!/usr/bin/env python3
"""
contests.csv -> MkDocs 同步脚本。

读取根目录的 contests.csv，重新生成 docs/index.md 里的总表，
并为新增的 slug 创建 docs/contests/<slug>.md 占位详情页（不会覆盖已有文件）。

CSV 列定义（必填项标 *）：
  - slug*    : 文件名短名，全小写英文 + -
  - name*    : 显示名（可含中文、逗号、引号）
  - date*    : 显示日期，格式 YYYY.M.D（也接受 YYYY.MM.DD / YYYY-MM-DD）
  - solved*  : 通过数，整数（O + F 的数量；脚本会用 problems 重算并 warn）
  - total*   : 题目数，整数
  - problems*: 状态序列，用 ; 分隔，长度必须 == total
               允许字符：
                 O  = 赛时过题
                 Ø  = 赛后补过
                 !  = 尝试但没过
                 .  = 未做
                 ?  = 待补
  - link     : 比赛链接（选填）
  - tags     : 空格分隔的 #tag（选填）

problems 列的写法示例（total=5 时）：
  "O;O;.;O;O"        ← 推荐，位置 = A B C D E
  "OO.OO"            ← 紧凑写法也行，脚本会展开
  "O;Ø;O;.;"         ← 含补过

用法：
  python3 tools/sync.py            # 应用变更（写 index.md + 新建占位页）
  python3 tools/sync.py --check    # 校验 CSV，不写任何文件
  python3 tools/sync.py --dry-run  # 打印将做什么，不写
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "contests.csv"
INDEX_MD = REPO_ROOT / "docs" / "index.md"
CONTESTS_DIR = REPO_ROOT / "docs" / "contests"
DATA_DIR = REPO_ROOT / "docs" / "data"
DATA_JSON = DATA_DIR / "contests.json"

# 表格列数完全由数据决定：列数 = CSV 里 max(total)。
# 加新比赛时 total 变大，列数自动扩展；删干净以后回到 0 列。

DATE_RE = re.compile(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$")
ALLOWED_PROBLEM_CHARS = set("OØ.!")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]*$")


@dataclass
class Contest:
    slug: str
    name: str
    date: str  # 已规范为 YYYY.M.D
    solved: int
    total: int
    problems: list[str]  # 长度 == total
    link: str
    tags: str

    @property
    def iso_date(self) -> str:
        """用于排序的 ISO 日期字符串。"""
        y, m, d = self.date.split(".")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


# ---------- 读 CSV ----------

def read_csv(path: Path) -> list[Contest]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"slug", "name", "date", "solved", "total", "problems"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"CSV 缺少必填列: {', '.join(sorted(missing))}")

        contests: list[Contest] = []
        seen: set[str] = set()
        for i, row in enumerate(reader, start=2):  # 行号从 2 开始（第 1 行是表头）
            row_num = i
            for k in required:
                if not (row.get(k) or "").strip():
                    sys.exit(f"第 {row_num} 行 `{k}` 列为空: {row}")

            slug = row["slug"].strip()
            if not SLUG_RE.match(slug):
                sys.exit(f"第 {row_num} 行 slug 非法: {slug!r}（只允许小写字母、数字、- _ .）")
            if slug in seen:
                sys.exit(f"第 {row_num} 行 slug 重复: {slug}")
            seen.add(slug)

            name = row["name"].strip()
            date_raw = row["date"].strip()
            m = DATE_RE.match(date_raw)
            if not m:
                sys.exit(f"第 {row_num} 行 date 格式不对: {date_raw!r}（要 YYYY.M.D）")
            # 保留用户输入的原始显示格式，只把分隔符统一成 `.`
            date_norm = date_raw.replace("/", ".").replace("-", ".")

            try:
                solved = int(row["solved"])
                total = int(row["total"])
            except ValueError:
                sys.exit(f"第 {row_num} 行 solved/total 不是整数: {row['solved']!r} / {row['total']!r}")

            if solved < 0 or total <= 0:
                sys.exit(f"第 {row_num} 行 solved/total 非法: solved={solved}, total={total}")
            if solved > total:
                sys.exit(f"第 {row_num} 行 solved ({solved}) 大于 total ({total})")

            problems = parse_problems(row["problems"].strip(), row_num)
            if len(problems) != total:
                sys.exit(
                    f"第 {row_num} 行 problems 长度 {len(problems)} ≠ total {total}: "
                    f"{row['problems']!r}"
                )
            # 校验每格
            for j, p in enumerate(problems, start=1):
                if p not in ALLOWED_PROBLEM_CHARS:
                    sys.exit(f"第 {row_num} 行 problems 第 {j} 个字符非法: {p!r}（只允许 {ALLOWED_PROBLEM_CHARS}）")

            contests.append(Contest(
                slug=slug,
                name=name,
                date=date_norm,
                solved=solved,
                total=total,
                problems=problems,
                link=row.get("link", "").strip(),
                tags=row.get("tags", "").strip(),
            ))
        return contests


def parse_problems(raw: str, row_num: int) -> list[str]:
    """支持 'O;O;.;O' 和 'OO.OO' 两种写法。"""
    raw = raw.strip()
    if not raw:
        return []
    # 紧凑写法：必须是 O/. 串且不含分隔符
    if ";" not in raw and "," not in raw and " " not in raw:
        chars = list(raw)
    else:
        # 显式写法：按 ; 切
        parts = [p.strip() for p in raw.split(";")]
        if any(not p for p in parts):
            sys.exit(f"第 {row_num} 行 problems 含有空段: {raw!r}")
        chars = parts
    return chars


# ---------- 生成表格 ----------

def problem_columns(contests: list[Contest]) -> int:
    return max((c.total for c in contests), default=0)


def render_table(contests: list[Contest]) -> str:
    cols = problem_columns(contests)
    letters = [chr(ord("A") + i) for i in range(cols)]

    # 表头：比赛 | 日期 | 题数 |  | A | B | ... | O
    # 编辑列放在题数后面，表头留空
    header = "| 比赛 | 日期 | 题数 |  | " + " | ".join(letters) + " |"
    align = "|:-----|:----:|:----:|:---:|" + "|".join([":-:"] * cols) + "|"

    # 编辑器 cache-bust：用 editor/index.html 的内容短哈希当 ?_t= 参数
    # 这样编辑器改了就自动失效浏览器缓存；编辑器没改就 hash 不变，index.md 不会 churn
    _editor_path = REPO_ROOT / "docs" / "editor" / "index.html"
    _editor_hash = hashlib.sha1(_editor_path.read_bytes() if _editor_path.exists() else b"").hexdigest()[:6]
    _editor_cache_bust = f"&_t={_editor_hash}"

    # 数据行
    lines = [header, align]
    for c in contests:
        # 题目格子：补齐到 cols，不够的填空
        rendered = list(c.problems) + [""] * (cols - len(c.problems))
        link_target = f"contests/{c.slug}.md"
        # index.md 在站点根，editor/ 是它的子目录，链接不加 ..
        edit_link = f"[✎](editor/?slug={c.slug}{_editor_cache_bust})"
        # 三段式统计：赛时+补题 / 赛时过题 / 总题数
        # 单一数据源：从 problems 字段算，不信任 CSV 的 solved 字段
        in_contest = sum(1 for p in c.problems if p == "O")
        total_solved = sum(1 for p in c.problems if p in ("O", "Ø"))
        if c.solved != total_solved:
            print(
                f"  ⚠ {c.slug}: CSV solved={c.solved} 与 problems 算出 {total_solved} 不一致，"
                f"以 problems 为准"
            )
        count = f"{total_solved}/{in_contest}/{c.total}"
        row = (
            f"| [{c.name}]({link_target}) "
            f"| {c.date} "
            f"| {count} "
            f"| {edit_link} "
            f"| " + " | ".join(rendered) + " |"
        )
        lines.append(row)
    return "\n".join(lines)


# ---------- 更新 index.md ----------

def update_index_md(contests: list[Contest], *, dry_run: bool) -> None:
    text = INDEX_MD.read_text(encoding="utf-8")

    table = render_table(contests)
    new_text = re.sub(
        r"<!-- SYNC:CONTESTS-START -->.*?<!-- SYNC:CONTESTS-END -->",
        f"<!-- SYNC:CONTESTS-START -->\n{table}\n<!-- SYNC:CONTESTS-END -->",
        text,
        count=1,
        flags=re.DOTALL,
    )

    # 统计：从 problems 字段算（与表格保持一致）
    total_solved = sum(
        1 for c in contests for p in c.problems if p in ("O", "Ø")
    )
    new_text = re.sub(
        r"<!-- SYNC:COUNT -->(\d+)<!-- /SYNC:COUNT -->",
        f"<!-- SYNC:COUNT -->{len(contests)}<!-- /SYNC:COUNT -->",
        new_text,
        count=1,
    )
    new_text = re.sub(
        r"<!-- SYNC:SOLVED -->(\d+)<!-- /SYNC:SOLVED -->",
        f"<!-- SYNC:SOLVED -->{total_solved}<!-- /SYNC:SOLVED -->",
        new_text,
        count=1,
    )

    if new_text == text:
        print("[index.md] 无变化")
        return

    if dry_run:
        print(f"[index.md] (dry-run) 将写入:\n{'-' * 40}\n{new_text}\n{'-' * 40}")
    else:
        INDEX_MD.write_text(new_text, encoding="utf-8")
        print(f"[index.md] 已更新（{len(contests)} 行，{total_solved} 题通过）")


# ---------- 创建占位详情页 ----------

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


def create_placeholders(contests: list[Contest], *, dry_run: bool) -> list[str]:
    created: list[str] = []
    for c in contests:
        target = CONTESTS_DIR / f"{c.slug}.md"
        if target.exists():
            continue
        y, m, d = c.date.split(".")
        body = CONTEST_TEMPLATE.format(
            slug=c.slug,
            name=c.name,
            date_iso=f"{int(y):04d}-{int(m):02d}-{int(d):02d}",
            link=c.link or "",
            solved=c.solved,
            total=c.total,
            tags=c.tags,
        )
        if dry_run:
            print(f"[create] (dry-run) {target.relative_to(REPO_ROOT)}")
        else:
            target.write_text(body, encoding="utf-8")
            print(f"[create] {target.relative_to(REPO_ROOT)}")
        created.append(c.slug)
    return created


# ---------- 入口 ----------


def write_data_json(contests: list[Contest], *, dry_run: bool) -> None:
    """生成 docs/data/contests.json 给前端编辑器用。

    仓库根的 contests.csv 不会在 mkdocs dev/serve 里被服务到，
    把它导出成 JSON 到 docs/ 下，前端 fetch 就能拿到。
    """
    payload = {
        "header": ["slug", "name", "date", "solved", "total", "problems", "link", "tags"],
        "rows": [
            {
                "slug": c.slug,
                "name": c.name,
                "date": c.date,
                "solved": c.solved,
                "total": c.total,
                "problems": ";".join(c.problems),
                "link": c.link,
                "tags": c.tags,
            }
            for c in contests
        ],
    }
    if dry_run:
        print(f"[data.json] (dry-run) {DATA_JSON.relative_to(REPO_ROOT)}")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[data.json] 已生成 {len(contests)} 条")


def main() -> None:
    parser = argparse.ArgumentParser(description="把 contests.csv 同步到 wiki")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="CSV 文件路径（默认 contests.csv）")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--dry-run", action="store_true", help="打印动作但不写")
    args = parser.parse_args()

    if not args.csv.exists():
        sys.exit(f"找不到 {args.csv}")

    contests = read_csv(args.csv)
    contests.sort(key=lambda c: c.iso_date, reverse=True)

    print(f"读取 {len(contests)} 条比赛记录")

    if args.check:
        print("✓ CSV 校验通过")
        return

    update_index_md(contests, dry_run=args.dry_run)
    created = create_placeholders(contests, dry_run=args.dry_run)
    write_data_json(contests, dry_run=args.dry_run)
    if created:
        print(f"新建占位页 {len(created)} 个: {', '.join(created)}")
    else:
        print("没有新 slug 需要建占位页")


if __name__ == "__main__":
    main()
