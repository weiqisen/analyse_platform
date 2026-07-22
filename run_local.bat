@echo off
REM 本地启动缺陷分析平台（连 65 上的 MySQL/MinIO，局域网可访问）
chcp 65001 >nul
set DB_HOST=10.10.96.65
set DB_PORT=3307
set DB_USER=root
set DB_PASSWORD=123456
set DB_NAME=analyse_platform
REM 外部系统地址（浏览器要能访问，用 IP）
set LS_URL=http://10.10.96.65:8080
set CS_URL=http://10.10.96.65:9700
set PORT=9573
echo 平台启动中... 本机及局域网访问 http://本机IP:9573
py -3.12 "%~dp0app\app.py"
