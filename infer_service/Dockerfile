FROM python:3.11-slim

# 服务器离线：wheel 与 ResNet18 权重都从构建上下文装，不联网
COPY wheels /tmp/wheels
RUN pip install --no-index --find-links=/tmp/wheels \
        torch torchvision flask pillow numpy boto3 \
    && rm -rf /tmp/wheels /root/.cache/pip

COPY resnet18-f37072fd.pth /weights/resnet18-f37072fd.pth
COPY infer_service.py /app/infer_service.py
WORKDIR /app

EXPOSE 9600
CMD ["python", "infer_service.py"]
