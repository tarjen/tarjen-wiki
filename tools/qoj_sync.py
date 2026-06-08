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

    def __init__(self, auth_cookie=None):
        from playwright.sync_api import sync_playwright
        self._p = sync_playwright().start()
        # 隐身 flags：让 CF 不把它当 headless bot
        # - disable-blink-features=AutomationControlled: 去掉 navigator.webdriver 标志
        # - disable-dev-shm-usage: GitHub Actions runner /dev/shm 小
        self._browser = self._p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        # 每个新页面加载前注入脚本：彻底盖掉 webdriver 痕迹
        self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        # QOJ 要登录才能看比赛页和提交页。从 QOJ_AUTH_COOKIE env 读：
        # - 整段 Cookie header：'a=1; b=2; c=3' → 三对 cookie 全部注入
        # - 单对：'uoj_remember_token=abc'
        # - 裸 value：'abc123' → 用默认名 uoj_remember_token
        # 必须在 new_page() 之前 add_cookies，否则第一次请求不带 cookie，触发 login 重定向
        if auth_cookie:
            pairs = _parse_cookie_kv(auth_cookie)
            # 过滤空 value；Playwright 拒收空字符串
            cookies = [
                {"name": n, "value": v, "domain": "qoj.ac", "path": "/",
                 "secure": True, "sameSite": "Lax"}
                for n, v in pairs if n and v
            ]
            if cookies:
                self._context.add_cookies(cookies)
                summary = ", ".join(f"{n}=<{len(v)} chars>" for n, v in pairs if n and v)
                print(f"[*] 已注入 {len(cookies)} 个 QOJ cookie：{summary}", file=sys.stderr)
            else:
                print("[!] QOJ_AUTH_COOKIE 为空，按匿名访问（比赛页可能 302 → /login）", file=sys.stderr)
        else:
            print("[!] 未提供 QOJ_AUTH_COOKIE，按匿名访问（比赛页可能 302 → /login）", file=sys.stderr)
        self._page = self._context.new_page()

    def get(self, url, params=None, _retries=2, _cf_timeout=120):
        full = url
        if params:
            full = url + ('&' if '?' in url else '?') + urlencode(params)
        # domcontentloaded 比 load 快；CF challenge 也会触发 load 事件但内容是空壳
        import time
        for attempt in range(_retries + 1):
            self._page.goto(full, wait_until="domcontentloaded", timeout=30000)
            # CF v5 JS challenge：title "Just a moment..." 或 URL 含 /challenge
            # Python 端轮询 page.title() —— 比 wait_for_function 可靠（CDP eval 偶尔丢）
            deadline = time.time() + _cf_timeout
            while time.time() < deadline:
                try:
                    title = self._page.title().lower()
                except Exception:
                    time.sleep(1)
                    continue
                if "just a moment" not in title and "checking your browser" not in title:
                    return _Response(self._page.content())
                time.sleep(1)
            # _cf_timeout 还在 CF 挑战页：reload 一次（cf_clearance 可能刚签发，刷新页面会带过去）
            if attempt < _retries:
                print(f"[!] CF {_cf_timeout}s 没解开，reload 重试 ({attempt + 1}/{_retries})...", file=sys.stderr)
                time.sleep(2)
                continue
            # 实在不行：截屏 + dump HTML 方便 debug
            self._dump_debug(full, reason=f"cf_timeout_{_cf_timeout}s")
            return _Response(self._page.content())

    def _dump_debug(self, url, reason=""):
        """CF 60s 没解开时，把截图 + HTML 写到 docs/data/，workflow 会传 artifact。"""
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            stem = re.sub(r"[^a-z0-9]+", "_", url.split("//", 1)[-1])[:50]
            html_path = Path(f"docs/data/qoj-debug-{stem}-{ts}.html")
            png_path = Path(f"docs/data/qoj-debug-{stem}-{ts}.png")
            html_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                html_path.write_text(self._page.content(), encoding="utf-8")
            except Exception as e:
                print(f"[!] dump HTML failed: {e}", file=sys.stderr)
            try:
                self._page.screenshot(path=str(png_path), full_page=True)
            except Exception as e:
                print(f"[!] dump screenshot failed: {e}", file=sys.stderr)
            print(f"[!] {reason} — {url} → {html_path}, {png_path}", file=sys.stderr)
        except Exception as e:
            print(f"[!] _dump_debug failed: {e}", file=sys.stderr)

    def close(self):
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._p.stop()


class _CffiSession:
    """curl_cffi 模拟 Chrome TLS 指纹 + JA3 签名，让 cf_clearance cookie 继续有效。

    Playwright 解开 CF 后，cf_clearance 会被绑到 (IP, UA, TLS 指纹)。
    用 curl_cffi impersonate="chrome120" 伪造同样的 TLS 指纹，cf_clearance 还能用，
    之后每个 /submissions 请求几百 ms 就回来，不用每次都跑 CF JS challenge。

    复用 Playwright 拿到的所有 cookies（uoj_remember_token + uoj_remember_token_check
    + UOJSESSID + cf_clearance），用 User-Agent 保持一致。
    """

    def __init__(self, cookies, user_agent):
        try:
            from curl_cffi import requests as cffi
        except ImportError as e:
            raise RuntimeError(
                "缺少 curl_cffi 依赖。CI workflow 应该 pip install curl-cffi。"
            ) from e
        # 用最新 Chrome 指纹（不指定版本号 → curl_cffi 默认取最新的）。
        # chrome120 是 2023-11 的，CF 参照早就升级了；旧版会被直接 403 而不是给 challenge 页。
        # 如果哪天 "chrome" 报错说不支持，再 pin 死到具体的 chrome131/chrome133。
        self._s = cffi.Session(impersonate="chrome")
        # Playwright context.cookies() 返回 list[dict]，key 是 name/value/domain/path/...
        # domain 加点号让 cf_clearance 跨子域生效；QOJ 全站都在 qoj.ac
        for c in cookies:
            self._s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ".qoj.ac"),
                path=c.get("path", "/"),
            )
        self._s.headers["User-Agent"] = user_agent
        # CF 现在查 Sec-Fetch-* 头——curl_cffi impersonate 模板不一定带这些（运行时头，不是 fingerprint）
        # 浏览器在 document 请求时会发这一组；缺一个就 403
        self._s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
        # 不要走代理；CI 默认不走，我们也不需要
        self._s.proxies = {}

    def get(self, url, params=None):
        full = url
        if params:
            full = url + ('&' if '?' in url else '?') + urlencode(params)
        r = self._s.get(full, timeout=30, allow_redirects=True)
        # 调试：403 时把 body 写出来，方便看 CF 怎么识破的
        if r.status_code >= 400:
            try:
                from pathlib import Path as _P
                dbg = _P(f"docs/data/qoj-debug-cffi-{r.status_code}.html")
                dbg.parent.mkdir(parents=True, exist_ok=True)
                dbg.write_text(r.text[:4000], encoding="utf-8")
                print(f"[!] curl_cffi 收到 {r.status_code}，body 前 200 字符：{r.text[:200]!r}", file=sys.stderr)
                print(f"    debug → {dbg}", file=sys.stderr)
            except Exception as e:
                print(f"[!] dump 403 body failed: {e}", file=sys.stderr)
        return _Response(r.text, status=r.status_code)

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass


class _HybridSession:
    """Playwright 跑前几个请求（解 CF + 拿 cf_clearance）→ 切到 curl_cffi 跑剩下的。

    为什么不全程 Playwright：/submissions 那个 CF 节点比 /contest/2564 严很多，
    Playwright 60s × 3 次都解不开。Playwright + curl_cffi 组合是最稳的：
    - Playwright 拿 cf_clearance（带正确 IP/UA/TLS 指纹）
    - curl_cffi 用同样的 UA + impersonate 同样的 TLS 指纹，cf_clearance 一直有效

    fetch_contest_meta 还在 Playwright 阶段（用 page.evaluate 解析 DOM），
    切到 curl_cffi 之前必须先跑完 fetch_contest_meta。
    """

    def __init__(self, auth_cookie=None):
        self._pw = _PlaywrightSession(auth_cookie=auth_cookie)
        self._cffi = None
        self._use_cffi = False

    @property
    def _page(self):
        """暴露给 fetch_contest_meta 的 page.evaluate 用。切到 curl_cffi 后会抛错。"""
        if self._use_cffi:
            raise RuntimeError(
                "session 已经在 curl_cffi 模式，page.evaluate 不可用；"
                "fetch_contest_meta 必须在 switch_to_cffi() 之前调用"
            )
        return self._pw._page

    def get(self, url, params=None, **_kwargs):
        if self._use_cffi:
            return self._cffi.get(url, params)
        return self._pw.get(url, params, **_kwargs)

    def switch_to_cffi(self):
        """捕获 Playwright 当前的 cookies + UA，构造 _CffiSession 接管后续请求。"""
        if self._use_cffi:
            return
        cookies = self._pw._context.cookies()
        try:
            ua = self._pw._context.user_agent
        except Exception:
            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        self._cffi = _CffiSession(cookies=cookies, user_agent=ua)
        self._use_cffi = True
        cf_present = any(c.get("name") == "cf_clearance" for c in cookies)
        names = [c.get("name") for c in cookies]
        print(
            f"[*] 切换到 curl_cffi：{len(cookies)} cookies "
            f"(cf_clearance={'✓' if cf_present else '✗'}, names={names})",
            file=sys.stderr,
        )

    def close(self):
        try:
            if self._cffi:
                self._cffi.close()
        finally:
            self._pw.close()


def _open_session():
    """打开一个 fetcher session。失败抛 RuntimeError 让 CI 重试更明显。"""
    cookie = os.environ.get("QOJ_AUTH_COOKIE", "").strip()
    try:
        return _HybridSession(auth_cookie=cookie or None)
    except ImportError as e:
        raise RuntimeError(
            "缺少 playwright 依赖。CI workflow 应该 pip install playwright + playwright install chromium。"
        ) from e


def _parse_cookie_kv(raw, default_name="uoj_remember_token"):
    """兼容两种粘法：
    - 整段 Cookie header（多对）：'uoj_remember_token=abc; uoj_remember_token_check=xyz; UOJSESSID=q'
      → 返回 [("uoj_remember_token", "abc"), ("uoj_remember_token_check", "xyz"), ("UOJSESSID", "q")]
    - 单对：'uoj_remember_token=abc' → [("uoj_remember_token", "abc")]
    - 裸 value：'abc123' → [(default_name, "abc123")]  （少见，但容错）
    剥首尾空白；剥 'Cookie:' 前缀（DevTools 复制时偶尔带）。
    """
    s = (raw or "").strip()
    if not s:
        return [(default_name, "")]
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    if ";" in s:
        # 多对：'a=1; b=2; c=3' → [(a,1), (b,2), (c,3)]
        out = []
        for part in s.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            n, _, v = part.partition("=")
            out.append((n.strip(), v.strip()))
        return out
    if "=" in s:
        n, _, v = s.partition("=")
        return [(n.strip(), v.strip())]
    return [(default_name, s)]


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
    if "Just a moment..." in html or "cf-mitigated" in html or "cf_chl_opt" in html:
        raise RuntimeError(
            f"qoj.ac Cloudflare 60s 内没解开 {url}。HTML + 截图已写到 docs/data/qoj-debug-* "
            "（artifact qoj-debug-html 里有）。可能 QOJ 启用了更激进的 CF 策略；"
            "GitHub Actions IP 段可能整体被标记为高风险，试试换 runner IP。"
        )


def fetch_contest_meta(session, contest_id):
    """从 contests 列表页找 (start_time, duration_hours)。

    用 page.evaluate 解析 DOM 而不是 regex：HTML 结构可能因时间/页面状态变，
    但语义稳定。失败时把 HTML 写到 docs/data/qoj-debug-list.html 方便排查。
    """
    resp = session.get(CONTEST_LIST_URL)
    resp.raise_for_status()
    html = resp.text
    _check_cf(html, CONTEST_LIST_URL)

    # 优先：DOM 评估（精确，不受 HTML whitespace/属性顺序影响）
    try:
        result = session._page.evaluate("""
            (cid) => {
                const sel = `a[href="/contest/${cid}"]`;
                const link = document.querySelector(sel);
                if (!link) return null;
                // 往祖先 tr 找，找同行的 timeanddate 和时长
                let row = link.closest('tr');
                if (!row) return { name: link.textContent.trim() };
                const td = row.querySelector('a[href*="timeanddate.com"]');
                let iso = null;
                if (td) {
                    const m = td.href.match(/iso=(\\d{8})T(\\d{4})/);
                    if (m) iso = [m[1], m[2]];
                }
                let duration = null;
                for (const t of row.querySelectorAll('td')) {
                    const s = t.textContent.trim();
                    if (/^\\d+$/.test(s)) { duration = parseInt(s, 10); break; }
                }
                return { name: link.textContent.trim(), iso, duration };
            }
        """, contest_id)
    except Exception as e:
        print(f"[!] DOM evaluate failed: {e}; falling back to regex", file=sys.stderr)
        result = None

    if result and result.get("iso"):
        try:
            start_time = datetime.strptime(
                result["iso"][0] + result["iso"][1], "%Y%m%d%H%M"
            ).replace(tzinfo=timezone.utc)
            return start_time, result.get("duration")
        except (ValueError, IndexError):
            pass

    # 备用：regex（DOM 评估失败或 FakeSession 测试用）
    pattern = (
        r'href="/contest/' + re.escape(contest_id) + r'"[^>]*>([^<]+)</a>'
        r'.*?'
        r'iso=(\d{8})T(\d{4})'
        r'.*?'
        r'<td[^>]*>\s*(\d+)\s*</td>'
    )
    m = re.search(pattern, html, re.DOTALL)
    if m:
        name = m.group(1).strip()
        try:
            start_time = datetime.strptime(
                m.group(2) + m.group(3), "%Y%m%d%H%M"
            ).replace(tzinfo=timezone.utc)
            return start_time, int(m.group(4))
        except ValueError:
            return name, None

    # 调试：写 HTML 到 docs/data/，workflow 会 commit 上去
    try:
        Path("docs/data/qoj-debug-list.html").write_text(html, encoding="utf-8")
        print(f"[!] contest {contest_id} 没在列表页找到；HTML 已写到 docs/data/qoj-debug-list.html", file=sys.stderr)
    except Exception:
        pass
    return None, None


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
    # QOJ 未登录会把 /contest/{id} 重定向到 /login（页面 title 变成 "Login - QOJ.ac"）
    if re.search(r'<title>\s*Login\s*-\s*QOJ\.ac\s*</title>', html, re.IGNORECASE):
        raise RuntimeError(
            f"QOJ 把 {CONTEST_PAGE_URL.format(cid=contest_id)} 重定向到登录页。"
            "Cookie 可能过期/复制错。QOJ 用的是 uoj_remember_token + uoj_remember_token_check + UOJSESSID 这三件套，"
            "重新去 F12 → Network → Request Headers → Cookie 整行复制（不要只粘一个）。"
        )
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
    # /submissions 那个 CF 节点比 /contest/2564 严很多（60s × 3 = 3 min 解不开，run 27092774087），
    # 给 180s × 2 = 6 min 一次；第一次拿到 cf_clearance 后剩下的题目就快了。
    # 总预算：1 × 6 min + 12 × 30s ≈ 12 min（13 道题 + 翻页），在 workflow 15 min 之内。
    _submissions_cf_timeout = 180
    _submissions_retries = 2
    while True:
        resp = session.get(
            SUBMISSIONS_URL,
            params={
                "submitter": username,
                "problem_id": problem_id,
                "page": page,
            },
            _cf_timeout=_submissions_cf_timeout,
            _retries=_submissions_retries,
        )
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
    parser.add_argument("--skip-list", action="store_true", help="跳过 contests 列表（不拿 start_time/duration）")
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
        # 1) 比赛名 + 题目（直接进比赛页；CF re-validation 概率低）
        url = CONTEST_PAGE_URL.format(cid=contest_id)
        print(f"[*] 拉 {url}", file=sys.stderr)
        name, problems = fetch_contest_page(session, contest_id)
        print(f"    name={name!r}  problems={len(problems)}", file=sys.stderr)
        if not problems:
            print(f"[!] 比赛 {contest_id} 一道题都没拿到", file=sys.stderr)
            sys.exit(1)

        # 2) 比赛元信息（start_time + duration → "赛中 AC" 判定用）
        # 不强求：拿不到就让用户填日期；submission 全标 Ø
        start_time, duration_hours = (None, None)
        if not args.skip_list:
            print(f"[*] 拉 {CONTEST_LIST_URL}（拿 start_time + duration）", file=sys.stderr)
            start_time, duration_hours = fetch_contest_meta(session, contest_id)
            if start_time is None:
                print(f"[!] 没拿到 start_time；submission 没法判断'赛中/赛后'，全标 Ø", file=sys.stderr)
            else:
                print(f"    start={start_time.isoformat()}  duration={duration_hours}h", file=sys.stderr)

        # 2.5) Playwright 已经解过 CF 了（至少 /contest/2564 一次 + /contests 一次），
        # cf_clearance 已经在 context cookies 里。切到 curl_cffi 跑剩下的 /submissions：
        # - /submissions 那个 CF 节点比 /contest/2564 严很多，Playwright 60s × 3 都解不开
        # - curl_cffi 用同样的 UA + impersonate="chrome120" → 同样的 TLS 指纹，cf_clearance 继续有效
        # - curl_cffi 一次请求 ~300ms vs Playwright 跑 CF JS challenge ~5-15s
        # 注意：必须在 fetch_contest_meta 之后切（fetch_contest_meta 还在用 page.evaluate）
        session.switch_to_cffi()

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
