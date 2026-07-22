# 样本上传与推理就绪 · RabbitMQ 消息契约

**文档版本：v1.1**（2026-07-21）

相对 v1.0 增量：
- 视觉工作流去掉「结果展示」，**到模型推理结束**
- 新增 **推理服务就绪通知** `inference.ready`（CubeStudio → 业务平台），供业务系统调用推理

平台（缺陷标注页批量上传样本）与 CubeStudio 之间，通过 RabbitMQ 交换样本上传与推理服务信息。

## RabbitMQ 连接
- 地址：`10.10.96.65:5672`（AMQP）
- Web 管理面板：`http://10.10.96.65:15672`
- 账号/密码：`admin` / `Hlxd@123456`
- vhost：`/`

## 交换机与队列
采用 **direct exchange**，消息 `delivery_mode=2`（持久化）。

| 用途 | Exchange | Routing Key / Queue | 方向 |
|---|---|---|---|
| 上传请求 | `defect.sample`（direct，持久化） | `sample.upload` | 平台 → CubeStudio |
| 处理回执 | `defect.sample`（同上） | `sample.upload.reply` | CubeStudio → 平台 |
| **推理就绪通知** | `defect.sample`（同上） | **`inference.ready`** | **CubeStudio → 平台** |
| **推理信息回执** | `defect.sample`（同上） | **`inference.ready.reply`** | **平台 → CubeStudio** |

- CubeStudio **消费** `sample.upload`；处理完往 `sample.upload.reply` 发回执。
- 平台 **消费** `sample.upload.reply`，按 `msg_id` 更新上传状态。
- 平台 **消费** `inference.ready`，按 `unit_key` / `project_id` 绑定可调用的推理地址。
- CubeStudio 在「一键部署推理」成功后 **发布** `inference.ready`（不要求平台回执）。

## 两个 ID 的职责（重要）
- **`msg_id`**：一次上传消息的唯一 ID，只用于**请求↔回执关联**。**每次上传都不同，别拿它当项目/数据集标识。**
- **`unit_key`**：**稳定的项目/数据集标识** = `项目编码_牌号编码_相机面编码`，统一大写、下划线连接（如 `XB_3302101_RIGHT`）。同一个「牌号×相机面」建模单元无论上传多少次，`unit_key` 始终相同。

> **CubeStudio 请按 `unit_key` upsert 项目**：已存在就把本次样本**追加**进去，不存在才新建。**切勿按 `msg_id` 建项目**。
> `cs_project_id` 仅当与 `unit_key` 指向同一项目时才会采用；若平台误传（如总是 `cs-proj-1`），CubeStudio 会忽略并以 `unit_key` 为准。

---

## 1. 上传请求（平台 → CubeStudio）
routing key `sample.upload`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",
  "unit_key": "XB_3302101_FRONT",
  "cs_project_id": "",
  "project": "小包CCD",
  "project_code": "XB",
  "brand": "玉溪（软）",
  "brand_code": "3302101",
  "face": "正面",
  "face_code": "front",
  "class_ids": [1, 2, 3, 4],
  "class_names": ["侧面翘边", "内衬顶部质量缺陷", "内衬高于商标", "商标接头"],
  "bucket": "defect-samples",
  "path": "XB/3302101/front/",
  "count": 12,
  "images": [
    "XB/3302101/front/1737000000001.jpg",
    "XB/3302101/front/1737000000002.jpg"
  ],
  "minio": {
    "endpoint": "http://10.10.96.65:9000",
    "access_key": "minioadmin",
    "secret_key": "minioadmin123"
  },
  "ts": 1737000000
}
```

### 缺陷分类字段
| 字段 | 说明 |
|------|------|
| `class_ids` | 平台缺陷类别 ID 列表（`label_classes.id`），上传前必选 |
| `class_names` | 缺陷类别**中文名称**列表，与标注界面标签名一一对应 |

CubeStudio 行为：
1. 将 `class_names` **合并写入** `VisionProject.labels`（多次上传做并集，去掉占位 `object`）
2. 创建 / 更新 Label Studio 项目的 `label_config`
3. 工作流**不再需要**人工「标注名称设置」步骤

## 2. 上传处理回执（CubeStudio → 平台）
routing key `sample.upload.reply`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",
  "status": "ok",
  "project_id": "12",
  "unit_key": "XB_3302101_FRONT",
  "message": "新建项目；已入库/追加 12 张，导入标注 12 张；标签 侧面翘边,内衬顶部质量缺陷,内衬高于商标,商标接头",
  "ts": 1737000100
}
```

## 3. 推理服务就绪通知（CubeStudio → 平台）【v1.1 新增】
routing key **`inference.ready`**，在工作流「一键部署推理服务」成功（健康检查通过）后发送。

**同一 `unit_key` / 项目再次部署会复用推理服务记录并再次发送本消息**（字段刷新为最新 endpoint / model_path / version），平台应按 `unit_key` **upsert** 本地推理配置。

```json
{
  "event": "inference.ready",
  "event_id": "inf-12-1721558400",
  "status": "ok",
  "unit_key": "XB_3302101_RIGHT",
  "project_id": "12",
  "project_name": "xb-3302101-right",
  "project_label": "小包CCD-玉溪（软）-尾部",
  "inference_service_id": 6,
  "service_name": "vis-xb-3302101-right",
  "model_name": "vis_xb_3302101_right",
  "model_version": "v202607211846",
  "model_path": "/mnt/admin/vision/xb-3302101-right/models/best.pt",
  "model_status": "online",
  "endpoint": "http://10.10.52.127:8090",
  "health_url": "http://10.10.52.127:8090/health",
  "predict_url": "http://10.10.52.127:8090/labelstudio/predict",
  "task_type": "detection",
  "class_names": ["侧面翘边", "内衬顶部质量缺陷"],
  "ts": 1721558400
}
```

> **关于访问地址（重要）**  
> - MQ / 界面下发的 `endpoint` 必须是**运行 Host Agent 机器的局域网 IP**（如 `http://10.10.52.127:8090`），**不要**使用 `host.docker.internal`。  
> - `host.docker.internal` 仅对本机 Docker 容器有效，其他业务机无法解析，即便改 hosts 也容易踩坑（IP 变更、多网卡、防火墙）。  
> - CubeStudio 配置项：`LOCAL_INFERENCE_PUBLIC_URL`（默认示例 `http://10.10.52.127:8090`）。本机 IP 变化时请改此配置并重新「一键部署」以刷新 MQ 通知。  
> - 业务侧若仍想用域名，可自行在 hosts 映射 `任意别名 → 上述 IP`，但**推荐直接用 IP**。  
> - 需保证：8090 对业务网段可达，Windows 防火墙放行，推理进程监听 `0.0.0.0`（已默认如此）。

### 字段说明
| 字段 | 说明 |
|------|------|
| `event` | 固定 `inference.ready` |
| `event_id` | 本次通知唯一 ID（非项目 ID） |
| `unit_key` | 与上传消息一致的稳定建模单元标识，**业务侧主键** |
| `project_id` | CubeStudio 视觉项目 ID |
| `inference_service_id` | 推理服务表主键，可在「推理服务管理」中查看 |
| `service_name` | 服务名（如 `vis-xb-3302101-right`） |
| `endpoint` | 推理服务根地址（**局域网 IP**，业务系统直连） |
| `health_url` | `GET` 健康检查 |
| `predict_url` | `POST` 预测接口（Label Studio 兼容协议） |
| `model_path` | 当前加载的模型路径（容器内路径） |
| `task_type` | `detection` / `segmentation` / `classification` |
| `class_names` | 当前项目缺陷标签（若有） |

### 业务平台建议行为
1. 消费 `inference.ready`，按 `unit_key` 保存/更新推理调用配置
2. **尽快向 `inference.ready.reply` 发送回执**（CubeStudio 页面「信息接收状态」依赖此回执）
3. 调用前可先 `GET health_url` 确认在线
4. 推理请求打到 `predict_url`（具体 body 与现有 Label Studio ML Backend 协议一致）
5. **不依赖** CubeStudio 工作流「结果展示」页（该步骤已取消）

### 推理信息回执（平台 → CubeStudio）【必接】
routing key **`inference.ready.reply`**，收到 `inference.ready` 并落库后发送：

```json
{
  "event": "inference.ready.ack",
  "event_id": "inf-12-1721558400",
  "unit_key": "XB_3302101_RIGHT",
  "project_id": "12",
  "status": "ok",
  "message": "已接收",
  "ts": 1721558410
}
```

| 字段 | 说明 |
|------|------|
| `event_id` | 原样带回通知中的 `event_id`（推荐） |
| `project_id` / `unit_key` | 至少提供其一，用于定位视觉项目 |
| `status` | `ok` / `success` / `acked` / `received` 均视为接收成功 |

CubeStudio 收到回执后，工作流「部署摘要 → 信息接收状态」显示：**信息已发送并接收成功**。
页面提供「信息重发」：点击后 30 秒冷却；未回执时可再次发送。

---

## 交互时序

### 样本上传
1. 用户选择缺陷分类、批量选图 → 平台上传到 MinIO
2. 平台发上传请求（含 `class_names`）→ 界面「处理中」
3. CubeStudio 按 `unit_key` upsert 项目 → 同步标签 → 拉图 → 导入 LS → 回执带 `project_id`
4. 平台按 `msg_id` 更新状态，回填 `project_id`

### 训练与推理就绪（工作流）
1. 用户在 CubeStudio 完成标注 → 训练 → **一键部署推理**
2. CubeStudio 创建/复用 InferenceService、拉起本机推理、健康检查通过
3. CubeStudio 发布 `inference.ready` → 平台按 `unit_key` 更新可调用地址 → **回执 `inference.ready.reply`**
4. **流程结束**（无「结果展示」步骤）；用户可在页面「信息重发」再次通知

## 约定要点
- 上传回执必须原样带回 `msg_id`
- **项目按 `unit_key` upsert**；回执带回 `project_id`
- **标签按 `class_names` 自动配置**
- **推理配置按 `unit_key` upsert**；以最新一条 `inference.ready` 为准
- 图片用消息内 `minio` + `images` 直接读

---

## CubeStudio 实现说明

### 标签同步
- 入口：`myapp/utils/sample_upload_mq.py` → `apply_class_labels_to_project` / `parse_class_names`
- 写入：`VisionProject.labels` + `expand.class_names` / `expand.class_ids` / `expand.labels_from_mq`
- LS：新建走 `create_labelstudio_project`；已存在且标签变更走 `update_labelstudio_label_config`（PATCH）

### 推理就绪通知
- 发布：`myapp/utils/sample_upload_mq.py` → `publish_inference_ready` / `resend_inference_ready_notify`
- 回执消费：同文件 `process_inference_ready_ack`（队列 `inference.ready.reply`）
- 触发：一键部署成功，或页面「信息重发」
- 配置：`INFERENCE_READY_ROUTING_KEY`、`INFERENCE_READY_REPLY_ROUTING_KEY`、`LOCAL_INFERENCE_PUBLIC_URL`

### 工作流 UI
- 步骤：**图片标注 → 模型训练 → 模型推理**（已去掉「结果展示」）
- 标注子步骤已去掉「标注名称设置」
- 嵌入默认 `phase=annotate`

### 代码入口
- 消费者：`myapp/utils/sample_upload_mq.py`
- 标签 XML：`myapp/utils/vision_workflow.py` → `build_label_config` / `update_labelstudio_label_config`
- 前端：`AnnotationStep.tsx`、`InferenceStep.tsx`、`VisionProjectWizard.tsx`
