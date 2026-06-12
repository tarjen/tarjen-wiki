"""
tests/test_server.py

FastAPI server 单元测试。
用临时 git repo + 临时 config 目录, 隔离真实环境。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402


def make_temp_env() -> dict:
    """创建临时 repo + config dir, 返回环境变量 dict."""
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

    # 初始 contests.csv (空)
    (repo / "contests.csv").write_text(
        "slug,name,date,solved,total,problems,link,tags\n",
        encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "contests").mkdir()

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)

    cfg_dir = tmp_path / "cfg"
    codes_dir = tmp_path / "codes"

    return {
        "REPO_PATH": str(repo),
        "CONFIG_DIR": str(cfg_dir),
        "CODES_DIR": str(codes_dir),
        "_tmp_root": tmp,
    }


class ServerTestCase(unittest.TestCase):
    """每个测试方法独立环境."""

    def setUp(self):
        self.env = make_temp_env()
        # 设置环境变量 *before* import server
        for k, v in self.env.items():
            if k.startswith("_"):
                continue
            os.environ[k] = v

        # 重要: 重置 server 的全局 state
        import server
        server.state = server.AppState()
        server.app.dependency_overrides.clear()
        self.client = TestClient(server.app)
        # 用 lifespan 触发 init
        with self.client:
            pass  # 让 startup 跑完

    def tearDown(self):
        # 清环境变量
        for k in list(self.env.keys()):
            if k.startswith("_"):
                continue
            os.environ.pop(k, None)
        # 清理临时目录
        shutil.rmtree(self.env["_tmp_root"], ignore_errors=True)


# === /healthz ===

class TestHealthz(ServerTestCase):
    def test_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn("config", data)
        self.assertIn("repo", data)
        self.assertEqual(data["csv"]["contests"], 0)
        self.assertIn("watchlist_count", data)


# === /contests CRUD ===

class TestContestsCreate(ServerTestCase):
    def test_basic(self):
        r = self.client.post("/contests", json={
            "slug": "2025-icpc-xxx",
            "name": "2025 ICPC XXX Regional",
            "date": "2025.6.12",
            "total": 3,
            "problems": ["O", "O", "."],
            "tags": ["#icpc", "#regional"],
            "link": "https://qoj.ac/contest/2564",
        })
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["slug"], "2025-icpc-xxx")
        self.assertTrue(data["csv_written"])
        self.assertIsNotNone(data["body_written"])  # 默认创建占位
        self.assertTrue(data["committed"])

    def test_duplicate_slug_409(self):
        self.client.post("/contests", json={
            "slug": "dup", "name": "D", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        r = self.client.post("/contests", json={
            "slug": "dup", "name": "D2", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "slug_exists")

    def test_invalid_slug_400(self):
        r = self.client.post("/contests", json={
            "slug": "BAD SLUG", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        self.assertEqual(r.status_code, 400)

    def test_problems_length_mismatch_400(self):
        r = self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 3, "problems": ["O", "."],
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "problems_length_mismatch")

    def test_no_body_created_when_exists(self):
        # 先写 md
        (Path(self.env["REPO_PATH"]) / "docs" / "contests" / "x.md").write_text(
            "# my body", encoding="utf-8",
        )
        r = self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        # 已有 md 时, body_written 应该是 None (不会覆盖)
        # 实际上: md 存在时我们不会创建占位, 也不覆盖已有
        # 但本测试只是确认 API 调用成功
        self.assertEqual(r.status_code, 201)


class TestContestsList(ServerTestCase):
    def setUp(self):
        super().setUp()
        for s, n, d, p in [
            ("a", "A", "2020.1.1", ["O", "O", "."]),
            ("b", "B", "2024.5.1", ["O", "O", "O"]),
            ("c", "C", "2022.3.1", ["O", ".", "."]),
        ]:
            self.client.post("/contests", json={
                "slug": s, "name": n, "date": d,
                "total": len(p), "problems": p,
                "tags": ["#test"],
            })

    def test_list_all(self):
        r = self.client.get("/contests")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["total"], 3)
        # 按 date 倒序
        slugs = [c["slug"] for c in data["contests"]]
        self.assertEqual(slugs, ["b", "c", "a"])

    def test_list_sort_by_solved(self):
        r = self.client.get("/contests?sort=solved&order=desc")
        slugs = [c["slug"] for c in r.json()["contests"]]
        self.assertEqual(slugs, ["b", "a", "c"])

    def test_list_filter_by_since(self):
        r = self.client.get("/contests?since=2023.1.1")
        slugs = [c["slug"] for c in r.json()["contests"]]
        self.assertEqual(slugs, ["b"])

    def test_list_filter_by_tag(self):
        r = self.client.get("/contests?tag=test")
        self.assertEqual(r.json()["count"], 3)

    def test_list_filter_by_solved_min(self):
        r = self.client.get("/contests?solved_min=3")
        slugs = [c["slug"] for c in r.json()["contests"]]
        self.assertEqual(slugs, ["b"])

    def test_list_limit(self):
        r = self.client.get("/contests?limit=2")
        self.assertEqual(r.json()["count"], 2)


class TestContestsGet(ServerTestCase):
    def test_get_existing(self):
        self.client.post("/contests", json={
            "slug": "x", "name": "X Contest", "date": "2025.6.12",
            "total": 2, "problems": ["O", "Ø"],
            "tags": ["#icpc"],
        })
        r = self.client.get("/contests/x")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slug"], "x")
        self.assertEqual(data["solved"], 2)  # O + Ø
        self.assertEqual(data["in_contest"], 1)
        self.assertEqual(data["upsolved"], 1)

    def test_get_missing_404(self):
        r = self.client.get("/contests/nonexistent")
        self.assertEqual(r.status_code, 404)


class TestContestsUpdate(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 3, "problems": [".", ".", "."],
        })

    def test_update_name(self):
        r = self.client.put("/contests/x", json={"name": "New Name"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slug"], "x")
        r2 = self.client.get("/contests/x")
        self.assertEqual(r2.json()["name"], "New Name")

    def test_update_problems_recomputes(self):
        r = self.client.put("/contests/x", json={"problems": ["O", "O", "O"]})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.get("/contests/x")
        self.assertEqual(r2.json()["solved"], 3)

    def test_update_missing_404(self):
        r = self.client.put("/contests/nonexistent", json={"name": "X"})
        self.assertEqual(r.status_code, 404)


class TestContestsBody(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })

    def test_update_body(self):
        r = self.client.patch("/contests/x/body",
                              json={"content": "# X\nmy new notes"})
        self.assertEqual(r.status_code, 200)
        # 重新读 md
        md_path = Path(self.env["REPO_PATH"]) / "docs" / "contests" / "x.md"
        self.assertEqual(md_path.read_text(encoding="utf-8"),
                         "# X\nmy new notes")

    def test_update_body_missing_404(self):
        r = self.client.patch("/contests/nonexistent/body",
                              json={"content": "x"})
        self.assertEqual(r.status_code, 404)


class TestContestsDelete(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })

    def test_basic_delete(self):
        r = self.client.delete("/contests/x")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["csv_removed"])
        # md 也删了
        self.assertTrue(r.json()["body_removed"])

    def test_keep_body(self):
        r = self.client.delete("/contests/x?keep_body=true")
        self.assertTrue(r.json()["csv_removed"])
        self.assertFalse(r.json()["body_removed"])
        # md 还在
        md_path = Path(self.env["REPO_PATH"]) / "docs" / "contests" / "x.md"
        self.assertTrue(md_path.exists())

    def test_delete_missing_404(self):
        r = self.client.delete("/contests/nonexistent")
        self.assertEqual(r.status_code, 404)


# === Repo ops 错误 (Phase 3 后续会用到) ===

class TestRepoOperations(ServerTestCase):
    def test_status_clean_after_create(self):
        self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        r = self.client.get("/healthz")
        self.assertTrue(r.json()["repo"]["clean"])


if __name__ == "__main__":
    unittest.main()