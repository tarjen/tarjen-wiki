"""
tests/platforms/test_qoj_real_standings.py

用真实 QOJ 1357 抓的 standings JS 数据验证 parser.
  standings  HTML: tests/fixtures/qoj_real/standings_1357.html
  contest    HTML: tests/fixtures/qoj_real/contest_1357.html
  submissions HTML: tests/fixtures/qoj_real/submissions_1357_tarjen.html

不连网络.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from platforms.qoj import QojClient, parse_qoj_js_literal  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qoj_real"


def make_mock_client() -> QojClient:
    return QojClient(
        cookies={
            "uoj_remember_token": "t",
            "uoj_remember_token_checksum": "c",
            "UOJSESSID": "s",
        },
        request_interval=0,
    )


def make_fixtures_fetch_fn():
    contest_html = (FIXTURES / "contest_1357.html").read_text(encoding="utf-8")
    standings_html = (FIXTURES / "standings_1357.html").read_text(encoding="utf-8")
    submissions_html = (FIXTURES / "submissions_1357_tarjen.html").read_text(encoding="utf-8")

    def fetch_fn(url, cookie):
        if "/standings" in url:
            return standings_html
        if "/submissions" in url:
            return submissions_html
        if "/contest/1357" in url:
            return contest_html
        return ""
    return fetch_fn


class TestParseQojJsLiteral(unittest.TestCase):
    """JS 字面量 -> JSON 转换 (含 $DEFAULT_DAT_PREFIX_N 等标识符)."""

    def test_basic(self):
        result = parse_qoj_js_literal('{"a": 1, "b": [2, 3]}')
        self.assertEqual(result, {"a": 1, "b": [2, 3]})

    def test_unquoted_identifier(self):
        # $DEFAULT_DAT_PREFIX_1 等没引号的标识符
        result = parse_qoj_js_literal(
            '{"$DEFAULT_DAT_PREFIX_1": [1, 2], "tarjen": [3, 4]}'
        )
        self.assertEqual(result, {
            "$DEFAULT_DAT_PREFIX_1": [1, 2],
            "tarjen": [3, 4],
        })

    def test_already_quoted_unchanged(self):
        # 已引用的 key 不要再加引号 (避免拆碎成 "C" + "ookie"...)
        result = parse_qoj_js_literal('{"Cookie_Creamm": []}')
        self.assertEqual(result, {"Cookie_Creamm": []})


class TestRealStandingsParser(unittest.TestCase):
    """解析真实 QOJ 1357 standings JS 数据."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "standings_1357.html").read_text(encoding="utf-8")
        cls.client = make_mock_client()

    def test_score_extracted(self):
        """tarjen 的 score 应有 B(1), I(8), G(6) 三个 AC."""
        score = self.client._parse_score_for_user(self.html, "tarjen")
        self.assertIn("1", score)  # B
        self.assertIn("6", score)  # G
        self.assertIn("8", score)  # I
        # 没提交的 A, C, D, E, F, H, J, K 不在 score 里
        self.assertNotIn("0", score)  # A
        self.assertNotIn("2", score)  # C
        self.assertEqual(len(score), 3)

    def test_score_tarjen_b(self):
        """B: AC 一次过, 26:27 = 1587s."""
        score = self.client._parse_score_for_user(self.html, "tarjen")
        entry = score["1"]
        self.assertEqual(entry[0], 100)  # score
        self.assertEqual(entry[1], 1587)  # time_sec
        self.assertEqual(entry[2], 1906653)  # sub_id
        self.assertEqual(entry[3], 0)  # failed_before

    def test_score_tarjen_i(self):
        """I: AC, 1:39:17 = 5957s, 1 次失败 (TL)."""
        score = self.client._parse_score_for_user(self.html, "tarjen")
        entry = score["8"]
        self.assertEqual(entry[0], 100)
        self.assertEqual(entry[1], 5957)
        self.assertEqual(entry[3], 1)  # 1 failed before

    def test_score_unknown_user(self):
        """不存在的用户返回 {}."""
        score = self.client._parse_score_for_user(self.html, "no-such-user")
        self.assertEqual(score, {})

    def test_dinal_score(self):
        """Dinal: 2 AC (B=1 一次过, I=8 5 次失败后过) + G 提交了但没过."""
        score = self.client._parse_score_for_user(self.html, "Dinal")
        # B (pid=1) AC
        self.assertEqual(score["1"][0], 100)
        self.assertEqual(score["1"][3], 0)
        # I (pid=8) AC, 5 次失败
        self.assertEqual(score["8"][0], 100)
        self.assertEqual(score["8"][3], 5)
        # G (pid=6) 提交了但没 AC
        self.assertEqual(score["6"][0], 0)


class TestRealGetUserStandings(unittest.TestCase):
    """完整 get_user_standings 流程 (含 letter 映射)."""

    @classmethod
    def setUpClass(cls):
        cls.client = make_mock_client()
        cls.client._fetch_fn = make_fixtures_fetch_fn()

    def test_tarjen_solved_bgi(self):
        """tarjen 解决了 B, G, I (3 题)."""
        result = self.client.get_user_standings("1357", "tarjen")
        self.assertEqual(set(result.keys()), {"B", "G", "I"})

    def test_tarjen_b(self):
        result = self.client.get_user_standings("1357", "tarjen")
        e = result["B"]
        self.assertEqual(e.letter, "B")
        self.assertEqual(e.score, 100)
        self.assertEqual(e.contest_time_seconds, 1587)
        self.assertEqual(e.failed_attempts, 0)
        self.assertEqual(e.submission_id, "1906653")
        self.assertEqual(e.verdict, "AC")

    def test_tarjen_i_with_fail(self):
        result = self.client.get_user_standings("1357", "tarjen")
        e = result["I"]
        self.assertEqual(e.score, 100)
        self.assertEqual(e.contest_time_seconds, 5957)
        self.assertEqual(e.failed_attempts, 1)

    def test_unknown_user_empty(self):
        result = self.client.get_user_standings("1357", "no-such-user")
        self.assertEqual(result, {})


class TestRealSubmissionsParser(unittest.TestCase):
    """解析真实 QOJ 1357 提交列表 (用于 upsolve)."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "submissions_1357_tarjen.html").read_text(encoding="utf-8")
        cls.client = make_mock_client()

    def test_4_rows_parsed(self):
        """tarjen 4 条提交: B AC, I TL, I AC, G AC."""
        subs = self.client._parse_submission_list(self.html, "1357")
        self.assertEqual(len(subs), 4)

    def test_b_ac(self):
        subs = self.client._parse_submission_list(self.html, "1357")
        b = next(s for s in subs if s.problem == "B")
        self.assertEqual(b.verdict, "AC")
        self.assertEqual(b.submission_id, "1906653")
        self.assertEqual(b.contest_time_seconds, 26 * 60 + 27)  # 0:26:27

    def test_i_tl_then_ac(self):
        subs = self.client._parse_submission_list(self.html, "1357")
        i_subs = [s for s in subs if s.problem == "I"]
        self.assertEqual(len(i_subs), 2)
        # 第一次 TL, 第二次 AC
        verdicts = [s.verdict for s in i_subs]
        self.assertIn("TL", verdicts)
        self.assertIn("AC", verdicts)

    def test_g_ac(self):
        subs = self.client._parse_submission_list(self.html, "1357")
        g = next(s for s in subs if s.problem == "G")
        self.assertEqual(g.verdict, "AC")
        self.assertEqual(g.submission_id, "1907042")
        self.assertEqual(g.contest_time_seconds, 2 * 3600 + 28 * 60 + 8)  # 2:28:08


if __name__ == "__main__":
    unittest.main()


class TestReal2521EdgeCases(unittest.TestCase):
    """Contest 2521 真实数据 — 测 hyphenated / CJK / mixed-case usernames."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "standings_2521.html").read_text(encoding="utf-8")
        cls.client = make_mock_client()

    def test_score_parses(self):
        """2521 score 148KB 全部能 parse (含 781 users, 大量 weird username)."""
        m_text = self.client._parse_score_for_user(self.html, "_doesnt_matter_")
        # 触发 parse 一次 (用空 user 拿到 score 整体)
        # 直接调 _parse_score_for_user 会抛 (user 不存在) — 改用 parse_qoj_js_literal
        import platforms.qoj as q
        m = q.RE_STANDINGS_JS.search(self.html)
        score = q.parse_qoj_js_literal(m.group(2))
        self.assertGreater(len(score), 100)  # 至少 100+ users

    def test_hyphenated_user(self):
        """Today-_- 出现 (今天 -_-, 名字带 hyphen) — 之前 regex 漏掉."""
        import platforms.qoj as q
        m = q.RE_STANDINGS_JS.search(self.html)
        score = q.parse_qoj_js_literal(m.group(2))
        self.assertIn("Today-_-", score)

    def test_ucup_team_prefix(self):
        """ucup-teamNNNN 模式 (13 个)."""
        import platforms.qoj as q
        m = q.RE_STANDINGS_JS.search(self.html)
        score = q.parse_qoj_js_literal(m.group(2))
        ucup = [k for k in score if k.startswith("ucup-team")]
        self.assertGreater(len(ucup), 5)

    def test_chinese_username_robustness(self):
        """中文 username 即使出现也能 parse (虽然 2521 没有, 加 unit 测)."""
        import platforms.qoj as q
        # 模拟一段含中文 username 的 score 块
        fake_score = '{"中文名":{"1":[100,123,1234,0,100,0,[]]},"sdu-一场伟大的魔术":{"2":[0,0,1235,1,100,0,[0]]}}'
        result = q.parse_qoj_js_literal(fake_score)
        self.assertIn("中文名", result)
        self.assertIn("sdu-一场伟大的魔术", result)
        # 数据正确
        self.assertEqual(result["中文名"]["1"][0], 100)
        self.assertEqual(result["sdu-一场伟大的魔术"]["2"][0], 0)

    def test_tarjen_in_2521(self):
        """tarjen 在 2521 解了 10 题 (A-H + K + L, 跳过 I/J/M)."""
        import platforms.qoj as q
        m = q.RE_STANDINGS_JS.search(self.html)
        score = q.parse_qoj_js_literal(m.group(2))
        tarjen = score.get("tarjen", {})
        # 10 个 solved
        solved = [pid for pid, entry in tarjen.items() if entry[0] == 100]
        self.assertEqual(len(solved), 10)


if __name__ == "__main__":
    unittest.main()


class TestRealSubmissionCodeParser(unittest.TestCase):
    """解析真实 QOJ submission 页面 (抓代码用)."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "submission_1336269.html").read_text(encoding="utf-8")
        cls.client = make_mock_client()

    def test_code_block_extracted(self):
        """真实 QOJ 用 <pre><code class="sh_cpp">...</code></pre> 包裹代码."""
        code, lang = self.client._parse_code(self.html)
        # 应该是真代码, 不是 UI 小块
        self.assertGreater(len(code), 200)
        # 看起来像 C++
        self.assertIn("#include", code)
        self.assertIn("using namespace std", code)

    def test_picks_longest_pre(self):
        """UI 里有几个 <pre> 小块 (短), 应该挑最长的那个."""
        candidates = __import__("platforms.qoj", fromlist=["RE_CODE_BLOCK"]).RE_CODE_BLOCK.findall(self.html)
        self.assertGreater(len(candidates), 1)  # 至少 2 个 <pre> (UI + code)
        code, _ = self.client._parse_code(self.html)
        # 选中的应该 >= 任何 UI <pre>
        for c in candidates:
            if len(c) < 100:
                self.assertGreater(len(code), len(c),
                    f"代码 ({len(code)}) 应当 > UI <pre> ({len(c)})")


if __name__ == "__main__":
    unittest.main()
