#!/usr/bin/env python3
"""
tools/platforms/qoj.py — QOJ (qoj.ac, UOJ fork) 平台客户端

QOJ 没有公开 API, HTML 抓取. 关键信息:
  - 比赛页:        https://qoj.ac/contest/<cid>             (公开题目列表)
  - standings:    https://qoj.ac/contest/<cid>/standings   (公开 JS 数据, 但有 cookie 看更全)
  - 提交列表:      https://qoj.ac/contest/<cid>/submissions?user=<name>&page=<n>
  - 单份提交:      https://qoj.ac/submission/<sid>

Auth: 3 个 cookie (uoj_remember_token / uoj_remember_token_checksum / UOJSESSID)

Standings 页是 JS 渲染 — 实际数据在 <script> 里的两个 JS 数组:
  standings=[ [solved, penalty, [user_obj], rank, pct], ... ]
  score={ "<user>": { "<pid>": [score, time_sec, sub_id, failed_before, full_score, ?, [tags]], ... } }
pid 是 0-indexed 题目序号 (按字母顺序 A=0, B=1, ...), 需要从比赛页拿到 letter→pid 映射.

提交列表页 (HTML):
  <tr>
    <td><a href="/submission/1907042">#1907042</a></td>
    <td><a href="/contest/1357/problem/7416">G. Grammarly</a></td>
    <td><span class="uoj-username" ...>tarjen</span><sup><a href="/contest/1357">#</a></sup></td>
    <td><a href="/submission/1907042" class="uoj-score" data-full="100" data-score="100">AC ✓</a></td>
    <td>25ms</td>
    <td>15976kb</td>
    <td>3.5kb</td>
    <td><small>02:28:08</small></td>
  </tr>

注意: QOJ 改版时 HTML 选择器可能失效, standings JS 格式也可能变. 都需要重新跑 fixtures.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .base import (
    CFBlockedError,
    ContestMeta,
    CookieExpiredError,
    NotFoundError,
    ParseError,
    PlatformClient,
    StandingsEntry,
    Submission,
    TimeoutError_,
    register,
)


# QOJ 用的 3 个 cookie key
COOKIE_KEY_TOKEN = "uoj_remember_token"
COOKIE_KEY_CHECKSUM = "uoj_remember_token_checksum"
COOKIE_KEY_SESSID = "UOJSESSID"

# 反爬虫: Cloudflare challenge 页特征
CF_CHALLENGE_MARKERS = [
    "Just a moment...",
    "请稍候...",
    "<title>Just a moment",
    "cf-challenge",
]

# QOJ 登录页特征 (UOJ 风格)
LOGIN_PAGE_MARKERS = [
    'action="/login"',
    "请先登录",
    "Please login first",
    "Please log in first",
    "Login Required",
]

# === HTML 解析用的正则 (QOJ 实际页面结构) ===

# 比赛页: title 优先取最后一个非空 <h1> (排除 navbar), fallback <title>
RE_CONTEST_TITLE_H1 = re.compile(
    r"<h1[^>]*>(.+?)</h1>",
    re.DOTALL,
)
RE_CONTEST_TITLE_TAG = re.compile(
    r"<title[^>]*>\s*(.+?)\s*</title>",
    re.DOTALL,
)
# 比赛页: 题目列表. 支持两种格式:
#   真 QOJ: <tr><td>A</td><td><a href="/contest/1357/problem/7410">Name</a></td></tr>
#   旧/mock: <li><a href="/contest/2564/problem/A">A</a></li>
# group(1) = letter (from <td>), group(2) = letter (from <a>) — 一个是空
RE_PROBLEM_LISTING = re.compile(
    r'(?:<tr[^>]*>\s*<td[^>]*>\s*([A-Z])\s*</td>\s*<td[^>]*>\s*<a[^>]*'
    r'href="/contest/\d+/problem/\w+"[^>]*>[^<]*</a>'
    r'|<li>\s*<a[^>]*href="/contest/\d+/problem/([A-Z])"[^>]*>[A-Z]</a>)',
    re.DOTALL,
)
# 比赛页: 起止时间 (UOJ/QOJ 格式: "Start: ... End: ...")
RE_CONTEST_TIMES = re.compile(
    r"Start:\s*(\d{4}-\d{2}-\d{2}[^,\n]*?)\s*End:\s*(\d{4}-\d{2}-\d{2}[^,\n]*?)(?:<|$)",
    re.IGNORECASE,
)

# Standings 页: JS 数据块 (在 <script> 里 inline 出来)
# 用 sentinel `};\nproblems=` 区分 score 的结束 (里面含嵌套 {})
# 中间可能有 fullscore=...; 行, 用行首 score= 锚定避免匹到 fullscore=
RE_STANDINGS_JS = re.compile(
    r"standings_version=\d+;[\s\S]*?standings=(\[\[.*?\]\]);[\s\S]*?"
    r"(?:^|\n)\s*(?<![A-Za-z])score=(\{[\s\S]*?\});\s*\n?\s*problems=",
    re.MULTILINE,
)
# Score entry: [score, time_sec, sub_id, failed_before, full_score, ?, [tags]]
# 注意: 内部可能含未引号字符串 (如 "$DEFAULT_DAT_PREFIX_1") — QOJ 用 JS 字面量不是 JSON

# 提交列表页: 一行 (tr) 解析. 抓关键字段 (顺序不固定, mock/旧版列序不同):
#   - submission ID (href="/submission/(\d+)")
#   - problem letter (problem link 文本首字母)
#   - verdict (AC/WA/TL/...) — 可能在 <a class="uoj-score"> 或裸 <td>
#   - submit time — H:MM:SS 或 wallclock datetime
RE_SUB_ROW = re.compile(r'<tr[^>]*>(?P<row>.*?)</tr>', re.DOTALL)
RE_SUB_ID = re.compile(r'href="/submission/(\d+)"')
RE_SUB_PROBLEM_LETTER = re.compile(r'href="/contest/\d+/problem/[A-Z0-9]+"[^>]*>\s*([A-Z])')
RE_SUB_VERDICT = re.compile(
    r'class="uoj-score"[^>]*>\s*([A-Za-z]+)\b'
    r'|<td[^>]*>\s*(AC|WA|TL|RE|ML|CE|SE)\s*</td>'
)
# User: 优先 uoj-username span, fallback profile 链接
RE_SUB_USER = re.compile(
    r'class="uoj-username"[^>]*>([^<]+)</span>'
    r'|<a[^>]*href="/user/profile/[^"]+"[^>]*>([^<]+)</a>'
)
RE_SUB_TIME_FIELD = re.compile(
    r'<td[^>]*>\s*(?:<small>)?\s*('
    r'\d{1,2}:\d{1,2}:\d{2}'  # 1:23:45
    r'|\d{1,2}:\d{2}'          # 1:23
    r'|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'  # 2025-06-09 22:14:00
    r')\s*(?:</small>)?\s*</td>',
    re.DOTALL,
)
# 长度: 纯数字 <td>...</td> (旧格式第 8 列)
RE_SUB_LENGTH = re.compile(r'<td[^>]*>\s*(\d+)\s*</td>')
# 语言: 含 C++ / Python / Java / Rust / Go / Pascal / Ruby 等关键字的 <td>
RE_SUB_LANG = re.compile(
    r'<td[^>]*>\s*([A-Za-z0-9+#.\s]+?(?:C\+\+|Python|Java|Rust|Go|Pascal|Ruby|Kotlin|Swift|Haskell|Lua)\w*)\s*</td>'
)

# 单份提交页: 提取代码
RE_CODE_BLOCK = re.compile(
    r'<pre[^>]*class="[^"]*code[^"]*"[^>]*>(.*?)</pre>',
    re.DOTALL,
)
RE_CODE_LANG = re.compile(
    r'(?:Language|语言)\s*[:：]?\s*([A-Za-z0-9+#.\s]+?)(?=<|$)',
    re.MULTILINE,
)


# === Cookie 加载 (Netscape 格式) ===

def parse_netscape_cookies(text: str, domain: str = "qoj.ac") -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        parts = re.split(r"\s+", line, maxsplit=6)
        if len(parts) < 7:
            continue
        cookie_domain, _, _, _, _, name, value = parts[:7]
        if domain in cookie_domain or cookie_domain.lstrip(".") == domain:
            cookies[name] = value
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# === 工具 ===

def html_unescape(s: str) -> str:
    return (s
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&amp;", "&"))


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_contest_time(text: str) -> int | None:
    text = text.strip()
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    except ValueError:
        return None
    return None


def parse_qoj_js_literal(s: str):
    """QOJ standings JS 数组不是合法 JSON, 用 state machine 转换.

    已知 QOJ 怪格式:
      - 未引号标识符: $DEFAULT_DAT_PREFIX_1, tarjen, Cookie_Creamm
      - 带 hyphen: Today-_-, ucup-team7572, blackF4n-Club
      - 中文 username: sdu-一场伟大的魔术
      - 数字 key: "1": [...] (其实没引号: 1: [...] 也行, JSON 不支持数字 key)
      - 已是合法 JSON 也别动

    策略: 逐字符扫, 跟踪 "在字符串里" vs "在代码里", 把代码区里
    `, ` { ` ` ` 紧跟着的 bare identifier 全加引号.
    """
    out = []
    i = 0
    n = len(s)
    in_string = False
    str_quote = None
    escape = False
    # 哪些字符算 "identifier 合法起始": 字母 / $ / _
    # 哪些算 "identifier 合法延续": 上面 + 数字 + hyphen + dot + 中文
    # 但我们不需要严格 — 遇到 "不在字符串里" 且 "看起来像 key" 就加引号
    ID_CONT = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$-一-鿿."
    )
    ID_START = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$"
    )

    def is_bare_key_start(pos: int) -> bool:
        """位置 pos 是不是 bare key 的开始?

        bare key 出现在:
          - `,` 之后, 跳过空白
          - `{` 之后, 跳过空白
        且以 ID_START 字符开始, 后面跟 ID_CONT.
        """
        # 找前一个非空白字符
        j = pos - 1
        while j >= 0 and s[j] in " \t\r\n":
            j -= 1
        if j < 0:
            return False
        if s[j] in ",{":
            return True
        return False

    def scan_bare_key(pos: int) -> tuple[str, int] | None:
        """从 pos 开始扫一个 bare key, 返回 (key, end_pos) 或 None."""
        if pos >= n or s[pos] not in ID_START:
            return None
        end = pos + 1
        while end < n and s[end] in ID_CONT:
            end += 1
        # bare key 必须跟 `:` (跳过空白)
        k = end
        while k < n and s[k] in " \t\r\n":
            k += 1
        if k >= n or s[k] != ":":
            return None
        return s[pos:end], end

    while i < n:
        c = s[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == str_quote:
                in_string = False
                str_quote = None
            i += 1
            continue

        # 在代码区
        if c in ('"', "'"):
            in_string = True
            str_quote = c
            out.append(c)
            i += 1
            continue

        if c.isalpha() or c == "_" or c == "$":
            # 可能是 bare key
            scanned = scan_bare_key(i)
            if scanned is not None:
                key, end = scanned
                # 加引号
                out.append('"')
                out.append(key)
                out.append('"')
                i = end
                continue
            # 不是 bare key (比如函数调用的一部分), 原样输出
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1

    return json.loads("".join(out))


# === Client ===

@register
class QojClient(PlatformClient):
    name = "qoj"

    BASE_URL = "https://qoj.ac"
    DEFAULT_TIMEOUT = 30
    DEFAULT_INTERVAL = 1.5

    def __init__(self, cookies: dict[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 request_interval: float = DEFAULT_INTERVAL,
                 fetch_fn=None):
        self.cookies = cookies or {}
        self.timeout = timeout
        self.request_interval = request_interval
        self._last_request_at = 0.0
        self._fetch_fn = fetch_fn

    # === PlatformClient 接口 ===

    def cookies_valid(self) -> bool:
        return all(k in self.cookies for k in (
            COOKIE_KEY_TOKEN, COOKIE_KEY_CHECKSUM, COOKIE_KEY_SESSID,
        ))

    def get_contest_meta(self, contest_id: str) -> ContestMeta:
        html = self._fetch(f"/contest/{contest_id}")
        return self._parse_contest_meta(html, contest_id)

    def get_user_submissions(self, contest_id: str, user: str) -> list[Submission]:
        """抓所有页 (含赛中 + 赛后), 调用方按时间筛."""
        all_subs = []
        page = 1
        while True:
            url = f"/contest/{contest_id}/submissions?user={user}&page={page}"
            html = self._fetch(url)
            subs = self._parse_submission_list(html, contest_id)
            if not subs:
                break
            all_subs.extend(subs)
            if len(subs) < 50:
                break
            page += 1
            if page > 100:
                break
        return all_subs

    def get_user_standings(self, contest_id: str, user: str) -> dict[str, StandingsEntry]:
        """从 /contest/<cid>/standings 读 JS 数据, 返回 user 的每题结果.

        返回: {letter: StandingsEntry} — 只含提交过的题.
        """
        # 1. 拿比赛页 → letter 顺序 (pid 是 0-indexed 字母序)
        meta_html = self._fetch(f"/contest/{contest_id}")
        letters = self._extract_problem_letters(meta_html)

        # 2. 拿 standings 页 → 解析 score[user]
        standings_html = self._fetch(f"/contest/{contest_id}/standings")
        score = self._parse_score_for_user(standings_html, user)

        # 3. pid → letter → StandingsEntry
        result: dict[str, StandingsEntry] = {}
        for pid_str, entry in score.items():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if pid < 0 or pid >= len(letters):
                continue
            letter = letters[pid]
            score_val, time_sec, sub_id, failed = entry[0], entry[1], entry[2], entry[3]
            verdict = "AC" if score_val == 100 else "WA"
            result[letter] = StandingsEntry(
                platform="qoj",
                problem_id=pid_str,
                letter=letter,
                score=score_val,
                contest_time_seconds=time_sec,
                submission_id=str(sub_id) if sub_id and sub_id != -1 else None,
                failed_attempts=failed,
                verdict=verdict,
            )
        return result

    def get_submission_code(self, submission_id: str) -> tuple[str, str]:
        html = self._fetch(f"/submission/{submission_id}")
        return self._parse_code(html)

    # === HTTP ===

    def _fetch(self, path_or_url: str) -> str:
        if self._fetch_fn is not None:
            cookie_str = cookie_header(self.cookies) if self.cookies else ""
            body = self._fetch_fn(path_or_url, cookie_str)
            status = 200
        else:
            import cloudscraper
            url = path_or_url if path_or_url.startswith("http") else self.BASE_URL + path_or_url
            scraper = cloudscraper.create_scraper()
            scraper.headers.update({
                "Cookie": cookie_header(self.cookies),
                "User-Agent": "Mozilla/5.0 (compatible; Wiki-Backend/1.0; +https://github.com/tarjen/tarjen-wiki)",
                "Accept": "text/html,application/xhtml+xml",
            })
            elapsed = time.time() - self._last_request_at
            if elapsed < self.request_interval:
                time.sleep(self.request_interval - elapsed)
            self._last_request_at = time.time()
            try:
                resp = scraper.get(url, timeout=self.timeout)
                status = resp.status_code
                body = resp.text
            except Exception as e:
                raise PlatformError(f"fetch 失败: {e}") from e

        if status in (401, 403) or self._is_login_page(body):
            raise CookieExpiredError(
                f"QOJ cookie 失效 (HTTP {status}, login page detected)"
            )
        if self._is_cf_challenge(body):
            raise CFBlockedError("Cloudflare challenge detected")
        if status == 404:
            raise NotFoundError(f"HTTP 404: {path_or_url}")
        if status >= 400:
            raise ParseError(f"HTTP {status}: {path_or_url}")
        return body

    @staticmethod
    def _is_login_page(body: str) -> bool:
        if not body:
            return False
        return any(m in body for m in LOGIN_PAGE_MARKERS)

    @staticmethod
    def _is_cf_challenge(body: str) -> bool:
        if not body:
            return False
        return any(m in body for m in CF_CHALLENGE_MARKERS)

    # === 解析 ===

    def _parse_contest_meta(self, html: str, contest_id: str) -> ContestMeta:
        all_h1 = RE_CONTEST_TITLE_H1.findall(html)
        contest_h1 = [h for h in all_h1
                     if h.strip() not in ("QOJ.ac", "QOJ", "QOJ.ac\n")]
        if contest_h1:
            raw_title = html_unescape(contest_h1[-1]).strip()
        else:
            title_match = RE_CONTEST_TITLE_TAG.search(html)
            if not title_match:
                raise ParseError(f"找不到 <h1> 或 <title>, HTML 长度 {len(html)}")
            raw_title = html_unescape(title_match.group(1)).strip()
        title = re.sub(r"\s*-\s*(Dashboard|Contest|QOJ\.ac|Login|Profile).*$",
                       "", raw_title, flags=re.IGNORECASE)
        title = re.sub(r"\s*-\s*(Dashboard|Contest|QOJ\.ac|Login|Profile).*$",
                       "", title, flags=re.IGNORECASE)
        title = title.strip()
        if not title:
            title = raw_title

        letters = self._extract_problem_letters(html)
        if not letters:
            raise ParseError(f"找不到 problem listing, HTML 长度 {len(html)}")
        problem_count = len(letters)

        start_time = end_time = None
        times = RE_CONTEST_TIMES.search(html)
        if times:
            start_time = times.group(1).strip()
            end_time = times.group(2).strip()

        return ContestMeta(
            platform="qoj",
            contest_id=contest_id,
            title=title,
            problem_count=problem_count,
            start_time=start_time,
            end_time=end_time,
            url=f"{self.BASE_URL}/contest/{contest_id}",
        )

    def _extract_problem_letters(self, html: str) -> list[str]:
        """从比赛页提取题目字母, 按 A, B, C, ... 顺序."""
        matches = RE_PROBLEM_LISTING.findall(html)
        # matches 是 list[tuple]: 每个 tuple 有一个非空元素 (letter)
        return [m[0] or m[1] for m in matches]

    def _parse_score_for_user(self, html: str, user: str) -> dict:
        """从 standings 页 JS 数据里找 user 的 score 表.

        返回: {pid_str: [score, time_sec, sub_id, failed_before, ...], ...}
        user 不存在时返回 {}.
        """
        m = RE_STANDINGS_JS.search(html)
        if not m:
            raise ParseError("找不到 standings JS 数据 (页面可能不是 standings 页)")
        score_str = m.group(2)
        # 转 JS 字面量为 JSON
        score_json = parse_qoj_js_literal(score_str)
        return score_json.get(user, {})

    def _parse_submission_list(self, html: str, contest_id: str) -> list[Submission]:
        # 找每行 (含 ID 的) 然后从行内抽 sub-field (避免列序假设)
        rows = RE_SUB_ROW.findall(html)
        subs = []
        for row in rows:
            # 跳过表头 (<th>) 和无 sub ID 的行
            sid_m = RE_SUB_ID.search(row)
            if not sid_m:
                continue
            letter_m = RE_SUB_PROBLEM_LETTER.search(row)
            if not letter_m:
                continue
            verdict_m = RE_SUB_VERDICT.search(row)
            if not verdict_m:
                continue
            verdict = (verdict_m.group(1) or verdict_m.group(2) or "").strip()
            time_m = RE_SUB_TIME_FIELD.search(row)
            time_text = time_m.group(1) if time_m else ""
            user_m = RE_SUB_USER.search(row)
            user = (user_m.group(1) or user_m.group(2) or "") if user_m else ""
            length_m = RE_SUB_LENGTH.search(row)
            length = int(length_m.group(1)) if length_m else None
            lang_m = RE_SUB_LANG.search(row)
            language = lang_m.group(1).strip() if lang_m else None
            # 区分 contest-relative (H:MM:SS) 和 wallclock (YYYY-MM-DD HH:MM:SS)
            contest_secs = None
            submitted_at = ""
            if re.match(r"\d{4}-\d{2}-\d{2}", time_text):
                submitted_at = time_text
            elif time_text:
                contest_secs = parse_contest_time(time_text)
            subs.append(Submission(
                platform="qoj",
                submission_id=sid_m.group(1),
                user=user.strip(),
                problem=letter_m.group(1),
                verdict=verdict,
                submitted_at=submitted_at,
                contest_time_seconds=contest_secs,
                language=language,
                code_length=length,
            ))
        return subs

    def _parse_code(self, html: str) -> tuple[str, str]:
        code_match = RE_CODE_BLOCK.search(html)
        if not code_match:
            raise ParseError("找不到 <pre class=code> 块")
        code = html_unescape(code_match.group(1)).strip()
        lang_match = RE_CODE_LANG.search(html)
        language = lang_match.group(1).strip() if lang_match else ""
        return code, language

    # === 静态工具 ===

    @staticmethod
    def from_cookie_file(path: Path) -> "QojClient":
        text = path.read_text(encoding="utf-8")
        cookies = parse_netscape_cookies(text)
        return QojClient(cookies=cookies)
