#!/usr/bin/env bash
# 启动 watermarks-remover（纯标准库，无需 Docker）
#
# 用法：
#   ./run.sh              # 同时启动 API(8765) 和 网页界面(8780)
#   ./run.sh api          # 只启动 API
#   ./run.sh ui           # 只启动 网页界面
#
# 可用环境变量覆盖：
#   PYTHON=/usr/bin/python3   指定解释器
#   WATERMARKS_HOST / WATERMARKS_PORT      API 监听地址（默认 127.0.0.1:8765，仅本机；
#                                          页面通过 UI 的 /api 代理访问，无需暴露）
#   UI_HOST / UI_PORT                      界面监听地址（默认 0.0.0.0:8780，局域网可访问）
#
# 启动方式说明：用 local-tools/detach.py 做「双重 fork + setsid」，
# 新进程进入独立会话，不会被当前终端的进程组连带回收，关掉终端也能继续跑。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
LOG_DIR="$DIR/logs"
DETACH="$DIR/local-tools/detach.py"

API_HOST="${WATERMARKS_HOST:-127.0.0.1}"
API_PORT="${WATERMARKS_PORT:-8765}"
UI_HOST="${UI_HOST:-0.0.0.0}"
UI_PORT="${UI_PORT:-8780}"

TARGET="${1:-all}"
mkdir -p "$LOG_DIR"

die() { echo "错误：$*" >&2; exit 1; }

[ -f "$DETACH" ] || die "找不到 $DETACH"

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
  || die "需要 Python 3.10+，当前 $("$PY" --version 2>&1)"

# 已在运行则跳过；返回 0 表示已就绪
already_running() {
  local pidfile="$1" port="$2"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null && return 0
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  return 1
}

wait_http() {
  local url="$1" name="$2" log="$3"
  for _ in $(seq 1 40); do
    curl -fs -m 2 "$url" >/dev/null 2>&1 && { echo "  ✓ $name 就绪"; return 0; }
    sleep 0.25
  done
  echo "  ✗ $name 启动失败，日志尾部：" >&2
  tail -15 "$log" >&2 2>/dev/null || true
  return 1
}

start_api() {
  if already_running "$LOG_DIR/server.pid" "$API_PORT"; then
    echo "API 已在运行 (PID $(cat "$LOG_DIR/server.pid" 2>/dev/null))"
    return 0
  fi
  lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1 && die "端口 $API_PORT 已被其他进程占用"
  echo "启动 API -> http://$API_HOST:$API_PORT"
  "$PY" "$DETACH" --pid "$LOG_DIR/server.pid" --log "$LOG_DIR/server.log" -- \
    "$PY" "$DIR/service/scripts/server.py" --host "$API_HOST" --port "$API_PORT"
  wait_http "http://$API_HOST:$API_PORT/health" "API ($API_PORT)" "$LOG_DIR/server.log"
}

start_ui() {
  if already_running "$LOG_DIR/ui.pid" "$UI_PORT"; then
    echo "界面已在运行 (PID $(cat "$LOG_DIR/ui.pid" 2>/dev/null))"
    return 0
  fi
  lsof -nP -iTCP:"$UI_PORT" -sTCP:LISTEN >/dev/null 2>&1 && die "端口 $UI_PORT 已被其他进程占用"
  echo "启动 网页界面 -> http://$UI_HOST:$UI_PORT"
  "$PY" "$DETACH" --pid "$LOG_DIR/ui.pid" --log "$LOG_DIR/ui.log" -- \
    "$PY" "$DIR/local-tools/ui.py" --host "$UI_HOST" --port "$UI_PORT" \
    --target "http://$API_HOST:$API_PORT"
  wait_http "http://$UI_HOST:$UI_PORT/" "界面 ($UI_PORT)" "$LOG_DIR/ui.log"
}

case "$TARGET" in
  api) start_api ;;
  ui)  start_ui ;;
  all) start_api; start_ui ;;
  *)   die "未知参数 '$TARGET'，可用：api | ui | all" ;;
esac

echo
echo "完成。浏览器打开： http://$UI_HOST:$UI_PORT"
echo "日志： $LOG_DIR/"
