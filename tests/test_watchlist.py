"""
tests/test_watchlist.py

watchlist.py 单元测试。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from watchlist import Watchlist, parse_watchlist, render_watchlist  # noqa: E402


class TestParseWatchlist(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_watchlist("alice\nbob\ncarol"), ["alice", "bob", "carol"])

    def test_with_comments(self):
        text = "# header\nalice\n# comment\nbob\n"
        self.assertEqual(parse_watchlist(text), ["alice", "bob"])

    def test_skip_empty(self):
        self.assertEqual(parse_watchlist("alice\n\nbob\n\n"), ["alice", "bob"])

    def test_strip_whitespace(self):
        self.assertEqual(parse_watchlist("  alice  \n\t bob \n"), ["alice", "bob"])

    def test_inline_comment(self):
        # '#' 后面都是注释
        self.assertEqual(parse_watchlist("alice # note\nbob"), ["alice", "bob"])

    def test_empty(self):
        self.assertEqual(parse_watchlist(""), [])

    def test_all_comments(self):
        self.assertEqual(parse_watchlist("# a\n# b\n"), [])


class TestRenderWatchlist(unittest.TestCase):
    def test_basic(self):
        text = render_watchlist(["alice", "bob"])
        self.assertIn("# Wiki watchlist", text)
        self.assertIn("alice", text)
        self.assertIn("bob", text)
        self.assertTrue(text.endswith("\n"))


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watchlist.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing(self):
        w = Watchlist(self.path)
        w.load()
        self.assertEqual(w.users(), [])

    def test_save_and_load_round_trip(self):
        w1 = Watchlist(self.path)
        w1.add(["alice", "bob"])
        self.assertTrue(self.path.exists())

        w2 = Watchlist(self.path)
        w2.load()
        self.assertEqual(w2.users(), ["alice", "bob"])

    def test_save_creates_parent_dirs(self):
        nested = self.tmp.name + "/a/b/c/watchlist.txt"
        w = Watchlist(Path(nested))
        w.add(["x"])
        self.assertTrue(Path(nested).exists())


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watchlist.txt"
        self.w = Watchlist(self.path)
        self.w.add(["alice", "bob", "carol"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_users(self):
        self.assertEqual(self.w.users(), ["alice", "bob", "carol"])

    def test_contains(self):
        self.assertTrue(self.w.contains("alice"))
        self.assertFalse(self.w.contains("dave"))

    def test_iter(self):
        self.assertEqual(list(self.w), ["alice", "bob", "carol"])

    def test_len(self):
        self.assertEqual(len(self.w), 3)


class TestAdd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watchlist.txt"
        self.w = Watchlist(self.path)
        self.w.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_one(self):
        added = self.w.add(["alice"])
        self.assertEqual(added, ["alice"])
        self.assertEqual(self.w.users(), ["alice"])

    def test_add_multiple(self):
        added = self.w.add(["alice", "bob"])
        self.assertEqual(added, ["alice", "bob"])
        self.assertEqual(self.w.users(), ["alice", "bob"])

    def test_add_skips_existing(self):
        self.w.add(["alice"])
        added = self.w.add(["alice", "bob"])
        self.assertEqual(added, ["bob"])
        self.assertEqual(self.w.users(), ["alice", "bob"])

    def test_add_skips_empty(self):
        added = self.w.add(["", "  ", "alice"])
        self.assertEqual(added, ["alice"])

    def test_add_strips_whitespace(self):
        added = self.w.add(["  alice  "])
        self.assertEqual(added, ["alice"])
        self.assertEqual(self.w.users(), ["alice"])

    def test_persists_after_add(self):
        self.w.add(["alice"])
        # 新建实例读
        w2 = Watchlist(self.path)
        w2.load()
        self.assertEqual(w2.users(), ["alice"])


class TestRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watchlist.txt"
        self.w = Watchlist(self.path)
        self.w.add(["alice", "bob", "carol"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_existing(self):
        removed = self.w.remove(["alice"])
        self.assertEqual(removed, ["alice"])
        self.assertEqual(self.w.users(), ["bob", "carol"])

    def test_remove_nonexisting(self):
        removed = self.w.remove(["dave"])
        self.assertEqual(removed, [])
        self.assertEqual(self.w.users(), ["alice", "bob", "carol"])

    def test_remove_mixed(self):
        removed = self.w.remove(["alice", "dave", "bob"])
        self.assertEqual(removed, ["alice", "bob"])
        self.assertEqual(self.w.users(), ["carol"])

    def test_persists_after_remove(self):
        self.w.remove(["alice"])
        w2 = Watchlist(self.path)
        w2.load()
        self.assertEqual(w2.users(), ["bob", "carol"])


class TestEdgeCases(unittest.TestCase):
    def test_add_does_not_write_if_no_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "watchlist.txt"
            w = Watchlist(path)
            w.add(["alice"])
            mtime1 = path.stat().st_mtime

            import time
            time.sleep(0.01)  # 确保 mtime 会变如果重写

            # 重复 add 不应写
            added = w.add(["alice"])
            self.assertEqual(added, [])
            self.assertEqual(path.stat().st_mtime, mtime1)


if __name__ == "__main__":
    unittest.main()