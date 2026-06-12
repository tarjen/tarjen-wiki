"""
tests/test_md_store.py

md_store.py 单元测试。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from csv_store import Contest  # noqa: E402
from md_store import CONTEST_TEMPLATE, MdStore  # noqa: E402


def make_contest(slug="2025-icpc-xxx", name="2025 ICPC XXX",
                 date="2025.6.12", total=13, solved=7,
                 link="https://x", tags="#icpc #regional") -> Contest:
    return Contest(
        slug=slug, name=name, date=date, solved=solved, total=total,
        problems=["O"] * solved + ["."] * (total - solved),
        link=link, tags=tags,
    )


class TestExists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = MdStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exists_false(self):
        self.assertFalse(self.store.exists("nonexistent"))

    def test_exists_true(self):
        self.store.write("x", "# X")
        self.assertTrue(self.store.exists("x"))


class TestReadWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = MdStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_creates_file(self):
        self.store.write("x", "# X\ncontent")
        self.assertTrue((self.dir / "x.md").exists())
        self.assertEqual((self.dir / "x.md").read_text(encoding="utf-8"), "# X\ncontent")

    def test_read(self):
        self.store.write("x", "# Hello\n")
        self.assertEqual(self.store.read("x"), "# Hello\n")

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.store.read("nonexistent")

    def test_write_overwrites(self):
        self.store.write("x", "old")
        self.store.write("x", "new")
        self.assertEqual(self.store.read("x"), "new")

    def test_write_creates_dir(self):
        nested = self.dir / "nested" / "deeper"
        store = MdStore(nested)
        store.write("x", "y")
        self.assertTrue((nested / "x.md").exists())

    def test_write_no_tmp_left(self):
        self.store.write("x", "y")
        tmp_files = list(self.dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [])


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = MdStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_existing(self):
        self.store.write("x", "y")
        result = self.store.delete("x")
        self.assertTrue(result)
        self.assertFalse(self.store.exists("x"))

    def test_delete_missing(self):
        result = self.store.delete("nonexistent")
        self.assertFalse(result)


class TestPlaceholder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = MdStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_placeholder_basic(self):
        c = make_contest(date="2025.6.12")
        body = self.store.placeholder(c)
        self.assertIn("# 2025 ICPC XXX", body)
        self.assertIn("比赛日期 | 2025-06-12", body)  # iso format
        self.assertIn("通过 | 7 / 13", body)
        self.assertIn("https://x", body)
        self.assertIn("#icpc #regional", body)

    def test_placeholder_includes_slug(self):
        c = make_contest(slug="2025-icpc-xxx-regional")
        body = self.store.placeholder(c)
        self.assertIn("2025-icpc-xxx-regional", body)

    def test_placeholder_no_link(self):
        c = make_contest(link="")
        body = self.store.placeholder(c)
        # 模板里 link 字段为空时, 表格行也应该有 (但内容为空)
        self.assertIn("| 比赛链接 |", body)

    def test_placeholder_date_zero_pad_iso(self):
        c = make_contest(date="2025.06.07")
        body = self.store.placeholder(c)
        self.assertIn("2025-06-07", body)


class TestIntegrationWithRealDir(unittest.TestCase):
    """Smoke test: 真实 docs/contests/ 目录."""

    @classmethod
    def setUpClass(cls):
        cls.real_dir = REPO_ROOT / "docs" / "contests"
        if not cls.real_dir.exists():
            cls.skipTest("no docs/contests/")

    def test_taichung_exists(self):
        store = MdStore(self.real_dir)
        self.assertTrue(store.exists("2024-icpc-asia-taichung"))
        content = store.read("2024-icpc-asia-taichung")
        self.assertIn("2024 ICPC Asia Taichung", content)


if __name__ == "__main__":
    unittest.main()