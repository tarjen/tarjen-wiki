# 比赛记录 Wiki

个人编程比赛记录系统, CLI + MkDocs 静态站, 托管在 GitHub Pages.

## 上手

```bash
# 1. 装 (一次性)
./bootstrap.sh

# 2. 配 QOJ cookie (3 个值, 交互式隐藏输入)
wiki cookies set

# 3. 配默认 user
wiki config set default_user.qoj tarjen

# 4. 比赛日: 抓比赛中表现 (in-contest + 赛后 都标 O)
wiki update 2521

# 5. 几天后: 检测补题 (. / ! → Ø)
wiki upsolve 2521

# 6. 抓代码 (自己 + watchlist + 每题最快 1 个 sample)
wiki codes 2521
wiki codes-list 2521            # 看抓了啥
wiki codes-show 2521 tarjen A   # 看具体某份

# 7. 推 GH Pages
mkdocs gh-deploy
```

## 命令

| 命令 | 作用 |
|------|------|
| `wiki doctor` | 健康检查 (cookie, git, env) |
| `wiki list [--tag X] [--sort solved] [--limit N] [--json]` | 列比赛 |
| `wiki show <slug>` | 看一场 |
| `wiki add` (无 flag 走交互) | 手动新增 |
| `wiki set <slug> [--status A=O] [--problems "O;.;!"]` | 改字段 |
| `wiki update <cid> [--user X] [--date YYYY.M.D] [-y]` | 抓比赛 (standings) |
| `wiki upsolve <cid> [--user X] [-y]` | 检测补题 (submissions) |
| `wiki codes <cid> [--only-mine] [--sample N] [-y]` | 抓代码 |
| `wiki codes-list <cid> [--problem A] [--user X] [--source mine\|sample]` | 列已抓 |
| `wiki codes-show <cid> <user> <problem>` | 看某份代码 (less) |
| `wiki cookies {import <file> \| set \| status}` | 配 cookie |
| `wiki watchlist {list \| add <user>... \| remove <user>...}` | 关注列表 |
| `wiki config {show \| set <k> <v> \| get <k> \| path}` | 管理 config |
| `wiki sync` | 重生成 docs/index.md + data/contests.json |
| `wiki serve` | 本地 mkdocs preview (前端) |

完整命令 + Python API: [tools/API.md](tools/API.md).

## 数据存哪

```
~/wiki/                                # git repo (REPO_PATH)
├── contests.csv                       # 唯一数据源
└── docs/contests/<slug>.md            # 详情页

~/.config/wiki/                        # 不在 repo
├── cookies/qoj.txt                    # Netscape cookie jar
├── config.json                        # { "default_user": {"qoj": "tarjen"} }
└── watchlist.txt

~/.local/share/wiki/codes/             # 不在 repo (gitignore 之外)
└── <platform>/<cid>/<problem>/<user>.<ext>
    e.g. ~/.local/share/wiki/codes/qoj/2521/A/tarjen.cpp
```

## 图例

| 符号 | 含义 |
|------|------|
| `O` | 通过 (赛中 + 赛后) |
| `Ø` | 赛中没过, 赛后补题过 |
| `!` | 试过没过 |
| `.` | 没提交 |

## 平台支持

macOS / Linux / Windows (Git Bash / cmd / PowerShell) 全平台. Python 3.9+.

## 文档

- [tools/API.md](tools/API.md) — CLI + Python API 详细参考
- [TESTING.md](TESTING.md) — 怎么跑测试
- [mkdocs.yml](mkdocs.yml) — 站点配置

## 设计原则

- **零网络服务**: 没有 HTTP server, 没有数据库
- **唯一数据源**: contests.csv 是 ground truth, git 是 history
- **不上传代码 / cookie**: 全在 `~/.local/share/wiki/`, 不在 repo
- **抓取失败不静默**: 错误详情打到 stderr

## 仓库结构

```
.
├── bin/                    # 跨平台 wrapper
├── docs/                   # MkDocs 源 (contests/ 是核心)
├── tools/                  # Python CLI + 库
│   ├── cli_main.py         # 入口 (所有 wiki <command>)
│   ├── csv_store.py        # contests.csv 读写
│   ├── md_store.py         # docs/contests/<slug>.md 读写
│   ├── git_ops.py          # git commit/push 包装
│   ├── import_logic.py     # update/upsolve 业务逻辑
│   ├── codes_logic.py      # codes 抓取业务
│   ├── codes_store.py      # 本地代码缓存
│   ├── watchlist.py        # 关注列表
│   ├── sync.py             # 重生成 index.md + contests.json
│   ├── platforms/          # OJ 客户端
│   │   ├── base.py         # 抽象接口
│   │   └── qoj.py          # QOJ 实现 (cloudscraper 绕 CF)
│   └── API.md
├── tests/                  # 单元测试 (335 tests)
├── contests.csv            # 唯一数据源
├── mkdocs.yml
├── requirements.txt
└── README.md
```

## 加新 OJ (CF / AtCoder / ...)

1. 写 `tools/platforms/<name>.py` (继承 `PlatformClient`, 实现 4 个 abstractmethod)
2. 在 `tools/platforms/__init__.py` 加 `@register` 装饰器
3. 写 `tests/platforms/test_<name>.py` + fixtures
4. ~200 行代码. API/CLI 不变, 多一个 `--platform cf` 选项.

## 部署

```bash
mkdocs gh-deploy
```

首次部署: GitHub 仓库 `Settings → Pages → Source: gh-pages / (root)`.

## 测试

```bash
make test              # Python + JS
make test-py           # 单独 Python
.venv/bin/python -m unittest discover tests/
.venv/bin/python -m unittest discover -s tests/platforms
```

**335 tests** (272 顶层 + 63 平台, 含真实 QOJ fixture).
