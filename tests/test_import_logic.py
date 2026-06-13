"""
tests/test_import_logic.py

import_logic.py 单元测试。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from csv_store import Contest, CsvStore  # noqa: E402
from md_store import MdStore  # noqa: E402
from git_ops import GitOps  # noqa: E402
from import_logic import (  # noqa: E402
    apply_update, apply_upsolve, build_update_preview, build_upsolve_preview,
    normalize_date_for_csv, slugify,
)


def make_env() -> dict:
    """建一个临时 git repo + 配置目录."""
    tmp = tempfile.mkdtemp()
    tmp_path = Path(tmp)
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    remote.mkdir()

    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"],
                   check=True, capture_output=True)

    (repo / "contests.csv").write_text(
        "slug,name,date,solved,total,problems,link,tags\n", encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "contests").mkdir()
    (repo / "tools").mkdir()

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)

    cfg_dir = tmp_path / "cfg"
    (cfg_dir / "cookies").mkdir(parents=True)

    # 写一个 QOJ cookie jar
    cookie_path = cfg_dir / "cookies" / "qoj.txt"
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tuoj_remember_token\tT\n"
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tuoj_remember_token_checksum\tC\n"
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tUOJSESSID\tS\n",
        encoding="utf-8",
    )

    return {"tmp_root": tmp, "repo": repo, "cfg": cfg_dir}


def cleanup(env):
    shutil.rmtree(env["tmp_root"], ignore_errors=True)


def setup_stores(env):
    csv = CsvStore(env["repo"] / "contests.csv")
    csv.load()
    md = MdStore(env["repo"] / "docs" / "contests")
    git = GitOps(env["repo"])
    return csv, md, git


# === HTML fixtures ===

CONTEST_HTML = """
<html><head><title>Contest 2564</title></head>
<body>
<h1>2025 ICPC XXX Regional</h1>
<div>Start: 2025-06-07 08:00:00 End: 2025-06-07 13:00:00</div>
<ul>
  <li><a href="/contest/2564/problem/A">A</a></li>
  <li><a href="/contest/2564/problem/B">B</a></li>
  <li><a href="/contest/2564/problem/C">C</a></li>
</ul>
</body></html>
"""

# Standings JS 数据. 0-indexed 题目 ID 对应字母 A=0, B=1, C=2.
# tarjen 提交过: A 一次过 (12:34=754s), B WA 后 AC (45:00=2700s, 1 个 failed before),
# C 没提交. score 字段: [score, time_sec, sub_id, failed_before, full_score, ?, [tags]]
STANDINGS_HTML = """
<html><body>
<script>
standings_version=2;
standings=[[200,3454,["tarjen",1500,2,"tarjen","rgb(0,0,0)",1,""],1,100.0]];
fullscore=300;
score={"tarjen":{"0":[100,754,10001,0,100,0,[]],"1":[100,2700,10003,1,100,0,[]]}};
problems=[100,200,300];
my_name="tarjen";
</script>
</body></html>
"""


def make_mock_fetch():
    def fetch_fn(url, cookie):
        if "/contest/2564/standings" in url:
            return STANDINGS_HTML
        if "/contest/2564" in url:
            return CONTEST_HTML
        raise Exception(f"unexpected URL: {url}")
    return fetch_fn


# === Helpers ===

class TestHelpers(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(slugify("2025 ICPC XXX Regional"), "2025-icpc-xxx-regional")

    def test_slugify_special_chars(self):
        self.assertEqual(slugify("Hello, World! 2024"), "hello-world-2024")

    def test_slugify_truncate(self):
        s = slugify("a" * 100)
        self.assertLessEqual(len(s), 60)

    def test_slugify_strips(self):
        self.assertEqual(slugify("---foo---"), "foo")

    def test_normalize_date(self):
        self.assertEqual(normalize_date_for_csv("2025-06-07T08:00:00Z"), "2025.6.7")
        # None 时 fallback 到今天
        today = normalize_date_for_csv(None)
        self.assertRegex(today, r"^\d{4}\.\d{1,2}\.\d{1,2}$")


# === Update preview ===

class TestBuildUpdatePreview(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.csv, _, _ = setup_stores(self.env)

    def tearDown(self):
        cleanup(self.env)

    def test_create_new(self):
        preview = build_update_preview(
            platform="qoj",
            contest_id="2564",
            user="tarjen",
            csv_store=self.csv,
            config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
        )
        self.assertEqual(preview.record_state, "create_new")
        self.assertEqual(preview.slug, "2025-icpc-xxx-regional")
        self.assertEqual(preview.contest["title"], "2025 ICPC XXX Regional")
        self.assertEqual(len(preview.problems), 3)
        # A: AC -> O
        self.assertEqual(preview.problems[0]["status"], "O")
        # B: 最后是 AC -> O (虽然第一次 WA, 但取最晚)
        self.assertEqual(preview.problems[1]["status"], "O")
        # C: 没提交 -> .
        self.assertEqual(preview.problems[2]["status"], ".")
        self.assertEqual(preview.summary, {"O": 2, "!": 0, "Ø": 0, ".": 1})

    def test_create_new_with_different_user(self):
        preview = build_update_preview(
            platform="qoj", contest_id="2564", user="alice",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
        )
        self.assertEqual(preview.username, "alice")
        self.assertEqual(preview.record_state, "create_new")

    def test_update_existing(self):
        # 先手动 add 一条同 slug
        self.csv.add(Contest(
            slug="2025-icpc-xxx-regional", name="Old Name",
            date="2025.6.7", solved=0, total=3,
            problems=[".", ".", "."], link="", tags="",
        ))
        self.csv.save()

        preview = build_update_preview(
            platform="qoj", contest_id="2564", user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
        )
        self.assertEqual(preview.record_state, "update_existing")
        self.assertTrue(preview.slug_exists)

    def test_slug_override(self):
        preview = build_update_preview(
            platform="qoj", contest_id="2564", user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
            slug_override="my-custom-slug",
        )
        self.assertEqual(preview.slug, "my-custom-slug")

    def test_missing_cookies_raises(self):
        # 删 cookies
        (self.env["cfg"] / "cookies" / "qoj.txt").unlink()
        with self.assertRaises(ValueError) as cm:
            build_update_preview(
                platform="qoj", contest_id="2564", user="tarjen",
                csv_store=self.csv, config_dir=self.env["cfg"],
                fetch_fn=make_mock_fetch(),
            )
        self.assertIn("cookie_missing_for_platform", str(cm.exception))


# === Apply update ===

class TestApplyUpdate(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.csv, self.md, self.git = setup_stores(self.env)

    def tearDown(self):
        cleanup(self.env)

    def test_create_new(self):
        preview = build_update_preview(
            platform="qoj", contest_id="2564", user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
        )
        result = apply_update(
            preview=preview, csv_store=self.csv, md_store=self.md,
            git_ops=self.git, create_body=True, run_sync=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.record_state, "create_new")
        self.assertTrue(result.csv_written)
        self.assertIsNotNone(result.body_written)
        self.assertTrue(self.csv.exists("2025-icpc-xxx-regional"))

    def test_update_existing(self):
        self.csv.add(Contest(
            slug="2025-icpc-xxx-regional", name="Old",
            date="2025.6.7", solved=0, total=3,
            problems=[".", ".", "."], link="", tags="",
        ))
        self.csv.save()

        preview = build_update_preview(
            platform="qoj", contest_id="2564", user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_mock_fetch(),
        )
        result = apply_update(
            preview=preview, csv_store=self.csv, md_store=self.md,
            git_ops=self.git, create_body=True, run_sync=False,
        )
        self.assertEqual(result.record_state, "update_existing")
        self.assertEqual(result.problems_before, [".", ".", "."])
        self.assertEqual(result.problems_after, ["O", "O", "."])


# === Upsolve preview ===

def make_post_contest_subs_html():
    """赛后 AC 提交 (没 contest_time, 只有日期)."""
    return """
    <html><body><table>
    <tr>
      <td><a href="/submission/20001">20001</a></td>
      <td><a href="/contest/2564/problem/C">C</a></td>
      <td><a href="/user/profile/tarjen">tarjen</a></td>
      <td>AC</td>
      <td>2025-06-09 22:14:00</td>
      <td>1024 KB</td>
      <td>GNU C++17</td>
      <td>1500</td>
    </tr>
    <tr>
      <td><a href="/submission/20002">20002</a></td>
      <td><a href="/contest/2564/problem/A">A</a></td>
      <td><a href="/user/profile/tarjen">tarjen</a></td>
      <td>AC</td>
      <td>2025-06-10 15:00:00</td>
      <td>1024 KB</td>
      <td>GNU C++17</td>
      <td>1240</td>
    </tr>
    </table></body></html>
    """


def make_post_contest_fetch():
    def fetch_fn(url, cookie):
        if "/contest/2564/submissions" in url:
            return make_post_contest_subs_html()
        if "/contest/2564" in url:
            return CONTEST_HTML
        raise Exception(f"unexpected URL: {url}")
    return fetch_fn


class TestBuildUpsolvePreview(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.csv, _, _ = setup_stores(self.env)
        # 先 add 一条已有记录 (curl 我 . . .)
        self.csv.add(Contest(
            slug="2025-icpc-xxx-regional", name="X",
            date="2025.6.7", solved=0, total=3,
            problems=["O", "O", "."], link="", tags="",
        ))
        self.csv.save()

    def tearDown(self):
        cleanup(self.env)

    def test_basic(self):
        preview = build_upsolve_preview(
            platform="qoj", contest_id="2564", slug=None, user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_post_contest_fetch(),
        )
        self.assertEqual(preview.slug, "2025-icpc-xxx-regional")
        self.assertEqual(preview.current_problems, ["O", "O", "."])
        # C . -> Ø (post AC)
        # A O 不变
        self.assertEqual(len(preview.changes), 1)
        ch = preview.changes[0]
        self.assertEqual(ch["letter"], "C")
        self.assertEqual(ch["before"], ".")
        self.assertEqual(ch["after"], "Ø")
        self.assertEqual(preview.summary["upsolved"], 1)

    def test_from_bang(self):
        # 已有记录中 B 是 !, 赛后 AC -> 变 Ø (from bang)
        self.csv.update("2025-icpc-xxx-regional",
                        problems=["O", "!", "."])
        self.csv.save()
        preview = build_upsolve_preview(
            platform="qoj", contest_id="2564", slug=None, user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_post_contest_fetch(),
        )
        # changes 应该含 C (. -> Ø) - 但是 A 在赛后也有 AC 但 A 已是 O, 不算 change
        # B 仍是 !, 赛后无 AC, 不变
        # C 是 . -> Ø
        self.assertEqual(preview.summary["upsolved"], 1)

    def test_no_post_subs(self):
        # 改 mock, 让 submissions 页返回空
        def fetch_fn(url, cookie):
            if "/submissions" in url:
                return "<html></html>"
            if "/contest/2564" in url:
                return CONTEST_HTML
        preview = build_upsolve_preview(
            platform="qoj", contest_id="2564", slug=None, user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=fetch_fn,
        )
        self.assertEqual(preview.changes, [])
        self.assertEqual(preview.summary["upsolved"], 0)


class TestApplyUpsolve(unittest.TestCase):
    def setUp(self):
        self.env = make_env()
        self.csv, self.md, self.git = setup_stores(self.env)
        self.csv.add(Contest(
            slug="2025-icpc-xxx-regional", name="X",
            date="2025.6.7", solved=0, total=3,
            problems=["O", "O", "."], link="", tags="",
        ))
        self.csv.save()

    def tearDown(self):
        cleanup(self.env)

    def test_apply(self):
        preview = build_upsolve_preview(
            platform="qoj", contest_id="2564", slug=None, user="tarjen",
            csv_store=self.csv, config_dir=self.env["cfg"],
            fetch_fn=make_post_contest_fetch(),
        )
        result = apply_upsolve(
            preview=preview, csv_store=self.csv, md_store=self.md,
            git_ops=self.git, push=False,
        )
        self.assertTrue(result.ok)
        # C 变 Ø
        c = self.csv.get("2025-icpc-xxx-regional")
        self.assertEqual(c.problems, ["O", "O", "Ø"])
        self.assertEqual(result.problems_before, ["O", "O", "."])
        self.assertEqual(result.problems_after, ["O", "O", "Ø"])


if __name__ == "__main__":
    unittest.main()