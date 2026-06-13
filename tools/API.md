# Wiki Backend API

> **状态**：v1.0 (Phase 1-4 + E2E 已完成, Phase 3.3 endpoints / 3.4 / 5+ 待做)
>
> 本文档描述**已实现**的 API 行为。所有端点都有测试覆盖（278 tests passing）。

---

## 0. 概述

### 架构

```
┌─────────────────────────────────────────────────────────────┐
│ 家里机器 (systemd --user 起 wiki-backend.service)           │
│                                                              │
│   FastAPI 服务监听 127.0.0.1:8001                           │
│         ↑                                                     │
│         ├── CLI (wiki 命令)  →  调 HTTP                     │
│         └── 浏览器 (localhost:8001/ui)  →  调 HTTP          │
│                                                              │
│   持久层:                                                     │
│     ~/wiki/contests.csv           (CSV source of truth)      │
│     ~/wiki/docs/contests/*.md     (手写详情页)              │
│     ~/.config/wiki/                                                │
│       ├── cookies/qoj.txt        (Netscape cookie jar)      │
│       ├── watchlist.txt          (关注用户列表)              │
│       └── backend.env            (环境变量)                  │
│     ~/.local/share/wiki/codes/   (代码缓存, gitignored)      │
└─────────────────────────────────────────────────────────────┘
                │ git push
                ▼
        ┌────────────────────┐
        │ GitHub Pages       │  ← 纯静态展示
        └────────────────────┘
```

### 启动

```bash
# 开发模式 (前台)
wiki serve

# 生产模式 (后续)
./bootstrap.sh --install-service   # 装 systemd 单元
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPO_PATH` | `cwd` | wiki git 仓库路径 |
| `CONFIG_DIR` | `~/.config/wiki` | 配置目录 |
| `CODES_DIR` | `~/.local/share/wiki/codes` | 代码缓存 |
| `BIND` | `127.0.0.1` | 监听地址 |
| `PORT` | `8001` | 监听端口 |

---

## 1. 通用响应格式

### 成功响应

裸对象或 `{"ok": true, ...}` 结构（不同端点略不同，见各端点）。

### 错误响应

```json
{
  "ok": false,
  "error": {
    "code": "slug_exists",
    "message": "slug already exists: 2025-icpc-xxx",
    "details": { "row_num": null, "slug": "2025-icpc-xxx" }
  }
}
```

### 错误码索引

| `code` | HTTP | 含义 |
|---|---|---|
| `repo_dirty` | 409 | 本地有未提交改动 |
| `repo_behind` | 409 | 本地落后 remote |
| `slug_exists` | 409 | slug 已存在 |
| `slug_not_found` | 404 | slug 不存在 |
| `slug_invalid` | 400 | slug 字符非法 |
| `date_invalid` | 400 | 日期格式不对 |
| `problems_length_mismatch` | 400 | problems 长度 ≠ total |
| `problems_invalid_char` | 400 | 含非 `OØ!.` 字符 |
| `name_empty` | 400 | name 为空 |
| `unknown_field` | 400 | 字段名未知 |
| `qoj_cookie_expired` | 401 | QOJ cookie 失效 |
| `qoj_cf_blocked` | 502 | CF challenge 拦截 |
| `qoj_parse_failed` | 422 | HTML 结构变了 |
| `qoj_timeout` | 408 | 抓取超时 |
| `qoj_contest_not_found` | 404 | contest_id 不存在 |
| `cookies_missing_for_platform` | 401 | cookie 文件不存在 |
| `gh_push_failed` | 502 | push 失败 |
| `no_user_specified` | 400 | 未传 user 且 config 无默认 |
| `invalid_request` | 400 | 参数格式不对 |
| `invalid_sort` | 400 | sort 字段非法 |

---

## 2. `GET /healthz`

健康检查。

### Response 200

```json
{
  "ok": true,
  "config": {
    "repo_path": "/home/tarjen/wiki",
    "config_dir": "/home/tarjen/.config/wiki",
    "codes_dir": "/home/tarjen/.local/share/wiki/codes"
  },
  "csv": { "contests": 23 },
  "repo": {
    "branch": "main",
    "clean": true,
    "ahead": 0,
    "behind": 0,
    "last_commit": {
      "sha": "abc12345",
      "message": "add(2025-icpc-xxx)",
      "time": "2026-06-13T10:30:00Z"
    }
  },
  "watchlist_count": 2,
  "uptime_seconds": 3600
}
```

### CLI

```bash
wiki doctor
```

---

## 3. `GET /contests`

列出比赛，支持筛选。

### Query 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `since` | string `YYYY-MM-DD` | 只看 >= 此日期 |
| `until` | string `YYYY-MM-DD` | 只看 <= 此日期 |
| `tag` | string（可重复） | 按标签筛选（支持 `test` 或 `#test`） |
| `solved_min` | int | 最少赛时+补题通过数 |
| `sort` | `date`/`solved`/`rate`/`total` | 排序字段 |
| `order` | `asc`/`desc` | 排序方向 |
| `limit` | int | 最多返回几条 |

### Response 200

```json
{
  "count": 5,
  "total": 23,
  "contests": [
    {
      "slug": "2025-icpc-nac",
      "name": "The 2025 ICPC North America Championship",
      "date": "2025.7.7",
      "solved": 7,
      "in_contest": 6,
      "upsolved": 1,
      "total": 13,
      "tags": ["#icpc", "#nac", "#2025"],
      "tags_raw": "#icpc #nac #2025",
      "link": "",
      "body_exists": true
    },
    ...
  ]
}
```

### CLI

```bash
wiki list
wiki list --since 2025.1.1 --tag icpc --sort rate --limit 10
wiki list --json   # 输出 JSON
```

---

## 4. `GET /contests/{slug}`

单场详情。

### Response 200

```json
{
  "slug": "2025-icpc-nac",
  "name": "The 2025 ICPC North America Championship",
  "date": "2025.7.7",
  "solved": 7,
  "in_contest": 6,
  "upsolved": 1,
  "total": 13,
  "problems": ["O", "O", "O", ".", ".", "O", "O", "O", ".", ".", ".", ".", "."],
  "tags": ["#icpc", "#nac", "#2025"],
  "tags_raw": "#icpc #nac #2025",
  "link": "",
  "body_exists": true,
  "body_path": "docs/contests/2025-icpc-nac.md",
  "body": "# 2025 ICPC North America Championship\n\n## Summary\n..."
}
```

`body` 在 `body_exists=true` 时是 md 内容，否则 `null`。

### CLI

```bash
wiki show 2025-icpc-nac            # 含 body
wiki show 2025-icpc-nac --no-body  # 只显示元信息
```

---

## 5. `POST /contests`

新增一场比赛（**同步**写 CSV + md + commit + push）。

### Request

```json
{
  "slug": "2025-icpc-xxx",
  "name": "2025 ICPC XXX Regional",
  "date": "2025.6.7",
  "total": 13,
  "problems": ["O","O","Ø","O","O","O",".",".","O",".",".",".","Ø"],
  "tags": ["#icpc", "#regional"],
  "link": "https://qoj.ac/contest/2564",
  "body": "# 完整题解...",
  "commit_message": "add(2025-icpc-xxx): manual"
}
```

字段：
- 必填：`slug`, `name`, `date`, `total`, `problems`
- 选填：`tags` (list), `link`, `body` (markdown), `commit_message`

### Response 201

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx",
  "csv_written": true,
  "body_written": "docs/contests/2025-icpc-xxx.md",
  "committed": true,
  "commit_sha": "abc12345",
  "pushed": true
}
```

### 行为

1. 校验 slug 合法 + 不重复
2. 校验 date 格式 (`YYYY.M.D` / `YYYY-MM-DD` / `YYYY/M/D`)
3. 校验 `len(problems) == total` 且字符 ∈ `{O, Ø, !, .}`
4. 写入 `contests.csv`
5. 如果 `body` 提供：写到 `docs/contests/<slug>.md`
6. 否则：如果 md 不存在，用占位模板创建
7. 调 `tools/sync.py` 重建 `docs/index.md` 和 `docs/data/contests.json`
8. `git add` + `commit` + `push`

如果 repo dirty（已有未提交改动），返回 409。

---

## 6. `PUT /contests/{slug}`

部分更新。

### Request（任意子集）

```json
{
  "name": "新名字",
  "date": "2025.6.8",
  "total": 14,
  "problems": ["O","O","Ø","O","O","O",".",".","O",".",".",".","Ø","."],
  "tags": ["#icpc", "#regional", "#2025"],
  "link": "https://...",
  "slug": "2025-icpc-xxx-v2",
  "commit_message": "rename + update tags"
}
```

特殊：`slug` 字段会**重命名**比赛（同时更新 md 文件名）。

### Response 200

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx-v2",
  "committed": true,
  "commit_sha": "def56789",
  "pushed": true
}
```

---

## 7. `PATCH /contests/{slug}/body`

只更新详情页 md。

### Request

两种格式：

```json
{ "content": "# 完整题解..." }
```

或纯 markdown (`Content-Type: text/markdown`)：

```bash
curl -X PATCH http://localhost:8001/contests/xxx/body \
  --data-binary @docs/contests/xxx.md \
  -H 'Content-Type: text/markdown'
```

### Response 200

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx",
  "body_written": "docs/contests/2025-icpc-xxx.md",
  "committed": true,
  "commit_sha": "...",
  "pushed": true
}
```

### CLI

```bash
wiki edit 2025-icpc-xxx   # 开 $EDITOR, 保存后自动调这个端点
```

---

## 8. `DELETE /contests/{slug}`

删除比赛。

### Query

| 参数 | 类型 | 说明 |
|---|---|---|
| `keep_body` | bool | 保留 md 文件，只删 CSV 行 |
| `commit_message` | string | 自定义 commit |

### Response 200

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx",
  "csv_removed": true,
  "body_removed": true,
  "committed": true,
  "commit_sha": "...",
  "pushed": true
}
```

### CLI

```bash
wiki rm 2025-icpc-xxx          # 交互确认 y/N
wiki rm 2025-icpc-xxx --yes    # 跳过确认
```

---

## 9. `POST /import/update-preview`

**只读**：从 OJ 抓比赛 + 你的赛中提交，返回映射后的 preview。**不写任何文件**。

### Request

```json
{
  "platform": "qoj",
  "contest_id": "2564",
  "user": "tarjen",
  "slug": "2025-icpc-xxx"   // 选填, 覆盖 slug 生成
}
```

`user` 不传时从 `config.json` 的 `default_user.qoj` 读。

### Response 200

```json
{
  "platform": "qoj",
  "type": "update",
  "record_state": "create_new",
  "slug": "2025-icpc-xxx-regional",
  "slug_exists": false,
  "contest": {
    "platform": "qoj",
    "contest_id": "2564",
    "title": "2025 ICPC XXX Regional",
    "problem_count": 13,
    "start_time": "2025-06-07T08:00:00",
    "end_time": "2025-06-07T13:00:00",
    "url": "https://qoj.ac/contest/2564"
  },
  "username": "tarjen",
  "total_problems": 13,
  "problems": [
    {"letter": "A", "status": "O", "verdict": "AC",  "contest_time": "0:12", "tries": 1},
    {"letter": "B", "status": "!", "verdict": "WA",  "contest_time": "0:30", "tries": 2},
    {"letter": "C", "status": ".", "verdict": null,  "no_submission": true},
    ...
  ],
  "summary": { "O": 6, "Ø": 0, "!": 2, ".": 5 },
  "suggested": {
    "slug": "2025-icpc-xxx-regional",
    "name": "2025 ICPC XXX Regional",
    "date": "2025.6.7",
    "link": "https://qoj.ac/contest/2564"
  },
  "fetch_seconds": 4.2
}
```

### 状态映射（赛中提交）

| QOJ verdict | wiki status |
|---|---|
| AC | `O` |
| WA / TLE / RE / MLE | `!` |
| 没提交 | `.` |

同题多次提交：取**最晚**一次（非 AC 覆盖 AC？取最晚；AC 覆盖 WA？取最晚）。

### CLI

```bash
wiki update 2564              # 调 preview, 显示, 等确认
wiki update 2564 --dry-run    # 只 preview, 不写
```

---

## 10. `POST /import/update-apply`

把 update-preview 应用到仓库。

### Request

```json
{
  "platform": "qoj",
  "preview": { /* 上一响应原文 */ },
  "overrides": {
    "slug": "...",         // 覆盖 preview.suggested.slug
    "name": "...",
    "date": "...",
    "tags": ["#icpc", "#regional"],
    "link": "...",
    "commit_message": "add(2025-icpc-xxx)"
  },
  "options": {
    "create_body": true,    // 默认 true: 没 md 时创建占位
    "run_sync": true,      // 默认 true: 跑 tools/sync.py
    "push": true            // 默认 true: commit + push
  }
}
```

### Response 201

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx-regional",
  "record_state": "create_new",
  "csv_written": true,
  "body_written": "docs/contests/2025-icpc-xxx-regional.md",
  "committed": true,
  "commit_sha": "abc12345",
  "pushed": true
}
```

### 行为

1. 解析 preview + overrides → Contest 对象
2. 自动检测 record_state：
   - slug 已在 contests.csv → `update_existing`
   - slug 不存在 → `create_new`
3. 写入 contests.csv（重算 solved）
4. 如果 `create_body=true` 且 md 不存在，写入占位
5. 调 `tools/sync.py`（如果 `run_sync=true`）
6. `git add` + `commit` + `push`（如果 `push=true`）

### CLI

```bash
wiki update 2564 -y   # 一条命令跑完 preview + apply
```

---

## 11. `POST /import/upsolve-preview`

**只读**：检查赛后补题。

### Request

```json
{
  "platform": "qoj",
  "contest_id": "2564",    // 或 "slug": "2025-icpc-xxx"
  "user": "tarjen",
  "since": "2025-06-07T13:00:00Z"   // 选填, 默认 contest.end_time
}
```

### Response 200

```json
{
  "platform": "qoj",
  "type": "upsolve",
  "slug": "2025-icpc-xxx-regional",
  "contest_id": "2564",
  "username": "tarjen",
  "since": "2025-06-07T13:00:00Z",
  "current_problems": ["O", "O", ".", "!", "O", ".", ".", "O", ".", ".", ".", "O", "O"],
  "changes": [
    {
      "letter": "C",
      "before": ".",
      "after": "Ø",
      "verdict": "AC",
      "submitted_at": "2025-06-09T22:14:00Z",
      "tries": 2,
      "submission_id": "20001",
      "reason": "post_contest_ac_from_untouched"
    },
    {
      "letter": "D",
      "before": "!",
      "after": "Ø",
      "verdict": "AC",
      "submitted_at": "2025-06-10T15:00:00Z",
      "tries": 5,
      "submission_id": "20002",
      "reason": "post_contest_ac_from_bang"
    }
  ],
  "summary": {
    "upsolved": 1,                 // . -> Ø 数量
    "upsolved_from_bang": 1,       // ! -> Ø 数量
    "no_change_attempts": 0,       // 赛后 WA 但无变化
    "skipped_already_o": 0         // O 不动
  }
}
```

### 判定逻辑

| 当前 | 赛后 | 变化 |
|---|---|---|
| `.` | AC | → `Ø`（upsolved） |
| `!` | AC | → `Ø`（upsolved_from_bang） |
| `O` 或 `Ø` | 任何 | 不变 |

赛后非 AC 提交不算"变化"——保持原状态。

### CLI

```bash
wiki upsolve 2025-icpc-xxx-regional    # 看变化, 等确认
wiki upsolve 2564                      # 用 cid 找 slug
```

---

## 12. `POST /import/upsolve-apply`

应用 upsolve 变化。

### Request

```json
{
  "platform": "qoj",
  "preview": { /* 上一响应 */ },
  "options": {
    "push": true,
    "commit_message": "upsolve(2025-icpc-xxx-regional)"
  }
}
```

### Response 200

```json
{
  "ok": true,
  "slug": "2025-icpc-xxx-regional",
  "record_state": "update_existing",
  "csv_written": true,
  "body_written": null,
  "committed": true,
  "commit_sha": "def56789",
  "pushed": true,
  "problems_before": ["O","O",".","!","O",".",".","O",".",".",".","O","O"],
  "problems_after":  ["O","O","Ø","Ø","O",".",".","O",".",".",".","O","O"]
}
```

---

## 13. `GET /import/contest/{cid}`

只取比赛元信息，不抓提交。

### Response 200

```json
{
  "platform": "qoj",
  "contest": {
    "contest_id": "2564",
    "title": "2025 ICPC XXX Regional",
    "problem_count": 13,
    "start_time": "2025-06-07T08:00:00",
    "end_time": "2025-06-07T13:00:00",
    "url": "https://qoj.ac/contest/2564"
  }
}
```

---

## 14. CLI 完整命令清单

```bash
# === 服务管理 (部分待实现) ===
wiki serve                     # 前台启动
wiki serve --install           # 装 systemd (Phase 5)

# === 健康检查 ===
wiki doctor

# === 浏览 ===
wiki list [--since] [--until] [--tag X] [--sort] [--limit] [--json]
wiki show <slug> [--body|--no-body]

# === 一日一赛 ===
wiki update <cid>          # ⭐ 主路径
  --platform X             # 默认 qoj
  --user NAME              # 默认 config.default_user.X
  --slug SLUG              # 覆盖 slug
  --dry-run                # 只 preview
  --yes / -y               # 跳过确认

wiki upsolve <cid_or_slug>
  --platform X
  --user NAME
  --since ISO              # 覆盖 contest.end_time
  --dry-run
  --yes / -y

# === Cookie 管理 (Phase 3.4 端点待实现) ===
wiki cookies import <file>
wiki cookies status [--platform X]

# === 待实现 ===
wiki codes <cid>           # Phase 3.3 endpoints
wiki watchlist             # Phase 3.4
wiki rm <slug>
wiki set <slug> --status ...
wiki edit <slug>
```

---

## 15. 每日工作流（已用 E2E 测试验证）

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
              │ [Y/n/d/e] y                      │
              └────────────────┬────────────────┘
                               │
                               ▼
            ┌─────────────────────────────┐
            │ 写入 contests.csv             │
            │ 创建 docs/contests/xxx.md    │
            │ 调 tools/sync.py             │
            │ git commit + push            │
            │ GH Actions → GH Pages        │
            └─────────────────────────────┘
                               │
                  几天/几周后 (补完题)
                               │
                               ▼
                    $ wiki upsolve 2564
                               │
              ┌────────────────┼────────────────┐
              │ 预览: C . -> Ø (补过)        │
              │ [Y/n] y                       │
              └────────────────┬────────────────┘
                               │
                               ▼
            ┌─────────────────────────────┐
            │ contests.csv 更新            │
            │ git commit + push            │
            └─────────────────────────────┘
```

---

## 16. 测试覆盖

| 模块 | 测试文件 | 数量 |
|---|---|---|
| csv_store | test_csv_store.py | 66 |
| md_store | test_md_store.py | 15 |
| git_ops | test_git_ops.py | 18 |
| watchlist | test_watchlist.py | 26 |
| codes_store | test_codes_store.py | 30 |
| platforms/base | test_platforms_base.py | 11 |
| platforms/qoj | platforms/test_qoj.py | 33 |
| server | test_server.py | 23 |
| import_logic | test_import_logic.py | 16 |
| codes_logic | test_codes_logic.py | 9 |
| cli | test_cli.py | 5 |
| e2e | test_e2e.py | 11 |
| 旧测试 (sync / requirements / bootstrap / deploy) | | ~48 |
| **总计** | | **278** |

---

## 17. 已知限制 / TODO

| 项 | 状态 | 影响 |
|---|---|---|
| `/codes/*` 端点 | 未实现 | code fetch 命令没法用 |
| `/import/cookies/*` | 未实现 | CLI 没法上传 cookie |
| `/watchlist` | 未实现 | CLI 没法管 watchlist |
| `/repo/*` | 未实现 | 无法 GET pull 状态 |
| `/stats` | 未实现 | 无 HTTP 端点 (GH Pages 客户端会做基础 stats) |
| systemd 单元 | 未实现 | 需手动启动后端 |
| 真实 QOJ HTML 测试 | 未做 | E2E 用 mock HTML, 实盘可能 regex 需要调 |
| QOJ 解析对真实改版的容错 | 未测 | fixtures 一旦失效会全报 ParseError |

---

## 18. 调试技巧

```bash
# 后端连不上: 先手动起
cd ~/wiki
.venv/bin/python -m tools.server

# CLI 连不上: 设 WIKI_API
WIKI_API=http://127.0.0.1:8001 wiki doctor

# 看后端日志: uvicorn 直接打 stdout
.venv/bin/python -m tools.server 2>&1 | tee /tmp/wiki.log

# Cookie 失效排查:
ls -la ~/.config/wiki/cookies/
cat ~/.config/wiki/cookies/qoj.txt | grep -c 'qoj.ac'   # 期望 3 行

# Repo 状态:
cd ~/wiki && git status
wiki doctor
```