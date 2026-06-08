#!/usr/bin/env python3
"""tools/qoj_sync.py — 从 qoj.ac 抓取指定比赛和指定用户的做题情况

写 docs/data/qoj-cache.json：保持已有 contest 条目不变，更新/追加当前 contest。
CI 提交后 main 触发 deploy；浏览器下次加载编辑器时轮询到这个文件的新条目。

数据流（Playwright 真 Chromium 一次跑完）：
  1) /contest/{id}              → 比赛名 + 题目列表（id/letter/title）
  2) /contests                  → 该场比赛的 start_time + duration_hours
  3) /results/QOJ{id}           → standings 表，挑 username 那一行的 12 题状态
                                   '+N HH:MM' = AC，'-' = 试过没 AC，'' = 没做

历史：早先用 /submissions 端点 per-problem 翻页；CF 把那个节点卡得很死，GitHub
Actions IP 段连 Turnstile 都过不去。standings 端点 CF 节点更宽松（2026-06 实测
能过），一次性拿全 12 题，O(1) 请求而不是 O(12) 翻页。

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
# /results/QOJ{cid} 一次性返回 standings 表格（行=用户，列=题，格=+/-/空），
# 比 /submissions 分页高效得多（一个 contest 一次请求即可）。/contest/{cid}/standings
# 在 GH Actions IP 段被 CF 卡住，/results/QOJ{cid} 的 CF 节点目前能过。
STANDINGS_URL = "https://qoj.ac/results/QOJ{cid}"
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
        # CF 中间态：page 还在 verification 阶段（"Verification successful. Waiting for qoj.ac"），
        # 标题既不是 "Just a moment..." 也不是 "checking your browser"，但页面还可能 navigation。
        # "verifying" / "verification successful" / "waiting for" 都视为 CF 等待中。
        cf_pending_markers = (
            "just a moment", "checking your browser",
            "verifying you are human", "verifying",
            "verification successful", "waiting for",
        )
        for attempt in range(_retries + 1):
            self._page.goto(full, wait_until="domcontentloaded", timeout=30000)
            # /submissions 触发的不是普通 JS challenge，是 Cloudflare Turnstile（managed）——
            # 一个 "Verify you are human" 复选框在 iframe 里，CF 不会自动放过。
            # 点一下让它走完 challenge（run 27116033698 之前 180s × 2 一直在等 auto-resolve，没戏）。
            self._solve_turnstile_if_present()
            # CF v5 JS challenge：title "Just a moment..." 或 URL 含 /challenge
            # Python 端轮询 page.title() —— 比 wait_for_function 可靠（CDP eval 偶尔丢）
            deadline = time.time() + _cf_timeout
            while time.time() < deadline:
                try:
                    title = self._page.title().lower()
                except Exception:
                    time.sleep(1)
                    continue
                if not any(m in title for m in cf_pending_markers):
                    # 真页面：等 load_state 避免 content() 报"page is navigating"
                    try:
                        self._page.wait_for_load_state("load", timeout=8000)
                    except Exception:
                        pass
                    try:
                        return _Response(self._page.content())
                    except Exception as e:
                        # content() 在 navigation 中失败：等一下再读
                        print(f"[!] content() 失败（{e}），1s 后重试", file=sys.stderr)
                        time.sleep(1)
                        try:
                            return _Response(self._page.content())
                        except Exception:
                            pass
                        continue
                time.sleep(1)
            # _cf_timeout 还在 CF 挑战页：reload 一次（cf_clearance 可能刚签发，刷新页面会带过去）
            if attempt < _retries:
                print(f"[!] CF {_cf_timeout}s 没解开，reload 重试 ({attempt + 1}/{_retries})...", file=sys.stderr)
                time.sleep(2)
                continue
            # 实在不行：截屏 + dump HTML 方便 debug
            self._dump_debug(full, reason=f"cf_timeout_{_cf_timeout}s")
            return _Response(self._page.content())

    def _solve_turnstile_if_present(self):
        """如果是 Cloudflare Turnstile challenge（不是普通 JS challenge），点掉复选框。

        CF Turnstile 把整个 widget 放在跨域 iframe 里（src 含 challenges.cloudflare.com/turnstile/...），
        复选框 input 在那个 iframe 里，class 包含 'cb-lb'。从主页面 frame 列表找就行。
        点完后等 3s 看 cf-turnstile-response input 有没有被填——填了说明 CF 接受了。
        """
        try:
            # 1) 找 Turnstile response input（CF 总是渲一个 hidden input[name='cf-turnstile-response']）
            # 等一下让它出现（Turnstile JS 是 async defer 加载的，domcontentloaded 时可能还没渲）
            try:
                self._page.locator("input[name='cf-turnstile-response']").wait_for(state="attached", timeout=10000)
            except Exception:
                print("[*] 没有 Turnstile response input（普通 challenge 或没 challenge）", file=sys.stderr)
                return
            print("[*] 检测到 Cloudflare Turnstile challenge", file=sys.stderr)
            resp_input = self._page.locator("input[name='cf-turnstile-response']")
            # 2) 等 Turnstile iframe 出现（最多 15s）
            iframe = None
            for _ in range(30):
                for frame in self._page.frames:
                    if "challenges.cloudflare.com" in (frame.url or "") and "/turnstile/" in (frame.url or ""):
                        iframe = frame
                        break
                if iframe:
                    break
                import time as _t
                _t.sleep(0.5)
            if not iframe:
                print("[!] Turnstile response input 有了但 iframe 没出现（invisible 模式？）", file=sys.stderr)
                return
            print(f"[*] Turnstile iframe 出现：{iframe.url[:80]}", file=sys.stderr)
            # 3) 找 checkbox / label 并点
            for sel in ("input[type='checkbox']", ".cb-lb", "label", "body"):
                try:
                    el = iframe.locator(sel).first
                    if el.count() == 0:
                        continue
                    try:
                        el.wait_for(state="visible", timeout=5000)
                    except Exception:
                        pass
                    el.click(timeout=2000, force=True)
                    print(f"[*] 点 Turnstile {sel} → done", file=sys.stderr)
                    # 给 Turnstile JS 3s 跑验证；看 response input 有没有值
                    import time as _t
                    for _ in range(30):
                        _t.sleep(0.1)
                        val = resp_input.evaluate("el => el.value")
                        if val and len(val) > 20:
                            print(f"[*] Turnstile 已通过（token {len(val)} chars），等页面跳转", file=sys.stderr)
                            return
                    print("[!] 点完 3s Turnstile response 还是空，CF 可能没接受", file=sys.stderr)
                    return
                except Exception as e:
                    print(f"[!] Turnstile 点 {sel} 失败：{e}", file=sys.stderr)
                    continue
            print("[!] Turnstile iframe 找到了但没点到 checkbox", file=sys.stderr)
        except Exception as e:
            print(f"[!] Turnstile solver 异常：{e}", file=sys.stderr)

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
    """保留为占位：早先用来在 CF 解开前用 curl_cffi 跑 /submissions，cf_clearance 的 TLS
    指纹绑定 + CF 服务器端 bot 检测让这条路彻底失败（run 27115687244、27116033698）。
    现在 /results/QOJ{cid} 一个端点搞定，curl_cffi 不再需要。
    """


def _open_session():
    """打开一个 fetcher session（就是 _PlaywrightSession）。失败抛 RuntimeError。"""
    cookie = os.environ.get("QOJ_AUTH_COOKIE", "").strip()
    try:
        return _PlaywrightSession(auth_cookie=cookie or None)
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

    CF 失败时返回 (None, None) 不抛——> /contests 也可能触发 Turnstile，
    让 fetch_user_submissions_for_problem 用全标 Ø 兜底，import 还是能完成。
    """
    try:
        resp = session.get(CONTEST_LIST_URL)
        resp.raise_for_status()
        html = resp.text
        _check_cf(html, CONTEST_LIST_URL)
    except RuntimeError as e:
        msg = str(e)
        if "Cloudflare" in msg or "Turnstile" in msg or "cf_timeout" in msg:
            print(f"[!] /contests CF 解不开（{e}），start_time/duration 拿不到；submission 全标 Ø", file=sys.stderr)
            session._cf_blocked_submissions = True
            return None, None
        raise

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


def fetch_standings_for_user(session, contest_id, username):
    """拉 /results/QOJ{cid}，从 standings 表里挑出 username 那一行的 12 题状态。

    Returns:
      list[{'letter', 'status', 'time'}] —— 12 个元素按字母顺序，status 是 '+N'/'-N'/'+'/'-'/''；
      time 是 'HH:MM'（从比赛开始的偏移），AC 才会有；或者 None（用户不在榜里）。

    Cell 格式（实采 /results/QOJ2564，row 58 = tarjen）：
      '+3 0:08'   → AC，3 次 WA 后通过，赛中 0:08 过题
      '+ 0:52'    → AC，0 次 WA（一发过），赛中 0:52 过题
      '-6 4:59'   → 试过 6 次，最后一次 4:59，没过
      '-'         → 试过，没过
      '' (空)      → 没做

    注意：standings 不告诉具体失败原因（WA / TLE / RE 都统称 '-'）。
    对应到我们的 O/Ø/!/. 表：+ → O/Ø，- → !，空 → .。

    CF 拦了：返回 None 并设 session._cf_blocked_submissions = True。
    登录重定向：抛 RuntimeError（让上层跟 /contest/{id} 一样显式报 cookie 过期）。
    """
    url = STANDINGS_URL.format(cid=contest_id)
    try:
        resp = session.get(url, _cf_timeout=120, _retries=1)
        resp.raise_for_status()
        html = resp.text
        _check_cf(html, url)
    except RuntimeError as e:
        msg = str(e)
        if "Cloudflare" in msg or "Turnstile" in msg or "cf_timeout" in msg:
            print(f"[!] /results CF 解不开（{e}），per-problem 状态拿不到", file=sys.stderr)
            session._cf_blocked_submissions = True
            return None
        raise

    # 跟 fetch_contest_page 一样：登录过期 QOJ 把 standings 也重定向到 /login
    if re.search(r'<title>\s*Login\s*-\s*QOJ\.ac\s*</title>', html, re.IGNORECASE):
        raise RuntimeError(
            f"QOJ 把 {url} 重定向到登录页。Cookie 可能过期/复制错。"
            "QOJ 用的是 uoj_remember_token + uoj_remember_token_check + UOJSESSID 这三件套，"
            "重新去 F12 → Network → Request Headers → Cookie 整行复制（不要只粘一个）。"
        )

    # /results 页 body 是 XHR 拉数据 + JS 渲染；用 page.evaluate 比 regex 解析 400KB HTML
    # 稳得多。Cell 格式（实采）：
    #   <center>+3<br><font size="1">0:08</font></center>   AC, 3 WAs, 0:08
    #   <center>+<br><font size="1">0:52</font></center>    AC, 0 WAs
    #   <center>-<br></center>                              tried, no AC
    #   <center><br></center>                               not tried
    try:
        result = session._page.evaluate("""
            (username) => {
                const rows = document.querySelectorAll('table tr');
                if (!rows.length) return { error: 'no_table' };
                // header: [Rank, Username, A..L, Solved, Penalty, Dirt]
                const ths = rows[0].querySelectorAll('th');
                const letters = [];
                for (let i = 2; i < ths.length - 3; i++) {
                    // 头部 cell 是 "A425/506" 这种：letter + solved/total 计数
                    const txt = ths[i].textContent.trim();
                    letters.push(txt.charAt(0));
                }
                for (const row of rows) {
                    const tds = row.querySelectorAll('td');
                    if (tds.length < 5) continue;
                    // 精确匹配 username（团队名 "Foo (Members)" 不会撞单人 username）
                    if (tds[1].textContent.trim() !== username) continue;
                    const cells = [];
                    for (let i = 2; i < tds.length - 3; i++) {
                        const center = tds[i].querySelector('center');
                        let status = '';
                        let time = '';
                        if (center) {
                            // firstChild 是 '+3' / '+' / '-' / '' (text node) 或 <br>
                            const fc = center.firstChild;
                            if (fc && fc.nodeType === 3) status = fc.textContent.trim();
                            const f = center.querySelector('font');
                            if (f) time = f.textContent.trim();
                        }
                        cells.push({ letter: letters[i-2], status, time });
                    }
                    return { letters, cells };
                }
                return { letters, cells: null };  // user not in standings
            }
        """, username)
    except Exception as e:
        print(f"[!] standings DOM evaluate failed: {e}", file=sys.stderr)
        return None

    if not result or not result.get("cells"):
        return None
    return result["cells"]


def _parse_relative_time(time_str, start_time):
    """把 '0:08' / '1:34:05' (从比赛开始的偏移) 转成绝对时间戳 'YYYY-MM-DD HH:MM:SS'。

    QOJ standings 格式：'H:MM' = hours:minutes（如 '0:08' = 8 分钟，'1:34' = 1h34m）；
    偶尔 'H:MM:SS' = hours:minutes:seconds（实际未见过，留兼容）。
    start_time 缺失或解析失败返回 ''。
    """
    if not time_str or start_time is None:
        return ""
    from datetime import timedelta
    s = time_str.strip()
    parts = s.split(":")
    try:
        if len(parts) == 2:
            hours, minutes = int(parts[0]), int(parts[1])
            offset = timedelta(hours=hours, minutes=minutes)
        elif len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            offset = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        else:
            return ""
        ac_time = start_time + offset
        return ac_time.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return ""


# ---------------- pure logic ----------------

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

        # 2.5) 不再切到 curl_cffi：CF 把 cf_clearance 绑到 TLS 指纹（JA3/JA4），
        # curl_cffi 的 chrome impersonation 跟 Playwright 的 Chromium 不完全一致，
        # CF 直接给 403 + 重新发 challenge，curl_cffi 跑不了 JS 永远解不开（run 27115687244）。
        # 全程 Playwright：/results/QOJ{cid} 一个端点一次性拿 standings 12 题状态，
        # 不再 per-problem 翻 /submissions（CF 严卡那个节点）。

        entry = {
            "name": name,
            "start_time": start_time.isoformat() if start_time else "",
            "duration_hours": int(duration_hours) if duration_hours else 0,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "problems": problems,
            "submissions": {},
        }

        # 3) 用户每题状态（一次性从 standings 拿）
        if not args.no_subs:
            print(f"[*] 拉 {STANDINGS_URL.format(cid=contest_id)}（一次性拿 12 题状态）", file=sys.stderr)
            cells = fetch_standings_for_user(session, contest_id, username)
            if cells is None:
                # CF 拦了：cache entry 加 cf_blocked=true，编辑器知道是拿不到不是没做
                if getattr(session, "_cf_blocked_submissions", False):
                    entry["cf_blocked"] = True
                # cells=None 也可能是用户没打这场比赛（不在榜里）——编辑器按全 . 处理
            else:
                # 用 problem letter 把 standings cell 关联到 problem id
                by_letter = {c["letter"]: c for c in cells}
                entry["submissions"][username] = {}
                for p in problems:
                    cell = by_letter.get(p["letter"])
                    if not cell:
                        continue
                    s = cell["status"]
                    if s.startswith("+"):
                        # AC：从 contest start 加 cell.time 偏移得到绝对时间
                        ac_at = _parse_relative_time(cell["time"], start_time)
                        in_contest = is_during_contest(ac_at, start_time, duration_hours) if ac_at else False
                        entry["submissions"][username][str(p["id"])] = {
                            "status": "AC",
                            "ac_at": ac_at,
                            "in_contest": in_contest,
                            "tried": True,
                        }
                    elif s.startswith("-"):
                        # 试过没过（具体 WA / TLE / RE / ... 不知道 → 一律标 '!'）
                        entry["submissions"][username][str(p["id"])] = {
                            "status": "WA",  # 编辑器 qojStatusToCell 把任何非 AC 都映射到 '!'
                            "ac_at": "",
                            "in_contest": False,
                            "tried": True,
                        }
                    # else: 空 cell → 用户没做，不写 entry（编辑器按 . 处理）
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
