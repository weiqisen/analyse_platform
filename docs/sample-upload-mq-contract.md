# 样本上传 · RabbitMQ 消息契约

平台（缺陷标注页批量上传样本）与算法侧服务之间，通过 RabbitMQ 交换样本上传信息。

## RabbitMQ 连接
- 地址：`10.10.96.65:5672`（AMQP）
- Web 管理面板：`http://10.10.96.65:15672`
- 账号/密码：`admin` / `Hlxd@123456`
- vhost：`/`

## 交换机与队列
采用 **direct exchange**，请求/回执分两个队列，用 `msg_id` 关联。

| 用途 | Exchange | Routing Key / Queue | 方向 |
|---|---|---|---|
| 上传请求 | `defect.sample`（direct，持久化） | `sample.upload` | 平台 → 算法侧 |
| 处理回执 | `defect.sample`（同上） | `sample.upload.reply` | 算法侧 → 平台 |

- 算法侧**消费** `sample.upload` 队列；处理完往 `sample.upload.reply` 发回执。
- 平台**消费** `sample.upload.reply` 队列，按 `msg_id` 更新上传状态。
- 消息 `delivery_mode=2`（持久化）。

## 上传请求消息（平台 → 算法侧）
routing key `sample.upload`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",       // 唯一ID，回执需原样带回
  "project": "小包CCD",                 // 检测项目
  "project_code": "JC003",
  "brand": "玉溪（软）",                 // 牌号
  "face": "正面",                       // 相机面标准名
  "face_code": "front",                 // 相机面英文编码
  "bucket": "defect-samples",           // MinIO 桶名
  "path": "小包CCD/玉溪（软）/front/",   // MinIO 目录前缀（图片都在此下）
  "count": 12,                          // 本次上传图片数
  "images": [                           // 图片对象 key 列表
    "小包CCD/玉溪（软）/front/1737000000001.jpg",
    "小包CCD/玉溪（软）/front/1737000000002.jpg"
  ],
  "minio": {                            // 便于算法侧直接读图
    "endpoint": "http://10.10.96.65:9000",
    "access_key": "minioadmin",
    "secret_key": "minioadmin123"
  },
  "ts": 1737000000                      // 秒级时间戳
}
```

## 处理回执消息（算法侧 → 平台）
routing key `sample.upload.reply`，JSON：
```json
{
  "msg_id": "u-20260721-abc123",   // 与请求相同
  "status": "ok",                  // ok=处理成功；error=失败
  "message": "已入库 12 张",        // 可选，展示给用户
  "ts": 1737000100
}
```

## 交互时序
1. 用户在缺陷标注页批量选图 → 平台上传到 MinIO `bucket/path` 下
2. 平台发**上传请求**到 `sample.upload`，界面显示「处理中」
3. 算法侧消费请求、处理（读图入库等），完成后发**回执**到 `sample.upload.reply`
4. 平台消费回执，按 `msg_id` 匹配 → 界面显示「上传成功」（或失败）

## 约定要点（请算法侧确认）
- 队列/exchange 名称如上，双方一致即可（可改，改完同步给我们）
- 回执必须原样带回 `msg_id`
- 图片已在 MinIO，算法侧用消息里的 `minio` 凭据 + `images` key 直接读，无需平台再传图
