"""
tests/test_codes_store.py

codes_store.py 单元测试。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from codes_store import CodeFile, CodesStore, ensure_gitignore, lang_to_ext  # noqa: E402


class TestLangToExt(unittest.TestCase):
    def test_cpp_variants(self):
        self.assertEqual(lang_to_ext("GNU C++17"), "cpp")
        self.assertEqual(lang_to_ext("C++17"), "cpp")
        self.assertEqual(lang_to_ext("GNU C++14"), "cpp")

    def test_python(self):
        self.assertEqual(lang_to_ext("Python 3"), "py")
        self.assertEqual(lang_to_ext("PyPy 3"), "py")

    def test_java(self):
        self.assertEqual(lang_to_ext("Java 17"), "java")

    def test_unknown(self):
        self.assertEqual(lang_to_ext("Brainfuck"), "txt")

    def test_none(self):
        self.assertEqual(lang_to_ext(None), "txt")

    def test_fuzzy(self):
        self.assertEqual(lang_to_ext("C++ (GCC 9)"), "cpp")
        self.assertEqual(lang_to_ext("python3"), "py")


class TestPathSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodesStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_rejects_dotdot_user(self):
        with self.assertRaises(ValueError):
            self.store.save(2564, "../escape", "A", "code")

    def test_save_rejects_slash_user(self):
        with self.assertRaises(ValueError):
            self.store.save(2564, "alice/bob", "A", "code")


class TestSaveRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodesStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_read(self):
        self.store.save(2564, "alice", "A", "int main(){}", language="GNU C++17")
        code = self.store.read(2564, "alice", "A")
        self.assertEqual(code, "int main(){}")

    def test_save_creates_dirs(self):
        self.store.save(2564, "alice", "A", "code", language="GNU C++17")
        p = Path(self.tmp.name) / "2564" / "alice" / "A.cpp"
        self.assertTrue(p.exists())

    def test_read_missing_returns_none(self):
        self.assertIsNone(self.store.read(2564, "nobody", "Z"))

    def test_exists(self):
        self.assertFalse(self.store.exists(2564, "alice", "A"))
        self.store.save(2564, "alice", "A", "code")
        self.assertTrue(self.store.exists(2564, "alice", "A"))

    def test_save_with_meta(self):
        self.store.save(
            2564, "alice", "A", "code",
            language="GNU C++17",
            verdict="AC",
            submission_id="12345",
            source="mine",
            contest_time="0:12",
        )
        files = self.store.list_files(2564)
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertEqual(f.user, "alice")
        self.assertEqual(f.problem, "A")
        self.assertEqual(f.language, "GNU C++17")
        self.assertEqual(f.verdict, "AC")
        self.assertEqual(f.submission_id, "12345")
        self.assertEqual(f.source, "mine")
        self.assertEqual(f.contest_time, "0:12")

    def test_save_overwrites(self):
        self.store.save(2564, "alice", "A", "old code")
        self.store.save(2564, "alice", "A", "new code")
        self.assertEqual(self.store.read(2564, "alice", "A"), "new code")

    def test_save_updates_index(self):
        self.store.save(2564, "alice", "A", "code", language="GNU C++17")
        idx = self.store.get_index(2564)
        self.assertEqual(idx["contest_id"], "2564")
        self.assertEqual(len(idx["files"]), 1)
        self.assertEqual(idx["files"][0]["user"], "alice")
        self.assertEqual(idx["files"][0]["filename"], "A.cpp")


class TestListFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodesStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_empty(self):
        self.assertEqual(self.store.list_files(2564), [])

    def test_list_nonexistent(self):
        self.assertEqual(self.store.list_files(9999), [])

    def test_list_all(self):
        self.store.save(2564, "alice", "A", "code_a", language="GNU C++17", source="mine")
        self.store.save(2564, "alice", "B", "code_b", language="Python 3", source="mine")
        self.store.save(2564, "bob", "A", "code_ba", language="GNU C++17",
                       source="watchlist")
        files = self.store.list_files(2564)
        self.assertEqual(len(files), 3)

    def test_list_filter_by_problem(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "alice", "B", "y")
        files = self.store.list_files(2564, problem="A")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].problem, "A")

    def test_list_filter_by_user(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "bob", "A", "y")
        files = self.store.list_files(2564, user="alice")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].user, "alice")

    def test_list_filter_by_source(self):
        self.store.save(2564, "alice", "A", "x", source="mine")
        self.store.save(2564, "bob", "A", "y", source="watchlist")
        files = self.store.list_files(2564, source="mine")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].user, "alice")


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodesStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_specific(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "alice", "B", "y")
        n = self.store.delete(2564, "alice", "A")
        self.assertEqual(n, 1)
        self.assertFalse(self.store.exists(2564, "alice", "A"))
        self.assertTrue(self.store.exists(2564, "alice", "B"))

    def test_delete_updates_index(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.delete(2564, "alice", "A")
        idx = self.store.get_index(2564)
        self.assertEqual(idx["files"], [])

    def test_delete_nonexistent_returns_zero(self):
        self.assertEqual(self.store.delete(2564, "nobody", "Z"), 0)


class TestClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodesStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_problem(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "alice", "B", "y")
        n = self.store.clean(2564, user="alice", problem="A")
        self.assertEqual(n, 1)
        self.assertFalse(self.store.exists(2564, "alice", "A"))
        self.assertTrue(self.store.exists(2564, "alice", "B"))

    def test_clean_user(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "alice", "B", "y")
        self.store.save(2564, "bob", "A", "z")
        n = self.store.clean(2564, user="alice")
        self.assertEqual(n, 2)
        self.assertFalse(self.store.exists(2564, "alice", "A"))
        self.assertTrue(self.store.exists(2564, "bob", "A"))

    def test_clean_all(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.save(2564, "bob", "B", "y")
        self.store.save(9999, "carol", "A", "z")  # different contest
        n = self.store.clean(2564)
        self.assertEqual(n, 2)
        # 9999 不受影响
        self.assertTrue(self.store.exists(9999, "carol", "A"))

    def test_clean_keeps_index(self):
        self.store.save(2564, "alice", "A", "x")
        self.store.clean(2564)
        # index.json 不会被删
        idx_path = Path(self.tmp.name) / "2564" / "index.json"
        self.assertTrue(idx_path.exists())


class TestEnsureGitignore(unittest.TestCase):
    def test_creates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "codes"
            ensure_gitignore(root)
            gi = root / ".gitignore"
            self.assertTrue(gi.exists())
            self.assertIn("*", gi.read_text(encoding="utf-8"))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "codes"
            ensure_gitignore(root)
            gi = root / ".gitignore"
            gi.write_text("custom\n", encoding="utf-8")
            ensure_gitignore(root)  # 不覆盖
            self.assertEqual(gi.read_text(encoding="utf-8"), "custom\n")


if __name__ == "__main__":
    unittest.main()