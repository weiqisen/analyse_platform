# 缺陷图片分析平台 —— 应用镜像
# 构建上下文是仓库根目录：  docker build -f deploy/app.Dockerfile -t analyse-platform-app:latest .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用层缓存（改代码不重装包）
COPY app/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# 应用源码
COPY app/ /app/
RUN mkdir -p /app/logs

EXPOSE 9573

# 单 worker + 多线程：app.py 在模块加载时起了一个 MQ 消费者线程和按需的分析工作线程，
# 多 worker 会重复消费 MQ，故固定 -w 1，用线程扛并发。
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "8", \
     "--timeout", "300", "-b", "0.0.0.0:9573", "app:app"]
