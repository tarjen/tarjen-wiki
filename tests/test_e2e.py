"""
tests/test_e2e.py

端到端测试: 模拟 "每天 wiki update 2564" 完整流程.

不连真实 QOJ (用 fetch_fn 注入), 但其他一切都是真的:
  - 真 git repo (本地 + bare remote)
  - 真 CSV 文件读写
  - 真 md 文件
  - 真 cookie jar 文件
  - 真 sys.path / import

测试流程 (test_01_daily_workflow 走完整个流程):
  1. healthz 检查
  2. update-preview (赛时数据)
  3. update-apply -> CSV + md + commit + push
  4. show 验证
  5. upsolve-preview (赛后数据) -> 发现 C 题补过
  6. upsolve-apply -> CSV 更新
  7. 最终状态检查

test_error_paths: 单独跑 (因为依赖前一个测试的状态, 所以跟 daily_workflow 隔离)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


CONTEST_HTML = """
<html><body>
<h1>2025 ICPC XXX Regional</h1>
<div>Start: 2025-06-07 08:00:00 End: 2025-06-07 13:00:00</div>
<ul>
  <li><a href="/contest/2564/problem/A">A</a></li>
  <li><a href="/contest/2564/problem/B">B</a></li>
  <li><a href="/contest/2564/problem/C">C</a></li>
</ul>
</body></html>
"""

# 赛中提交 (周六 2025-06-07): A AC, B WA, C WA, D WA
IN_CONTEST_SUBS = """
<html><body><table>
<tr>
  <td><a href="/submission/10001">10001</a></td>
  <td><a href="/contest/2564/problem/A">A</a></td>
  <td><a href="/user/profile/tarjen">tarjen</a></td>
  <td>AC</td>
  <td>0:12:34</td>
  <td>1024 KB</td>
  <td>GNU C++17</td>
  <td>1240</td>
</tr>
<tr>
  <td><a href="/submission/10002">10002</a></td>
  <td><a href="/contest/2564/problem/B">B</a></td>
  <td><a href="/user/profile/tarjen">tarjen</a></td>
  <td>WA</td>
  <td>0:30:00</td>
  <td>1024 KB</td>
  <td>GNU C++17</td>
  <td>1300</td>
</tr>
<tr>
  <td><a href="/submission/10003">10003</a></td>
  <td><a href="/contest/2564/problem/C">C</a></td>
  <td><a href="/user/profile/tarjen">tarjen</a></td>
  <td>WA</td>
  <td>1:30:00</td>
  <td>1024 KB</td>
  <td>GNU C++17</td>
  <td>1500</td>
</tr>
<tr>
  <td><a href="/submission/10004">10004</a></td>
  <td><a href="/contest/2564/problem/D">D</a></td>
  <td><a href="/user/profile/tarjen">tarjen</a></td>
  <td>WA</td>
  <td>2:00:00</td>
  <td>1024 KB</td>
  <td>GNU C++17</td>
  <td>1700</td>
</tr>
</table></body></html>
"""

# 赛后提交 (周一开始): C AC (补过), D WA (继续尝试)
POST_CONTEST_SUBS = """
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
  <td><a href="/contest/2564/problem/D">D</a></td>
  <td><a href="/user/profile/tarjen">tarjen</a></td>
  <td>WA</td>
  <td>2025-06-10 15:00:00</td>
  <td>1024 KB</td>
  <td>GNU C++17</td>
  <td>1700</td>
</tr>
</table></body></html>
"""


def setup_test_env() -> dict:
    """建临时 git repo + cookie + bare remote."""
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

    cfg = tmp_path / "cfg"
    (cfg / "cookies").mkdir(parents=True)
    (cfg / "cookies" / "qoj.txt").write_text(
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tuoj_remember_token\tT\n"
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tuoj_remember_token_checksum\tC\n"
        ".qoj.ac\tTRUE\t/\tFALSE\t0\tUOJSESSID\tS\n",
        encoding="utf-8",
    )

    return {"tmp_root": Path(tmp), "repo": repo, "cfg": cfg,
            "remote": remote, "port": 18901}


def make_mock_factory():
    """返回 (mock_make_client, set_subs_html)."""
    from platforms.qoj import QojClient

    def mock_make_client(platform, config_dir):
        cookies = {"uoj_remember_token": "T", "uoj_remember_token_checksum": "C",
                   "UOJSESSID": "S"}
        client = QojClient(cookies=cookies, request_interval=0)

        def fetch_fn(url, cookie):
            if "/submissions" in url:
                return mock_make_client.current_subs_html[0]
            if "/submission/" in url:
                sid = url.rstrip("/").split("/")[-1]
                return f'<pre class="code">// code {sid}</pre><div>Language: GNU C++17</div>'
            if "/contest/" in url:
                return CONTEST_HTML
            raise Exception(f"unexpected URL: {url}")
        client._fetch_fn = fetch_fn
        return client

    mock_make_client.current_subs_html = [IN_CONTEST_SUBS]
    return mock_make_client


class _ServerBase(unittest.TestCase):
    """启动 server + 注入 mock. setUpClass / tearDownClass 各跑一次."""

    @classmethod
    def setUpClass(cls):
        cls.env = setup_test_env()

        for k in ["REPO_PATH", "CONFIG_DIR", "CODES_DIR"]:
            os.environ.pop(k, None)
        os.environ["REPO_PATH"] = str(cls.env["repo"])
        os.environ["CONFIG_DIR"] = str(cls.env["cfg"])
        os.environ["CODES_DIR"] = str(cls.env["tmp_root"] / "codes")

        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import server
        import import_logic
        server.state = server.AppState()

        # 保存原始 make_client, tearDownClass 时恢复 (避免污染其他测试文件)
        cls._orig_make_client_import_logic = import_logic.make_client
        cls._orig_make_client_server = server.make_client

        mock_make_client = make_mock_factory()
        import_logic.make_client = mock_make_client
        server.make_client = mock_make_client
        cls._mock_make_client = mock_make_client

        import uvicorn
        config = uvicorn.Config(server.app, host="127.0.0.1",
                               port=cls.env["port"], log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()

        import httpx
        for _ in range(50):
            try:
                r = httpx.get(f"http://127.0.0.1:{cls.env['port']}/healthz",
                              timeout=0.5)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        cls.base = f"http://127.0.0.1:{cls.env['port']}"
        cls.client = httpx.Client(base_url=cls.base, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.server_thread.join(timeout=5)
        # 恢复原始 make_client (避免污染其他测试模块)
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import import_logic
        import server
        import_logic.make_client = cls._orig_make_client_import_logic
        server.make_client = cls._orig_make_client_server
        for k in ["REPO_PATH", "CONFIG_DIR", "CODES_DIR"]:
            os.environ.pop(k, None)
        shutil.rmtree(cls.env["tmp_root"], ignore_errors=True)


class TestDailyWorkflow(_ServerBase):
    """完整 daily workflow: 比赛日 import + 几天后 upsolve."""

    def test_01_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["csv"]["contests"], 0)
        self.assertTrue(data["repo"]["clean"])

    def test_02_list_empty(self):
        r = self.client.get("/contests")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_03_update_preview(self):
        """比赛日: 调 update-preview."""
        r = self.client.post("/import/update-preview", json={
            "platform": "qoj", "contest_id": "2564", "user": "tarjen",
        })
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["record_state"], "create_new")
        self.assertEqual(data["slug"], "2025-icpc-xxx-regional")
        self.assertEqual(data["contest"]["title"], "2025 ICPC XXX Regional")
        self.assertEqual(len(data["problems"]), 3)
        self.assertEqual(data["problems"][0]["status"], "O")  # A AC
        self.assertEqual(data["problems"][1]["status"], "!")  # B WA
        self.assertEqual(data["problems"][2]["status"], "!")  # C WA
        self.assertEqual(data["summary"], {"O": 1, "!": 2, "Ø": 0, ".": 0})

    def test_04_update_apply(self):
        """比赛日: apply -> CSV + md + commit + push."""
        preview = self.client.post("/import/update-preview", json={
            "platform": "qoj", "contest_id": "2564", "user": "tarjen",
        }).json()

        r = self.client.post("/import/update-apply", json={
            "platform": "qoj", "preview": preview, "overrides": {},
            "options": {"create_body": True, "run_sync": True, "push": True},
        })
        self.assertEqual(r.status_code, 200)
        result = r.json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["record_state"], "create_new")
        self.assertEqual(result["slug"], "2025-icpc-xxx-regional")
        self.assertTrue(result["csv_written"])
        self.assertIsNotNone(result["body_written"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])

        # 验证 CSV
        csv_text = (self.env["repo"] / "contests.csv").read_text(encoding="utf-8")
        self.assertIn("2025-icpc-xxx-regional", csv_text)
        self.assertIn("O;!;!", csv_text)

        # 验证 md 详情页
        md_path = (self.env["repo"] / "docs" / "contests"
                   / "2025-icpc-xxx-regional.md")
        self.assertTrue(md_path.exists())
        self.assertIn("2025 ICPC XXX Regional",
                      md_path.read_text(encoding="utf-8"))

        # 验证 git log
        log = subprocess.run(
            ["git", "-C", str(self.env["repo"]), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("add(2025-icpc-xxx-regional)", log.stdout)

        # 验证 remote 收到
        remote_log = subprocess.run(
            ["git", "-C", str(self.env["remote"]), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("add(2025-icpc-xxx-regional)", remote_log.stdout)

    def test_05_show_after_update(self):
        r = self.client.get("/contests/2025-icpc-xxx-regional")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slug"], "2025-icpc-xxx-regional")
        self.assertEqual(data["solved"], 1)
        self.assertEqual(data["in_contest"], 1)
        self.assertEqual(data["upsolved"], 0)
        self.assertEqual(data["problems"], ["O", "!", "!"])
        self.assertTrue(data["body_exists"])

    def test_06_upsolve_preview(self):
        """几天后: 切换 mock 到赛后数据, 跑 upsolve-preview."""
        self._mock_make_client.current_subs_html[0] = POST_CONTEST_SUBS

        r = self.client.post("/import/upsolve-preview", json={
            "platform": "qoj", "contest_id": "2564", "user": "tarjen",
        })
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slug"], "2025-icpc-xxx-regional")
        self.assertEqual(data["current_problems"], ["O", "!", "!"])
        self.assertEqual(len(data["changes"]), 1)
        ch = data["changes"][0]
        self.assertEqual(ch["letter"], "C")
        self.assertEqual(ch["before"], "!")
        self.assertEqual(ch["after"], "Ø")
        self.assertEqual(data["summary"]["upsolved"], 0)
        self.assertEqual(data["summary"]["upsolved_from_bang"], 1)

    def test_07_upsolve_apply(self):
        self._mock_make_client.current_subs_html[0] = POST_CONTEST_SUBS

        preview = self.client.post("/import/upsolve-preview", json={
            "platform": "qoj", "contest_id": "2564", "user": "tarjen",
        }).json()

        r = self.client.post("/import/upsolve-apply", json={
            "platform": "qoj", "preview": preview,
            "options": {"push": True},
        })
        self.assertEqual(r.status_code, 200)
        result = r.json()
        self.assertEqual(result["slug"], "2025-icpc-xxx-regional")
        self.assertEqual(result["problems_before"], ["O", "!", "!"])
        self.assertEqual(result["problems_after"], ["O", "!", "Ø"])
        self.assertTrue(result["committed"])

        csv_text = (self.env["repo"] / "contests.csv").read_text(encoding="utf-8")
        self.assertIn("O;!;Ø", csv_text)

    def test_08_final_state(self):
        r = self.client.get("/contests/2025-icpc-xxx-regional")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["problems"], ["O", "!", "Ø"])
        self.assertEqual(data["solved"], 2)  # O + Ø
        self.assertEqual(data["in_contest"], 1)
        self.assertEqual(data["upsolved"], 1)

        r = self.client.get("/healthz")
        self.assertTrue(r.json()["repo"]["clean"])


class TestErrorPaths(_ServerBase):
    """错误路径 (独立测试, 不依赖 TestDailyWorkflow 的状态)."""

    def test_get_nonexistent(self):
        r = self.client.get("/contests/nonexistent")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "slug_not_found")

    def test_create_invalid_problems_length(self):
        r = self.client.post("/contests", json={
            "slug": "x", "name": "X", "date": "2025.1.1",
            "total": 5, "problems": ["O", "."],
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "problems_length_mismatch")

    def test_create_invalid_slug(self):
        r = self.client.post("/contests", json={
            "slug": "BAD SLUG", "name": "X", "date": "2025.1.1",
            "total": 1, "problems": ["."],
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "slug_invalid")


if __name__ == "__main__":
    unittest.main()