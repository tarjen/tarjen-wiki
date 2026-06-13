# Wiki Backend — API

> **v1.x 重构后**：CLI 直接调用本地模块，无 HTTP server，无数据库。
>
> 327 tests passing (含 16 个真实 QOJ fixture 测试)。

## 目录

1. **CLI API** — 终端命令 (用户视角)
2. **Python API** — 模块 import (开发者视角)
3. 架构 / 数据流 / 环境变量
4. 实现细节 (各模块关键设计)
5. 每日工作流
6. 错误处理
7. 测试覆盖
8. 安装 & 升级
9. 已知限制 / TODO
10. 调试技巧

---

# 1. CLI API

> **上手**：`<no flag>` 几乎都走交互式 prompt。`--dry-run` 永远只预览不写。
> 所有命令都遵守 `wiki <command> --help`。

## 1.1 健康检查

```bash
wiki doctor [-v]
```

输出：
```
✓ 数据 store 加载完成
  仓库: /home/tarjen/wiki
  比赛: 23
  watchlist: 2 人
  git: main clean, ahead=0
  QOJ cookie: ~/.config/wiki/cookies/qoj.txt (240 bytes)
```
`-v` 还会显示 CSV 警告 (字段缺失, 行号等)。

## 1.2 浏览

```bash
wiki list [--since YYYY-MM-DD] [--until YYYY-MM-DD] \
          [--tag X] (可重复) \
          [--solved-min N] [--sort date|solved|rate|total] \
          [--order asc|desc] [--limit N] [--json]

wiki show <slug> [--body/--no-body]   # 默认含 body
```

## 1.3 CRUD — 手工 & 改

### 1.3.1 手动新增 (无 cookie, 无网络, **上手就能用**)

```bash
# 交互模式 — 零 flag, 一路回车就行
wiki add

# 全 flag 模式 — 一次给齐, 适合脚本
wiki add --slug X --name Y --date Z --total N --problems "O;O;.;" \
         [--tags "#icpc #regional"] [--link URL] [--body "..."] [-y]
```

交互模式会问: `slug → name → date → total → (逐题 O/Ø/!/.) → link`。
`problems` 格式: 分号分隔, `O`=赛中过, `Ø`=补题过, `!`=试过没过, `.`=未提交。
**不需要 QOJ cookie, 不需要联网**, 适合手填老比赛 / CF / AtCoder。

### 1.3.2 改字段

```bash
wiki set <slug> [--name X] [--date Z] [--total N] [--problems "O;O;."] \
                [--tags "#x #y"] [--link URL] \
                [--status A=O] (可重复, 按位置改) \
                [-y]
```

### 1.3.3 删

```bash
wiki rm <slug> [--keep-body] [-y]
```

### 1.3.4 编辑 md (开 $EDITOR)

```bash
wiki edit <slug>
```

## 1.4 QOJ 导入（核心日常）

```bash
# 比赛日: 从 standings 抓当前状态
wiki update <cid> [--platform X] [--user NAME] [--slug X] [--date YYYY.M.D] \
                 [--dry-run] [-y]

# 几天后: 从 submissions 检测补题 (变 .→Ø 或 !→Ø)
wiki upsolve <cid_or_slug> [--platform X] [--user NAME] [--since ISO] \
                   [--dry-run] [-y]
```

| 命令 | 数据源 | 用途 |
|---|---|---|
| `update` | `/contest/<cid>/standings` (JS 数组) | 赛中结果, 一次性写 |
| `upsolve` | `/contest/<cid>/submissions?user=X` (HTML) | 赛后补题检测 |

行为：
- `update`: 把 standings 映射为 O/!/.，自动 create_new 或 update_existing
- `upsolve`: 把比赛后新 AC 的题从 ./! 升级为 Ø
- 默认每次有 Y/n 确认
- `--yes` / `-y` 跳过确认
- `--dry-run` 只预览不写
- `--user X` 覆盖 config 里的默认 user（多账号场景）
- `--date X` 覆盖 CSV 的 date 字段 (QOJ 不公开 start_time 时手动设)

## 1.5 代码抓取

```bash
wiki codes <cid> [--platform X] [--user NAME] \
              [--only-mine] [--only-watchlist] [--no-watchlist] \
              [--sample N] (默认 1, others 每题抽几个) \
              [--problem A,B,C] [--status AC|WA|ALL] [--refresh] [-y]

wiki codes-list <cid> [--problem A] [--user X] [--source mine|watchlist|sample|other]

wiki codes-show <cid> <user> <problem>   # 打开 (less)
```

策略（默认）：
- 自己：全部 verdict（含 WA/TLE，复盘用）
- watchlist 用户：所有 AC
- 其他人：每题最早 AC 的前 N 个

## 1.6 Cookie / Watchlist

```bash
# Cookie
wiki cookies import <file>          # Netscape jar 文件
wiki cookies set <platform>         # 交互式输入 3 个值 (隐藏)
wiki cookies status [--platform X]

# Watchlist
wiki watchlist list
wiki watchlist add <user>...       # 空格分隔多个
wiki watchlist remove <user>...
```

## 1.7 其他

```bash
wiki sync                            # 跑 tools/sync.py (重建 docs/index.md + data/contests.json)
wiki serve                           # mkdocs preview (前端静态页, 非后端)
```

---

# 2. Python API

CLI 直接 import 这些，所以它们是事实上的"API"。

## 2.1 `csv_store.CsvStore` — CSV 持久层

```python
from csv_store import CsvStore, Contest

store = CsvStore(Path("contests.csv"))
store.load()                                    # 加载所有

# 查询
store.all()                                     # list[Contest], 按日期倒序
store.get(slug)                                 # Contest | None
store.exists(slug)                              # bool
len(store)                                      # int

# 修改
store.add(contest)                              # 写内存
store.update(slug, name=..., problems=[...])    # 改字段
store.delete(slug)                              # 删
store.save()                                    # 原子写盘
```

## 2.2 `md_store.MdStore` — 详情页 md 持久层

```python
from md_store import MdStore

md = MdStore(Path("docs/contests"))
md.exists(slug)                                 # bool
md.read(slug)                                   # str
md.write(slug, content)                         # 原子写
md.delete(slug)                                 # bool
md.placeholder(contest)                         # str (默认模板)
```

## 2.3 `git_ops.GitOps` — git 包装

```python
from git_ops import GitOps, GitPushError

git = GitOps(Path("~/wiki"))
git.status()                                    # RepoStatus (clean, ahead, behind, ...)
git.add(["contests.csv"])
git.commit("add(xxx)")                          # sha
git.push()                                      # 可能抛 GitPushError
git.commit_and_push("msg", ["paths"])           # (sha, pushed) 或抛 GitPushError
git.pull()                                      # 本地脏时抛 GitConflictError
```

## 2.4 `import_logic` — QOJ 导入 (preview + apply)

```python
from import_logic import (
    build_update_preview, apply_update,
    build_upsolve_preview, apply_upsolve,
    UpdatePreview, UpsolvePreview, ApplyResult,
)

# 比赛日
preview = build_update_preview(
    platform="qoj", contest_id="2564", user="tarjen",
    csv_store=csv, config_dir=cfg, slug_override=None,
)
result = apply_update(
    preview=preview, csv_store=csv, md_store=md, git_ops=git,
    overrides={}, create_body=True, run_sync=True, push=True,
)
# result: ApplyResult(slug, record_state, csv_written, body_written,
#                    committed, commit_sha, pushed, problems_before, problems_after)

# 几天后补题
preview = build_upsolve_preview(
    platform="qoj", contest_id="2564", slug=None, user="tarjen",
    csv_store=csv, config_dir=cfg, since_override=None,
)
result = apply_upsolve(
    preview=preview, csv_store=csv, md_store=md, git_ops=git,
)
```

## 2.5 `platforms.qoj.QojClient` — QOJ (qoj.ac, UOJ fork)

```python
from platforms.qoj import QojClient

client = QojClient(cookies={"uoj_remember_token": "...", ...})
client.cookies_valid()                          # bool
client.get_contest_meta("2564")                 # ContestMeta
client.get_user_standings("2564", "tarjen")     # dict[letter, StandingsEntry]
client.get_user_submissions("2564", "tarjen")   # list[Submission]
client.get_submission_code("12345")             # (code: str, language: str)
```

注: 工厂 `from platforms import get_client_class` 按 platform 名分发, 未来加 CF/AtCoder 不变 API。

## 2.6 `codes_logic` — 代码抓取编排

```python
from codes_logic import FetchRequest, fetch_codes

req = FetchRequest(
    platform="qoj", cid="2564", username="tarjen",
    fetch_self=True, fetch_watchlist=True,
    fetch_others="top_n_fastest", others_n=1,
    problems=None, skip_existing=True, request_interval=1.5,
)

result = fetch_codes(req, platform_factory, codes_store, watchlist)
# result.fetched, result.skipped_existing, result.errors, result.files, ...
```

## 2.7 `watchlist.Watchlist` — 关注列表

```python
wl = Watchlist(Path("~/.config/wiki/watchlist.txt"))
wl.load()
wl.users()                                      # list[str]
wl.contains("alice")                            # bool
wl.add(["carol"])
wl.remove(["alice"])
wl.save()
```

---

# 3. 架构 / 数据流 / 环境

## 3.1 数据流

```
$ wiki update 2564
  ↓
bin/wiki (wrapper, exec venv python)
  ↓
tools/cli_main.py (click)
  ↓ 直接 import
tools/csv_store.py      ← contests.csv (本地)
tools/md_store.py       ← docs/contests/<slug>.md (本地)
tools/git_ops.py        ← git commit/push (本地仓库 + 网络 push)
tools/import_logic.py   ← 业务逻辑
tools/platforms/qoj.py  ← 抓 QOJ (唯一网络: qoj.ac)
tools/watchlist.py      ← ~/.config/wiki/watchlist.txt
tools/codes_store.py    ← ~/.local/share/wiki/codes/ (gitignored)
  ↓
contests.csv + docs/contests/*.md + git push → GitHub → GH Pages
```

**唯一网络出口**：
1. `platforms.qoj._fetch` 抓 qoj.ac（CF 信任家庭 IP）
2. `git_ops.push` 推 GitHub

**全部本地**：
- CSV 读写
- md 读写
- git commit / status / log
- watchlist / codes 缓存

## 3.2 启动

```bash
# Mac/Linux: bin/wiki (bash)
wiki doctor

# Windows cmd / PowerShell:
bin\wiki.cmd doctor
# 或 PowerShell:
& .\bin\wiki.ps1 doctor

# mkdocs preview (前端静态页, 可选, 跨平台):
wiki serve
```

## 3.3 平台支持

| 平台 | 状态 | 启动方式 |
|---|---|---|
| macOS | ✅ 已测 | `wiki xxx` (bin/wiki bash) |
| Linux | ✅ CI 测 | `wiki xxx` |
| Windows (Git Bash) | ✅ 应该可以 | `wiki xxx` (同 bash wrapper) |
| Windows (cmd) | ✅ | `bin\wiki.cmd xxx` |
| Windows (PowerShell) | ✅ | `& .\bin\wiki.ps1 xxx` |

Python 部分完全跨平台（pathlib / subprocess / urllib 都 OK）。
Python 代码本身在 macOS / Linux / Windows 都跑同一套，差异只在 wrapper。

## 3.4 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPO_PATH` | `cwd` | wiki git 仓库路径 |
| `CONFIG_DIR` | `~/.config/wiki` | cookie / watchlist / config.json |
| `CODES_DIR` | `~/.local/share/wiki/codes` | 代码缓存 (gitignored) |
| `EDITOR` | `vi` | `wiki edit` 用 |

## 3.5 文件位置

```
~/wiki/                                    # git 仓库 (REPO_PATH)
├── contests.csv                           # 唯一数据源
└── docs/contests/<slug>.md                # 手写详情页

~/.config/wiki/
├── config.json                            # { "default_user": {"qoj": "tarjen"} }
├── cookies/qoj.txt                        # Netscape cookie jar
└── watchlist.txt                          # 一行一个用户

~/.local/share/wiki/
├── codes/<cid>/<user>/<prob>.<ext>        # QOJ 抓的代码 (gitignored)
└── wiki.log                               # 后端日志 (现在没用, 留作历史)
```

---

# 4. 实现细节

## 4.1 QOJ standings JS parser

**为啥不解析 submissions HTML?** 提交页 HTML 列序不稳 (旧版和新版差一列), 一改版就废. Standings 页是 JS 渲染, 但 JS 数据 inline 在 `<script>` 里, 是结构化的:

```js
standings_version=2;
standings=[[solved, penalty, [user_obj], rank, pct], ...];
fullscore=1100;
score={"<user>": {"<pid>": [score, time_sec, sub_id, failed_before, ...]}, ...};
problems=[pid_list];
```

`pid` 是 0-indexed 字母序 (0=A, 1=B, ..., 8=I, ...). 用 contests 页拿到 `letters = [A, B, C, ...]` 后映射.

JS → JSON 转换 (`parse_qoj_js_literal`):
- 唯一非 JSON 部分是 `$DEFAULT_DAT_PREFIX_N` 这种未引号标识符
- 用 `(?<!\w)score=` 锚定避免匹到 `fullscore=...`
- regex 把孤立的 identifier 加引号 (`\$?[\w$]+` 模式)

StandingsEntry 数据:
```python
@dataclass
class StandingsEntry:
    platform: str
    problem_id: str    # "0".."10"
    letter: str        # "A".."K"
    score: int         # 0-100
    contest_time_seconds: int
    submission_id: str | None
    failed_attempts: int
    verdict: str       # "AC" / "WA"
```

## 4.2 QOJ submissions row parser

提交列表行结构不稳 (列序有变), 用"行内 sub-field 提取"代替固定列序:

```python
RE_SUB_ROW = re.compile(r'<tr[^>]*>(?P<row>.*?)</tr>', re.DOTALL)
RE_SUB_ID = re.compile(r'href="/submission/(\d+)"')
RE_SUB_PROBLEM_LETTER = re.compile(r'href="/contest/\d+/problem/[A-Z0-9]+"[^>]*>\s*([A-Z])')
RE_SUB_VERDICT = re.compile(r'class="uoj-score"[^>]*>\s*([A-Za-z]+)\b|<td[^>]*>\s*(AC|WA|TL|RE|ML|CE|SE)\s*</td>')
RE_SUB_TIME_FIELD = re.compile(r'<td[^>]*>\s*(?:<small>)?\s*(\d{1,2}:\d{1,2}:\d{2}|...)\s*</td>')
RE_SUB_USER = re.compile(r'class="uoj-username"[^>]*>([^<]+)</span>|<a[^>]*href="/user/profile/[^"]+"[^>]*>([^<]+)</a>')
```

行内分别抓 ID/problem/verdict/user/time, 鲁棒于列序变化.

## 4.3 cookie 处理 (3 个)

QOJ 用 UOJ 风格:
- `uoj_remember_token` (long-lived)
- `uoj_remember_token_checksum` (HMAC 校验)
- `UOJSESSID` (session)

3 个缺一不可, 失效时 `401/403` 或返回登录页 (`action="/login"`/`请先登录`).

Netscape 格式:
```
.qoj.ac  TRUE  /  FALSE  1924958400  uoj_remember_token  <value>
```

## 4.4 Cloudflare bypass

`urllib` 直连 qoj.ac 会撞 CF JS challenge (403). 用 `cloudscraper` (Chromium fingerprint) 绕过.
`requirements.txt`: `cloudscraper==1.2.71`.

## 4.5 git ops

`commit_and_push` 错误处理:
- 本地脏 → `GitConflictError` (让用户先手动处理)
- 提交成功 + push 失败 → 返回 `(sha, False)`, **不抛** (commit 落本地最重要, push 之后重试)
- CLI 层根据 `(sha, pushed)` 提示用户

`commit` 用 `git commit -m msg -- paths` (只 add 特定文件), 避免误把 docs/editor/ 等加进去.

## 4.6 原子写

`csv_store.save()` 和 `md_store.write()` 都用 `tmp + rename`:
```python
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(content)
os.replace(tmp, path)  # 原子 (POSIX), Windows 上 replace 也原子
```
进程意外 kill 不会留半截文件.

## 4.7 加新 OJ（CF / AtCoder / ...）

1. 写 `tools/platforms/codeforces.py`（继承 `PlatformClient`, 实现 4 个 abstractmethod）
2. 在 `tools/platforms/__init__.py` 加 `@register` 装饰器
3. 写 `tests/platforms/test_codeforces.py` + fixtures
4. ~200 行代码

**API / CLI 完全不变**, 只多一个 `--platform cf` 选项.

---

# 5. 每日工作流（已用集成测试验证）

```
                    ┌──────────────────────┐
                    │ 打完比赛 (当天晚上)  │
                    └──────────┬───────────┘
                               │
                               ▼
                    $ wiki update 2564
                               │
              ┌────────────────┼────────────────┐
              │ 预览: A O ! ...                  │
              │ [Y/n] y                          │
              └────────────────┬────────────────┘
                               │
                               ▼
            ┌─────────────────────────────┐
            │ 写入 contests.csv             │
            │ 创建 docs/contests/xxx.md    │
            │ tools/sync.py (更新 index)    │
            │ git commit + push             │
            │ GH Actions → GH Pages         │
            └─────────────────────────────┘

                  几天/几周后 (补完题)
                               │
                               ▼
                    $ wiki upsolve 2564
                               │
              ┌────────────────┼────────────────┐
              │ 预览: C ! -> Ø (补过)         │
              │ [Y/n] y                        │
              └────────────────┬────────────────┘
                               │
                               ▼
            ┌─────────────────────────────┐
            │ contests.csv 更新             │
            │ git commit + push             │
            └─────────────────────────────┘
```

---

# 6. 错误处理

## 6.1 CLI 命令失败

- 退出码非 0
- 错误信息打到 stderr（Click 默认行为）
- 看日志：`~/.local/share/wiki/wiki.log`（如果用了 wrapper）

## 6.2 常见错误及修法

| 错误 | 原因 | 修法 |
|---|---|---|
| `✗ slug 不存在: X` | CSV 里没这条 | `wiki list` 找对的 slug |
| `✗ cookie 未配置` | `~/.config/wiki/cookies/qoj.txt` 缺失 | `wiki cookies import ~/Downloads/qoj.txt` |
| `✗ cookie_missing_for_platform: qoj` | 同上 | 同上 |
| `✗ QOJ cookie 失效` | cookie 过期（7-30 天） | 重新从浏览器导出 + import |
| `✗ CF challenge detected` | qoj.ac 把请求当 bot | 等几分钟，或换家庭 IP |
| `✗ QOJ contest not found` | cid 错 | 检查 contest ID |
| `✗ git push 失败` | 没配 remote / 网络 / 权限 | `git remote -v` 检查 |
| `⚠ push 失败但 commit 成功` | commit OK, push 网络问题 | 之后手动 `git push` |

---

# 7. 测试覆盖（327 tests）

| 模块 | 测试文件 | 数量 |
|---|---|---|
| csv_store | test_csv_store.py | 66 |
| md_store | test_md_store.py | 15 |
| git_ops | test_git_ops.py | 18 |
| watchlist | test_watchlist.py | 26 |
| codes_store | test_codes_store.py | 30 |
| platforms/base | test_platforms_base.py | 11 |
| platforms/qoj (含真实 QOJ 1357 fixture) | platforms/test_qoj.py + test_qoj_real_standings.py | 33 + 16 |
| import_logic | test_import_logic.py | 16 |
| codes_logic | test_codes_logic.py | 9 |
| integration | test_integration.py | 7 (端到端, 真 git repo) |
| cli | test_cli.py | 8 (click.testing) |
| 旧测试 (sync / requirements / bootstrap / deploy) | | ~48 |
| **总计** | | **327** |

---

# 8. 安装 & 升级

## 8.1 首次安装

```bash
cd ~/wiki
./bootstrap.sh          # 装 venv + 依赖 + 跑 sync.py + 装 wiki wrapper 到 ~/.local/bin/wiki
~/.local/bin/wiki doctor  # 验证
```

## 8.2 加依赖

```bash
.venv/bin/pip install foo
.venv/bin/pip freeze | grep ^foo== >> requirements.txt
git add requirements.txt
```

## 8.3 加新 OJ（CF / AtCoder / ...）

1. 写 `tools/platforms/codeforces.py`（继承 `PlatformClient`）
2. 在 `tools/platforms/__init__.py` 加 `@register` 装饰器
3. 写 `tests/platforms/test_codeforces.py` + fixtures
4. ~200 行代码

**API / CLI 完全不变**, 只多一个 platform 可选.

---

# 9. 已知限制 / TODO

| 项 | 状态 | 影响 |
|---|---|---|
| QOJ 解析对真实改版的容错 | 部分测了 | 16 个真实 fixture 测试覆盖 1357, 改版后可能需要调 regex |
| `/codes/*` HTTP endpoint | N/A（HTTP 层已删） | 没有外部 HTTP 接口了 |
| systemd 自启 | 未实现 | 不需要: 没有后端进程要管 |
| 多用户/远程访问 | 明确不支持 | 单用户 CLI 工具 |
| QOJ `start_time` 不公开 | 影响 | `wiki update` 日期默认今天, 需 `--date` 覆盖 |

---

# 10. 调试技巧

```bash
# 1. CLI 报莫名错: 看 Python traceback
.venv/bin/python -m tools.cli_main update 2564

# 2. 测 QOJ 抓取 (不写)
wiki update 2564 --dry-run

# 3. 看 CSV 实际内容
head -5 contests.csv

# 4. 看 git 状态
git status
git log --oneline -5

# 5. 跑测试
.venv/bin/python -m unittest discover tests/

# 6. 跑单个测试
.venv/bin/python -m unittest tests.test_cli.TestCLIBasic.test_doctor -v

# 7. Cookie 失效排查
cat ~/.config/wiki/cookies/qoj.txt | grep -c 'qoj.ac'   # 期望 3 行
ls -la ~/.config/wiki/cookies/
```
