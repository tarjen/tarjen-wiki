"""
tests/test_csv_store.py

csv_store.py 单元测试。
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from csv_store import (  # noqa: E402
    ALLOWED_PROBLEM_CHARS,
    Contest,
    CsvStore,
    CsvValidationError,
    DATE_RE,
    HEADER,
    SLUG_RE,
    normalize_date,
    parse_problems,
    problems_to_string,
)


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def make_row(slug: str = "2024-test", total: int = 5, solved: int | None = None,
             date: str = "2024.5.1", problems: str = "O;O;.;O;O",
             name: str = "Test", link: str = "", tags: str = "") -> list[str]:
    # 默认根据 problems 算 solved, 避免 "solved 不一致" warning
    if solved is None:
        solved = sum(1 for p in problems.split(";") if p in ("O", "Ø"))
    return [slug, name, date, str(solved), str(total), problems, link, tags]


# === parse_problems ===

class TestParseProblems(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(parse_problems("OO.OO"), ["O", "O", ".", "O", "O"])

    def test_explicit(self):
        self.assertEqual(parse_problems("O;Ø;O;.;!"), ["O", "Ø", "O", ".", "!"])

    def test_with_spaces(self):
        self.assertEqual(parse_problems("O ; O ; ."), ["O", "O", "."])

    def test_empty(self):
        self.assertEqual(parse_problems(""), [])

    def test_empty_segment_raises(self):
        with self.assertRaises(ValueError):
            parse_problems("O;;O")


# === Date / slug regex ===

class TestDateRe(unittest.TestCase):
    def test_accepts_dots(self):
        self.assertTrue(DATE_RE.match("2024.5.1"))

    def test_accepts_dashes(self):
        self.assertTrue(DATE_RE.match("2024-05-01"))

    def test_accepts_slashes(self):
        self.assertTrue(DATE_RE.match("2024/5/1"))

    def test_rejects_garbage(self):
        self.assertFalse(DATE_RE.match("May 1, 2024"))
        self.assertFalse(DATE_RE.match("abc"))


class TestSlugRe(unittest.TestCase):
    def test_simple(self):
        self.assertTrue(SLUG_RE.match("abc"))
        self.assertTrue(SLUG_RE.match("2024-icpc"))

    def test_with_underscore_dot(self):
        self.assertTrue(SLUG_RE.match("a_b.c"))

    def test_rejects_space(self):
        self.assertFalse(SLUG_RE.match("BAD SLUG"))

    def test_rejects_uppercase(self):
        self.assertFalse(SLUG_RE.match("ABC"))


class TestNormalizeDate(unittest.TestCase):
    def test_slash(self):
        self.assertEqual(normalize_date("2024/5/1"), "2024.5.1")

    def test_dash_preserves_zero_pad(self):
        # 保留用户输入的零填充格式, 只换分隔符
        self.assertEqual(normalize_date("2024-05-01"), "2024.05.01")

    def test_already_dots(self):
        self.assertEqual(normalize_date("2024.5.1"), "2024.5.1")

    def test_already_dots_padded(self):
        self.assertEqual(normalize_date("2024.05.01"), "2024.05.01")


# === Contest properties ===

class TestContestProperties(unittest.TestCase):
    def _make(self, problems):
        return Contest(
            slug="x", name="X", date="2024.5.1", solved=0,
            total=len(problems), problems=problems, link="", tags="",
        )

    def test_iso_date(self):
        c = self._make(["."])
        self.assertEqual(c.iso_date, "2024-05-01")

    def test_iso_date_double_digit(self):
        c = Contest("x", "X", "2024.12.31", 0, 1, ["."], "", "")
        self.assertEqual(c.iso_date, "2024-12-31")

    def test_in_contest_solved(self):
        self.assertEqual(self._make(["O", "O", ".", "!"]).in_contest_solved, 2)

    def test_upsolved(self):
        self.assertEqual(self._make(["Ø", "O", "Ø"]).upsolved, 2)

    def test_tried_unsolved(self):
        self.assertEqual(self._make(["!", "O", "!"]).tried_unsolved, 2)

    def test_untouched(self):
        self.assertEqual(self._make([".", "O", "."]).untouched, 2)

    def test_recompute_solved(self):
        c = self._make(["O", "O", "Ø", ".", "!"])
        self.assertEqual(c.recompute_solved(), 3)  # 2 O + 1 Ø
        self.assertEqual(c.solved, 3)

    def test_tags_list(self):
        c = Contest("x", "X", "2024.5.1", 0, 1, ["."], "", "#icpc #regional")
        self.assertEqual(c.tags_list, ["#icpc", "#regional"])

    def test_tags_list_empty(self):
        c = Contest("x", "X", "2024.5.1", 0, 1, ["."], "", "")
        self.assertEqual(c.tags_list, [])


class TestProblemsToString(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(problems_to_string(["O", "O", "."]), "O;O;.")

    def test_with_unicode(self):
        self.assertEqual(problems_to_string(["O", "Ø", "!"]), "O;Ø;!")


# === Load ===

class TestLoad(unittest.TestCase):
    def test_minimal(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row()])
            store = CsvStore(p)
            store.load()
            contests = store.all()
            self.assertEqual(len(contests), 1)
            self.assertEqual(contests[0].slug, "2024-test")
            self.assertEqual(contests[0].problems, ["O", "O", ".", "O", "O"])
            self.assertEqual(contests[0].solved, 4)  # O+O+O+O

    def test_load_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            p.write_text(",".join(HEADER) + "\n", encoding="utf-8")
            store = CsvStore(p)
            store.load()
            self.assertEqual(len(store.all()), 0)

    def test_load_nonexistent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nope.csv"
            store = CsvStore(p)
            store.load()
            self.assertEqual(len(store.all()), 0)

    def test_load_missing_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, ["slug", "name", "date"], [["x", "X", "2024.1.1"]])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("缺少必填列", str(cm.exception))

    def test_load_bad_slug(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(slug="BAD SLUG")])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("slug 非法", str(cm.exception))
            self.assertEqual(cm.exception.row_num, 2)
            self.assertEqual(cm.exception.slug, "BAD SLUG")

    def test_load_bad_date(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(date="May 1")])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("date 格式不对", str(cm.exception))

    def test_load_total_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(total=0)])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("total", str(cm.exception))

    def test_load_problems_length_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(total=5, problems="O;O;.;O")])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("长度", str(cm.exception))

    def test_load_invalid_char(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(total=3, problems="O;X;O")])
            store = CsvStore(p)
            with self.assertRaises(CsvValidationError) as cm:
                store.load()
            self.assertIn("字符非法", str(cm.exception))

    def test_load_solved_inconsistent_warns_only(self):
        """solved 列与 problems 不一致时只 warn, 不报错."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            # problems 有 2 O + 1 Ø = 3 通过, 但 solved 列写 0
            write_csv(p, HEADER, [make_row(total=3, solved=0, problems="O;O;Ø")])
            store = CsvStore(p)
            # 应该 warn 到 stderr, 不抛异常
            store.load()
            c = store.get("2024-test")
            self.assertEqual(c.solved, 3)  # 用 problems 重算的

    def test_load_sorted_by_date_desc(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [
                make_row(slug="old", date="2020.1.1"),
                make_row(slug="new", date="2024.5.1"),
            ])
            store = CsvStore(p)
            store.load()
            self.assertEqual([c.slug for c in store.all()], ["new", "old"])

    def test_load_normalizes_date(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row(date="2024/05/01")])
            store = CsvStore(p)
            store.load()
            self.assertEqual(store.get("2024-test").date, "2024.05.01")
            # 但 iso_date 总是零填充 (用于排序)
            self.assertEqual(store.get("2024-test").iso_date, "2024-05-01")


# === Query ===

class TestQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c.csv"
        write_csv(self.path, HEADER, [
            make_row(slug="a", date="2020.1.1"),
            make_row(slug="b", date="2024.5.1"),
            make_row(slug="c", date="2022.3.1"),
        ])
        self.store = CsvStore(self.path)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_get(self):
        c = self.store.get("b")
        self.assertIsNotNone(c)
        self.assertEqual(c.name, "Test")

    def test_get_missing(self):
        self.assertIsNone(self.store.get("nonexistent"))

    def test_exists(self):
        self.assertTrue(self.store.exists("a"))
        self.assertFalse(self.store.exists("nonexistent"))

    def test_len(self):
        self.assertEqual(len(self.store), 3)

    def test_iter(self):
        slugs = [c.slug for c in self.store]
        self.assertEqual(slugs, ["b", "c", "a"])  # sorted desc

    def test_contains(self):
        self.assertIn("a", self.store)
        self.assertNotIn("nonexistent", self.store)


# === Add ===

class TestAdd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c.csv"
        self.store = CsvStore(self.path)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_valid(self):
        c = Contest(
            "new-slug", "New Contest", "2025.6.12", 0, 3,
            ["O", "Ø", "."], "", "#icpc",
        )
        self.store.add(c)
        self.assertTrue(self.store.exists("new-slug"))
        stored = self.store.get("new-slug")
        self.assertEqual(stored.solved, 2)  # O + Ø, 自动重算
        self.assertEqual(stored.tags_list, ["#icpc"])

    def test_add_recomputes_solved(self):
        """即使调用方传了错 solved, 也会被覆盖."""
        c = Contest("x", "X", "2025.6.12", 999, 3,
                    ["O", ".", "."], "", "")  # solved 传错
        self.store.add(c)
        self.assertEqual(self.store.get("x").solved, 1)  # 实际只有 1 个 O

    def test_add_duplicate_raises(self):
        c = Contest("dup", "Dup", "2025.6.12", 0, 3, [".", ".", "."], "", "")
        self.store.add(c)
        with self.assertRaises(CsvValidationError) as cm:
            self.store.add(c)
        self.assertIn("已存在", str(cm.exception))

    def test_add_invalid_slug_raises(self):
        c = Contest("BAD SLUG", "X", "2025.6.12", 0, 1, ["."], "", "")
        with self.assertRaises(CsvValidationError):
            self.store.add(c)

    def test_add_empty_name_raises(self):
        c = Contest("ok", "  ", "2025.6.12", 0, 1, ["."], "", "")
        with self.assertRaises(CsvValidationError):
            self.store.add(c)

    def test_add_problems_length_mismatch_raises(self):
        c = Contest("ok", "X", "2025.6.12", 0, 3, [".", "."], "", "")
        with self.assertRaises(CsvValidationError) as cm:
            self.store.add(c)
        self.assertIn("长度", str(cm.exception))

    def test_add_invalid_char_raises(self):
        c = Contest("ok", "X", "2025.6.12", 0, 2, ["O", "X"], "", "")
        with self.assertRaises(CsvValidationError):
            self.store.add(c)


# === Update ===

class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c.csv"
        write_csv(self.path, HEADER, [make_row(slug="x", total=3, problems=".;.;.")])
        self.store = CsvStore(self.path)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_name(self):
        self.store.update("x", name="New Name")
        self.assertEqual(self.store.get("x").name, "New Name")

    def test_update_missing_raises(self):
        with self.assertRaises(CsvValidationError):
            self.store.update("nonexistent", name="X")

    def test_update_invalid_field_raises(self):
        with self.assertRaises(CsvValidationError) as cm:
            self.store.update("x", nonexistent_field="X")
        self.assertIn("未知字段", str(cm.exception))

    def test_update_problems_recomputes_solved(self):
        self.store.update("x", problems=["O", "O", "O"])
        self.assertEqual(self.store.get("x").solved, 3)

    def test_update_tags(self):
        self.store.update("x", tags="#new #tags")
        self.assertEqual(self.store.get("x").tags, "#new #tags")


# === Delete ===

class TestDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c.csv"
        write_csv(self.path, HEADER, [make_row(slug="x")])
        self.store = CsvStore(self.path)
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_existing(self):
        deleted = self.store.delete("x")
        self.assertEqual(deleted.slug, "x")
        self.assertFalse(self.store.exists("x"))
        self.assertEqual(len(self.store), 0)

    def test_delete_missing_raises(self):
        with self.assertRaises(CsvValidationError) as cm:
            self.store.delete("nonexistent")
        self.assertIn("不存在", str(cm.exception))


# === Save / round-trip ===

class TestSave(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [
                make_row(slug="a", date="2020.1.1"),
                make_row(slug="b", date="2024.5.1"),
            ])
            store1 = CsvStore(p)
            store1.load()
            store1.save()

            store2 = CsvStore(p)
            store2.load()
            self.assertEqual([c.slug for c in store2.all()], ["b", "a"])

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "subdir" / "c.csv"
            store = CsvStore(p)
            store.load()
            store.add(Contest("new", "X", "2025.6.12", 0, 1, ["."], "", ""))
            store.save()
            self.assertTrue(p.exists())

    def test_save_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            write_csv(p, HEADER, [make_row()])
            store = CsvStore(p)
            store.load()
            store.save()
            # 不应留下 .tmp 文件
            tmp_files = list(Path(d).glob("*.tmp"))
            self.assertEqual(tmp_files, [])

    def test_save_full_round_trip(self):
        """加几条, save, 重 load, 字段完整保留."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            store1 = CsvStore(p)
            store1.load()
            store1.add(Contest(
                "z", "Z Contest", "2025.6.12", 0, 5,
                ["O", "O", "Ø", "!", "."], "https://x", "#tag #another",
            ))
            store1.add(Contest(
                "a", "A Contest", "2024.1.1", 0, 2,
                ["O", "Ø"], "", "#old",
            ))
            store1.save()

            store2 = CsvStore(p)
            store2.load()
            self.assertEqual(len(store2), 2)

            c_z = store2.get("z")
            self.assertEqual(c_z.name, "Z Contest")
            self.assertEqual(c_z.problems, ["O", "O", "Ø", "!", "."])
            self.assertEqual(c_z.link, "https://x")
            self.assertEqual(c_z.tags, "#tag #another")
            self.assertEqual(c_z.solved, 3)  # O+O+Ø

            # 按日期倒序
            self.assertEqual([c.slug for c in store2.all()], ["z", "a"])

    def test_update_then_save(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            store1 = CsvStore(p)
            store1.load()
            store1.add(Contest("x", "X", "2025.6.12", 0, 1, ["."], "", ""))
            store1.save()

            store2 = CsvStore(p)
            store2.load()
            store2.update("x", problems=["O"])
            store2.save()

            store3 = CsvStore(p)
            store3.load()
            self.assertEqual(store3.get("x").problems, ["O"])
            self.assertEqual(store3.get("x").solved, 1)


# === End-to-end with existing CSV (smoke) ===

class TestEndToEndWithRealCsv(unittest.TestCase):
    """Smoke test: load real contests.csv from the repo."""

    @classmethod
    def setUpClass(cls):
        cls.real_csv = REPO_ROOT / "contests.csv"
        if not cls.real_csv.exists():
            cls.skipTest("no contests.csv in repo root")

    def test_load_real(self):
        store = CsvStore(self.real_csv)
        store.load()
        contests = store.all()
        self.assertGreater(len(contests), 0)
        # 每条都有合法字段
        for c in contests:
            self.assertTrue(SLUG_RE.match(c.slug))
            self.assertTrue(DATE_RE.match(c.date))
            self.assertEqual(len(c.problems), c.total)
            self.assertEqual(c.solved, c.in_contest_solved + c.upsolved)


if __name__ == "__main__":
    unittest.main()