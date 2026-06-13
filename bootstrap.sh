#!/usr/bin/env bash
# bootstrap.sh — 一键环境搭建
#
# 用法：
#   ./bootstrap.sh                # 装 venv + pip install + 跑 sync.py + 装 wiki CLI
#   ./bootstrap.sh --serve        # 上面 + mkdocs serve
#   ./bootstrap.sh --no-sync      # 跳过 sync.py
#   ./bootstrap.sh --no-cli       # 跳过装 wiki wrapper
#   ./bootstrap.sh --clean        # 删 .venv 重建
#
# 不会触碰 GitHub PAT —— token 在浏览器里填（带密码加密，Dev #4 改的）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- 解析参数 ----
DO_SERVE=0
DO_SYNC=1
DO_CLI=1
DO_CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --serve)    DO_SERVE=1 ;;
    --no-sync)  DO_SYNC=0 ;;
    --no-cli)    DO_CLI=0 ;;
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

# ---- 6. 装 wiki CLI wrapper (bin/wiki) ----
install_cli_wrapper() {
    local src="$REPO_ROOT/bin/wiki"
    local dst_dir="$HOME/.local/bin"
    local dst="$dst_dir/wiki"

    if [[ ! -x "$src" ]]; then
        warn "bin/wiki 不存在或不可执行, 跳过"
        return
    fi
    mkdir -p "$dst_dir"
    ln -sf "$src" "$dst"

    # 提示用户把 ~/.local/bin 加到 PATH (如果没有)
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$dst_dir"; then
        warn "$dst_dir 不在 PATH, 加到 ~/.zshrc 或 ~/.bashrc:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
    ok "wiki CLI 已装到 $dst"
}

if [[ "$DO_CLI" -eq 1 ]]; then
    say "装 wiki CLI wrapper..."
    install_cli_wrapper
fi

# ---- 7. 完成提示 ----
echo
ok "${BOLD}环境就绪${OFF}"
echo
echo "${DIM}常用命令：${OFF}"
echo "  wiki doctor                          # 健康检查 (会自动起后端)"
echo "  wiki list                            # 列比赛"
echo "  wiki update 2564                     # 导入 QOJ 一场比赛"
echo "  wiki upsolve 2025-icpc-xxx          # 检测补题"
echo "  wiki serve                           # 前台启动后端 (Ctrl+C 退出)"
echo
echo "${DIM}配置位置：${OFF}"
echo "  ~/.config/wiki/cookies/qoj.txt       # QOJ cookie jar"
echo "  ~/.config/wiki/watchlist.txt         # 关注用户"
echo "  ~/.local/share/wiki/wiki.log         # 后端日志"
echo
echo "${DIM}GitHub Token：${OFF}"
echo "  打开 http://127.0.0.1:8001/healthz 检查, 或编辑 ~/.config/wiki/config.json 加 GH token"
echo

# ---- 8. --serve ----
if [[ "$DO_SERVE" -eq 1 ]]; then
    say "启动 mkdocs serve (Ctrl+C 退出)..."
    exec "$VENV/bin/mkdocs" serve
fi
