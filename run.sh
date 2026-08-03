#!/usr/bin/env bash
# 后厨管家 AI 问数服务 · 启动脚本（零依赖，仅需 Python 3.8+）
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi

echo "启动前请确认已创建 .env（可复制 .env.example）。"
"$PY" app.py "$@"
