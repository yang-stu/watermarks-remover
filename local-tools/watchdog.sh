#!/usr/bin/env bash
# 看门狗：检查「UI -> API」整条链路，异常则重启服务
#
#   ./watchdog.sh          单次检查（适合 cron / launchd）
#   ./watchdog.sh --loop   常驻循环，每 60 秒检查一次（由 run.sh 用 detach.py 启动）
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$DIR/logs/watchdog.log"
STAMP_FILE="$DIR/logs/.last_restart"
MIN_INTERVAL=180   # 两次重启最小间隔（秒），避免异常时反复重启

mkdir -p "$DIR/logs"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

healthy() {
  curl -fs -m 5 -o /dev/null "http://127.0.0.1:8780/" || return 1
  curl -fs -m 8 "http://127.0.0.1:8780/api/health" 2>/dev/null | grep -q '"ok": *true' || return 1
  return 0
}

check_once() {
  healthy && return 0

  local now last
  now=$(date +%s)
  if [ -f "$STAMP_FILE" ]; then
    last=$(cat "$STAMP_FILE" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt $MIN_INTERVAL ]; then
      log "健康检查失败，但距上次重启不足 ${MIN_INTERVAL}s，跳过"
      return 0
    fi
  fi
  echo "$now" > "$STAMP_FILE"

  log "健康检查失败，开始恢复"

  local name pid pidf
  for name in server ui; do
    pidf="$DIR/logs/$name.pid"
    if [ -f "$pidf" ]; then
      pid=$(cat "$pidf" 2>/dev/null || true)
      [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null && log "已停止 $name (PID $pid)"
      rm -f "$pidf"
    fi
  done

  sleep 1
  local port
  for port in 8765 8780; do
    for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
      kill "$pid" 2>/dev/null && log "兜底停止端口 $port 的 PID $pid"
    done
  done
  sleep 1

  if NO_WATCHDOG=1 "$DIR/run.sh" api >/dev/null 2>&1 && NO_WATCHDOG=1 "$DIR/run.sh" ui >> "$LOG" 2>&1; then
    log "重启完成"
  else
    log "重启失败"
  fi
}

if [ "${1:-}" = "--loop" ]; then
  log "看门狗启动（每 60s 检查一次）"
  while :; do
    check_once
    sleep 60
  done
else
  check_once
fi
