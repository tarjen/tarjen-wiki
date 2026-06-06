"""
tests/test_qoj_sync.py
针对 tools/qoj_sync.py 的解析逻辑测试。

不真打 qoj.ac（CI 装不了 curl_cffi 的 wheel 是一回事，CF 会拦是另一回事）；
用 mock session 喂 HTML 字符串，验证正则/状态映射/缓存读写/赛中判断。
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# 把 tools/ 加进 path，让 import 找到
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import qoj_sync  # noqa: E402


# ---------------- fetch_contest_meta ----------------

CONTESTS_LIST_HTML = """
<html><body>
<table>
  <tr>
    <td><a href="/contest/2563">Foo</a></td>
    <td><a href="https://www.timeanddate.com/worldclock/fixedtime.html?iso=20251120T1000" target="_blank">2025-11-20 10:00</a></td>
    <td>5</td>
  </tr>
  <tr>
    <td><a href="/contest/2564">CCPC 2025 Women</a></td>
    <td><a href="https://www.timeanddate.com/worldclock/fixedtime.html?iso=20251130T0930" target="_blank">2025-11-30 09:30</a></td>
    <td>5</td>
  </tr>
</table>
</body></html>
"""


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = {}  # url → text

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        return FakeResp(self.responses.get(url, ""))


class TestFetchContestMeta(unittest.TestCase):
    def test_finds_target_contest(self):
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_LIST_URL] = CONTESTS_LIST_HTML
        start, dur = qoj_sync.fetch_contest_meta(s, "2564")
        self.assertEqual(dur, 5)
        # 2025-11-30 09:30 UTC
        self.assertEqual(start, datetime(2025, 11, 30, 9, 30, tzinfo=timezone.utc))

    def test_missing_contest_returns_none_pair(self):
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_LIST_URL] = CONTESTS_LIST_HTML
        start, dur = qoj_sync.fetch_contest_meta(s, "9999")
        self.assertIsNone(start)
        self.assertIsNone(dur)

    def test_cf_challenge_raises_friendly_error(self):
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_LIST_URL] = "<html>Just a moment... cf-mitigated</html>"
        with self.assertRaises(RuntimeError) as cm:
            qoj_sync.fetch_contest_meta(s, "2564")
        self.assertIn("Cloudflare", str(cm.exception))


# ---------------- fetch_contest_page ----------------

CONTEST_PAGE_HTML = """
<html><body>
  <h1>CCPC 2025 Women</h1>
  <table class="table table-bordered">
    <tr><td>A</td><td><a href="/contest/2564/problem/18314">#18314. Hello</a></td></tr>
    <tr><td>B</td><td><a href="/contest/2564/problem/18315">#18315. World</a></td></tr>
    <tr><td>C</td><td><a href="/contest/2564/problem/18316">#18316. Foo Bar</a></td></tr>
  </table>
</body></html>
"""


class TestFetchContestPage(unittest.TestCase):
    def test_extracts_name_and_problems(self):
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_PAGE_URL.format(cid="2564")] = CONTEST_PAGE_HTML
        name, problems = qoj_sync.fetch_contest_page(s, "2564")
        self.assertEqual(name, "CCPC 2025 Women")
        self.assertEqual(len(problems), 3)
        self.assertEqual(problems[0], {"id": 18314, "letter": "A", "title": "#18314. Hello"})
        self.assertEqual(problems[1]["letter"], "B")
        self.assertEqual(problems[2]["id"], 18316)

    def test_no_h1_uses_fallback_name(self):
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_PAGE_URL.format(cid="42")] = "<html><body>no h1</body></html>"
        name, problems = qoj_sync.fetch_contest_page(s, "42")
        self.assertEqual(name, "Contest 42")
        self.assertEqual(problems, [])


# ---------------- fetch_user_submissions_for_problem ----------------

def _sub_row(result_inner, time_str="2025-11-30 14:32:00"):
    """拼一行 <tr>，result_inner 是 Result 单元格的 HTML 内部，时间是 Submit Time 单元格。"""
    return (
        f'<tr>'
        f'<td><a href="/submission/12345">#12345</a></td>'
        f'<td><a href="/contest/2564/problem/18314">#18314</a></td>'
        f'<td><a href="/user/profile/tarjen">tarjen</a></td>'
        f'<td>{result_inner}</td>'
        f'<td>123ms</td>'
        f'<td>4567kb</td>'
        f'<td>C++17</td>'
        f'<td>1.2kb</td>'
        f'<td><small>{time_str}</small></td>'
        f'<td><small>{time_str}</small></td>'
        f'</tr>'
    )


def _submissions_html(rows_html):
    return f'<html><body><table><thead><tr><th>ID</th></tr></thead><tbody>{rows_html}</tbody></table></body></html>'


class TestFetchUserSubmissions(unittest.TestCase):
    def _run(self, rows):
        s = FakeSession()
        s.responses[qoj_sync.SUBMISSIONS_URL] = _submissions_html(rows)
        return qoj_sync.fetch_user_submissions_for_problem(s, "tarjen", 18314)

    def test_accepted_score_100(self):
        rows = _sub_row('<a class="uoj-score">100</a>')
        out = self._run(rows)
        self.assertEqual(out, [("AC", "2025-11-30 14:32:00")])

    def test_partial_score_mapped_to_SN(self):
        rows = _sub_row('<a class="uoj-score">40</a>')
        out = self._run(rows)
        self.assertEqual(out, [("S40", "2025-11-30 14:32:00")])

    def test_wa_string_mapped(self):
        rows = _sub_row('<a class="small">Wrong Answer</a>')
        out = self._run(rows)
        self.assertEqual(out, [("WA", "2025-11-30 14:32:00")])

    def test_ce_string_mapped(self):
        rows = _sub_row('<a class="small">Compile Error</a>')
        out = self._run(rows)
        self.assertEqual(out, [("CE", "2025-11-30 14:32:00")])

    def test_unknown_status_mapped_to_double_question(self):
        rows = _sub_row('<a class="small">Banana Split</a>')
        out = self._run(rows)
        self.assertEqual(out, [("??", "2025-11-30 14:32:00")])

    def test_multiple_subs_returned_in_order(self):
        # QOJ 列表是新的在前；脚本原样返回，调用方自己取 min(ac)
        rows = (
            _sub_row('<a class="small">Wrong Answer</a>', "2025-11-30 14:00:00")
            + _sub_row('<a class="uoj-score">100</a>', "2025-11-30 14:30:00")
        )
        out = self._run(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0], "WA")
        self.assertEqual(out[1][0], "AC")

    def test_no_time_in_cell_skipped(self):
        # 时间缺失的行应该被跳过
        bad_row = _sub_row('<a class="uoj-score">100</a>').replace(
            '<small>2025-11-30 14:32:00</small>', '<small>unknown</small>'
        )
        out = self._run(bad_row)
        # 解析时 row 没找到时间，但 status 还在；脚本把 t="" 写入
        # 我们期望 status 仍被记录，时间空字符串
        self.assertEqual(out, [("AC", "")])


# ---------------- pure logic: best_status / is_during_contest ----------------

class TestBestStatus(unittest.TestCase):
    def test_none_when_empty(self):
        self.assertIsNone(qoj_sync.best_status([]))

    def test_ac_returned_with_earliest_time(self):
        # QOJ 新的在前 → 第一个 AC 是最晚的；best_status 取 min(ac_times)
        out = qoj_sync.best_status([
            ("AC", "2025-11-30 14:50:00"),
            ("AC", "2025-11-30 14:30:00"),
            ("AC", "2025-11-30 14:40:00"),
        ])
        self.assertEqual(out, ("AC", "2025-11-30 14:30:00"))

    def test_no_ac_returns_first_error(self):
        out = qoj_sync.best_status([("WA", "t1"), ("TLE", "t2")])
        self.assertEqual(out, ("WA", ""))

    def test_ac_mixed_with_errors_returns_ac(self):
        out = qoj_sync.best_status([("WA", "t1"), ("AC", "t2"), ("TLE", "t3")])
        self.assertEqual(out, ("AC", "t2"))


class TestIsDuringContest(unittest.TestCase):
    # 比赛：2025-11-30 09:30 UTC 开始，持续 5 小时 → 14:30 UTC 结束
    start = datetime(2025, 11, 30, 9, 30, tzinfo=timezone.utc)

    def test_during_contest(self):
        self.assertTrue(qoj_sync.is_during_contest("2025-11-30 12:00:00", self.start, 5))

    def test_exactly_at_start(self):
        self.assertTrue(qoj_sync.is_during_contest("2025-11-30 09:30:00", self.start, 5))

    def test_exactly_at_end(self):
        self.assertTrue(qoj_sync.is_during_contest("2025-11-30 14:30:00", self.start, 5))

    def test_after_contest(self):
        self.assertFalse(qoj_sync.is_during_contest("2025-11-30 15:00:00", self.start, 5))

    def test_before_contest(self):
        self.assertFalse(qoj_sync.is_during_contest("2025-11-30 09:00:00", self.start, 5))

    def test_empty_string(self):
        self.assertFalse(qoj_sync.is_during_contest("", self.start, 5))

    def test_no_start_time(self):
        self.assertFalse(qoj_sync.is_during_contest("2025-11-30 12:00:00", None, 5))

    def test_no_duration(self):
        self.assertFalse(qoj_sync.is_during_contest("2025-11-30 12:00:00", self.start, 0))

    def test_bad_time_string(self):
        self.assertFalse(qoj_sync.is_during_contest("garbage", self.start, 5))


# ---------------- cache I/O ----------------

class TestCacheIO(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        cache = qoj_sync.load_cache(path="/nonexistent/abc.json")
        self.assertEqual(cache["version"], 1)
        self.assertEqual(cache["contests"], {})

    def test_load_corrupted_returns_empty(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{garbage")
            p = f.name
        try:
            cache = qoj_sync.load_cache(path=p)
            self.assertEqual(cache["contests"], {})
        finally:
            Path(p).unlink()

    def test_save_then_load_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cache.json"
            cache = {
                "version": 1,
                "contests": {
                    "2564": {"name": "X", "problems": [{"id": 1, "letter": "A", "title": "t"}]},
                },
            }
            qoj_sync.save_cache(cache, path=p)
            self.assertTrue(p.exists())
            reloaded = qoj_sync.load_cache(path=p)
            self.assertEqual(reloaded["contests"]["2564"]["name"], "X")
            # updated_at 被脚本自己设了
            self.assertNotEqual(reloaded["updated_at"], "")

    def test_save_preserves_existing_contests(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cache.json"
            qoj_sync.save_cache({
                "contests": {
                    "1": {"name": "old"},
                    "2": {"name": "old2"},
                },
            }, path=p)
            # 模拟 main 跑：覆盖 1，追加 3
            cache = qoj_sync.load_cache(path=p)
            cache["contests"]["1"] = {"name": "new"}
            cache["contests"]["3"] = {"name": "new3"}
            qoj_sync.save_cache(cache, path=p)
            reloaded = qoj_sync.load_cache(path=p)
            self.assertEqual(reloaded["contests"]["1"]["name"], "new")
            self.assertEqual(reloaded["contests"]["2"]["name"], "old2")  # 保留
            self.assertEqual(reloaded["contests"]["3"]["name"], "new3")  # 新增


# ---------------- _check_cf ----------------

class TestCheckCf(unittest.TestCase):
    def test_just_a_moment(self):
        with self.assertRaises(RuntimeError):
            qoj_sync._check_cf("<html>Just a moment...</html>", "x")

    def test_cf_mitigated_header_text(self):
        with self.assertRaises(RuntimeError):
            qoj_sync._check_cf("cf-mitigated: challenge", "x")

    def test_normal_html_passes(self):
        qoj_sync._check_cf("<html><body>ok</body></html>", "x")  # 不抛


# ---------------- main: argv / env parsing (smoke test) ----------------

class TestMainArgv(unittest.TestCase):
    def test_missing_both_args_exits_2(self):
        # 清掉 env 避免污染
        import os
        old = {k: os.environ.get(k) for k in ("CONTEST_ID", "USERNAME")}
        for k in old: os.environ.pop(k, None)
        try:
            with self.assertRaises(SystemExit) as cm:
                qoj_sync.main()
            self.assertEqual(cm.exception.code, 2)
        finally:
            for k, v in old.items():
                if v is not None: os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
