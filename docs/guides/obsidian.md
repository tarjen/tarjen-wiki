# 用 Obsidian 编辑 Wiki

适合日常零散补题解、想做笔记体系的用户。Obsidian 的 markdown 编辑器比纯文本编辑器好用：表格可视化、自动补全、双链、tag 系统都有。

## 一次性设置

### 1. 把 `docs/` 当成 Obsidian vault

Obsidian 的 vault 就是一个文件夹。直接把 `docs/` 打开即可：

1. 打开 Obsidian → 「打开另一个 vault」 → 「打开」 → 选 `docs/` 文件夹
2. Obsidian 会问要不要信任文件夹里所有插件——这里 docs/ 不会有可执行代码，放心信任

打开后，左边栏会看到 `index.md` / `contests/` / `platforms/` / `guides/` 这些文件，可以像普通笔记软件一样浏览。

### 2. 装两个插件

进 Obsidian 的「设置 → 第三方插件 → 社区插件市场」搜索安装：

- **Obsidian Git** — 自动 `git add / commit / push`，写完一行命令都不用敲
- **Editing Toolbar**（可选）— 给 markdown 编辑器加一个浮动工具栏，快捷插入表格

也可以不装插件，手动 `git push` 也行。

### 3. 配置 Obsidian Git

「设置 → Obsidian Git」里配置：

- **Vault 备份间隔**：每 10 分钟（或你觉得合适的间隔）
- **自动 Push**：开
- **自动 Pull**：开
- **提交消息模板**：`{{date}} update`

这样你改完一个文件，等几分钟它就自动提交并 push 到 GitHub。

## 写新比赛 / 改表格的姿势

### 姿势 A（推荐）：用 CSV

1. 回到本仓库的根目录，用 Excel / 飞书 / Numbers 打开 `contests.csv`
2. 加一行，problems 列写 `O;.;O;O;...`（分号分隔，顺序就是 A B C D ...）
3. 跑 `python3 tools/sync.py` 重新生成总表 + 建占位页
4. 跑 `mkdocs gh-deploy` 部署

Obsidian 里这步可以不做，CSV 是仓库根目录的，不在 docs/ 里。

### 姿势 B（纯 Obsidian）

直接在 Obsidian 里编辑 `contests/<slug>.md`，记每题思路。改完后让 Obsidian Git 自动 push 就行。**但**总表 `docs/index.md` 还是得手改——这块 Obsidian 编辑器支持表格可视化，把光标放在表格里会浮出表格工具栏，能加列、加行、拖动列，**比手写 markdown 表格友好很多**。

### 不要混用

如果你同时在用姿势 A（CSV + sync.py）和姿势 B（Obsidian 改 index.md），注意：
- CSV 改完跑 sync.py 会**覆盖** `index.md` 里的 SYNC 块
- Obsidian 改完总表后，**别再跑 sync.py**，否则手写的总表会被 CSV 覆盖
- 建议：表格维护走 CSV，详情页笔记走 Obsidian

## 编辑效果对比

| 操作 | 手写 markdown | Obsidian |
|------|---------------|----------|
| 加一行表格 | 数列数、对齐 `\|` | 工具栏点「+」 |
| 加一列题目 | 改表头 + 改 23 行 | 工具栏点「+ 列」 |
| 加超链接 | 手打 `[text](url)` | `[[` 自动弹补全 |
| 看反向链接 | 没法看 | 点开文件，看「Backlinks」面板 |
| 全文搜索 | `grep` / IDE | 侧边栏搜 |

## 一个常见坑：表格列宽

Obsidian 编辑器里的表格看着很整齐，但渲染到 Material 主题里首列（比赛名）会被压窄。这是 Material 主题的限制（窄屏表格自动横滚）。

解决：在 `docs/assets/stylesheets/extra.css` 里加 `min-width: 18em` 给首列（已经加好了）。如果你以后想控制其他列的宽度，给那一行加个 `{: style="min-width: 5em"}`（需要 markdown 扩展 `attr_list`，本项目已开）。
