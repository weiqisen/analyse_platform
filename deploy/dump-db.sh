#!/usr/bin/env bash
# 在【现 65 服务器】上执行：把当前 MySQL 数据导出到 deploy/mysql-init/10-data.sql。
# 新服务器 mysql 容器首次启动（空库）会自动执行该文件，把车间/机台/牌号/相机面/工单/
# 推理服务等真实配置一并带过去。仅导数据，不导 Label Studio（已下线）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p mysql-init

# 65 上 MySQL 跑在容器里。按实际容器名改（docker ps | grep mysql 查）。
MYSQL_CONTAINER="${MYSQL_CONTAINER:-mysql_container}"
DB_NAME="${DB_NAME:-analyse_platform}"
DB_PASS="${DB_PASS:-123456}"

echo "从容器 $MYSQL_CONTAINER 导出库 $DB_NAME ..."
docker exec "$MYSQL_CONTAINER" sh -c \
  "exec mysqldump -uroot -p'$DB_PASS' --no-tablespaces --single-transaction \
   --default-character-set=utf8mb4 --set-gtid-purged=OFF $DB_NAME" \
  > mysql-init/10-data.sql

echo "已导出 mysql-init/10-data.sql（$(wc -l < mysql-init/10-data.sql) 行，$(du -h mysql-init/10-data.sql | cut -f1)）"
echo "提示：图片(MinIO)与标注(CubeStudio侧)不在此文件内，按现场重新采集/对接。"
