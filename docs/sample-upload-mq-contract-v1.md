# 样本上传 · RabbitMQ 消息契约

**文档版本：v1**（2026-07-21，含 unit_key 稳定标识 + 缺陷分类多选数组）

平台（缺陷标注页批量上传样本）与CubeStudio服务之间，通过 RabbitMQ 交换样本上传信息。

## RabbitMQ 连接
- 地址：`10.10.96.65:5672`（AMQP）
- Web 管理面板：`http://10.10.96.65:15672`
- 账号/密码：`admin` / `Hlxd@123456`
- vhost：`/`

## 交换机与队列
采用 **direct exchange**，请求/回执分两个队列，用 `msg_id` 关联。

| 用途 | Exchange | Routing Key / Queue | 方向 |
|---|---|---|---|
| 上传请求 | `defect.sample`（direct，持久化） | `sample.upload` | 平台 → CubeStudio |
| 处理回执 | `defect.sample`（同上） | `sample.upload.reply` | CubeStudio → 平台 |

- CubeStudio**消费** `sample.upload` 队列；处理完往 `sample.upload.reply` 发回执。
- 平台**消费** `sample.upload.reply` 队列，按 `msg_id` 更新上传状态。
- 消息 `delivery_mode=2`（持久化）。

## 两个ID的职责（重要）
- **`msg_id`**：一次上传消息的唯一ID，只用于**请求↔回执关联**（追踪本批状态 处理中→成功）。**每次上传都不同，别拿它当项目/数据集标识。**
- **`unit_key`**：**稳定的项目/数据集标识** = `项目编码_牌号编码_相机面编码`，统一大写、下划线连接（如 `XB_3302101_FRONT`）。同一个「牌号×相机面」建模单元无论上传多少次，`unit_key` 始终相同。（注意它与 MinIO 目录前缀 `XB/3302101/front/` 是两码事：前者是给 CubeStudio 当项目 key 的逻辑标识，后者是对象存储路径。）

> **CubeStudio 请按 `unit_key` upsert 项目**：已存在就把本次样本**追加**进去，不存在才新建。**切勿按 `msg_id` 建项目**，否则同一单元多次上传会被建成多个项目。
> 平台的标注链路（建标注项目的 REST 接口）会传**同一个 `unit_key`**，两条链路指向同一个 CubeStudio 项目。

## 上传请求消息（平台 → CubeStudio）
routing key `sample.upload`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",       // 本次上传唯一ID，仅做回执关联，回执需原样带回
  "unit_key": "XB_3302101_FRONT",      // 【项目标识】稳定不变(大写下划线)，CubeStudio 按此 upsert 项目
  "cs_project_id": "",                 // 若平台已知该单元的CubeStudio项目ID则带上(优先用它);为空表示尚未建立
  "project": "小包CCD",                 // 检测项目(中文名，给人看)
  "project_code": "XB",                 // 检测项目编码(用于路径)
  "brand": "玉溪（软）",                 // 牌号(中文名)
  "brand_code": "3302101",              // 牌号编码(用于路径)
  "face": "正面",                       // 相机面标准名(中文)
  "face_code": "front",                 // 相机面编码(用于路径)
  "class_ids": [12, 15],               // 【缺陷分类】平台维护的缺陷类别ID数组(可多选，label_classes.id)
  "class_names": ["侧面翘边", "商标歪斜"], // 【缺陷分类】缺陷类别名称数组，与 class_ids 一一对应
  "bucket": "defect-samples",           // MinIO 桶名
  "path": "XB/3302101/front/",          // MinIO 目录前缀=项目编码/牌号编码/相机面编码/
  "count": 12,                          // 本次上传图片数
  "images": [                           // 图片对象 key 列表(全编码路径，无中文)
    "XB/3302101/front/1737000000001.jpg",
    "XB/3302101/front/1737000000002.jpg"
  ],
  "minio": {                            // 便于CubeStudio直接读图
    "endpoint": "http://10.10.96.65:9000",
    "access_key": "minioadmin",
    "secret_key": "minioadmin123"
  },
  "ts": 1737000000                      // 秒级时间戳
}
```

## 处理回执消息（CubeStudio → 平台）
routing key `sample.upload.reply`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",   // 与请求相同
  "status": "ok",                  // ok=处理成功；error=失败
  "project_id": "cs-proj-1001",    // 【建议】本次 upsert 命中/新建的CubeStudio项目ID；平台会回填并在后续上传里带回(cs_project_id)
  "message": "已入库 12 张",        // 可选，展示给用户
  "ts": 1737000100
}
```

> **缺陷分类**：`class_ids/class_names` 是平台侧维护的缺陷类别（每个建模单元可绑多个，上传前在行编辑弹窗多选），**本批所有图片同属这些类别**。CubeStudio 可据此给样本打标签。

## 交互时序
1. 用户在缺陷标注页**选择缺陷分类**、批量选图 → 平台上传到 MinIO `bucket/path` 下
2. 平台发**上传请求**到 `sample.upload`，界面显示「处理中」
3. CubeStudio 按 `unit_key`（或平台已带的 `cs_project_id`）**找到或新建**唯一项目，把 `images` 追加进去；完成后发**回执**到 `sample.upload.reply`，带回 `project_id`
4. 平台消费回执，按 `msg_id` 匹配 → 界面显示「上传成功」（或失败），并把 `project_id` 回填到该建模单元

## 约定要点（请CubeStudio确认）
- 队列/exchange 名称如上，双方一致即可（可改，改完同步给我们）
- 回执必须原样带回 `msg_id`
- **项目按 `unit_key` upsert，不要按 `msg_id` 建项目**；回执尽量带回 `project_id`
- 图片已在 MinIO，CubeStudio用消息里的 `minio` 凭据 + `images` key 直接读，无需平台再传图
