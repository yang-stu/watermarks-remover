FROM python:3.13-slim

# nginx-light: 反向代理 + 可选 Basic Auth；bash: 入口脚本
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx-light bash \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

WORKDIR /app
COPY service ./service
COPY local-tools ./local-tools
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && python3 -m py_compile service/scripts/server.py local-tools/ui.py local-tools/video_meta.py

# 2G 内存机器：单文件上限降到 100MB（base64 后约 137MB，nginx 已相应放宽）
ENV WATERMARKS_MAX_INPUT_BYTES=104857600 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
