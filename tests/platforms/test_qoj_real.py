"""
tests/platforms/test_qoj_real.py

用真实 QOJ HTML 验证 parser.
fixtures 在 tests/fixtures/qoj_real/

不连网络 — 直接读 fixture 文件.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from platforms.qoj import QojClient  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qoj_real"


def make_mock_client() -> QojClient:
    """构造 client (不连网络, 只用来跑 parser)."""
    return QojClient(
        cookies={
            "uoj_remember_token": "t",
            "uoj_remember_token_checksum": "c",
            "UOJSESSID": "s",
        },
        request_interval=0,
    )


class TestRealContestMeta(unittest.TestCase):
    """解析真实 QOJ 比赛页 /contest/QOJ1357."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "contest_1357.html").read_text(encoding="utf-8")
        cls.client = make_mock_client()

    def test_title_extracted(self):
        meta = self.client._parse_contest_meta(self.html, "1357")
        # 真实比赛名: "Petrozavodsk Summer 2019. Day 2. 300iq Contest 2, Grand Prix of Kazan"
        self.assertIn("Petrozavodsk", meta.title)
        self.assertIn("300iq", meta.title)
        # 不应该带 " - Dashboard - Contest - QOJ.ac" 后缀
        self.assertNotIn("- QOJ.ac", meta.title)
        self.assertNotIn("Dashboard", meta.title)

    def test_problem_count(self):
        meta = self.client._parse_contest_meta(self.html, "1357")
        # 真实比赛 11 题 (A-K)
        self.assertEqual(meta.problem_count, 11)

    def test_url(self):
        meta = self.client._parse_contest_meta(self.html, "1357")
        self.assertEqual(meta.url, "https://qoj.ac/contest/1357")

    def test_problem_id_in_url(self):
        """QOJ 实际 problem_id 是数字 (7410), 不是字母."""
        meta = self.client._parse_contest_meta(self.html, "1357")
        # 检查 problem listing regex 能不能正确匹配 letter
        import re
        from platforms.qoj import RE_PROBLEM_LISTING
        matches = RE_PROBLEM_LISTING.findall(self.html)
        self.assertGreater(len(matches), 0)
        # 第一条匹配: real QOJ 是 ("A",) (letter in <td>)
        self.assertEqual(matches[0][0], "A")
        # 也验证 problem_id 在 URL 里 (用更宽松的正则)
        url_match = re.search(r'/contest/\d+/problem/(\d+)', self.html)
        self.assertIsNotNone(url_match)
        self.assertEqual(url_match.group(1), "7410")  # Apollonian Network 是 A 题


class TestRealCFChallenge(unittest.TestCase):
    """CF challenge 页应该被识别 (但没真实 fixture, 跳过)."""

    @unittest.skip("没有真实 CF fixture (之前抓的 results.html 是 cache header, 不是 CF)")
    def test_cf_challenge_detected(self):
        cf_html = (FIXTURES / "results.html").read_text(encoding="utf-8")
        client = make_mock_client()
        self.assertTrue(client._is_cf_challenge(cf_html))

    def test_real_results_not_cf(self):
        """Petrozavodsk 2019 results 是真页面, 不是 CF."""
        html = (FIXTURES / "results_all.html").read_text(encoding="utf-8")
        client = make_mock_client()
        self.assertFalse(client._is_cf_challenge(html))


if __name__ == "__main__":
    unittest.main()