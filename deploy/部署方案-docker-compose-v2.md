# 缺陷图片分析平台 · docker-compose 部署方案 v2

**面向：现场新服务器（内网、离线）。**
一句话流程：在有网机器把镜像和数据打包进 `deploy/` → 整包拷到新服务器 → 填强口令 → 一条命令起服务。

> v2 相对 v1：强口令强制化（未设口令直接启动失败）、默认收敛对外端口、新增「生产环境安全」章节。

---

## 1. 组件与边界

| 组件 | 怎么来 | 说明 |
|---|---|---|
| **app**（本平台） | 本编排自建镜像 | Flask，端口 9573 |
| **mysql** | 本编排（mysql:8.0.33） | 容器内自建，首启自动导入 65 的数据 |
| **rabbitmq** | 本编排（rabbitmq:3.13-management） | 5672/15672，CubeStudio 也连它 |
| **MinIO** | 现场已有 | 不在编排内，地址存数据库随 dump 带过来 |
| **CubeStudio** | 另一台机器 | 外部，用 `CS_URL`/`WORKFLOW_URL` 指过去 |

> Label Studio 已从平台下线，不再部署。标注/训练/部署推理全在 CubeStudio 工作流里完成，
> 平台只做样本上传（MQ）+ 消费 `inference.ready` 拿推理地址。

## 2. 目录

```
deploy/
├── docker-compose.yml          # 编排（app + mysql + rabbitmq）
├── app.Dockerfile              # 应用镜像
├── .env.example                # 配置模板 → 复制成 .env 按现场改
├── save-images.sh              # 【有网机器】构建+导出镜像到 images/
├── dump-db.sh                  # 【现 65】导出数据库到 mysql-init/10-data.sql
├── load-and-up.sh              # 【新服务器】导入镜像 + 起服务
├── images/                     # 导出的镜像 tar（save-images.sh 生成）
└── mysql-init/                 # 10-data.sql 放这里，mysql 首启自动导入
```

## 3. 前置条件（新服务器）

- 已装 Docker（≥ 20.10）与 Docker Compose v2（`docker compose version` 能用）。
- 放行端口：9573（平台）、5672/15672（MQ）、按需 3307（MySQL 外部工具）。
- 能访问现场 MinIO 的 IP:9000，以及 CubeStudio 所在机器。

## 4. 打包（有网机器，一次）

```bash
cd deploy
bash save-images.sh          # 构建 app 镜像 + 拉 mysql/rabbitmq + docker save 到 images/
```

在现 65 上导出数据库（把真实配置一起带走）：

```bash
# 65 上执行，MYSQL_CONTAINER 按 docker ps 里的实际名改
MYSQL_CONTAINER=mysql_container bash deploy/dump-db.sh
```

> 图片在 MinIO、标注在 CubeStudio 侧，都不在 dump 里，按现场重新采集/对接。

## 5. 部署（新服务器）

把整个 `deploy/` 目录（含 `images/*.tar` 和 `mysql-init/10-data.sql`）拷过去，然后：

```bash
cd deploy
cp .env.example .env
vim .env                     # 填强口令(DB_PASSWORD/MQ_PASS/ADMIN_PASSWORD)+地址(CS_URL/WORKFLOW_URL)
bash load-and-up.sh          # 导入镜像 + docker compose up -d
```

- 强口令生成：`openssl rand -base64 24`。
- **未填口令会直接启动失败**（compose 用 `${VAR:?}` 强校验），这是刻意的，防弱默认口令漏进生产。
- 访问 `http://<新服务器IP>:9573/`。空库首次登录用 `admin` + 你设的 `ADMIN_PASSWORD`；
  若用了 65 的数据 dump，则沿用原口令，**登录后立刻到「用户管理」改强口令**。

## 6. 起来之后

1. **确认 MinIO 地址**：登录 → 基础配置 → 采集配置 → 数据源，若现场 MinIO IP 与 65 不同，改成现场地址。
2. **确认 CubeStudio 连通**：模型页看是否收到 `inference.ready`（MQ 里 CubeStudio 发过来的推理服务通知）。
3. **机台配置**：基础配置 → 机组/产线，给每台机配实时图像目录、勾检测项目、开工作状态。

## 7. 运维

```bash
docker compose ps                      # 看状态
docker compose logs -f app             # 看应用日志（或挂载卷 app-logs）
docker compose restart app             # 只重启应用
docker compose down                    # 停（保留数据卷）
docker compose up -d                   # 起
```

- 数据持久化在命名卷：`mysql-data` / `rabbitmq-data` / `app-logs`。`down` 不删卷，`down -v` 才删。
- **只更新应用代码**：重跑 `save-images.sh` 生成新 `images/app.tar` → 新服务器 `docker load -i images/app.tar` → `docker compose up -d app`。数据库不动。

## 8. 生产环境安全（强口令）

**口令分两类：我方部署时自己设的（必须设强），和连外部系统必须对齐的（由对方定）。**

| 口令 | 归属 | 要求 |
|---|---|---|
| `DB_PASSWORD` | 我方（容器内 MySQL root） | **设强**。`${DB_PASSWORD:?}`，不设则启动失败 |
| `MQ_USER` / `MQ_PASS` | 我方（容器内 RabbitMQ） | **设强**，且**同步给 CubeStudio**（两边要一致，否则收不到推理通知） |
| `ADMIN_PASSWORD` | 我方（平台管理员） | 空库首建时**设强**；迁数据时登录后在 UI 改 |
| `WS_USER` / `WS_PASS` | 外部（现场工控机 SFTP） | 由工控机决定，不是我方能自定义的；没有工控机数据源可留空 |
| MinIO AccessKey/SecretKey | 外部（现场 MinIO） | 在「采集配置 → 数据源」里配，用现场**单独创建的强密钥**，别用 minioadmin 默认 |

**网络暴露收敛（已在 compose 里默认处理）：**
- MySQL 端口**不对外发布**——app 走容器内网 `mysql:3306`，宿主/局域网不需要连库。确需外部工具时再放开 compose 里注释的 `ports`。
- RabbitMQ 管理面板 15672 **只绑 `127.0.0.1`**，不暴露到局域网；要远程看改成 `15672:15672`。
- AMQP 5672 必须发布（CubeStudio 要连）；平台 9573 必须发布（浏览器访问）。

**其它：**
- `.env` 含明文口令，已在 `.gitignore` 里，**不要提交、不要外发**。权限收紧：`chmod 600 .env`。
- 首次上线后确认 65 dump 带来的 `admin` 口令已改、演示账号 `zhangsan` 若不需要则停用/删除。

## 9. 已知注意点

- **app 固定单 worker**（gunicorn `-w 1`）：模块加载时起了一个 MQ 消费者线程，多 worker 会重复消费。并发靠线程（`--threads 8`），内网够用。
- **首次启动顺序**：app 用 `depends_on: service_healthy` 等 mysql/rabbitmq 就绪；mysql 只在**空库**时才导入 `mysql-init/*.sql`，已有数据卷不会重复导。
- **MQ 账号**：`.env` 里的 `MQ_USER/MQ_PASS` 要和 CubeStudio 侧约定一致，否则收不到 `inference.ready`。
- **时区**：容器默认 UTC，工单/排程按本地时刻判断。若现场对时间敏感，给 mysql 和 app 服务加 `TZ=Asia/Shanghai` 环境变量（可在 .env 和 compose 里补）。
