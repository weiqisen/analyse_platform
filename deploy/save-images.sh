#!/usr/bin/env bash
# 在【有 docker + 外网】的机器上执行：构建应用镜像、拉依赖镜像，全部导出到 images/。
# 产物 images/*.tar 连同整个 deploy/ 目录拷到离线新服务器。
set -euo pipefail
cd "$(dirname "$0")"

MYSQL_IMG="mysql:8.0.33"
MQ_IMG="rabbitmq:3.12-management"
APP_IMG="analyse-platform-app:latest"

mkdir -p images

echo "[1/4] 构建应用镜像 $APP_IMG ..."
docker build -f app.Dockerfile -t "$APP_IMG" ..

echo "[2/4] 拉取依赖镜像 ..."
docker pull "$MYSQL_IMG"
docker pull "$MQ_IMG"

echo "[3/4] 导出镜像到 images/ ..."
docker save "$APP_IMG"   -o images/app.tar
docker save "$MYSQL_IMG" -o images/mysql.tar
docker save "$MQ_IMG"    -o images/rabbitmq.tar

echo "[4/4] 完成。"
ls -lh images/
echo
echo "下一步：把整个 deploy/ 目录（含 images/ 和 mysql-init/10-data.sql）拷到新服务器，"
echo "在新服务器执行  bash load-and-up.sh"
