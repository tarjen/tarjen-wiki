#!/usr/bin/env python3
"""tools/qoj_sync.py — 从 qoj.ac 抓取指定比赛和指定用户的做题情况

写 docs/data/qoj-cache.json：保持已有 contest 条目不变，更新/追加当前 contest。
CI 提交后 main 触发 deploy；浏览器下次加载编辑器时轮询到这个文件的新条目。

CF 拦截：qoj.ac 走 Cloudflare v5 managed JS challenge。curl_cffi 这种"指纹+TLS 伪造"被 403。
用 Playwright 真 Chromium 跑 JS 验证 → CF 当正常浏览器放行。

运行：
    python3 tools/qoj_sync.py 2564 tarjen
    # 或
    CONTEST_ID=2564 USERNAME=tarjen python3 tools/qoj_sync.py
    # 只看 JSON，不写文件：
    python3 tools/qoj_sync.py 2564 tarjen --dry-run
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

CONTEST_LIST_URL = "https://qoj.ac/contests"
CONTEST_PAGE_URL = "https://qoj.ac/contest/{cid}"
SUBMISSIONS_URL = "https://qoj.ac/submissions"
CACHE_PATH = Path("docs/data/qoj-cache.json")


# ---------------- Session abstraction ----------------
# 真实运行时：Playwright 真浏览器
# 单元测试时：FakeSession（看 tests/test_qoj_sync.py）
# 两个都实现同一个接口：.get(url, params=None) → response-like（有 .text, .raise_for_status()）

class _Response:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _PlaywrightSession:
    """headless Chromium 一次启动，跨多次 get 复用。闭包管理生命周期。"""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        self._browser = self._p.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        self._page = self._context.new_page()

    def get(self, url, params=None):
        full = url
        if params:
            full = url + ('&' if '?' in url else '?') + urlencode(params)
        self._page.goto(full, wait_until="domcontentloaded", timeout=30000)
        # CF JS 挑战：title 含 "Just a moment" 时等 networkidle（JS 跑完 CF 验证）
        # 实测 5–10s 解决；给 30s 余量
        if "Just a moment" in self._page.title():
            self._page.wait_for_load_state("networkidle", timeout=30000)
        return _Response(self._page.content())

    def close(self):
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._p.stop()


def _open_session():
    """打开一个 fetcher session。失败抛 RuntimeError 让 CI 重试更明显。"""
    try:
        return _PlaywrightSession()
    except ImportError as e:
        raise RuntimeError(
            "缺少 playwright 依赖。CI workflow 应该 pip install playwright + playwright install chromium。"
        ) from e


# UOJ 在 <a class="small">RESULT_ERROR</a> 里写的原文
# Source: judger/uoj_judger/include/uoj_run.h:169-189
STATUS_TO_CODE = {
    "Accepted": "AC",
    "Wrong Answer": "WA",
    "Runtime Error": "RE",
    "Time Limit Exceeded": "TLE",
    "Memory Limit Exceeded": "MLE",
    "Output Limit Exceeded": "OLE",
    "Compile Error": "CE",
    "Judgment Failed": "SE",
    "Dangerous Syscalls": "DGS",
    "Unknown Result": "??",
}


# ---------------- HTTP helpers ----------------

def _check_cf(html, url):
    """检测 Cloudflare 挑战页：如果 Playwright 拿到的是 CF 验证页（没解开），让用户看到清晰错误。"""
    # CF "Just a moment..." 标题在 _PlaywrightSession 已经等 networkidle 解决了
    # 但如果 <title> 还在 / 或页面 body 还是 cf-mitigated 标记，说明 30s 没解完
    if "Just a moment..." in html or "cf-mitigated" in html or "cf_chl_opt" in html:
        raise RuntimeError(
            f"qoj.ac Cloudflare 30s 内没解开 {url}。可能 QOJ 启用了更激进的 CF 策略；"
            "试试加 --headed 看截图，或换住宅 IP。"
        )


def fetch_contest_meta(session, contest_id):
    """从 contests 列表页找 (start_time, duration_hours)。

    列表页一行（实测）：
    <a href="/contest/2564" ...>Name</a>
    <a href="https://www.timeanddate.com/worldclock/.../?iso=20251130T0930" target="_blank">2025-11-30 09:30</a>
    <td>5</td>   ← 持续小时
    """
    resp = session.get(CONTEST_LIST_URL)
    resp.raise_for_status()
    html = resp.text
    _check_cf(html, CONTEST_LIST_URL)
    pattern = (
        r'href="/contest/' + re.escape(contest_id) + r'"[^>]*>([^<]+)</a>'
        r'.*?'
        r'iso=(\d{8})T(\d{4})'
        r'.*?'
        r'<td[^>]*>(\d+)</td>'
    )
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return None, None
    name = m.group(1).strip()
    iso_date, iso_time = m.group(2), m.group(3)
    try:
        start_time = datetime.strptime(iso_date + iso_time, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return name, None
    duration_hours = int(m.group(4))
    return start_time, duration_hours


def fetch_contest_page(session, contest_id):
    """拿比赛名（h1）和题目列表（dashboard 第一个 table）。

    题目行：
    <td>A</td>
    <td><a href="/contest/{cid}/problem/{pid}">#{pid}. Title</a></td>
    """
    resp = session.get(CONTEST_PAGE_URL.format(cid=contest_id))
    resp.raise_for_status()
    html = resp.text
    _check_cf(html, CONTEST_PAGE_URL.format(cid=contest_id))
    name_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    name = name_m.group(1).strip() if name_m else f"Contest {contest_id}"
    problems = []
    for m in re.finditer(
        r'<a\s+href="/contest/' + re.escape(contest_id) + r'/problem/(\d+)"\s*>([^<]+)</a>',
        html,
    ):
        pid = int(m.group(1))
        title = m.group(2).strip()
        letter = chr(ord('A') + len(problems))
        problems.append({"id": pid, "letter": letter, "title": title})
    return name, problems


def fetch_user_submissions_for_problem(session, username, problem_id):
    """拉指定 user+problem 的全部 submissions（分页）。

    返回 [(status_code, "YYYY-MM-DD HH:MM:SS"), ...]，按 QOJ 默认顺序（最新在前）。
    状态判定：
    - <a class="uoj-score">N</a> → 数字得分；100 = AC，其它记 S{N}
    - <a class="small">TEXT</a> → 状态文字，映射到 STATUS_TO_CODE
    """
    out = []
    page = 1
    while True:
        resp = session.get(SUBMISSIONS_URL, params={
            "submitter": username,
            "problem_id": problem_id,
            "page": page,
        })
        resp.raise_for_status()
        html = resp.text
        _check_cf(html, SUBMISSIONS_URL)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        body_rows = [r for r in rows if '<th' not in r]
        if not body_rows:
            break
        page_has_data = False
        for row in body_rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 9:
                continue
            result_html = cells[3]
            time_html = cells[8]
            score_m = re.search(r'<a class="uoj-score">(\d+)</a>', result_html)
            small_m = re.search(r'<a class="small">([^<]+)</a>', result_html)
            if score_m:
                score = int(score_m.group(1))
                status = "AC" if score == 100 else f"S{score}"
            elif small_m:
                raw = small_m.group(1).strip()
                status = STATUS_TO_CODE.get(raw, "??")
            else:
                continue
            time_m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', time_html)
            t = time_m.group(1) if time_m else ""
            out.append((status, t))
            page_has_data = True
        if not page_has_data or len(body_rows) < 10:
            break
        page += 1
        if page > 50:
            print(f"[!] problem_id={problem_id} 翻了 50 页，截断", file=sys.stderr)
            break
    return out


# ---------------- pure logic ----------------

def best_status(subs):
    """从一组 (status, time) 里挑最优。返回 ("AC", earliest_ac_time) 或 (first_error, "") 或 None。"""
    if not subs:
        return None
    acs = [t for s, t in subs if s == "AC"]
    if acs:
        return ("AC", min(acs))
    return (subs[0][0], "")


def is_during_contest(ac_time_str, start_time, duration_hours):
    """判断 ac_time_str 是否在 [start_time, start_time + duration_hours] 区间内。"""
    if not ac_time_str or start_time is None or not duration_hours:
        return False
    try:
        ac_time = datetime.strptime(ac_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    start_ts = start_time.timestamp()
    end_ts = start_ts + int(duration_hours) * 3600
    ac_ts = ac_time.replace(tzinfo=timezone.utc).timestamp()
    return start_ts <= ac_ts <= end_ts


# ---------------- cache I/O ----------------

def load_cache(path=CACHE_PATH):
    if isinstance(path, str):
        path = Path(path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "updated_at": "", "contests": {}}


def save_cache(cache, path=CACHE_PATH):
    if isinstance(path, str):
        path = Path(path)
    cache["version"] = cache.get("version", 1)
    cache["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache.setdefault("contests", {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------- main ----------------

def main():
    parser = argparse.ArgumentParser(description="QOJ 比赛/用户数据抓取 → docs/data/qoj-cache.json")
    parser.add_argument("contest_id", nargs="?", help="QOJ contest id (e.g. 2564)")
    parser.add_argument("username", nargs="?", help="QOJ username (e.g. tarjen)")
    parser.add_argument("--dry-run", action="store_true", help="只打印抓到的 JSON，不写文件")
    parser.add_argument("--no-subs", action="store_true", help="只抓比赛元信息，不抓 submissions")
    args = parser.parse_args()

    contest_id = (args.contest_id or os.environ.get("CONTEST_ID", "")).strip()
    username = (args.username or os.environ.get("USERNAME", "")).strip()
    if not contest_id or not username:
        print("用法: python3 tools/qoj_sync.py <contest_id> <username>", file=sys.stderr)
        print("     或 CONTEST_ID=... USERNAME=... python3 tools/qoj_sync.py", file=sys.stderr)
        sys.exit(2)

    print(f"[*] 抓取 contest={contest_id} user={username}", file=sys.stderr)
    session = _open_session()
    try:
        # 1) 比赛元信息
        print(f"[*] 拉 {CONTEST_LIST_URL}", file=sys.stderr)
        start_time, duration_hours = fetch_contest_meta(session, contest_id)
        if start_time is None:
            print(f"[!] 列表页没找到 contest {contest_id}（CF 没解开？或 id 错？）", file=sys.stderr)
        else:
            print(f"    start={start_time.isoformat()}  duration={duration_hours}h", file=sys.stderr)

        # 2) 比赛名 + 题目
        url = CONTEST_PAGE_URL.format(cid=contest_id)
        print(f"[*] 拉 {url}", file=sys.stderr)
        name, problems = fetch_contest_page(session, contest_id)
        print(f"    name={name!r}  problems={len(problems)}", file=sys.stderr)
        if not problems:
            print(f"[!] 比赛 {contest_id} 一道题都没拿到", file=sys.stderr)
            sys.exit(1)

        entry = {
            "name": name,
            "start_time": start_time.isoformat() if start_time else "",
            "duration_hours": int(duration_hours) if duration_hours else 0,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "problems": problems,
            "submissions": {},
        }

        # 3) 用户每题状态
        if not args.no_subs:
            entry["submissions"][username] = {}
            for p in problems:
                print(f"[*] 拉 submissions problem_id={p['id']} ({p['letter']})", file=sys.stderr)
                subs = fetch_user_submissions_for_problem(session, username, p["id"])
                if not subs:
                    continue
                status, ac_at = best_status(subs)
                in_contest = is_during_contest(ac_at, start_time, duration_hours) if status == "AC" else False
                entry["submissions"][username][str(p["id"])] = {
                    "status": status,
                    "ac_at": ac_at,
                    "in_contest": in_contest,
                    "tried": True,
                }
    finally:
        session.close()

    # 4) 写 cache
    if args.dry_run:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return
    cache = load_cache()
    cache["contests"][contest_id] = entry
    save_cache(cache)
    print(f"[✓] 写入 {CACHE_PATH}（contest {contest_id}，user {username}）", file=sys.stderr)


if __name__ == "__main__":
    main()
