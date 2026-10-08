#!/bin/sh
# 沙盒容器入口（FR-2.4.3 / FR-3.10）：
#   1. 首次启动安装 seed（幂等：已有状态跳过，重置走 reset 接口）
#   2. 登录 demo 用户签发配对码（token 落盘后重启免配对）
#   3. daemon 前台常驻（WS 出站连 cloud；写操作全在容器内虚拟目录上）
set -e

: "${SKILLOS_ROOT:=/runtime}"
: "${SKILLOS_CLOUD_HTTP:=http://cloud:8000}"
: "${SKILLOS_CLOUD_WS:=ws://cloud:8000/ws/agent}"
export SKILLOS_ROOT SKILLOS_SANDBOX=1

echo "[sandbox] install-seed (idempotent)"
python scripts/sandbox_bootstrap.py install-seed --root "$SKILLOS_ROOT" --seed /seed

TOKEN_FILE="$SKILLOS_ROOT/.skillos/token"
if [ -f "$TOKEN_FILE" ]; then
  echo "[sandbox] token 已存在，跳过配对"
  exec skillos daemon --cloud "$SKILLOS_CLOUD_WS"
fi

echo "[sandbox] pair with cloud"
PAIR_CODE=$(python scripts/sandbox_bootstrap.py pair --cloud "$SKILLOS_CLOUD_HTTP")
exec skillos daemon --cloud "$SKILLOS_CLOUD_WS" --pair "$PAIR_CODE"
