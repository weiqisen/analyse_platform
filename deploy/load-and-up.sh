#!/usr/bin/env bash
# 在【离线新服务器】上执行：导入镜像 + 起服务。
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] 导入镜像 ..."
# 支持 .tar 和 .tar.gz（docker load 自动识别 gzip）
for t in images/*.tar images/*.tar.gz; do
  [ -e "$t" ] || continue
  echo "  load $t"
  docker load -i "$t"
done

echo "[2/4] 检查 .env（可选，没有也能起）..."
[ -f .env ] && echo "  用 .env 覆盖默认配置" || echo "  无 .env，用 compose 内置默认（含强口令）"

echo "[3/4] 检查数据库导入文件 ..."
if [ ! -f mysql-init/10-data.sql ]; then
  echo "  警告：未找到 mysql-init/10-data.sql，将以空库启动（app 自动建表，但没有现场配置）。"
fi

echo "[4/4] 启动 ..."
docker compose up -d
sleep 3
docker compose ps
echo
echo "完成。浏览器访问  http://<本机IP>:${APP_PUBLISH_PORT:-9573}/   默认 admin / admin123"
