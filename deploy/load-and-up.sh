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

echo "[2/4] 检查 .env ..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  已从 .env.example 生成 .env —— 请先编辑 .env 填强口令(DB_PASSWORD/MQ_PASS/ADMIN_PASSWORD)"
  echo "  和地址(CS_URL/WORKFLOW_URL)，再重跑本脚本。生成口令： openssl rand -base64 24"
  exit 1
fi
if grep -q '<生成强口令>' .env; then
  echo "  ✗ .env 里还有未替换的 <生成强口令> 占位符，请先填成真实强口令再启动。"
  exit 1
fi

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
