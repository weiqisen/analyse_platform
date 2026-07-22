# 推理预测 API 说明

**文档版本：v1.0**（2026-07-22）  
配套消息契约：[`sample-upload-mq-contract-v1.1.md`](./sample-upload-mq-contract-v1.1.md)

业务系统在收到 RabbitMQ `inference.ready` 通知后，使用消息中的地址调用推理服务。

| 字段（来自 MQ） | 典型值 | 用途 |
|---|---|---|
| `endpoint` | `http://10.10.52.127:8090` | 服务根地址 |
| `health_url` | `http://10.10.52.127:8090/health` | 健康检查 |
| `predict_url` | `http://10.10.52.127:8090/labelstudio/predict` | **目标检测预测** |
| `model_path` | `/mnt/admin/vision/xb-3302101-right/models/best.pt` | 推荐随请求传入 |

> IP 以实际 MQ 消息为准（由 CubeStudio 配置 `LOCAL_INFERENCE_PUBLIC_URL` 决定）。请保证业务机到该 IP:8090 网络可达。

---

## 1. 健康检查

### `GET /health`

确认推理进程在线（建议调用预测前先检查）。

**请求示例**

```bash
curl -s http://10.10.52.127:8090/health
```

**响应示例**

```json
{
  "status": "UP",
  "default_model": "/data/k8s/kubeflow/pipeline/workspace/admin/vision/xb-3302101-right/models/best.pt",
  "cached_models": 1
}
```

| 字段 | 说明 |
|------|------|
| `status` | `UP` 表示服务可用 |
| `default_model` | 当前默认权重路径（一键部署/热加载后写入） |
| `cached_models` | 已缓存模型数量 |

---

## 2. 预测接口（核心）

### `POST /labelstudio/predict`

- Content-Type: `application/json`
- 超时建议：单张图 **30–120s**（首次加载模型更慢，建议 **300s**）
- 协议：兼容 Label Studio ML Backend 的预测结果格式

### 请求体

```json
{
  "model_path": "/mnt/admin/vision/xb-3302101-right/models/best.pt",
  "label_config": "<View><Image name=\"image\" value=\"$image\"/><RectangleLabels name=\"label\" toName=\"image\"><Label value=\"内衬顶部质量缺陷\"/><Label value=\"商标纸歪斜\"/></RectangleLabels></View>",
  "tasks": [
    {
      "data": {
        "image": "http://10.10.96.65:9000/defect-samples/XB/3302101/right/1737000000001.jpg"
      }
    }
  ]
}
```

#### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `model_path` | 强烈建议 | 模型权重路径。**优先使用 MQ 消息中的 `model_path`**。若省略且服务端已设置默认模型，也可推理；跨项目调用时务必显式传入。 |
| `tasks` | 是 | 任务列表，可批量（建议单次 1–8 张，避免超时） |
| `tasks[].data.image` | 是 | 图片路径，见下方「图片路径约定」 |
| `label_config` | 推荐 | Label Studio 风格 XML。用于：① 决定输出标签名；② `cls_id` 与中文类名映射。不传则使用模型内置英文/数字类名。 |

#### 图片路径约定（重要）

`tasks[].data.image` 支持以下写法：

| 写法 | 示例 | 说明 |
|------|------|------|
| **HTTP(S) URL（推荐业务侧）** | `http://10.10.96.65:9000/defect-samples/XB/3302101/right/xxx.jpg` | MinIO / 对象存储预签名或公开 URL；服务端**下载到临时文件 → 推理 → 自动删除** |
| 容器挂载路径 | `/mnt/admin/vision/项目名/images/xxx.jpg` | 与 MQ `model_path` 同前缀风格 |
| Label Studio local-files | `/labelstudio/data/local-files/?d=admin/vision/项目名/images/xxx.jpg` | `d=` 后为工作区相对路径 |
| 宿主机绝对路径 | `E:\...\workspace\admin\vision\...\xxx.jpg` | Windows 本机路径 |

**业务侧推荐：** 直接传 MinIO 可访问的完整 URL（需推理机能访问该地址；预签名 URL 注意有效期）。

URL 拉图行为：
1. 识别 `http://` / `https://`
2. 下载到临时目录（默认系统 temp/`cube-inference-url`）
3. 推理完成后**无论成功失败都会删除**临时文件
4. 下载超时默认 60s（环境变量 `INFERENCE_URL_TIMEOUT`）

> 不支持：业务机本地盘绝对路径（推理机读不到）、纯 base64（可另提需求）。

#### `label_config` 示例（检测框）

```xml
<View>
  <Image name="image" value="$image"/>
  <RectangleLabels name="label" toName="image">
    <Label value="内衬顶部质量缺陷"/>
    <Label value="内衬高于商标"/>
    <Label value="商标纸歪斜"/>
  </RectangleLabels>
</View>
```

- `RectangleLabels` 的子 `Label value` 顺序应与训练类别顺序一致时，映射最准确。  
- 类别名建议直接使用 MQ / 项目中的 `class_names`。

### 响应体

```json
{
  "model_version": "best.pt",
  "results": [
    {
      "score": 86,
      "model_version": "best.pt",
      "result": [
        {
          "original_width": 1920,
          "original_height": 1080,
          "image_rotation": 0,
          "from_name": "label",
          "to_name": "image",
          "type": "rectanglelabels",
          "origin": "prediction",
          "score": 91,
          "value": {
            "x": 12,
            "y": 34,
            "width": 20,
            "height": 15,
            "rotation": 0,
            "rectanglelabels": ["商标纸歪斜"]
          }
        }
      ]
    }
  ]
}
```

#### 响应字段

| 字段 | 说明 |
|------|------|
| `results` | 与请求 `tasks` **一一对应** |
| `results[].result` | 该图的检测框列表；无目标时为 `[]` |
| `results[].score` | 该图平均置信度 ×100（整数，约 0–100） |
| `result[].value.x/y/width/height` | **相对整图的百分比坐标**（0–100），左上角为原点 |
| `result[].value.rectanglelabels` | 缺陷类别中文名（或模型类名） |
| `result[].score` | 单框置信度 ×100 |
| `result[].original_width/height` | 原图像素宽高 |
| `error` | 仅在未配置模型等失败时出现（如 `"未配置模型路径"`） |

#### 坐标换算（像素）

```text
px_x      = x / 100 * original_width
px_y      = y / 100 * original_height
px_width  = width / 100 * original_width
px_height = height / 100 * original_height
```

---

## 3. 调用示例

### cURL

```bash
curl -s -X POST "http://10.10.52.127:8090/labelstudio/predict" \
  -H "Content-Type: application/json" \
  -d "{
    \"model_path\": \"/mnt/admin/vision/xb-3302101-right/models/best.pt\",
    \"label_config\": \"<View><Image name=\\\"image\\\" value=\\\"\\$image\\\"/><RectangleLabels name=\\\"label\\\" toName=\\\"image\\\"><Label value=\\\"商标纸歪斜\\\"/></RectangleLabels></View>\",
    \"tasks\": [
      {
        \"data\": {
          \"image\": \"http://10.10.96.65:9000/defect-samples/XB/3302101/right/1737000000001.jpg\"
        }
      }
    ]
  }"
```

### Python

```python
import requests

PREDICT_URL = "http://10.10.52.127:8090/labelstudio/predict"
MODEL_PATH = "/mnt/admin/vision/xb-3302101-right/models/best.pt"
LABEL_CONFIG = """
<View>
  <Image name="image" value="$image"/>
  <RectangleLabels name="label" toName="image">
    <Label value="内衬顶部质量缺陷"/>
    <Label value="商标纸歪斜"/>
  </RectangleLabels>
</View>
"""

payload = {
    "model_path": MODEL_PATH,
    "label_config": LABEL_CONFIG,
    "tasks": [
        {"data": {"image": "http://10.10.96.65:9000/defect-samples/XB/3302101/right/1737000000001.jpg"}},
    ],
}

# 建议：先健康检查
assert requests.get("http://10.10.52.127:8090/health", timeout=10).json().get("status") == "UP"

resp = requests.post(PREDICT_URL, json=payload, timeout=300)
resp.raise_for_status()
data = resp.json()

for i, item in enumerate(data.get("results") or []):
    boxes = item.get("result") or []
    print(f"task#{i}: {len(boxes)} boxes, score={item.get('score')}")
    for box in boxes:
        v = box["value"]
        print(
            v["rectanglelabels"][0],
            f"conf={box.get('score')}",
            f"xywh%={v['x']},{v['y']},{v['width']},{v['height']}",
        )
```

### 推荐调用流程（对接 MQ）

```text
1. 消费 inference.ready
2. 保存 unit_key → { endpoint, predict_url, model_path, class_names }
3. 回执 inference.ready.reply（status=ok）
4. 业务检品时：
   a. GET health_url
   b. 确保图片已在共享工作区（或使用已入库路径）
   c. POST predict_url（带 model_path + label_config + tasks）
   d. 解析 results[].result 中的框与类别
```

---

## 4. 可选：热加载模型

### `POST /load`

一般由 CubeStudio 一键部署自动调用；业务侧通常**不必**调用。

```json
{ "model_path": "/mnt/admin/vision/xb-3302101-right/models/best.pt" }
```

成功响应：`{"status":"ok","backend":"yolov8","model_path":"...","message":"模型已加载 (yolov8)"}`

---

## 5. 错误与排障

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 连接超时 / 拒绝 | 防火墙、IP 变更、8090 未启动 | 检查 `health`；确认 `LOCAL_INFERENCE_PUBLIC_URL`；放行 8090 |
| `results` 为空且无框 | 图中无目标，或图片/URL 拉失败 | 看推理机日志；核对 MinIO URL 是否可达、预签名是否过期 |
| URL 下载超时 | 推理机访问不到 MinIO / 网络慢 | 确认 `10.10.96.65:9000` 等对推理机可达；调大 `INFERENCE_URL_TIMEOUT` |
| `error: 未配置模型路径` | 未传 `model_path` 且无默认模型 | 使用 MQ 中的 `model_path` |
| 类别名不对 | 未传 `label_config` 或标签顺序不一致 | 按 `class_names` 组装 `label_config` |
| 首次很慢 | 冷启动加载 YOLO 权重 | 加大超时；或先调 `/load` 预热 |

---

## 6. 与 MQ 字段对应关系

| MQ `inference.ready` | 本 API |
|---|---|
| `predict_url` | 本页 `POST` 地址 |
| `health_url` | `GET /health` |
| `model_path` | 请求体 `model_path` |
| `class_names` | 用于拼 `label_config` 的 `<Label value="..."/>` |
| `unit_key` | 业务侧路由到哪套模型配置 |

---

## 8. 预测结果图上传 MinIO（defect-outputs）

预测成功后，服务端会：

1. 在原图上绘制检测框，生成 `*_pred.jpg`
2. 上传到 MinIO bucket **`defect-outputs`**
3. 用 `stat_object` **校验**上传成功；失败则最多重试 **3** 次
4. 3 次仍失败 → 写入数据库表 `vision_inference_output_exception`，可在 CubeStudio「模型推理 → 异常项查看」中批量重传

### 对象键规则
若源图为：
`http://10.10.96.65:9000/defect-samples/XB/3302101/right/a.jpg`  
则输出为：
`defect-outputs` / `XB/3302101/right/a_pred.jpg`

### 默认 MinIO
| 项 | 值 |
|----|----|
| endpoint | `http://10.10.96.65:9000` |
| access_key | `minioadmin` |
| secret_key | `minioadmin123` |
| bucket | `defect-outputs` |

可通过请求体字段 `minio` 覆盖，或环境变量 `DEFECT_OUTPUT_MINIO_*`。

### 请求扩展字段
| 字段 | 说明 |
|------|------|
| `upload_output` | 默认 `true`；设为 `false` 可跳过结果图上传 |
| `unit_key` | 可选，用于异常归属与路径兜底 |
| `project_id` | 可选，视觉项目 ID，写入异常表 |

### 响应中的 output
```json
{
  "results": [
    {
      "result": [ ... ],
      "score": 86,
      "output": {
        "uploaded": true,
        "bucket": "defect-outputs",
        "object_key": "XB/3302101/right/a_pred.jpg",
        "url": "http://10.10.96.65:9000/defect-outputs/XB/3302101/right/a_pred.jpg",
        "attempts": 1
      }
    }
  ],
  "outputs": [ ... ]
}
```

上传失败时 `uploaded=false`，并含 `error`；同时会尝试上报异常项。

---

## 9. 限制说明（当前版本）

1. 单进程本地 YOLO 推理（CPU），吞吐有限，请控制并发与批量大小。  
2. **推荐**传 MinIO HTTP(S) URL；本地 `/mnt/...` 路径仍兼容。URL 图会临时下载并在推理后清理。  
3. 检测输出为矩形框（`rectanglelabels`）；结果图上传到 `defect-outputs`。  
4. 无鉴权：请限制在内网访问，勿对公网裸露 8090。  
5. 业务调用建议在请求中带上 `project_id` / `unit_key`，便于异常项归属到正确视觉项目。
