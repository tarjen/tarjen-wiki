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


class FakePage:
    """fetch_standings_for_user 用 page.evaluate() 拿 standings JSON-like 数据。
    测试里直接 .evaluate_result = {...} 模拟即可。"""

    def __init__(self):
        self.evaluate_result = None
        self.evaluate_calls = []

    def evaluate(self, js, *args):
        self.evaluate_calls.append((js, args))
        return self.evaluate_result


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = {}  # url → text
        self._page = FakePage()
        self._cf_blocked_submissions = False

    def get(self, url, params=None, **kwargs):
        # 接受 _cf_timeout / _retries 等 kwargs（real session 才有）—— 测试用不到，吞掉即可
        self.calls.append({"url": url, "params": params, "kwargs": kwargs})
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

    def test_cf_challenge_fail_soft(self):
        # fetch_contest_meta 在 CF 卡住时不再抛——> fail-soft 返回 (None, None)
        # 上下文：GitHub Actions IP 段可能被 CF 标记，/contests 拿不到时不能整个 import 失败
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_LIST_URL] = "<html>Just a moment... cf-mitigated</html>"
        start, dur = qoj_sync.fetch_contest_meta(s, "2564")
        self.assertIsNone(start)
        self.assertIsNone(dur)


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

    def test_login_redirect_raises_friendly_error(self):
        # 模拟 cookie 过期/没填/不够：QOJ 把 /contest/2564 302 到 /login
        s = FakeSession()
        s.responses[qoj_sync.CONTEST_PAGE_URL.format(cid="2564")] = (
            "<html><head><title>Login - QOJ.ac</title></head><body>login form</body></html>"
        )
        with self.assertRaises(RuntimeError) as cm:
            qoj_sync.fetch_contest_page(s, "2564")
        self.assertIn("登录", str(cm.exception))
        self.assertIn("uoj_remember_token", str(cm.exception))


# ---------------- fetch_standings_for_user ----------------
#
# fetch_standings_for_user 用 page.evaluate() 解析 DOM（不解析 400KB 的 regex）。
# 测试里直接设 FakePage.evaluate_result 模拟 DOM 评估结果。
# 数据形态（跟 qoj_sync.py 里 page.evaluate 那段 JS 同步）：
#   {"letters": ["A", "B", ...], "cells": [{"letter", "status", "time"}, ...]}
# 0 cells = 用户不在榜里。

STANDINGS_USER_ROW_HTML = """
<html><body>
  <table>
    <tr>
      <th>Rank.</th><th>Username</th>
      <th>A425/506</th><th>B265/711</th><th>C391/1211</th>
      <th>D72/159</th><th>E131/498</th><th>F31/120</th>
    </tr>
    <tr>
      <td>1</td><td>winner (Foo, Bar)</td>
      <td><center>+<br><font size="1">0:02</font></center></td>
      <td><center>+<br><font size="1">0:12</font></center></td>
      <td><center>+1<br><font size="1">0:26</font></center></td>
      <td><center>+<br><font size="1">0:33</font></center></td>
      <td><center>-<br></center></td>
      <td><center>-6<br><font size="1">2:14</font></center></td>
    </tr>
    <tr>
      <td>58</td><td>tarjen</td>
      <td><center>+3<br><font size="1">0:08</font></center></td>
      <td><center>+<br><font size="1">0:52</font></center></td>
      <td><center>+<br><font size="1">0:57</font></center></td>
      <td><center>+3<br><font size="1">1:13</font></center></td>
      <td><center>+1<br><font size="1">0:46</font></center></td>
      <td><center>-<br></center></td>
    </tr>
  </table>
</body></html>
"""


class TestFetchStandings(unittest.TestCase):
    def _run(self, page_result):
        s = FakeSession()
        s.responses[qoj_sync.STANDINGS_URL.format(cid="2564")] = STANDINGS_USER_ROW_HTML
        s._page.evaluate_result = page_result
        return qoj_sync.fetch_standings_for_user(s, "2564", "tarjen")

    def test_finds_tarjen_with_mixed_results(self):
        # 模拟 DOM 评估结果：tarjen 那一行 6 个 cell (A-F)
        page_result = {
            "letters": ["A", "B", "C", "D", "E", "F"],
            "cells": [
                {"letter": "A", "status": "+3", "time": "0:08"},
                {"letter": "B", "status": "+", "time": "0:52"},
                {"letter": "C", "status": "+", "time": "0:57"},
                {"letter": "D", "status": "+3", "time": "1:13"},
                {"letter": "E", "status": "+1", "time": "0:46"},
                {"letter": "F", "status": "-", "time": ""},
            ],
        }
        out = self._run(page_result)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0], {"letter": "A", "status": "+3", "time": "0:08"})
        self.assertEqual(out[5], {"letter": "F", "status": "-", "time": ""})

    def test_user_not_in_standings_returns_none(self):
        # page.evaluate 返回 cells=null（用户不在榜里——没打这场比赛）
        page_result = {"letters": ["A", "B"], "cells": None}
        out = self._run(page_result)
        self.assertIsNone(out)

    def test_no_table_returns_none(self):
        page_result = {"error": "no_table"}
        out = self._run(page_result)
        self.assertIsNone(out)

    def test_cf_challenge_fail_soft(self):
        # /results HTML 是 CF 挑战页 → 抛 RuntimeError 走 fail-soft 分支
        s = FakeSession()
        s.responses[qoj_sync.STANDINGS_URL.format(cid="2564")] = (
            "<html>Just a moment... cf-mitigated</html>"
        )
        out = qoj_sync.fetch_standings_for_user(s, "2564", "tarjen")
        self.assertIsNone(out)
        self.assertTrue(s._cf_blocked_submissions)

    def test_login_redirect_raises_friendly_error(self):
        # cookie 过期：QOJ 把 /results 也重定向到 /login
        s = FakeSession()
        s.responses[qoj_sync.STANDINGS_URL.format(cid="2564")] = (
            "<html><head><title>Login - QOJ.ac</title></head><body>login form</body></html>"
        )
        with self.assertRaises(RuntimeError) as cm:
            qoj_sync.fetch_standings_for_user(s, "2564", "tarjen")
        self.assertIn("登录", str(cm.exception))
        self.assertIn("uoj_remember_token", str(cm.exception))

    def test_uses_cf_timeout_and_retries(self):
        # /results 第一次 120s × 2 = 4 min 解 challenge；确保调用时传对了
        s = FakeSession()
        s.responses[qoj_sync.STANDINGS_URL.format(cid="2564")] = STANDINGS_USER_ROW_HTML
        s._page.evaluate_result = {"letters": ["A"], "cells": []}
        qoj_sync.fetch_standings_for_user(s, "2564", "tarjen")
        self.assertEqual(len(s.calls), 1)
        kw = s.calls[0]["kwargs"]
        self.assertEqual(kw["_cf_timeout"], 120)
        self.assertEqual(kw["_retries"], 1)


# ---------------- _parse_relative_time ----------------

class TestParseRelativeTime(unittest.TestCase):
    # 比赛 2025-11-30 09:30:00 UTC 开始
    start = datetime(2025, 11, 30, 9, 30, 0, tzinfo=timezone.utc)

    def test_minutes_only(self):
        # 0:08 → 09:30 + 8min = 09:38
        self.assertEqual(
            qoj_sync._parse_relative_time("0:08", self.start),
            "2025-11-30 09:38:00",
        )

    def test_hours_minutes_seconds(self):
        # 1:34:05 → 09:30 + 1h34m5s = 11:04:05
        self.assertEqual(
            qoj_sync._parse_relative_time("1:34:05", self.start),
            "2025-11-30 11:04:05",
        )

    def test_over_one_hour(self):
        # 5h contest；5:00:00 → 14:30
        self.assertEqual(
            qoj_sync._parse_relative_time("5:00:00", self.start),
            "2025-11-30 14:30:00",
        )

    def test_empty_string(self):
        self.assertEqual(qoj_sync._parse_relative_time("", self.start), "")

    def test_no_start_time(self):
        self.assertEqual(qoj_sync._parse_relative_time("0:08", None), "")

    def test_garbage_input(self):
        self.assertEqual(qoj_sync._parse_relative_time("not a time", self.start), "")

    def test_strips_whitespace(self):
        self.assertEqual(
            qoj_sync._parse_relative_time("  0:08  ", self.start),
            "2025-11-30 09:38:00",
        )


# ---------------- pure logic: is_during_contest ----------------

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


# ---------------- _parse_cookie_kv ----------------

class TestParseCookieKv(unittest.TestCase):
    def test_value_only_uses_default_name(self):
        # 裸 value；用默认 uoj_remember_token
        self.assertEqual(
            qoj_sync._parse_cookie_kv("abc123def456"),
            [("uoj_remember_token", "abc123def456")],
        )

    def test_name_equals_value_split(self):
        # 单对：DevTools "Copy" 整个 cookie 行偶尔会带 "uoj_remember_token=..."
        self.assertEqual(
            qoj_sync._parse_cookie_kv("uoj_remember_token=abc123def456"),
            [("uoj_remember_token", "abc123def456")],
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            qoj_sync._parse_cookie_kv("  uoj_remember_token=abc  "),
            [("uoj_remember_token", "abc")],
        )

    def test_strips_cookie_prefix(self):
        # Set-Cookie 风格的整行
        self.assertEqual(
            qoj_sync._parse_cookie_kv("Cookie: uoj_remember_token=abc"),
            [("uoj_remember_token", "abc")],
        )

    def test_empty_returns_empty_list_pair(self):
        self.assertEqual(qoj_sync._parse_cookie_kv(""), [("uoj_remember_token", "")])

    def test_custom_default_name(self):
        self.assertEqual(
            qoj_sync._parse_cookie_kv("xyz", default_name="UOJ_Auth"),
            [("UOJ_Auth", "xyz")],
        )

    # 多 cookie（QOJ 用 uoj_remember_token + uoj_remember_token_check + UOJSESSID 三件套）
    def test_multi_cookie_header_split(self):
        # 整段 Cookie header：'a=1; b=2; c=3'
        out = qoj_sync._parse_cookie_kv(
            "uoj_remember_token=58AuJ; uoj_remember_token_check=97191a; UOJSESSID=0d65sv"
        )
        self.assertEqual(out, [
            ("uoj_remember_token", "58AuJ"),
            ("uoj_remember_token_check", "97191a"),
            ("UOJSESSID", "0d65sv"),
        ])

    def test_multi_cookie_with_extra_whitespace_and_prefix(self):
        # DevTools 复制整段时偶尔会带 "Cookie:" 前缀和奇怪空白
        out = qoj_sync._parse_cookie_kv(
            "Cookie: uoj_remember_token=abc ;  uoj_remember_token_check=xyz  ;  UOJSESSID=q"
        )
        self.assertEqual(out, [
            ("uoj_remember_token", "abc"),
            ("uoj_remember_token_check", "xyz"),
            ("UOJSESSID", "q"),
        ])

    def test_multi_cookie_skips_empty_pairs(self):
        # 容错：双分号 / 末尾分号
        out = qoj_sync._parse_cookie_kv("a=1;; b=2;")
        self.assertEqual(out, [("a", "1"), ("b", "2")])

    def test_summary_unpacks_pairs_not_cookies_dicts(self):
        # 回归测试：之前 _PlaywrightSession.__init__ 用 for n,v in cookies
        # 拿 dict 列表解包成 2 个 var 报 "too many values to unpack"。
        # 修复：用 pairs (list[tuple]) 来生成日志 summary。
        pairs = qoj_sync._parse_cookie_kv("uoj_remember_token=58AuJ; UOJSESSID=0d65sv")
        cookies = [
            {"name": n, "value": v, "domain": "qoj.ac", "path": "/",
             "secure": True, "sameSite": "Lax"}
            for n, v in pairs if n and v
        ]
        # 这一行模拟 _PlaywrightSession 里的 summary 生成；不能 raise
        summary = ", ".join(f"{n}=<{len(v)} chars>" for n, v in pairs if n and v)
        self.assertEqual(summary, "uoj_remember_token=<5 chars>, UOJSESSID=<6 chars>")
        # cookies 仍是 dict 列表（4 键），不会跟 pairs 混淆
        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["name"], "uoj_remember_token")


if __name__ == "__main__":
    unittest.main()
