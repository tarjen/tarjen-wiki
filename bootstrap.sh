#!/usr/bin/env bash
# bootstrap.sh — 一键环境搭建
#
# 用法：
#   ./bootstrap.sh                # 装 venv + pip install + 跑 sync.py
#   ./bootstrap.sh --serve        # 上面 + mkdocs serve
#   ./bootstrap.sh --no-sync      # 跳过 sync.py
#   ./bootstrap.sh --clean        # 删 .venv 重建
#
# 不会触碰 GitHub PAT —— token 在浏览器里填（带密码加密，Dev #4 改的）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- 解析参数 ----
DO_SERVE=0
DO_SYNC=1
DO_CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --serve)    DO_SERVE=1 ;;
    --no-sync)  DO_SYNC=0 ;;
    --clean)    DO_CLEAN=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "❌ 未知参数: $arg"; exit 1 ;;
  esac
done

# ---- 颜色（终端不支持就退化） ----
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; OFF=""
fi
say()  { echo "${BOLD}▶${OFF} $*"; }
ok()   { echo "${GREEN}✓${OFF} $*"; }
warn() { echo "${YELLOW}⚠${OFF} $*"; }
die()  { echo "${RED}✖${OFF} $*" >&2; exit 1; }

# ---- 1. Python ----
say "检查 Python..."
PYTHON=""
for cand in python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [[ "$major" -ge 3 && "$minor" -ge 9 ]]; then
      PYTHON="$cand"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "找不到 Python 3.9+。装一下：brew install python@3.11"
ok "Python $($PYTHON --version | awk '{print $2}') ($PYTHON)"

# ---- 2. --clean ----
if [[ "$DO_CLEAN" -eq 1 && -d .venv ]]; then
  say "清掉旧 .venv..."
  rm -rf .venv
fi

# ---- 3. 创建 venv ----
if [[ ! -d .venv ]]; then
  say "创建 .venv..."
  "$PYTHON" -m venv .venv
fi
VENV=".venv"
ok ".venv 就绪"

# ---- 4. pip install ----
say "装依赖（requirements.txt）..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r requirements.txt --quiet
ok "依赖装好"

# ---- 5. sync.py ----
if [[ "$DO_SYNC" -eq 1 ]]; then
  if [[ -f tools/sync.py ]]; then
    say "跑 tools/sync.py（生成 docs/index.md + data/contests.json）..."
    if "$VENV/bin/python" tools/sync.py; then
      ok "sync.py 成功"
    else
      warn "sync.py 失败（可能是 contests.csv 缺/格式问题），先不阻塞你。手动跑：$VENV/bin/python tools/sync.py"
    fi
  fi
fi

# ---- 6. 完成提示 ----
echo
ok "${BOLD}环境就绪${OFF}"
echo
echo "${DIM}常用命令：${OFF}"
echo "  $VENV/bin/mkdocs serve                # 本地预览（http://127.0.0.1:8000）"
echo "  $VENV/bin/mkdocs gh-deploy --force --clean  # 部署到 GitHub Pages"
echo "  $VENV/bin/python tools/sync.py        # contests.csv → docs/"
echo "  make test                             # 跑全部测试"
echo
echo "${DIM}GitHub Token：${OFF}"
echo "  打开 http://127.0.0.1:8000/editor/?view=table"
echo "  展开底部「⚙ GitHub Token 配置」→ 粘 PAT → 保存"
echo "  建议接着点「🔒 用密码加密」（关闭浏览器后要密码解锁）"
echo

# ---- 7. --serve ----
if [[ "$DO_SERVE" -eq 1 ]]; then
  say "启动 mkdocs serve (Ctrl+C 退出)..."
  exec "$VENV/bin/mkdocs" serve
fi
