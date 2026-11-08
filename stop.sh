#!/usr/bin/env bash
# 停止 watermarks-remover
#
# 用法：
#   ./stop.sh          # 停止 API(8765) 和 网页界面(8780)
#   ./stop.sh api      # 只停 API
#   ./stop.sh ui       # 只停 网页界面
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$DIR/logs"
TARGET="${1:-all}"

stop_one() {
  local pidfile="$1" pattern="$2" name="$3"

  if [ -f "$pidfile" ]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 25); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "  $name 优雅停止超时，强制终止"
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo "  $name 已停止 (PID $pid)"
      rm -f "$pidfile"
      return 0
    fi
    rm -f "$pidfile"
  fi

  # 没有 pidfile 就按进程名兜底
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    pkill -f "$pattern" 2>/dev/null || true
    echo "  $name 已按进程名停止"
  else
    echo "  $name 未在运行"
  fi
}

case "$TARGET" in
  api) stop_one "$LOG_DIR/server.pid" "service/scripts/server.py" "API" ;;
  ui)  stop_one "$LOG_DIR/ui.pid"     "local-tools/ui.py"          "界面" ;;
  all) stop_one "$LOG_DIR/ui.pid"     "local-tools/ui.py"          "界面"
       stop_one "$LOG_DIR/server.pid" "service/scripts/server.py" "API" ;;
  *)   echo "未知参数 '$TARGET'，可用：api | ui | all" >&2; exit 1 ;;
esac
