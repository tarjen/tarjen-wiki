"""
tests/test_codes_logic.py

codes_logic.py 单元测试. 用 mock fetch_fn 跑完整流程.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from codes_logic import (  # noqa: E402
    FetchRequest, fetch_codes,
)
from codes_store import CodesStore  # noqa: E402
from platforms.qoj import QojClient, Submission  # noqa: E402
from watchlist import Watchlist  # noqa: E402


# === Mock 数据 ===

MOCK_SUBMISSIONS = [
    Submission(
        platform="qoj", submission_id="10001", user="tarjen",
        problem="A", verdict="AC", submitted_at="",
        contest_time_seconds=12*60, language="GNU C++17", code_length=1240,
    ),
    Submission(
        platform="qoj", submission_id="10002", user="tarjen",
        problem="D", verdict="WA", submitted_at="",
        contest_time_seconds=90*60, language="GNU C++17", code_length=1300,
    ),
    Submission(
        platform="qoj", submission_id="10003", user="alice",
        problem="A", verdict="AC", submitted_at="",
        contest_time_seconds=18*60, language="GNU C++17", code_length=1100,
    ),
    Submission(
        platform="qoj", submission_id="10004", user="alice",
        problem="B", verdict="AC", submitted_at="",
        contest_time_seconds=25*60, language="Python 3", code_length=892,
    ),
    Submission(
        platform="qoj", submission_id="10005", user="bob",
        problem="A", verdict="AC", submitted_at="",
        contest_time_seconds=24*60, language="GNU C++17", code_length=1300,
    ),
    Submission(
        platform="qoj", submission_id="10006", user="bob",
        problem="B", verdict="WA", submitted_at="",   # 非 AC, 跳过
        contest_time_seconds=99*60, language="GNU C++17", code_length=1300,
    ),
    Submission(
        platform="qoj", submission_id="10007", user="carol",
        problem="A", verdict="AC", submitted_at="",
        contest_time_seconds=10*60, language="GNU C++17", code_length=1503,
    ),
]


CODE_BODY = "// fake code for {user}/{problem}"


def make_factory():
    """构造返回 mock QojClient 的 factory. 覆盖 standings/submissions/code."""
    from platforms.base import FastestACEntry, StandingsEntry

    def _standings_for(user):
        """从 MOCK_SUBMISSIONS 算 user 的 standings dict."""
        result = {}
        for s in MOCK_SUBMISSIONS:
            if s.user != user:
                continue
            verdict = "AC" if s.verdict == "AC" else "WA"
            failed = 0  # mock 简化: 不算 failed attempts
            result[s.problem] = StandingsEntry(
                platform="qoj",
                problem_id=str(ord(s.problem) - ord("A")),
                letter=s.problem,
                score=100 if s.verdict == "AC" else 0,
                contest_time_seconds=s.contest_time_seconds or 0,
                submission_id=s.submission_id,
                failed_attempts=failed,
                verdict=verdict,
            )
        return result

    def _all_standings(exclude_users):
        """从 MOCK_SUBMISSIONS 算所有用户每题 AC (按时间排)."""
        per_prob: dict[str, list[FastestACEntry]] = {}
        for s in MOCK_SUBMISSIONS:
            if s.user in exclude_users:
                continue
            if s.verdict != "AC":
                continue
            per_prob.setdefault(s.problem, []).append(FastestACEntry(
                user=s.user, time_seconds=s.contest_time_seconds or 0,
                submission_id=s.submission_id,
            ))
        for L in per_prob:
            per_prob[L].sort(key=lambda e: e.time_seconds)
        return per_prob

    def factory(platform):
        cookies = {"uoj_remember_token": "t", "uoj_remember_token_checksum": "c",
                   "UOJSESSID": "s"}
        client = QojClient(cookies=cookies, request_interval=0)

        def fetch_fn(url, cookie):
            if "/submission/" in url:
                sid = url.rstrip("/").split("/")[-1]
                for s in MOCK_SUBMISSIONS:
                    if s.submission_id == sid:
                        return f'<pre><code class="sh_cpp">{CODE_BODY.format(user=s.user, problem=s.problem)}</code></pre><div>Language: {s.language or "GNU C++17"}</div>'
                raise Exception(f"unknown sid {sid}")
            return "<html></html>"
        client._fetch_fn = fetch_fn
        # 覆盖 standings API: 从 MOCK_SUBMISSIONS 算
        client.get_user_standings = lambda cid, user: _standings_for(user)
        client.get_all_user_standings = lambda cid, exclude_users=None: _all_standings(exclude_users or set())
        return client
    return factory


def make_env():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name)


# === Tests ===

class TestFetchBasic(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx, self.tmp = make_env()
        self.codes = CodesStore(self.tmp)
        self.watchlist = Watchlist(self.tmp / "watchlist.txt")
        self.watchlist.add(["alice", "bob"])

    def tearDown(self):
        self.tmp_ctx.cleanup()

    def test_basic(self):
        req = FetchRequest(
            platform="qoj", cid="2564", username="tarjen",
            request_interval=0,
        )
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # tarjen A + D (WA), alice (watchlist) A + B, bob (watchlist) A, carol (sample) A
        # 总共 6 份
        self.assertEqual(result.fetched, 6)
        self.assertEqual(result.errors, 0)

        # 文件存在
        self.assertTrue(self.codes.exists("qoj", "2564", "A", "tarjen"))
        self.assertTrue(self.codes.exists("qoj", "2564", "D", "tarjen"))  # 自己 WA 也存
        self.assertTrue(self.codes.exists("qoj", "2564", "A", "alice"))
        self.assertTrue(self.codes.exists("qoj", "2564", "B", "alice"))
        self.assertTrue(self.codes.exists("qoj", "2564", "A", "bob"))
        self.assertTrue(self.codes.exists("qoj", "2564", "A", "carol"))

        # 自己的源码标记
        files = self.codes.list_files("qoj", "2564")
        by_user = {f.user: f for f in files}
        self.assertEqual(by_user["tarjen"].source, "mine")
        self.assertEqual(by_user["alice"].source, "watchlist")
        self.assertEqual(by_user["bob"].source, "watchlist")
        self.assertEqual(by_user["carol"].source, "sample")

    def test_skip_existing(self):
        # 第一次抓
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          request_interval=0)
        fetch_codes(req, make_factory(), self.codes, self.watchlist)

        # 第二次抓, 应该全跳过
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        self.assertEqual(result.fetched, 0)
        self.assertEqual(result.skipped_existing, 6)

    def test_no_skip_when_disabled(self):
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          skip_existing=False, request_interval=0)
        fetch_codes(req, make_factory(), self.codes, self.watchlist)
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # 第二次也全部重新抓 (覆盖)
        self.assertEqual(result.fetched, 6)

    def test_no_watchlist(self):
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          fetch_watchlist=False, request_interval=0)
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # 自己 (A, D) + others (alice A, alice B, bob A, carol A 进 others)
        # others_n=1 默认, A 取最早 (carol 10min), B 取 1 个 (alice) = 2
        # 总共 2 + 2 = 4
        self.assertEqual(result.fetched, 4)
        # alice A 不在 (因为 carol 更快), alice B 在 (B 题只有 alice)
        self.assertFalse(self.codes.exists("qoj", "2564", "A", "alice"))
        self.assertTrue(self.codes.exists("qoj", "2564", "B", "alice"))

    def test_no_others(self):
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          fetch_others="none", request_interval=0)
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # 自己 + watchlist = 5 (tarjen A+D, alice A+B, bob A)
        self.assertEqual(result.fetched, 5)
        self.assertFalse(self.codes.exists("qoj", "2564", "A", "carol"))

    def test_others_n(self):
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          fetch_others="top_n_fastest", others_n=2,
                          request_interval=0)
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # mine (2) + watchlist AC (3) + others top_n=2 from carol (1, 因为 carol 只有 A 一份)
        # = 2 + 3 + 1 = 6
        self.assertEqual(result.fetched, 6)
        # carol 在 others 里只有 A
        self.assertTrue(self.codes.exists("qoj", "2564", "A", "carol"))

    def test_filter_by_problems(self):
        req = FetchRequest(platform="qoj", cid="2564", username="tarjen",
                          problems=["A"], request_interval=0)
        result = fetch_codes(req, make_factory(), self.codes, self.watchlist)
        # 只抓 A 题: tarjen A, alice A, bob A, carol A = 4
        self.assertEqual(result.fetched, 4)
        self.assertFalse(self.codes.exists("qoj", "2564", "D", "tarjen"))
        self.assertFalse(self.codes.exists("qoj", "2564", "B", "alice"))


class TestTaskManagement(unittest.TestCase):
    def test_create_and_get(self):
        from codes_logic import create_task, get_task, list_tasks
        t = create_task("2564")
        self.assertEqual(t.cid, "2564")
        self.assertEqual(t.status, "started")
        self.assertIsNotNone(get_task(t.task_id))
        self.assertIn(t, list_tasks())

    def test_get_missing(self):
        from codes_logic import get_task
        self.assertIsNone(get_task("nonexistent"))


if __name__ == "__main__":
    unittest.main()