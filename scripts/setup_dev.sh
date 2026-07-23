#!/usr/bin/env bash
# setup_dev.sh — 一键搭建本地开发环境
#   1. 选择 Python 3.10~3.13（优先 python3.12；3.14 装不上 torch/sentence-transformers，直接拒绝）
#   2. 创建 .venv 并安装 requirements.txt
#   3. 补齐 .env（从 .env.example 复制）
#   4. 按序跑演示数据种子脚本（幂等，可重复执行）
#   5. 可选：启动 uvicorn 开发服务
#
# 用法：
#   bash scripts/setup_dev.sh              # 全流程（装环境 + 前端依赖 + 灌数据，不启动服务）
#   bash scripts/setup_dev.sh --serve      # 全流程 + 同时启动后端(8080)与前端(5173)，Ctrl+C 一起停
#   bash scripts/setup_dev.sh --skip-seed  # 只装环境，不灌数据
#   bash scripts/setup_dev.sh --tfidf      # 跳过 torch/sentence-transformers（装其余依赖，语义检索降级为 tfidf）
set -euo pipefail
cd "$(dirname "$0")/.."

SERVE=0
SKIP_SEED=0
TFIDF=0
for arg in "$@"; do
  case "$arg" in
    --serve)     SERVE=1 ;;
    --skip-seed) SKIP_SEED=1 ;;
    --tfidf)     TFIDF=1 ;;
    -h|--help)
      sed -n '1,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（用 --help 看用法）"; exit 1 ;;
  esac
done

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }

# ---------- 1. 选 Python ----------
pick_python() {
  for c in python3.12 python3.11 python3.10 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      case "$v" in
        3.10|3.11|3.12|3.13) echo "$c"; return 0 ;;
      esac
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  err "找不到 Python 3.10~3.13。请先安装：brew install python@3.12"
  exit 1
}
log "使用解释器: $PY ($("$PY" --version 2>&1))"

# ---------- 2. venv + 依赖 ----------
if [ ! -d .venv ]; then
  log "创建虚拟环境 .venv"
  "$PY" -m venv .venv
else
  log ".venv 已存在，跳过创建"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null

if [ "$TFIDF" -eq 1 ]; then
  log "TFIDF 模式：安装除 torch/sentence-transformers 外的依赖"
  grep -vE '^(sentence-transformers|scikit-learn)' requirements.txt > /tmp/rrp_requirements_light.txt
  pip install -r /tmp/rrp_requirements_light.txt
  export EMBEDDING_PROVIDER=tfidf
  log "已设 EMBEDDING_PROVIDER=tfidf（语义检索降级为关键词模式）"
else
  log "安装依赖（首次含 torch CPU 版，约 200MB+，请耐心）"
  pip install -r requirements.txt
fi

# ---------- 3. .env ----------
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  log "已从 .env.example 生成 .env（如需切换 LLM/嵌入服务请自行修改）"
fi

# ---------- 3.5. 前端依赖 ----------
if command -v npm >/dev/null 2>&1; then
  if [ ! -d frontend/node_modules ]; then
    log "首次安装前端依赖（frontend/npm install）"
    (cd frontend && npm install)
  else
    log "frontend/node_modules 已存在，跳过 npm install"
  fi
else
  err "未找到 npm，跳过前端依赖安装（前端将无法启动；请安装 Node.js 18+ 后重跑）"
fi

# ---------- 4. 种子数据（幂等） ----------
if [ "$SKIP_SEED" -eq 0 ]; then
  for s in seed_tenants seed_regulations seed_report_packs seed_terms seed_demo_extra; do
    log "灌入演示数据: scripts/${s}.py"
    python "scripts/${s}.py"
  done
else
  log "--skip-seed：跳过演示数据"
fi

log "✅ 环境就绪。激活方式: source .venv/bin/activate"
log "   启动服务: bash scripts/setup_dev.sh --serve（前后端一起起）"
log "   前端入口: http://localhost:5173  后端API: http://localhost:8080/docs"
log "   登录账号: admin / Admin@1234（T001+T002）, zhangsan / Zhangsan@1234（仅 T001）"
log "   走查清单: docs/端到端演示走查清单.md"

# ---------- 5. 可选启动（前后端一起） ----------
if [ "$SERVE" -eq 1 ]; then
  if ! command -v npm >/dev/null 2>&1 || [ ! -d frontend/node_modules ]; then
    err "前端依赖不可用，仅启动后端（8080）"
    exec uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload
  fi
  log "启动后端 uvicorn（8080）+ 前端 Vite（5173），Ctrl+C 同时停止两者"
  uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload &
  UV_PID=$!
  trap 'kill $UV_PID 2>/dev/null' EXIT INT TERM
  cd frontend && exec npm run dev
fi
