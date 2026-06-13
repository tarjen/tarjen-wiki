# Wiki Backend — 架构 & API

> **v1.0 重构后**：CLI 直接调用本地模块，无 HTTP server，无数据库。
>
> 257 tests passing。

---

## 0. 架构

### 数据流

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
tools/qoj_client.py     ← 抓 QOJ (唯一网络: qoj.ac)
tools/watchlist.py      ← ~/.config/wiki/watchlist.txt
tools/codes_store.py    ← ~/.local/share/wiki/codes/ (gitignored)
  ↓
contests.csv + docs/contests/*.md + git push → GitHub → GH Pages
```

**唯一网络出口**：
1. `qoj_client.get_*` 抓 qoj.ac（CF 信任家庭 IP）
2. `git_ops.push` 推 GitHub

**全部本地**：
- CSV 读写
- md 读写
- git commit / status / log
- watchlist / codes 缓存

### 启动

```bash
# CLI (不需要任何 server)
wiki doctor

# 后端: 不需要! 全 CLI 直调
# mkdocs preview (前端静态页, 可选):
wiki serve               # 跑 mkdocs serve -a 127.0.0.1:8000
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPO_PATH` | `cwd` | wiki git 仓库路径 |
| `CONFIG_DIR` | `~/.config/wiki` | cookie / watchlist / config.json |
| `CODES_DIR` | `~/.local/share/wiki/codes` | 代码缓存 (gitignored) |
| `EDITOR` | `vi` | `wiki edit` 用 |

### 文件位置

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

## 1. CLI 完整命令

### 1.1 健康检查

```bash
wiki doctor
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

### 1.2 浏览

```bash
wiki list [--since YYYY-MM-DD] [--until YYYY-MM-DD] \
          [--tag X] (可重复) \
          [--solved-min N] [--sort date|solved|rate|total] \
          [--order asc|desc] [--limit N] [--json]

wiki show <slug> [--body/--no-body]   # 默认含 body
```

### 1.3 CRUD

```bash
# 新增
wiki add --slug X --name Y --date Z --total N --problems "O;O;.;" \
         [--tags "#icpc #regional"] [--link URL] [--body "..."] [-y]

# 改字段
wiki set <slug> [--name X] [--date Z] [--total N] [--problems "O;O;."] \
                [--tags "#x #y"] [--link URL] \
                [--status A=O] (可重复, 按位置改) \
                [-y]

# 删
wiki rm <slug> [--keep-body] [-y]

# 编辑 md (开 $EDITOR)
wiki edit <slug>
```

### 1.4 QOJ 导入（核心日常）

```bash
# 比赛日 import
wiki update <cid> [--platform X] [--user NAME] [--slug X] [--dry-run] [-y]

# 几天后检测补题
wiki upsolve <cid_or_slug> [--platform X] [--user NAME] [--since ISO] [--dry-run] [-y]
```

行为：
- `update`: 从 QOJ 抓赛中提交，映射为 O/!/.，自动 create_new 或 update_existing
- `upsolve`: 抓赛后提交，识别 . 和 ! → Ø 的变化
- 默认每次有 Y/n 确认
- `--yes` / `-y` 跳过确认
- `--dry-run` 只预览不写
- `--user X` 覆盖 config 里的默认 user（多账号场景）

### 1.5 代码抓取

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

### 1.6 Cookie / Watchlist

```bash
# Cookie
wiki cookies import <file>          # Netscape jar 文件
wiki cookies status [--platform X]

# Watchlist
wiki watchlist list
wiki watchlist add <user>...       # 空格分隔多个
wiki watchlist remove <user>...
```

### 1.7 其他

```bash
wiki sync                            # 跑 tools/sync.py (重建 docs/index.md + data/contests.json)
wiki serve                           # mkdocs preview (前端静态页, 非后端)
```

---

## 2. Python 模块 API（程序化调用）

CLI 直接 import 这些，所以它们是事实上的"API"。

### 2.1 `csv_store.CsvStore`

```python
from csv_store import CsvStore, Contest

store = CsvStore(Path("contests.csv"))
store.load()                                    # 加载所有

# 查询
store.all()                                     # list[Contest], 按日期倒序
store.get(slug)                                 # Contest | None
store.exists(slug)                             # bool
len(store)                                      # int

# 修改
store.add(contest)                              # 写内存
store.update(slug, name=..., problems=[...])    # 改字段
store.delete(slug)                              # 删
store.save()                                    # 原子写盘
```

### 2.2 `md_store.MdStore`

```python
from md_store import MdStore

md = MdStore(Path("docs/contests"))
md.exists(slug)
md.read(slug)                                   # str
md.write(slug, content)                         # 原子写
md.delete(slug)                                 # bool
md.placeholder(contest)                        # str (默认模板)
```

### 2.3 `git_ops.GitOps`

```python
from git_ops import GitOps, GitPushError

git = GitOps(Path("~/wiki"))
git.status()                                     # RepoStatus (clean, ahead, behind, ...)
git.add(["contests.csv"])
git.commit("add(xxx)")                           # sha
git.push()                                       # 可能抛 GitPushError
git.commit_and_push("msg", ["paths"])            # (sha, pushed) 或抛 GitPushError
git.pull()                                       # 本地脏时抛 GitConflictError
```

### 2.4 `import_logic` — QOJ 导入

```python
from import_logic import (
    build_update_preview, apply_update,
    build_upsolve_preview, apply_upsolve,
    UpdatePreview, UpsolvePreview, ApplyResult,
)

# 一日一赛
preview = build_update_preview(
    platform="qoj", contest_id="2564", user="tarjen",
    csv_store=csv, config_dir=cfg, slug_override=None,
)
result = apply_update(
    preview=preview, csv_store=csv, md_store=md, git_ops=git,
    overrides={}, options={"create_body": True, "run_sync": True, "push": True},
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

### 2.5 `qoj_client.QojClient`

```python
from platforms.qoj import QojClient

client = QojClient(cookies={"uoj_remember_token": "...", ...})
client.cookies_valid()                  # bool
client.get_contest_meta("2564")         # ContestMeta
client.get_user_submissions("2564", "tarjen")   # list[Submission]
client.get_submission_code("12345")     # (code: str, language: str)
```

### 2.6 `codes_logic`

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

### 2.7 `watchlist.Watchlist`

```python
wl = Watchlist(Path("~/.config/wiki/watchlist.txt"))
wl.load()
wl.users()                                # list[str]
wl.contains("alice")                      # bool
wl.add(["carol"])
wl.remove(["alice"])
wl.save()
```

---

## 3. 每日工作流（已用集成测试验证）

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

## 4. 测试覆盖（257 tests）

| 模块 | 测试文件 | 数量 |
|---|---|---|
| csv_store | test_csv_store.py | 66 |
| md_store | test_md_store.py | 15 |
| git_ops | test_git_ops.py | 18 |
| watchlist | test_watchlist.py | 26 |
| codes_store | test_codes_store.py | 30 |
| platforms/base | test_platforms_base.py | 11 |
| platforms/qoj | platforms/test_qoj.py | 33 |
| import_logic | test_import_logic.py | 16 |
| codes_logic | test_codes_logic.py | 9 |
| integration | test_integration.py | 7 (端到端, 真 git repo) |
| cli | test_cli.py | 8 (click.testing) |
| 旧测试 (sync / requirements / bootstrap / deploy) | | ~48 |
| **总计** | | **257** |

---

## 5. 错误处理

### CLI 命令失败

- 退出码非 0
- 错误信息打到 stderr（Click 默认行为）
- 看日志：`~/.local/share/wiki/wiki.log`（如果用了 wrapper）

### 常见错误及修法

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

## 6. 安装 & 升级

### 首次安装

```bash
cd ~/wiki
./bootstrap.sh          # 装 venv + 依赖 + 跑 sync.py + 装 wiki wrapper 到 ~/.local/bin/wiki
~/.local/bin/wiki doctor  # 验证
```

### 加依赖

```bash
.venv/bin/pip install foo
.venv/bin/pip freeze | grep ^foo== >> requirements.txt
git add requirements.txt
```

### 加新 OJ（CF / AtCoder / ...）

1. 写 `tools/platforms/codeforces.py`（继承 `PlatformClient`）
2. 在 `tools/platforms/__init__.py` 加 `@register` 装饰器
3. 写 `tests/platforms/test_codeforces.py` + fixtures
4. ~200 行代码

**API / CLI 完全不变**，只多一个 platform 可选。

---

## 7. 已知限制 / TODO

| 项 | 状态 | 影响 |
|---|---|---|
| 真实 QOJ HTML 验证 | 未做 | E2E 用 mock HTML, 实盘可能 regex 需要调 |
| QOJ 解析对真实改版的容错 | 未测 | fixtures 一旦失效会全报 ParseError |
| `/codes/*` HTTP endpoint | N/A（HTTP 层已删） | 没有外部 HTTP 接口了 |
| systemd 自启 | 未实现 | 不需要: 没有后端进程要管 |
| 多用户/远程访问 | 明确不支持 | 单用户 CLI 工具 |

---

## 8. 调试技巧

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