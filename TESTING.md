# Testing

> **335 tests passing** (272 单元 + 63 平台)
>
> 1 skipped: CF challenge 检测 (没真实 CF fixture)

## 跑测试

```bash
# 全部
make test                              # 跑 Python + JS

# 单独 Python
make test-py                           # 跑全部 Python
.venv/bin/python -m unittest discover tests/   # 等价

# 平台测试 (QOJ parser, 真实 fixture)
.venv/bin/python -m unittest discover -s tests/platforms

# 跑单个文件
.venv/bin/python -m unittest tests.test_csv_store

# 跑单个 case
.venv/bin/python -m unittest tests.test_csv_store.TestParseProblems.test_basic -v

# JS 测试 (用 node --test)
make test-js
node --test tests/js/*.test.js
```

## 覆盖总览

| 模块 | 测试文件 | 数量 |
|---|---|---|
| `csv_store.py` (contests.csv 读写) | `test_csv_store.py` | 66 |
| `md_store.py` (md 详情页) | `test_md_store.py` | 15 |
| `git_ops.py` (git 包装) | `test_git_ops.py` | 18 |
| `watchlist.py` (关注列表) | `test_watchlist.py` | 26 |
| `codes_store.py` (代码缓存) | `test_codes_store.py` | 30 |
| `codes_logic.py` (抓取业务) | `test_codes_logic.py` | 9 |
| `platforms/base.py` (抽象) | `test_platforms_base.py` | 11 |
| `platforms/qoj.py` (QOJ 客户端) | `test_qoj.py` + `test_qoj_real.py` + `test_qoj_real_standings.py` | 33 + 8 + 22 = 63 |
| `import_logic.py` (QOJ 导入) | `test_import_logic.py` | 16 |
| `cli_main.py` (Click) | `test_cli.py` | 8 |
| `cli config` | `test_cli_config.py` | 12 |
| 集成 (端到端, 真 git repo) | `test_integration.py` | 10 |
| 旧测试 (sync / requirements / bootstrap / deploy) | 多个 | ~48 |
| **总计** | | **335** |

## 真实 QOJ fixture 测试

`tests/fixtures/qoj_real/` 含**真实抓的** QOJ HTML:

| Fixture | 来源 | 测什么 |
|---|---|---|
| `contest_1357.html` | `/contest/1357` | problem listing 解析, title 提取 |
| `standings_1357.html` | `/contest/1357/standings` | JS 数据 parse (tarjen 解 B/G/I) |
| `submissions_1357_tarjen.html` | `/contest/1357/submissions?user=tarjen` | submissions row parse |
| `standings_2521.html` | `/contest/2521/standings` | 148KB JS, 781 users, hyphen/CJK username |
| `submission_1336269.html` | `/submission/1336269` | 单份提交页 code block |

`TestReal2521EdgeCases` 覆盖:
- `Today-_-` (hyphenated username)
- `ucup-teamNNNN` (13 个)
- `sdu-一场伟大的魔术` (CJK, mock 测)
- 2521 中 tarjen 解 10 题 (A B C D E F G H K L)

## 单元测试覆盖 (按模块)

### `csv_store.py` (66)
- CSV parse: 正常 / 缺列 / 引号内逗号 / 字段含 `;`
- `parse_problems()`: `O;.;!` / `O,O,O` / 空 / 越界
- `recompute_solved()`: 自动重算
- save/load round-trip
- 警告列表 (字段缺失)

### `md_store.py` (15)
- read / write / exists / delete
- placeholder 生成
- 路径安全 (拒绝 `..` / `/`)

### `git_ops.py` (18)
- status / commit / push / pull
- conflict (脏 tree) 检测
- push 失败不抛, 返回 (sha, False)

### `watchlist.py` (26)
- 增删查
- 持久化
- 含空行/重复行的健壮性

### `codes_store.py` (30)
- 新结构 `/<platform>/<cid>/<problem>/<user>.<ext>` 路径
- save/overwrite/并发 ext
- exists/read/list/clean
- index.json 同步
- 旧路径向后兼容 (读取)

### `codes_logic.py` (9)
- 完整 fetch_codes 流程 (mock client)
- mine + watchlist + others 桶
- skip_existing 行为
- top_n_fastest (新 standings 路径)

### `platforms/qoj.py` (33 + 30 fixture)
- `_parse_score_for_user` 真实 JS 数据
- `_parse_submission_list` 真实 HTML
- `_parse_code` 真实提交页
- 跨域 / 无效 username 处理
- 边界: 0 提交 / 错误页 / 登录页

### `import_logic.py` (16)
- `build_update_preview` 端到端
- `apply_update` 创建新 / 更新已存在
- `build_upsolve_preview` (./! → Ø)
- `apply_upsolve`

### 集成 (10)
- 完整 daily workflow: `update → apply → commit → push`
- 真 git repo + 真 bare remote
- upsolve 后再 update

## 加新测试

```bash
# 加单元测试
$EDITOR tests/test_<your_module>.py
# 继承 unittest.TestCase
class TestFeatureX(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(...)

# 加 QOJ fixture 测试
$EDITOR tests/platforms/test_qoj_<your_case>.py
# 用 tests/fixtures/qoj_real/ 里的真实 HTML
```

## 覆盖率目标

- 业务逻辑 (csv / md / git / import / codes): 高 (>80%)
- 平台 parser (QOJ): 高 (含真实 fixture)
- CLI: 中 (主要路径有覆盖, 边缘 flag 没全)
- 工具函数 (sync / bootstrap / deploy): 低 (难 unit test, 靠集成测)

## CI

`.github/workflows/` 跑 `make test` 在 macOS / Linux / Windows × Python 3.9 / 3.11 / 3.12.
失败会 push 到 PR.
