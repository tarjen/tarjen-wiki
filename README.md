# 比赛记录 Wiki

一个基于 [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) 的个人编程比赛记录 wiki，托管在 GitHub Pages。

## 特性

- 主页是张总表，按日期倒序排列所有比赛，单元格用 `O` / `.` / `!` / `?` 标记每题状态
- 每场比赛名都链接到一个独立的详情页，记录每题思路
- 支持浅色 / 深色模式、中文搜索、响应式表格
- 全部内容用 markdown 维护，编辑器友好

## 图例

| 符号 | 含义 |
|------|------|
| `O` | 通过 |
| `.` | 未通过或未尝试 |
| `!` | 赛中未通过，赛后补题通过 |
| `?` | 待补 |

## 本地预览

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdocs serve
```

访问 http://127.0.0.1:8000

## 部署到 GitHub Pages

```bash
mkdocs gh-deploy
```

首次部署后到 GitHub 仓库 `Settings → Pages`，Source 选 `Deploy from a branch`，Branch 选 `gh-pages` / `(root)`。

## 添加新比赛

1. 在 `docs/index.md` 总表里加一行
2. 在 `docs/contests/` 下新建 `YYYY-简短名.md`，按模板填内容
3. `mkdocs gh-deploy`
