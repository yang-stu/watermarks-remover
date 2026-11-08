#!/usr/bin/env bash
# 容器入口：启动 API(8765) + UI(8780)（仅容器内部），nginx 对外暴露 8080
# 可选鉴权：设置环境变量 AUTH_USER / AUTH_PASS 后开启 HTTP Basic Auth
set -euo pipefail

API_HOST=127.0.0.1
API_PORT=8765
UI_PORT=8780
LISTEN_PORT="${LISTEN_PORT:-8080}"

health() {
  python3 - "$1" <<'EOF'
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=2)
except Exception:
    sys.exit(1)
EOF
}

echo "[entrypoint] 启动 API (:${API_PORT})..."
python3 service/scripts/server.py --host "$API_HOST" --port "$API_PORT" >/var/log/wm-api.log 2>&1 &
for _ in $(seq 1 40); do
  health "http://${API_HOST}:${API_PORT}/health" && break
  sleep 0.25
done
health "http://${API_HOST}:${API_PORT}/health" || { echo "API 启动失败："; tail -20 /var/log/wm-api.log; exit 1; }
echo "[entrypoint] API 就绪"

echo "[entrypoint] 启动 UI (:${UI_PORT})..."
python3 local-tools/ui.py --host 127.0.0.1 --port "$UI_PORT" \
  --target "http://${API_HOST}:${API_PORT}" >/var/log/wm-ui.log 2>&1 &
for _ in $(seq 1 40); do
  health "http://127.0.0.1:${UI_PORT}/" && break
  sleep 0.25
done
health "http://127.0.0.1:${UI_PORT}/" || { echo "UI 启动失败："; tail -20 /var/log/wm-ui.log; exit 1; }
echo "[entrypoint] UI 就绪"

# 生成 nginx 配置（heredoc 直接渲染，避免模板替换踩坑）
AUTH_LINES=""
if [ -n "${AUTH_USER:-}" ] && [ -n "${AUTH_PASS:-}" ]; then
  echo "[entrypoint] 已开启 Basic Auth (用户: ${AUTH_USER})"
  printf '%s:{PLAIN}%s\n' "$AUTH_USER" "$AUTH_PASS" > /etc/nginx/htpasswd
  AUTH_LINES=$'    auth_basic "watermarks-remover";\n    auth_basic_user_file /etc/nginx/htpasswd;\n'
else
  echo "[entrypoint] 未设置 AUTH_USER/AUTH_PASS，无鉴权模式"
fi

cat > /etc/nginx/conf.d/wm.conf <<EOF
server {
    listen ${LISTEN_PORT};
    server_name _;

    # 单文件 100MB 上限，base64 后约 137MB，放宽到 200m
    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:${UI_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_request_buffering off;
    }

${AUTH_LINES}}
EOF

echo "[entrypoint] 启动 nginx (:${LISTEN_PORT})..."
exec nginx -g "daemon off;"
