"""
sync.py 单元测试。

跑法：
  python3 -m unittest discover tests/ -v
或：
  make test
"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# 让 import 找到 tools/
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import sync  # noqa: E402


# ---------- helpers ----------

def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def make_valid_row(slug: str = "2024-test", total: int = 5, solved: int = 3,
                   date: str = "2024.5.1", problems: str = "O;O;.;O;O",
                   name: str = "Test", link: str = "", tags: str = "") -> list[str]:
    return [slug, name, date, str(solved), str(total), problems, link, tags]


# ---------- parsing ----------

class TestParseProblems(unittest.TestCase):
    """problems 列的两种写法都应被接受。"""

    def test_compact_form(self):
        self.assertEqual(sync.parse_problems("OO.OO", 1), ["O", "O", ".", "O", "O"])

    def test_explicit_form(self):
        self.assertEqual(
            sync.parse_problems("O;Ø;O;.;!", 1),
            ["O", "Ø", "O", ".", "!"],
        )

    def test_with_spaces_around_separator(self):
        self.assertEqual(
            sync.parse_problems("O ; O ; . ; O", 1),
            ["O", "O", ".", "O"],
        )

    def test_empty(self):
        self.assertEqual(sync.parse_problems("", 1), [])

    def test_empty_segment_errors(self):
        with self.assertRaises(SystemExit) as cm:
            sync.parse_problems("O;;O", 1)
        self.assertIn("空段", str(cm.exception))

    def test_invalid_char_errors(self):
        # parse_problems 本身只切分；合法性在 read_csv 阶段检查
        # 这里确认它不会静默吞掉
        self.assertEqual(sync.parse_problems("X;O", 1), ["X", "O"])


class TestDateRe(unittest.TestCase):
    def test_dots(self):
        self.assertIsNotNone(sync.DATE_RE.match("2024.5.1"))

    def test_dashes(self):
        self.assertIsNotNone(sync.DATE_RE.match("2024-05-01"))

    def test_slashes(self):
        self.assertIsNotNone(sync.DATE_RE.match("2024/5/1"))

    def test_rejects_garbage(self):
        self.assertIsNone(sync.DATE_RE.match("May 1, 2024"))
        self.assertIsNone(sync.DATE_RE.match("abc"))
        # 注：regex 不查月份/日期范围，靠 ISO 字符串排序时自然排错位置
        # 真正校验月份范围靠人工或未来加（看 sync.py 顶部注释）


# ---------- read_csv ----------

class TestReadCsv(unittest.TestCase):
    HEADER = ["slug", "name", "date", "solved", "total", "problems", "link", "tags"]

    def test_minimal_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [make_valid_row()])
            cs = sync.read_csv(p)
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.slug, "2024-test")
        self.assertEqual(c.iso_date, "2024-05-01")  # 零填充
        self.assertEqual(c.problems, ["O", "O", ".", "O", "O"])

    def test_missing_required_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, ["slug", "name", "date"], [["x", "X", "2024.1.1"]])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("缺少必填列", str(cm.exception))

    def test_empty_required_value(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [["x", "X", "", "1", "1", "O"]])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("date", str(cm.exception))

    def test_bad_slug(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [make_valid_row(slug="BAD SLUG")])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("slug 非法", str(cm.exception))

    def test_duplicate_slug(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [
                make_valid_row(slug="dup"),
                make_valid_row(slug="dup"),
            ])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("slug 重复", str(cm.exception))

    def test_solved_exceeds_total(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [make_valid_row(total=3, solved=5)])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("solved", str(cm.exception))

    def test_problems_length_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [make_valid_row(total=5, problems="O;O;.;O")])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("长度", str(cm.exception))

    def test_invalid_problem_char(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [make_valid_row(total=3, problems="O;X;O")])
            with self.assertRaises(SystemExit) as cm:
                sync.read_csv(p)
            self.assertIn("problems 第 2 个字符非法", str(cm.exception))

    def test_solved_field_normalized_from_problems(self):
        """solved 列允许与 problems 算出来的不一致（同步脚本会 warn），不应报错。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            # problems 有 2 个 O + 1 个 Ø = 3 通过，但 solved 列写 0
            write_csv(p, self.HEADER, [make_valid_row(total=3, solved=0, problems="O;O;Ø")])
            cs = sync.read_csv(p)
        self.assertEqual(len(cs), 1)

    def test_sorted_by_date_desc(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, self.HEADER, [
                make_valid_row(slug="old", date="2020.1.1"),
                make_valid_row(slug="new", date="2024.5.1"),
            ])
            cs = sync.read_csv(p)
        # 排序在 main() 里做，不在 read_csv 里
        cs.sort(key=lambda c: c.iso_date, reverse=True)
        self.assertEqual([c.slug for c in cs], ["new", "old"])


# ---------- render_table ----------

class TestRenderTable(unittest.TestCase):
    def test_columns_match_max_total(self):
        cs = [
            sync.Contest("a", "A", "2024.1.1", 1, 3, ["O", "O", "."], "", ""),
            sync.Contest("b", "B", "2024.2.1", 1, 5, ["O", ".", ".", ".", "O"], "", ""),
        ]
        table = sync.render_table(cs)
        # 表头有 A B C D E
        self.assertIn("| A | B | C | D | E |", table)
        # 短行（A 只有 3 题）后面应该有 2 个空格子
        for line in table.splitlines():
            if line.startswith("| [A]"):
                self.assertIn("|  |  |", line)

    def test_zero_contests(self):
        self.assertIn("| 比赛 | 日期 | 题数 |  |  |", sync.render_table([]))


# ---------- 端到端：写一个临时项目跑 main() ----------

class TestEndToEnd(unittest.TestCase):
    """模拟一个最小项目，跑 main() 不报错并产生预期文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 必要的目录
        (self.root / "docs" / "contests").mkdir(parents=True)
        (self.root / "docs" / "data").mkdir(parents=True)
        (self.root / "docs" / "index.md").write_text(
            textwrap.dedent("""\
                # 主页
                累计 < !-- SYNC:COUNT -->0<!-- /SYNC:COUNT --> 场
                通过 < !-- SYNC:SOLVED -->0<!-- /SYNC:SOLVED --> 题
                <!-- SYNC:CONTESTS-START -->
                <!-- SYNC:CONTESTS-END -->
            """).replace(" < ", "<"),
            encoding="utf-8",
        )
        (self.root / "contests.csv").write_text(
            "slug,name,date,solved,total,problems,link,tags\n"
            "2024-t,A,2024.5.1,2,3,O;O;.,,\n"
            "2020-b,B,2020.1.1,1,2,O;.,,\n",
            encoding="utf-8",
        )
        # 备份模块级常量并指向 tmp
        self._orig_root = sync.REPO_ROOT
        self._orig_csv = sync.CSV_PATH
        self._orig_index = sync.INDEX_MD
        self._orig_contests = sync.CONTESTS_DIR
        self._orig_data = sync.DATA_DIR
        self._orig_json = sync.DATA_JSON
        sync.REPO_ROOT = self.root
        sync.CSV_PATH = self.root / "contests.csv"
        sync.INDEX_MD = self.root / "docs" / "index.md"
        sync.CONTESTS_DIR = self.root / "docs" / "contests"
        sync.DATA_DIR = self.root / "docs" / "data"
        sync.DATA_JSON = self.root / "docs" / "data" / "contests.json"

    def tearDown(self):
        sync.REPO_ROOT = self._orig_root
        sync.CSV_PATH = self._orig_csv
        sync.INDEX_MD = self._orig_index
        sync.CONTESTS_DIR = self._orig_contests
        sync.DATA_DIR = self._orig_data
        sync.DATA_JSON = self._orig_json
        self.tmp.cleanup()

    def test_main_creates_placeholder_and_data_json(self):
        # 模拟 CLI 参数
        import argparse
        args = argparse.Namespace(
            csv=sync.CSV_PATH,
            check=False,
            dry_run=False,
        )
        # 直接调内部函数（避免依赖 argparse CLI）
        contests = sync.read_csv(args.csv)
        contests.sort(key=lambda c: c.iso_date, reverse=True)
        sync.update_index_md(contests, dry_run=False)
        sync.create_placeholders(contests, dry_run=False)
        sync.write_data_json(contests, dry_run=False)

        # 占位页
        self.assertTrue((sync.CONTESTS_DIR / "2024-t.md").exists())
        self.assertTrue((sync.CONTESTS_DIR / "2020-b.md").exists())
        # data json
        import json
        data = json.loads(sync.DATA_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data["header"][0], "slug")
        self.assertEqual(len(data["rows"]), 2)
        # index.md 标记被替换
        idx = sync.INDEX_MD.read_text(encoding="utf-8")
        self.assertIn("<!-- SYNC:COUNT -->2<!-- /SYNC:COUNT -->", idx)
        self.assertIn("<!-- SYNC:SOLVED -->3<!-- /SYNC:SOLVED -->", idx)
        # 排序：2024 在前
        self.assertLess(
            idx.find("[A](contests/2024-t.md)"),
            idx.find("[B](contests/2020-b.md)"),
        )

    def test_existing_placeholder_not_overwritten(self):
        target = sync.CONTESTS_DIR / "2024-t.md"
        target.write_text("# 用户手写的笔记，不要覆盖\n", encoding="utf-8")
        import argparse
        args = argparse.Namespace(csv=sync.CSV_PATH, check=False, dry_run=False)
        contests = sync.read_csv(args.csv)
        sync.create_placeholders(contests, dry_run=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "# 用户手写的笔记，不要覆盖\n")

    def test_check_flag_does_not_write(self):
        # 直接跑 CLI 一次确认 --check 模式
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "sync.py"), "--csv",
             str(sync.CSV_PATH), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 主页模板未被改（占位标记里仍是 0）
        idx = sync.INDEX_MD.read_text(encoding="utf-8")
        self.assertIn("<!-- SYNC:COUNT -->0<!-- /SYNC:COUNT -->", idx)
        # 没有新建占位页
        self.assertEqual(list(sync.CONTESTS_DIR.iterdir()), [])


# ---------- Contest.iso_date ----------

class TestIsoDate(unittest.TestCase):
    def test_zero_pads(self):
        c = sync.Contest("x", "X", "2024.5.1", 0, 1, ["."], "", "")
        self.assertEqual(c.iso_date, "2024-05-01")

    def test_double_digit_unchanged(self):
        c = sync.Contest("x", "X", "2024.12.31", 0, 1, ["."], "", "")
        self.assertEqual(c.iso_date, "2024-12-31")


if __name__ == "__main__":
    unittest.main()
