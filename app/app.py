# -*- coding: utf-8 -*-
"""缺陷图片分析平台 — Flask 后端"""
import os
import gzip
import json
import shutil
import logging
import hashlib
import functools
import random
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import IntegrityError
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# MySQL 连接配置（可用环境变量覆盖）
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3307"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "123456")
DB_NAME = os.environ.get("DB_NAME", "analyse_platform")

# 工控机（模式二）SFTP 凭据（三台模拟环境统一，生产应存 terminal_config）
WS_USER = os.environ.get("WS_USER", "root")
WS_PASS = os.environ.get("WS_PASS", "hlxd@123")

# 外部系统集成参数的兜底默认值。运行期一律走 get_cfg()：优先读 integration_config 表
# （集成配置页可改、即时生效），表里没配才回落到这里的环境变量。
CFG_DEFAULTS = {
    "cs_url": os.environ.get("CS_URL", ""),                 # CubeStudio（预留）
    "cs_token": os.environ.get("CS_TOKEN", ""),
    "workflow_url": os.environ.get("WORKFLOW_URL", "http://10.10.52.127"),
    "mq_host": os.environ.get("MQ_HOST", "10.10.96.65"),
    "mq_port": os.environ.get("MQ_PORT", "5672"),
    "mq_user": os.environ.get("MQ_USER", "admin"),
    "mq_pass": os.environ.get("MQ_PASS", "Hlxd@123456"),
    "sample_bucket": os.environ.get("SAMPLE_BUCKET", "defect-samples"),
}
CFG_LABELS = [
    ("cs_url", "CubeStudio 地址", "预留，本机资源不足未部署"),
    ("cs_token", "CubeStudio Token", "预留"),
    ("workflow_url", "视觉工作流地址", "工作流平台基地址(仅IP:端口)。进入工作流时拼 /frontend/visionWorkflow/embed/{unit_key}?embed=1"),
    ("mq_host", "RabbitMQ 地址", "样本上传消息队列，如 10.10.96.65"),
    ("mq_port", "RabbitMQ 端口", "AMQP 端口，默认 5672"),
    ("mq_user", "RabbitMQ 账号", ""),
    ("mq_pass", "RabbitMQ 密码", ""),
    ("sample_bucket", "样本桶", "批量上传样本的 MinIO 桶名，不存在自动新建"),
]

_cfg_cache = {"t": 0.0, "d": {}}


def get_cfg(key, default=None):
    """读集成配置：integration_config 表优先，未配则回落 CFG_DEFAULTS（环境变量）。
    带 5 秒缓存，配置页改完刷新即生效，不用重启服务。"""
    import time
    now = time.time()
    if now - _cfg_cache["t"] > 5:
        try:
            rows = get_db().execute("SELECT cfg_key, cfg_value FROM integration_config").fetchall()
            _cfg_cache["d"] = {r["cfg_key"]: r["cfg_value"] for r in rows}
            _cfg_cache["t"] = now
        except Exception:
            pass  # 建表前/连不上库时回落默认值
    v = _cfg_cache["d"].get(key)
    return v if v else (CFG_DEFAULTS.get(key, "") if default is None else default)

app = Flask(__name__)
app.secret_key = "yancao-analyse-platform-secret-2026"


# ---------------------------------------------------------------- 日志
# 格式对齐统一日志平台（logback 风格）：时间(毫秒) [线程] 级别 logger - 消息
# 三路输出：控制台(journald 采集)、全量滚动文件、ERROR 单独文件；历史文件 gzip。
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(BASE_DIR, "logs"))
_LOG_FMT = "%(asctime)s.%(msecs)03d [%(threadName)s] %(levelname)s %(name)s - %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 100 * 1024 * 1024   # 单文件 100M
_BACKUPS = 50                    # ×100M ≈ 5G，对齐 totalSizeCap


def _gzip_namer(name):
    return name + ".gz"


def _gzip_rotator(source, dest):
    with open(source, "rb") as sf, gzip.open(dest, "wb") as df:
        shutil.copyfileobj(sf, df)
    os.remove(source)


def _rolling_handler(path, level):
    h = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATEFMT))
    h.namer = _gzip_namer
    h.rotator = _gzip_rotator
    return h


def setup_logging():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass
    fmt = logging.Formatter(_LOG_FMT, _LOG_DATEFMT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 重复初始化时先清掉旧 handler（reload/gunicorn 多 worker 场景）
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        root.addHandler(_rolling_handler(os.path.join(LOG_DIR, "app.log"), logging.INFO))
        root.addHandler(_rolling_handler(os.path.join(LOG_DIR, "error.log"), logging.ERROR))
    except Exception as _e:
        root.warning("文件日志初始化失败，仅控制台输出：%s", _e)
    # Flask/werkzeug 交给 root 统一输出
    app.logger.handlers = []
    app.logger.propagate = True
    app.logger.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # 屏蔽逐条请求行，保留告警
    # 三方库 INFO 太吵（尤其 pika 每次连接刷十几行），压到 WARNING 只留关键业务日志
    for _noisy in ("pika", "botocore", "boto3", "s3transfer", "urllib3", "paramiko"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


setup_logging()

# 检测项目改由 detect_items 表驱动，见 inject_globals()


# ---------------------------------------------------------------- 数据库
def _connect():
    return pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                           password=DB_PASSWORD, database=DB_NAME,
                           charset="utf8mb4", cursorclass=DictCursor,
                           autocommit=False)


class _DB:
    """兼容原 sqlite3 写法的薄封装：db.execute(sql, params) 返回游标，
    自动把 ? 占位符转换为 MySQL 的 %s。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        if params:
            cur.execute(sql.replace("?", "%s"), params)
        else:
            cur.execute(sql.replace("?", "%s"))
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    if "db" not in g:
        g.db = _DB(_connect())
    return g.db


def paginate(db, base_sql, params=(), page=1, size=10):
    """后端分页：对任意 SELECT 加 COUNT + LIMIT/OFFSET，返回 (rows, pg)。
    pg = {page,size,total,pages}，配合前端通用分页条 .pager 使用。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    total = db.execute("SELECT COUNT(*) AS c FROM (" + base_sql + ") _t", params).fetchone()["c"]
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    rows = db.execute(base_sql + " LIMIT %d OFFSET %d" % (size, (page - 1) * size), params).fetchall()
    return rows, {"page": page, "size": size, "total": total, "pages": pages}


def paginate_list(items, page=1, size=10):
    """对内存列表分页（用于聚合结果/S3 列表等非 SQL 数据）。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    return items[(page - 1) * size: page * size], {"page": page, "size": size, "total": total, "pages": pages}


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _has_column(db, table, column):
    r = db.execute("SELECT COUNT(*) AS c FROM information_schema.columns "
                   "WHERE table_schema=? AND table_name=? AND column_name=?",
                   (DB_NAME, table, column)).fetchone()
    return bool(r["c"])


def migrate_db(db):
    """给既有表补列。CREATE TABLE IF NOT EXISTS 只建新表不改老表，
    而 MySQL 8 的 ADD COLUMN 不支持 IF NOT EXISTS，只能先查 information_schema。"""
    adds = [
        # 检测项目升级为一等公民：自己挂外部系统项目 id
        ("detect_items", "cs_project_id", "VARCHAR(64) DEFAULT '' COMMENT '对应 CubeStudio 项目组(预留)'"),
        # 缺陷类别与标注图片归属到检测项目
        ("label_classes", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("label_images", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("inference_results", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        # 新架构推理：结果不再来自 label_images，直接记 机台/相机面/单元/对象key
        ("inference_results", "machine", "VARCHAR(64) DEFAULT '' COMMENT '机台/产线'"),
        ("inference_results", "face_name", "VARCHAR(64) DEFAULT '' COMMENT '相机面'"),
        ("inference_results", "unit_id", "INT DEFAULT 0 COMMENT '建模单元(model_units.id)'"),
        ("inference_results", "src_key", "VARCHAR(512) DEFAULT '' COMMENT '对象key'"),
        # 数据源独立化：产线引用某个数据源（多产线可共用）。storage_config/terminal_config
        # 退化为由数据源派生的按产线缓存（rebuild_line_cfg 重建），现有读代码不用改。
        ("prod_lines", "source_id", "INT DEFAULT 0 COMMENT '引用的数据源(data_sources.id)'"),
        # 样本上传：msg_id 只做消息关联，项目归属改用稳定的 unit_key（CubeStudio 按此 upsert，
        # 避免同一建模单元多次上传被建成多个项目）。
        ("sample_uploads", "unit_key", "VARCHAR(160) DEFAULT '' COMMENT '建模单元稳定标识=项目编码_牌号编码_相机面编码(大写)'"),
        ("sample_uploads", "unit_id", "INT DEFAULT 0 COMMENT '建模单元(model_units.id), 0=尚未建单元'"),
        ("sample_uploads", "cs_project_id", "VARCHAR(64) DEFAULT '' COMMENT 'CubeStudio项目ID(回执回填)'"),
        ("sample_uploads", "class_id", "INT DEFAULT 0 COMMENT '缺陷分类ID(label_classes.id), 上传必选'"),
        ("sample_uploads", "class_name", "VARCHAR(512) DEFAULT '' COMMENT '缺陷分类名称(可多个, / 分隔)'"),
        ("workshops", "path_alias", "VARCHAR(32) DEFAULT '' COMMENT 'MinIO路径别名, 如 jb1'"),
        ("prod_lines", "path_alias", "VARCHAR(32) DEFAULT '' COMMENT 'MinIO路径别名, 如 a01'"),
        # 实时图像目录挪到机台：前缀就是 车间别名/机台别名，检测项目段运行时按上下文追加。
        # 原先挂在 model_units 上，同一机台的 N牌号×6面 存着同一个字符串，改一次要改 6N 次。
        ("prod_lines", "rt_type", "VARCHAR(16) DEFAULT '' COMMENT '实时目录类型: minio/ftp'"),
        ("prod_lines", "rt_bucket", "VARCHAR(128) DEFAULT '' COMMENT '桶名(minio), 空=用数据源输入桶'"),
        ("prod_lines", "rt_path", "VARCHAR(512) DEFAULT '' COMMENT '实时目录前缀(车间别名/机台别名)'"),
        # 工单：班次只能到"半天"，且没法处理跨班/临时换牌号。图片文件名是毫秒时间戳，
        # 有了起止时间就能按图片时刻精确落到工单上，班次匹配退化为兜底。
        ("machine_schedule", "order_no", "VARCHAR(64) DEFAULT '' COMMENT '工单号'"),
        ("machine_schedule", "start_at", "DATETIME NULL COMMENT '工单开始时间'"),
        ("machine_schedule", "end_at", "DATETIME NULL COMMENT '工单结束时间'"),
        ("detect_items", "path_alias", "VARCHAR(64) DEFAULT '' COMMENT 'MinIO路径别名'"),
    ]
    added = set()
    for table, col, ddl in adds:
        if not _has_column(db, table, col):
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
            added.add(table)
    if added:
        db.commit()

    # 数据源独立化：首次把既有 storage_config/terminal_config 回填成 data_sources，
    # 并给产线设 source_id。按 (类型,地址,桶/目录) 去重 —— 共用一个服务端只建一个数据源。
    if "prod_lines" in added and not db.execute("SELECT COUNT(*) AS c FROM data_sources").fetchone()["c"]:
        seen = {}  # (type,addr,bucket_or_dir) -> data_source id
        for l in db.execute("SELECT * FROM prod_lines").fetchall():
            sc = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
            tc = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
            key = sid = None
            if sc and sc["server_addr"]:
                key = ("minio", sc["server_addr"], sc["in_bucket"] or "")
                if key not in seen:
                    seen[key] = db.execute(
                        "INSERT INTO data_sources(name,type,server_addr,username,password,in_bucket,out_bucket) "
                        "VALUES(?,?,?,?,?,?,?)",
                        ("MinIO %s" % sc["server_addr"], "minio", sc["server_addr"],
                         sc["username"], sc["password"], sc["in_bucket"], sc["out_bucket"] or "")).lastrowid
                sid = seen[key]
            elif tc and tc["sys_addr"]:
                key = ("ftp", tc["sys_addr"], tc["ng_dir"] or "")
                if key not in seen:
                    seen[key] = db.execute(
                        "INSERT INTO data_sources(name,type,server_addr,username,password,ng_dir) "
                        "VALUES(?,?,?,?,?,?)",
                        ("FTP %s" % tc["sys_addr"], "ftp", tc["sys_addr"],
                         WS_USER, WS_PASS, tc["ng_dir"])).lastrowid
                sid = seen[key]
            if sid:
                db.execute("UPDATE prod_lines SET source_id=? WHERE id=?", (sid, l["id"]))
        db.commit()

    # 实时目录与工作状态从建模单元下放到机台：analysis_jobs / analysis_logs 的主体
    # 从 unit_id 换成 (line_id, item_id)。两张表都是纯运行态数据，直接重建不迁移。
    if _has_column(db, "analysis_jobs", "unit_id"):
        db.execute("DROP TABLE IF EXISTS analysis_jobs")
        db.execute("DROP TABLE IF EXISTS analysis_logs")
        db.execute("""CREATE TABLE analysis_jobs(
            line_id INT NOT NULL COMMENT '机台(prod_lines.id)',
            item_id INT NOT NULL COMMENT '检测项目(detect_items.id)',
            status VARCHAR(16) DEFAULT 'idle' COMMENT 'idle/running/done/error/stopped',
            enabled INT DEFAULT 0 COMMENT '工作状态开关: 1开启 0关闭',
            total INT DEFAULT 0 COMMENT '目录总张数',
            read_cnt INT DEFAULT 0 COMMENT '已读取张数',
            analyzed INT DEFAULT 0 COMMENT '已分析(新推理入库)',
            skipped INT DEFAULT 0 COMMENT '已跳过(之前分析过)',
            msg VARCHAR(255) DEFAULT '' COMMENT '最新消息',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            PRIMARY KEY (line_id, item_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='实时目录分析任务(按 机台×检测项目)'""")
        db.execute("""CREATE TABLE analysis_logs(
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            line_id INT NOT NULL COMMENT '机台(prod_lines.id)',
            item_id INT NOT NULL COMMENT '检测项目(detect_items.id)',
            ts DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
            level VARCHAR(8) DEFAULT 'info' COMMENT 'info/ok/warn/error',
            src_key VARCHAR(512) DEFAULT '' COMMENT '图片key',
            detail VARCHAR(512) DEFAULT '' COMMENT '调用过程/结果',
            KEY idx_job_ts (line_id, item_id, id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='分析调用审计日志'""")
        db.commit()
        # 老的单元级实时目录配置搬到机台上（同机台多单元取第一条，本来就是同一份）
        if _has_column(db, "model_units", "rt_type"):
            for u in db.execute(
                    "SELECT * FROM model_units WHERE rt_type<>'' AND rt_line_id<>0").fetchall():
                # 单元里的 rt_path 是 车间/机台/检测项目 三段，机台上只留前两段
                prefix = "/".join((u["rt_path"] or "").strip("/").split("/")[:2])
                db.execute("UPDATE prod_lines SET rt_type=?,rt_bucket=?,rt_path=? "
                           "WHERE id=? AND rt_type=''",
                           (u["rt_type"], u["rt_bucket"] or "", prefix, u["rt_line_id"]))
            db.commit()

    # inference_results 换新架构：不再挂 label_images，image_id 应可空且去掉 uq_img
    # 唯一键。旧表 image_id NOT NULL + UNIQUE(image_id)，新流程不提供 image_id 会插入失败。
    if _has_column(db, "inference_results", "image_id"):
        col = db.execute("SELECT is_nullable AS n FROM information_schema.columns WHERE table_schema=? "
                         "AND table_name='inference_results' AND column_name='image_id'",
                         (DB_NAME,)).fetchone()
        if col and str(col["n"]).upper() == "NO":
            try:
                db.execute("ALTER TABLE inference_results DROP INDEX uq_img")
            except Exception:
                pass
            db.execute("ALTER TABLE inference_results MODIFY image_id INT NULL COMMENT '旧字段, 新架构不用'")
            db.commit()

    # 图像分类(相机面)归属检测项目：加 item_id，唯一键从 raw_name 改为 (item_id,raw_name)，
    # 现有数据迁到「小包CCD」。不同检测项目可各自配置相机面。
    xb = db.execute("SELECT id FROM detect_items WHERE short_name=? OR name=?",
                    ("小包CCD", "小包CCD检测")).fetchone()
    xb_id = xb["id"] if xb else 0
    if not _has_column(db, "camera_faces", "item_id"):
        db.execute("ALTER TABLE camera_faces ADD COLUMN item_id INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'")
        if xb_id:
            db.execute("UPDATE camera_faces SET item_id=? WHERE item_id=0", (xb_id,))
        try:
            db.execute("ALTER TABLE camera_faces DROP INDEX uq_raw")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE camera_faces ADD UNIQUE KEY uq_item_raw (item_id, raw_name)")
        except Exception:
            pass
        db.commit()

    # 相机面映射种子：现场用 is7600C_x 技术码，首次预置这 6 面（is7600C 机型），归到小包CCD
    if not db.execute("SELECT COUNT(*) AS c FROM camera_faces").fetchone()["c"]:
        seed_faces = [
            ("is7600C_D", "正面", "front", 1), ("is7600C_U", "反面", "back", 2),
            ("is7600C_L", "前部", "left", 3), ("is7600C_R", "尾部", "right", 4),
            ("is7600C_zuo", "六面相机左", "six_left", 5),
            ("is7600C_you", "六面相机右", "six_right", 6),
        ]
        for raw, name, code, order in seed_faces:
            db.execute("INSERT INTO camera_faces(item_id,raw_name,face_name,face_code,machine_model,sort_order) "
                       "VALUES(?,?,?,?,?,?)", (xb_id, raw, name, code, "is7600C", order))
        db.commit()

    # 一次性引导：把没归属的既有数据归到「小包CCD」项目下
    if xb_id:
        db.execute("UPDATE label_classes SET item_id=? WHERE item_id=0", (xb_id,))
        db.execute("UPDATE label_images SET item_id=? WHERE item_id=0", (xb_id,))
        db.commit()

    # unit_sample_class 唯一键从 (brand,face_id) 换成 (brand,face_id,class_id)，
    # 支持一个建模单元绑定多个缺陷分类（CubeStudio 多选需求）。
    if _has_column(db, "unit_sample_class", "class_id"):
        idxs = db.execute("SELECT index_name FROM information_schema.statistics "
                          "WHERE table_schema=? AND table_name='unit_sample_class' "
                          "AND index_name='uq_brand_face'", (DB_NAME,)).fetchall()
        if idxs:
            try:
                db.execute("ALTER TABLE unit_sample_class DROP INDEX uq_brand_face")
                db.execute("ALTER TABLE unit_sample_class ADD UNIQUE KEY uq_brand_face_class "
                           "(brand, face_id, class_id)")
                db.commit()
            except Exception:
                pass  # 可能已经切过了，忽略

    # sample_uploads.class_name 从 VARCHAR(64) 扩到 VARCHAR(512)，支持多分类拼接
    if _has_column(db, "sample_uploads", "class_name"):
        col = db.execute("SELECT character_maximum_length AS n FROM information_schema.columns "
                         "WHERE table_schema=? AND table_name='sample_uploads' AND column_name='class_name'",
                         (DB_NAME,)).fetchone()
        if col and col["n"] and col["n"] < 512:
            db.execute("ALTER TABLE sample_uploads MODIFY class_name VARCHAR(512) "
                       "DEFAULT '' COMMENT '缺陷分类名称(可多个, / 分隔)'")
            db.commit()


def init_db():
    db = _DB(_connect())
    ddl = [
        """CREATE TABLE IF NOT EXISTS users(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '用户ID',
            username VARCHAR(64) UNIQUE NOT NULL COMMENT '登录用户名',
            password VARCHAR(64) NOT NULL COMMENT '密码(MD5)',
            realname VARCHAR(64) DEFAULT '' COMMENT '真实姓名',
            role VARCHAR(16) DEFAULT 'user' COMMENT '角色: admin管理员 / user普通用户',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='用户表'""",
        """CREATE TABLE IF NOT EXISTS detect_items(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            code VARCHAR(64) NOT NULL COMMENT '项目编码',
            name VARCHAR(128) NOT NULL COMMENT '项目名称',
            short_name VARCHAR(64) DEFAULT '' COMMENT '简称',
            path_alias VARCHAR(64) DEFAULT '' COMMENT 'MinIO路径别名',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='检测项目表'""",
                """CREATE TABLE IF NOT EXISTS prod_lines(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            code VARCHAR(64) NOT NULL COMMENT '产线编码',
            name VARCHAR(64) NOT NULL COMMENT '产线名称',
            workshop VARCHAR(64) DEFAULT '' COMMENT '所属车间',
            area VARCHAR(64) DEFAULT '' COMMENT '所属区域',
            path_alias VARCHAR(32) DEFAULT '' COMMENT 'MinIO路径别名, 如 a01',
            rt_type VARCHAR(16) DEFAULT '' COMMENT '实时目录类型: minio/ftp',
            rt_bucket VARCHAR(128) DEFAULT '' COMMENT '桶名(minio), 空=用数据源输入桶',
            rt_path VARCHAR(512) DEFAULT '' COMMENT '实时目录前缀(车间别名/机台别名)',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='机组/产线表'""",        """CREATE TABLE IF NOT EXISTS brands(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            code VARCHAR(64) NOT NULL COMMENT '牌号编码',
            spec VARCHAR(128) NOT NULL COMMENT '品规/规格',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='牌号/品规表'""",
        """CREATE TABLE IF NOT EXISTS storage_config(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            line_id INT COMMENT '所属产线(prod_lines.id)',
            server_addr VARCHAR(128) COMMENT '存储服务地址',
            in_bucket VARCHAR(64) COMMENT '输入桶名',
            username VARCHAR(64) COMMENT '存储服务用户名(Access Key)',
            password VARCHAR(128) COMMENT '存储服务密码(Secret Key)',
            out_bucket VARCHAR(64) COMMENT '输出桶名',
            UNIQUE KEY uq_line (line_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='对象存储配置表(按产线)'""",
        """CREATE TABLE IF NOT EXISTS terminal_config(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            line_id INT COMMENT '关联产线ID(prod_lines.id)',
            sys_addr VARCHAR(128) COMMENT '终端系统地址',
            ng_dir VARCHAR(255) COMMENT 'NG图片目录',
            date_dir VARCHAR(32) DEFAULT 'YYYYMMDD' COMMENT '日期目录格式',
            str_pos VARCHAR(32) DEFAULT '1,8' COMMENT '字符串截取位置',
            shift_dir VARCHAR(64) DEFAULT '早、中、晚' COMMENT '班次目录名',
            cam_count INT DEFAULT 4 COMMENT '相机数量',
            cam_dirs VARCHAR(64) DEFAULT '1#,2#,3#,4#' COMMENT '相机目录名',
            brand_dirs VARCHAR(255) DEFAULT '' COMMENT '牌号目录名'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='终端采集配置表'""",
        """CREATE TABLE IF NOT EXISTS data_sources(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            name VARCHAR(64) NOT NULL COMMENT '数据源名称',
            type VARCHAR(16) NOT NULL COMMENT '类型: minio/ftp',
            server_addr VARCHAR(128) COMMENT '服务地址 host:port',
            username VARCHAR(64) COMMENT '用户名/AccessKey',
            password VARCHAR(128) COMMENT '密码/SecretKey',
            in_bucket VARCHAR(64) DEFAULT '' COMMENT '输入桶(minio)',
            out_bucket VARCHAR(64) DEFAULT '' COMMENT '输出桶(minio)',
            ng_dir VARCHAR(255) DEFAULT '' COMMENT 'NG图目录(ftp)',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='数据源(独立管理, 多产线可共用)'""",
        """CREATE TABLE IF NOT EXISTS label_classes(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            name VARCHAR(64) NOT NULL COMMENT '缺陷分类名称',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='缺陷标注分类表'""",
        """CREATE TABLE IF NOT EXISTS analysis_jobs(
            line_id INT NOT NULL COMMENT '机台(prod_lines.id)',
            item_id INT NOT NULL COMMENT '检测项目(detect_items.id)',
            status VARCHAR(16) DEFAULT 'idle' COMMENT 'idle/running/done/error/stopped',
            enabled INT DEFAULT 0 COMMENT '工作状态开关: 1开启 0关闭',
            total INT DEFAULT 0 COMMENT '目录总张数',
            read_cnt INT DEFAULT 0 COMMENT '已读取张数',
            analyzed INT DEFAULT 0 COMMENT '已分析(新推理入库)',
            skipped INT DEFAULT 0 COMMENT '已跳过(之前分析过)',
            msg VARCHAR(255) DEFAULT '' COMMENT '最新消息',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            PRIMARY KEY (line_id, item_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='实时目录分析任务(按 机台×检测项目)'""",
        """CREATE TABLE IF NOT EXISTS sample_uploads(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            msg_id VARCHAR(64) NOT NULL COMMENT '消息ID(与MQ回执关联)',
            project VARCHAR(64) COMMENT '检测项目',
            brand VARCHAR(64) COMMENT '牌号',
            face VARCHAR(64) COMMENT '相机面',
            face_code VARCHAR(32) COMMENT '相机面编码',
            bucket VARCHAR(64) COMMENT 'MinIO桶',
            path VARCHAR(512) COMMENT 'MinIO目录前缀',
            img_count INT DEFAULT 0 COMMENT '图片数',
            unit_key VARCHAR(160) DEFAULT '' COMMENT '建模单元稳定标识=项目编码_牌号编码_相机面编码(大写,如XB_3302101_FRONT), CubeStudio按此upsert项目',
            unit_id INT DEFAULT 0 COMMENT '建模单元(model_units.id), 0=尚未建单元',
            cs_project_id VARCHAR(64) DEFAULT '' COMMENT 'CubeStudio项目ID(回执回填)',
            class_id INT DEFAULT 0 COMMENT '缺陷分类ID(label_classes.id), 上传必选',
            class_name VARCHAR(512) DEFAULT '' COMMENT '缺陷分类名称(可多个, / 分隔), 随MQ消息发给CubeStudio',
            status VARCHAR(16) DEFAULT 'processing' COMMENT 'processing/done/error',
            reply_msg VARCHAR(255) DEFAULT '' COMMENT '算法侧回执消息',
            created_by VARCHAR(64) DEFAULT '' COMMENT '上传人',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_msg (msg_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='样本上传记录(MQ交互状态)'""",
        # 缺陷分类按建模单元(牌号×相机面=一个unit_key)绑定：一个单元一个分类，
        # 该单元下所有样本上传都用它。上传前必须先在行编辑弹窗里设置。
        """CREATE TABLE IF NOT EXISTS unit_sample_class(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            brand VARCHAR(64) NOT NULL COMMENT '牌号/品规',
            face_id INT NOT NULL COMMENT '相机面(camera_faces.id)',
            unit_key VARCHAR(160) DEFAULT '' COMMENT '建模单元稳定标识(冗余，便于核对)',
            class_id INT DEFAULT 0 COMMENT '缺陷分类ID(label_classes.id)',
            class_name VARCHAR(64) DEFAULT '' COMMENT '缺陷分类名称',
            updated_by VARCHAR(64) DEFAULT '' COMMENT '最后修改人',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_brand_face_class (brand, face_id, class_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='建模单元的缺陷分类绑定(一单元可多分类)'""",
        """CREATE TABLE IF NOT EXISTS analysis_logs(
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            line_id INT NOT NULL COMMENT '机台(prod_lines.id)',
            item_id INT NOT NULL COMMENT '检测项目(detect_items.id)',
            ts DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
            level VARCHAR(8) DEFAULT 'info' COMMENT 'info/ok/warn/error',
            src_key VARCHAR(512) DEFAULT '' COMMENT '图片key',
            detail VARCHAR(512) DEFAULT '' COMMENT '调用过程/结果',
            KEY idx_job_ts (line_id, item_id, id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='分析调用审计日志'""",
        """CREATE TABLE IF NOT EXISTS machine_schedule(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            machine VARCHAR(64) NOT NULL COMMENT '机台/产线(如 a01)',
            sched_date VARCHAR(16) NOT NULL COMMENT '日期 YYYYMMDD',
            shift VARCHAR(16) DEFAULT '' COMMENT '班次(空=全天)',
            brand VARCHAR(64) NOT NULL COMMENT '该时段生产的牌号',
            order_no VARCHAR(64) DEFAULT '' COMMENT '工单号',
            start_at DATETIME NULL COMMENT '工单开始时间',
            end_at DATETIME NULL COMMENT '工单结束时间',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            UNIQUE KEY uq_sched (machine, sched_date, shift)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='机台工单/排程(机台×时段→牌号), 推理时反查牌号'""",
        """CREATE TABLE IF NOT EXISTS line_items(
            line_id INT NOT NULL COMMENT '机台(prod_lines.id)',
            item_id INT NOT NULL COMMENT '检测项目(detect_items.id)',
            PRIMARY KEY (line_id, item_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='机台装了哪些检测项目(一对多), 工作状态按此逐项开关'""",
        """CREATE TABLE IF NOT EXISTS model_units(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            brand VARCHAR(64) NOT NULL COMMENT '牌号/品规',
            face_id INT NOT NULL COMMENT '相机面(camera_faces.id)',
            cs_project_id VARCHAR(64) DEFAULT '' COMMENT '对方CubeStudio项目ID(标注/训练)',
            cs_project_url VARCHAR(512) DEFAULT '' COMMENT '对方项目内嵌URL',
            model_id VARCHAR(64) DEFAULT '' COMMENT '已绑定的推理模型ID(来自对方)',
            model_version VARCHAR(64) DEFAULT '' COMMENT '已绑定模型版本',
            model_endpoint VARCHAR(512) DEFAULT '' COMMENT '推理服务地址',
            annotated INT DEFAULT 0 COMMENT '已标注数(轮询对方刷新)',
            total INT DEFAULT 0 COMMENT '任务总数(轮询对方刷新)',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            UNIQUE KEY uq_brand_face (brand, face_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='建模单元(牌号×相机面), 菜单②③④枢纽'""",
        """CREATE TABLE IF NOT EXISTS camera_faces(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            raw_name VARCHAR(128) NOT NULL COMMENT 'MinIO 路径别名(现场相机面目录名), 如 is7600C_D',
            face_name VARCHAR(64) NOT NULL COMMENT '标准面名(界面显示), 如 正面',
            face_code VARCHAR(32) NOT NULL COMMENT '标准面编码(传对方模型接口), 如 front',
            machine_model VARCHAR(64) DEFAULT '' COMMENT '机型, 如 is7600C',
            sort_order INT DEFAULT 0 COMMENT '面序号 1-6',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            UNIQUE KEY uq_raw (raw_name)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='相机面映射表(路径别名→标准面)'""",
        # label_tasks(LS标注进度缓存) / model_versions(LS导出的样本集版本) 随 Label Studio
        # 下线一并废弃，不再建表。collect_history 更早废弃：历史采集页直接按 label_images 实时聚合，
        # 该表只存过种子假数据，无任何代码读取。
        """CREATE TABLE IF NOT EXISTS label_images(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            brand VARCHAR(64) NOT NULL COMMENT '牌号',
            source VARCHAR(16) NOT NULL COMMENT '数据源: minio对象存储 / terminal工控机',
            src_key VARCHAR(512) NOT NULL COMMENT '对象key或工控机文件路径',
            line_name VARCHAR(32) DEFAULT '' COMMENT '所属产线',
            width INT DEFAULT 0 COMMENT '图片宽(像素)',
            height INT DEFAULT 0 COMMENT '图片高(像素)',
            annotated INT DEFAULT 0 COMMENT '是否已标注: 1是 0否',
            UNIQUE KEY uq_brand_key (brand, src_key(300))
        ) DEFAULT CHARSET=utf8mb4 COMMENT='标注图片表'""",
        """CREATE TABLE IF NOT EXISTS workshops(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            name VARCHAR(64) NOT NULL COMMENT '车间名称',
            path_alias VARCHAR(32) DEFAULT '' COMMENT 'MinIO路径别名, 如 jb1',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='车间字典表'""",
        """CREATE TABLE IF NOT EXISTS areas(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            name VARCHAR(64) NOT NULL COMMENT '区域名称',
            workshop VARCHAR(64) DEFAULT '' COMMENT '所属车间',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='区域字典表'""",
        """CREATE TABLE IF NOT EXISTS inference_results(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            image_id INT NOT NULL COMMENT '关联label_images.id',
            line_name VARCHAR(32) COMMENT '产线',
            brand VARCHAR(64) COMMENT '牌号',
            img_date VARCHAR(32) COMMENT '采集日期',
            shift VARCHAR(16) COMMENT '班次',
            is_defect INT DEFAULT 1 COMMENT '判定: 1真缺陷 0误剔',
            class_id INT COMMENT '缺陷分类ID',
            class_name VARCHAR(64) COMMENT '缺陷分类',
            confidence DOUBLE COMMENT '置信度',
            model_version VARCHAR(32) COMMENT '推理模型版本',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '推理时间',
            UNIQUE KEY uq_img (image_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='推理结果表'""",
        """CREATE TABLE IF NOT EXISTS inference_services(
            unit_key VARCHAR(160) PRIMARY KEY COMMENT '建模单元稳定标识(业务侧主键)',
            project_id VARCHAR(64) DEFAULT '' COMMENT 'CubeStudio视觉项目ID',
            project_label VARCHAR(255) DEFAULT '' COMMENT '对方项目显示名',
            service_id VARCHAR(64) DEFAULT '' COMMENT '对方推理服务表主键',
            service_name VARCHAR(128) DEFAULT '' COMMENT '推理服务名',
            model_name VARCHAR(128) DEFAULT '' COMMENT '模型名',
            model_version VARCHAR(64) DEFAULT '' COMMENT '模型版本',
            model_path VARCHAR(512) DEFAULT '' COMMENT '模型路径(容器内)',
            model_status VARCHAR(32) DEFAULT '' COMMENT '模型状态',
            endpoint VARCHAR(512) DEFAULT '' COMMENT '推理服务根地址',
            health_url VARCHAR(512) DEFAULT '' COMMENT '健康检查URL',
            predict_url VARCHAR(512) DEFAULT '' COMMENT '预测URL(LS ML Backend协议)',
            task_type VARCHAR(32) DEFAULT '' COMMENT 'detection/segmentation/classification',
            class_names VARCHAR(1024) DEFAULT '' COMMENT '项目缺陷标签(逗号分隔)',
            event_id VARCHAR(128) DEFAULT '' COMMENT '最近一次通知的event_id',
            acked INT DEFAULT 0 COMMENT '回执是否已发出: 1是 0否',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='推理服务(按unit_key upsert, 来自MQ inference.ready)'""",
        """CREATE TABLE IF NOT EXISTS integration_config(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            cfg_key VARCHAR(64) NOT NULL COMMENT '配置项键',
            cfg_value TEXT COMMENT '配置项值',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_cfg_key (cfg_key)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='外部系统集成配置表(CubeStudio/RabbitMQ)'""",
    ]
    for stmt in ddl:
        db.execute(stmt)
    db.commit()
    migrate_db(db)
    if db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
        db.execute("INSERT INTO users(username,password,realname,role) VALUES(?,?,?,?)",
                   ("admin", md5("admin123"), "管理员", "admin"))
        db.execute("INSERT INTO users(username,password,realname,role) VALUES(?,?,?,?)",
                   ("zhangsan", md5("123456"), "张三", "user"))
        # 检测项目
        for i, (name, short) in enumerate([("烟支外观检测", "烟支外观"), ("五轮成像检测", "五轮成像"),
                                           ("小包CCD检测", "小包CCD"), ("散包检测", "散包检测"),
                                           ("条外观检测", "条外观")], 1):
            db.execute("INSERT INTO detect_items(code,name,short_name) VALUES(?,?,?)",
                       ("JC%03d" % i, name, short))
        # 产线（3 条，分别对应 10.10.96.65 / 66 / 67）
        line_plan = [
            ("YZ-A01", "A01", "卷包一车间", "一区"),
            ("YZ-A02", "A02", "卷包一车间", "一区"),
            ("YZ-A03", "A03", "卷包二车间", "二区"),
        ]
        line_ids = {}
        for code, name, ws, area in line_plan:
            line_ids[name] = db.execute(
                "INSERT INTO prod_lines(code,name,workshop,area) VALUES(?,?,?,?)",
                (code, name, ws, area)).lastrowid
        # 牌号
        for code, spec in [("3301231", "中华（软）"), ("3301232", "中华（硬）"),
                           ("3301001", "玉溪（硬）"),
                           ("3302101", "玉溪（软）"), ("3302102", "玉溪（创客）"),
                           ("3302103", "玉溪（缤果爆）"), ("3303001", "云烟（紫）")]:
            db.execute("INSERT INTO brands(code,spec) VALUES(?,?)", (code, spec))
        # 车间 / 区域 字典
        for w in ["卷包一车间", "卷包二车间"]:
            db.execute("INSERT INTO workshops(name) VALUES(?)", (w,))
        for name, ws in [("一区", "卷包一车间"), ("二区", "卷包二车间")]:
            db.execute("INSERT INTO areas(name,workshop) VALUES(?,?)", (name, ws))
        # 对象存储配置：A01 产线指向 10.10.96.65 上的 MinIO
        db.execute("INSERT INTO storage_config(line_id,server_addr,in_bucket,username,password,out_bucket) "
                   "VALUES(?,?,?,?,?,?)",
                   (line_ids["A01"], "10.10.96.65:9000", "defect-raw", "minioadmin", "minioadmin123", "defect-outputs"))
        # 终端配置（模式二）：A02->10.10.96.66，A03->10.10.96.67 工控机 NG 目录
        db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (line_ids["A02"], "10.10.96.66:29", "/data/ng/NG_IMG", "YYYYMMDD", "1,8",
                    "早、中、晚", 3, "1#,2#,3#", "中华（软）"))
        db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (line_ids["A03"], "10.10.96.67:29", "/data/ng/NG_IMG", "YYYYMMDD", "1,8",
                    "早、中、晚", 2, "1#,2#", "云烟（紫）"))
        xb_classes = ["侧面翘边", "内衬顶部质量缺陷", "内衬高于商标", "商标接头", "商标歪斜",
                      "商标歪斜错位", "商标纸接头", "商标纸歪斜", "商标裁切错误", "商标错牙",
                      "封签接头", "封签歪斜", "封签破损", "封签粘贴不牢", "封签裁切错误",
                      "小盒不洁", "小盒侧边飞边", "小盒侧面粘贴不牢", "小盒侧飞边", "小盒倒",
                      "小盒底部折叠不良", "小盒底部粘贴不牢", "小盒底部翘边", "小盒底部翻折破损",
                      "小盒底部飞边", "小盒破损触皱", "小盒触皱", "小盒触皱破损", "底部翘边",
                      "无商标", "无封签", "烟支外漏", "缺封签", "顶部内衬破损"]
        for c in xb_classes:
            db.execute("INSERT INTO label_classes(name) VALUES(?)", (c,))
        # 不灌模型版本 / 历史采集的种子数据：这些表只应由真实动作写入 ——
        # 模型版本来自「发布样本集」，历史采集来自实际扫描数据源。编造的演示数据
        # 混在里面分不清真假，比空表更糟。
        db.commit()
    db.close()


# ---------------------------------------------------------------- 登录
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if session.get("role") != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrapper


def cur_detect_item():
    """顶栏选中的检测项目行。整套流程（采集→标注→模型→分析）都是在某个检测项目
    之下进行的，各页面据此过滤；此前这个下拉框选了不起任何作用。"""
    db = get_db()
    cur = request.args.get("item") or ""
    if cur:
        r = db.execute("SELECT * FROM detect_items WHERE (short_name=? OR name=?) AND status=1",
                       (cur, cur)).fetchone()
        if r:
            return dict(r)
    # 默认落在「配了数据源、且真采到图」的项目上。此前取 id 最小的，正好是空的
    # 烟支外观 —— 每次打开系统都是一片 0，有真图的项目反而要手动切过去。
    r = db.execute(
        "SELECT d.* FROM detect_items d JOIN label_images i ON i.item_id=d.id "
        "WHERE d.status=1 GROUP BY d.id ORDER BY COUNT(i.id) DESC, d.id LIMIT 1").fetchone()
    if not r:
        r = db.execute("SELECT * FROM detect_items WHERE status=1 ORDER BY id LIMIT 1").fetchone()
    return dict(r) if r else None


@app.context_processor
def inject_globals():
    """顶栏检测项目全局下拉（工作上下文）。det_items 供下拉，g_cur_det 是当前选中，
    存 session 跨页保持。cur_item/cur_item_id 兼容采集页旧用法。"""
    items, cur_id, cur_name = [], 0, ""
    try:
        db = get_db()
        items = [dict(r) for r in db.execute(
            "SELECT id,name,short_name,path_alias FROM detect_items WHERE status=1 ORDER BY id").fetchall()]
        if items:
            _, cur_id = detect_item_ctx(db)
            row = next((i for i in items if i["id"] == cur_id), None)
            cur_name = (row["short_name"] or row["name"]) if row else ""
    except Exception:
        pass  # 建表前或登录页连不上库时不阻塞页面渲染
    return dict(G_DET_ITEMS=items, g_cur_det=cur_id, g_cur_det_name=cur_name,
                cur_item=cur_name, cur_item_id=cur_id,
                DETECT_ITEMS=[(i["short_name"] or i["name"]) for i in items])


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if row and row["password"] == md5(password):
            if not row["status"]:
                app.logger.warning("登录被拒：账号已停用 user=%s ip=%s", username, ip)
                flash("账号已停用，请联系管理员", "error")
            else:
                session["uid"] = row["id"]
                session["username"] = row["username"]
                session["realname"] = row["realname"] or row["username"]
                session["role"] = row["role"]
                app.logger.info("登录成功 user=%s role=%s ip=%s", username, row["role"], ip)
                return redirect(request.args.get("next") or url_for("index"))
        else:
            app.logger.warning("登录失败：用户名或密码错误 user=%s ip=%s", username, ip)
            flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    app.logger.info("登出 user=%s", session.get("username", ""))
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- 首页
@app.route("/")
@login_required
def index():
    # 首页已去掉，根路径直接进「① 图片采集」
    return redirect(url_for("collect", tab="config"))


# ---------------------------------------------------------------- 用户管理
@app.route("/users")
@login_required
@admin_required
def users():
    rows, pg = paginate(get_db(), "SELECT * FROM users ORDER BY id", page=request.args.get("page"))
    return render_template("users.html", rows=rows, pg=pg, active="users")


@app.route("/users/save", methods=["POST"])
@login_required
@admin_required
def users_save():
    db = get_db()
    uid = request.form.get("id")
    username = request.form.get("username", "").strip()
    realname = request.form.get("realname", "").strip()
    role = request.form.get("role", "user")
    password = request.form.get("password", "")
    if uid:
        if password:
            db.execute("UPDATE users SET username=?,realname=?,role=?,password=? WHERE id=?",
                       (username, realname, role, md5(password), uid))
        else:
            db.execute("UPDATE users SET username=?,realname=?,role=? WHERE id=?",
                       (username, realname, role, uid))
    else:
        try:
            db.execute("INSERT INTO users(username,password,realname,role) VALUES(?,?,?,?)",
                       (username, md5(password or "123456"), realname, role))
        except IntegrityError:
            flash("用户名已存在", "error")
            return redirect(url_for("users"))
    db.commit()
    flash("保存成功", "success")
    return redirect(url_for("users"))


@app.route("/users/toggle/<int:uid>", methods=["POST"])
@login_required
@admin_required
def users_toggle(uid):
    db = get_db()
    db.execute("UPDATE users SET status=1-status WHERE id=? AND username<>'admin'", (uid,))
    db.commit()
    return redirect(url_for("users"))


@app.route("/users/delete/<int:uid>", methods=["POST"])
@login_required
@admin_required
def users_delete(uid):
    db = get_db()
    db.execute("DELETE FROM users WHERE id=? AND username<>'admin'", (uid,))
    db.commit()
    flash("已删除", "success")
    return redirect(url_for("users"))


# ---------------------------------------------------------------- 基础配置
# 按业务层次排序：组织架构 → 检测配置 → 生产 → 系统
CONFIG_TABS = [("workshops", "车间"), ("areas", "区域"), ("lines", "机组/产线"),
               ("items", "检测项目"), ("faces", "图像分类"), ("defects", "缺陷分类"),
               ("brands", "牌号/品规"), ("schedule", "机台排程")]


@app.route("/config/<tab>")
@login_required
def config(tab):
    if tab not in dict(CONFIG_TABS):
        abort(404)
    db = get_db()
    if tab == "integration":
        # 系统集成页面已不再使用，直接回退到默认页签
        return redirect(url_for("config", tab="workshops"))
    if tab == "faces":
        # 图像分类(相机面)按检测项目配置：下拉选检测项目，只显示该项目的相机面
        det_items = [dict(r) for r in db.execute(
            "SELECT * FROM detect_items WHERE status=1 ORDER BY id").fetchall()]
        cur_det = request.args.get("det", type=int) or (det_items[0]["id"] if det_items else 0)
        frows, pg = paginate(db, "SELECT * FROM camera_faces WHERE item_id=? "
                             "ORDER BY sort_order, id", (cur_det,),
                             page=request.args.get("page"))
        return render_template("config_faces.html", tab=tab, tabs=CONFIG_TABS,
                               rows=[dict(r) for r in frows], pg=pg,
                               det_items=det_items, cur_det=cur_det, active="config")
    base_sql = {
        "items": "SELECT * FROM detect_items ORDER BY id",
        "lines": "SELECT * FROM prod_lines ORDER BY id",
        "brands": "SELECT * FROM brands ORDER BY id",
        "workshops": "SELECT * FROM workshops ORDER BY id",
        "areas": "SELECT * FROM areas ORDER BY id",
        "defects": "SELECT * FROM label_classes ORDER BY id",
        "schedule": "SELECT * FROM machine_schedule ORDER BY sched_date DESC, machine, shift",
    }[tab]
    rows, pg = paginate(db, base_sql, page=request.args.get("page"))
    data = [dict(r) for r in rows]
    workshops = [dict(w) for w in db.execute("SELECT * FROM workshops WHERE status=1 ORDER BY id").fetchall()]
    areas = [dict(a) for a in db.execute("SELECT * FROM areas WHERE status=1 ORDER BY id").fetchall()]
    sched_brands = [dict(b) for b in db.execute("SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    extra = {}
    if tab == "schedule":
        # 机台下拉给排程用：值是路径别名(现场目录名)，避免手打错和产线编码混用
        extra["sched_lines"] = [{"alias": sched_machine_of(l), "name": l["name"]}
                                for l in db.execute(
                                    "SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall()]
        # datetime 交给 tojson 会变成 HTTP 日期串(datetime-local 认不了、修改弹窗回显不出来)，
        # 统一转成 "YYYY-MM-DD HH:MM:SS" 字符串，列表展示和回填都用它
        for r in data:
            r["start_at"] = str(r["start_at"])[:19] if r["start_at"] else ""
            r["end_at"] = str(r["end_at"])[:19] if r["end_at"] else ""
    if tab == "lines":
        # 机台行上直接展示：实时目录（当前检测项目下的完整前缀）、工作状态、当前运行工单
        det_items, cur_det = detect_item_ctx(db)
        # 变量名不能叫 cur_item —— inject_globals 里那个是顶栏项目名(字符串)，面包屑在用
        cur_det_item = next((i for i in det_items if i["id"] == cur_det), None)
        today, shift = datetime.now().strftime("%Y%m%d"), cur_shift()
        now = datetime.now()
        for r in data:
            r["ws_alias"] = next((w["path_alias"] or w["name"] for w in workshops
                                  if w["name"] == r["workshop"]), r["workshop"])
            r["line_alias"] = (r["path_alias"] or r["code"] or "").strip()
            # 本机台装了哪些检测项目 → 每个项目一行「目录 + 开关 + 进度」
            bound = [i["item_id"] for i in db.execute(
                "SELECT item_id FROM line_items WHERE line_id=?", (r["id"],)).fetchall()]
            r["item_ids"] = bound
            r["items"] = []
            for it in det_items:
                if it["id"] not in bound:
                    continue
                job = db.execute("SELECT * FROM analysis_jobs WHERE line_id=? AND item_id=?",
                                 (r["id"], it["id"])).fetchone()
                r["items"].append({"id": it["id"], "name": it["short_name"] or it["name"],
                                   "rt_full": line_rt_prefix(r, it),
                                   "job": dict(job) if job else None})
            wo = db.execute(
                "SELECT * FROM machine_schedule WHERE machine=? AND status=1 "
                "AND start_at IS NOT NULL AND end_at IS NOT NULL AND start_at<=? AND end_at>? "
                "ORDER BY start_at DESC LIMIT 1",
                (sched_machine_of(r), now, now)).fetchone()
            r["cur_wo"] = dict(wo) if wo else None
            r["cur_brand"] = wo["brand"] if wo else resolve_brand(db, sched_machine_of(r), today, shift)
        extra = {"det_items": det_items, "cur_det": cur_det, "cur_det_item": cur_det_item,
                 "today": today, "cur_shift_name": shift}
    return render_template("config.html", tab=tab, tabs=CONFIG_TABS, rows=data, pg=pg,
                           workshops=workshops, areas=areas, sched_brands=sched_brands,
                           active="config", **extra)


@app.route("/config/integration/save", methods=["POST"])
@login_required
def config_integration_save():
    """保存外部系统集成参数到 integration_config，即时生效（get_cfg 带 5 秒缓存）。"""
    db = get_db()
    for key, _, _ in CFG_LABELS:
        val = request.form.get(key, "").strip()
        db.execute("INSERT INTO integration_config(cfg_key,cfg_value) VALUES(?,?) "
                   "ON DUPLICATE KEY UPDATE cfg_value=VALUES(cfg_value)", (key, val))
    db.commit()
    _cfg_cache["t"] = 0.0  # 立刻失效，不用等缓存过期
    app.logger.info("集成配置已保存 keys=%s by=%s",
                    ",".join(k for k, _, _ in CFG_LABELS), session.get("username", ""))
    flash("集成配置已保存，即时生效", "success")
    return redirect(url_for("config", tab="integration"))


@app.route("/config/integration/test/<what>", methods=["POST"])
@login_required
def config_integration_test(what):
    """用表单里的当前值测连通性，不依赖已保存的配置。"""
    import urllib.request
    if what == "cs":
        url = request.form.get("cs_url", "").strip()
        if not url:
            return jsonify({"ok": False, "msg": "未配置。CubeStudio 需独立部署（磁盘≥500G、Docker≥19.03、建议带 GPU）"})
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return jsonify({"ok": True, "msg": "地址可达（HTTP %d）" % r.status})
        except Exception as e:
            return jsonify({"ok": False, "msg": "连接失败：%s" % str(e)[:110]})
    return jsonify({"ok": False, "msg": "未知测试项"}), 400


@app.route("/config/<tab>/save", methods=["POST"])
@login_required
def config_save(tab):
    db = get_db()
    f = request.form
    rid = f.get("id")
    if tab == "items":
        if rid:
            db.execute("UPDATE detect_items SET code=?,name=?,short_name=?,path_alias=? WHERE id=?",
                       (f["code"], f["name"], f.get("short_name", ""),
                        f.get("path_alias", "").strip().strip("/"), rid))
        else:
            db.execute("INSERT INTO detect_items(code,name,short_name,path_alias) VALUES(?,?,?,?)",
                       (f["code"], f["name"], f.get("short_name", ""),
                        f.get("path_alias", "").strip().strip("/")))
    elif tab == "lines":
        if rid:
            db.execute("UPDATE prod_lines SET code=?,name=?,workshop=?,area=?,path_alias=? WHERE id=?",
                       (f["code"], f["name"], f.get("workshop", ""), f.get("area", ""),
                        f.get("path_alias", "").strip(), rid))
        else:
            rid = db.execute("INSERT INTO prod_lines(code,name,workshop,area,path_alias) "
                             "VALUES(?,?,?,?,?)",
                             (f["code"], f["name"], f.get("workshop", ""), f.get("area", ""),
                              f.get("path_alias", "").strip())).lastrowid
        # 本机台装了哪些检测项目（一对多）。取消勾选的解绑，同时停掉它的分析任务
        picked = set(request.form.getlist("items", type=int))
        db.execute("DELETE FROM line_items WHERE line_id=?", (rid,))
        for iid in picked:
            db.execute("INSERT INTO line_items(line_id,item_id) VALUES(?,?)", (rid, iid))
        if picked:
            ph = ",".join(["?"] * len(picked))
            db.execute("UPDATE analysis_jobs SET enabled=0 WHERE line_id=? AND item_id NOT IN (%s)"
                       % ph, [rid] + list(picked))
        else:
            db.execute("UPDATE analysis_jobs SET enabled=0 WHERE line_id=?", (rid,))
    elif tab == "brands":
        if rid:
            db.execute("UPDATE brands SET code=?,spec=? WHERE id=?", (f["code"], f["spec"], rid))
        else:
            db.execute("INSERT INTO brands(code,spec) VALUES(?,?)", (f["code"], f["spec"]))
    elif tab == "workshops":
        if rid:
            db.execute("UPDATE workshops SET name=?,path_alias=? WHERE id=?",
                       (f["name"], f.get("path_alias", "").strip(), rid))
        else:
            db.execute("INSERT INTO workshops(name,path_alias) VALUES(?,?)",
                       (f["name"], f.get("path_alias", "").strip()))
    elif tab == "areas":
        if rid:
            db.execute("UPDATE areas SET name=?,workshop=? WHERE id=?", (f["name"], f.get("workshop", ""), rid))
        else:
            db.execute("INSERT INTO areas(name,workshop) VALUES(?,?)", (f["name"], f.get("workshop", "")))
    elif tab == "defects":
        if rid:
            db.execute("UPDATE label_classes SET name=? WHERE id=?", (f["name"], rid))
        else:
            db.execute("INSERT INTO label_classes(name) VALUES(?)", (f["name"],))
    elif tab == "schedule":
        # 起止时间用 <input type="datetime-local">，值形如 2026-07-21T06:00
        start = (f.get("start_at", "") or "").strip().replace("T", " ")
        end = (f.get("end_at", "") or "").strip().replace("T", " ")
        date = f.get("sched_date", "").strip()
        if not date and start:      # 填了开始时间就不必再手填日期
            date = start[:10].replace("-", "")
        vals = (f["machine"].strip(), date, f.get("shift", "").strip(), f["brand"].strip(),
                f.get("order_no", "").strip(), start or None, end or None)
        if rid:
            db.execute("UPDATE machine_schedule SET machine=?,sched_date=?,shift=?,brand=?,"
                       "order_no=?,start_at=?,end_at=? WHERE id=?", vals + (rid,))
        else:
            db.execute("INSERT INTO machine_schedule(machine,sched_date,shift,brand,order_no,"
                       "start_at,end_at) VALUES(?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE "
                       "brand=VALUES(brand),order_no=VALUES(order_no),"
                       "start_at=VALUES(start_at),end_at=VALUES(end_at)", vals)
    elif tab == "faces":
        det = f.get("item_id", type=int) or 0
        vals = (det, f["raw_name"].strip().strip("/"), f["face_name"].strip(),
                f.get("face_code", "").strip(), f.get("sort_order", 0) or 0)
        if rid:
            db.execute("UPDATE camera_faces SET item_id=?,raw_name=?,face_name=?,face_code=?,"
                       "sort_order=? WHERE id=?", vals + (rid,))
        else:
            # 自动发现里「一键映射」也走这里，同项目同名目录已存在则更新，避免唯一键冲突
            db.execute("INSERT INTO camera_faces(item_id,raw_name,face_name,face_code,"
                       "sort_order) VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE "
                       "face_name=VALUES(face_name),face_code=VALUES(face_code),"
                       "sort_order=VALUES(sort_order)", vals)
        db.commit()
        flash("保存成功", "success")
        return redirect(url_for("config", tab="faces", det=det))
    db.commit()
    flash("保存成功", "success")
    return redirect(url_for("config", tab=tab))


@app.route("/config/<tab>/delete/<int:rid>", methods=["POST"])
@login_required
def config_delete(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands",
             "workshops": "workshops", "areas": "areas", "defects": "label_classes",
             "faces": "camera_faces", "schedule": "machine_schedule"}.get(tab)
    if not table:
        abort(404)
    db = get_db()
    db.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
    db.commit()
    flash("已删除", "success")
    return redirect(url_for("config", tab=tab, det=request.args.get("det", type=int)))


@app.route("/config/<tab>/toggle/<int:rid>", methods=["POST"])
@login_required
def config_toggle(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands",
             "workshops": "workshops", "areas": "areas", "defects": "label_classes",
             "faces": "camera_faces", "schedule": "machine_schedule"}.get(tab)
    if not table:
        abort(404)
    db = get_db()
    db.execute("UPDATE %s SET status=1-status WHERE id=?" % table, (rid,))
    db.commit()
    return redirect(url_for("config", tab=tab, det=request.args.get("det", type=int)))


# ---------------------------------------------------------------- 图像采集
COLLECT_TABS = [("config", "采集配置"), ("monitor", "采集监控"),
                ("history", "历史采集"), ("images", "图片查询")]


def rebuild_line_cfg(db, line_id):
    """按产线的 source_id 从 data_sources 重建其 storage_config/terminal_config 缓存。
    data_sources 是主表；这两张按产线的表是派生缓存，让现有按 line_id 读的代码不用改。"""
    db.execute("DELETE FROM storage_config WHERE line_id=?", (line_id,))
    db.execute("DELETE FROM terminal_config WHERE line_id=?", (line_id,))
    line = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not line or not line["source_id"]:
        db.commit()
        return
    ds = db.execute("SELECT * FROM data_sources WHERE id=? AND status=1", (line["source_id"],)).fetchone()
    if not ds:
        db.commit()
        return
    if ds["type"] == "minio":
        db.execute("INSERT INTO storage_config(line_id,server_addr,in_bucket,username,password,out_bucket) "
                   "VALUES(?,?,?,?,?,?)", (line_id, ds["server_addr"], ds["in_bucket"],
                                          ds["username"], ds["password"], ds["out_bucket"]))
    else:  # ftp
        db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir) VALUES(?,?,?)",
                   (line_id, ds["server_addr"], ds["ng_dir"]))
    db.commit()


def rebuild_all_line_cfg(db):
    """某数据源改动后，重建所有引用它的产线缓存。"""
    for l in db.execute("SELECT id FROM prod_lines WHERE source_id>0").fetchall():
        rebuild_line_cfg(db, l["id"])


def get_s3(cfg):
    """按给定 storage_config 行构建 MinIO/S3 客户端。"""
    import boto3
    from botocore.client import Config
    s3 = boto3.client("s3", endpoint_url="http://" + (cfg["server_addr"] or ""),
                      aws_access_key_id=cfg["username"] or "",
                      aws_secret_access_key=cfg["password"] or "",
                      region_name="us-east-1",
                      config=Config(signature_version="s3v4", connect_timeout=4,
                                    read_timeout=8, retries={"max_attempts": 1}))
    return s3, cfg


def line_storage_cfg(line_name):
    """返回该产线的对象存储配置(storage_config)行，无则 None。"""
    return get_db().execute(
        "SELECT s.* FROM storage_config s JOIN prod_lines p ON s.line_id=p.id WHERE p.name=?",
        (line_name,)).fetchone()


def line_terminal_cfg(line_row):
    """产线为工控机采集模式则返回其 terminal_config，否则 None（对象存储模式）。"""
    if not line_row:
        return None
    return get_db().execute("SELECT * FROM terminal_config WHERE line_id=?",
                            (line_row["id"],)).fetchone()


def _ws_sftp(tcfg):
    """按 terminal_config.sys_addr(host:port) 建到工控机的 SFTP 连接。"""
    import paramiko
    host, _, port = (tcfg["sys_addr"] or "").partition(":")
    t = paramiko.Transport((host, int(port or 22)))
    t.connect(username=WS_USER, password=WS_PASS)
    return t, paramiko.SFTPClient.from_transport(t)


def sftp_list_images(tcfg, max_n=500):
    """递归列工控机 ng_dir 下的图片，返回 [{path, rel, name}, ...]。"""
    import stat
    root = (tcfg["ng_dir"] or "/").rstrip("/") or "/"
    t, sftp = _ws_sftp(tcfg)
    out = []

    def walk(p):
        if len(out) >= max_n:
            return
        try:
            entries = sftp.listdir_attr(p)
        except IOError:
            return
        for e in sorted(entries, key=lambda x: x.filename):
            if len(out) >= max_n:
                return
            full = p + "/" + e.filename
            if stat.S_ISDIR(e.st_mode):
                walk(full)
            elif e.filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                out.append({"path": full, "rel": full[len(root):].lstrip("/"), "name": e.filename})

    try:
        walk(root)
    finally:
        t.close()
    return out


def face_display_map():
    """{路径别名(现场目录名): 标准面名}，用于把 is7600C_D 这类技术码显示成「正面」。"""
    return {r["raw_name"]: r["face_name"] for r in
            get_db().execute("SELECT raw_name,face_name FROM camera_faces WHERE status=1").fetchall()}


SHIFT_NAMES = ("早班", "中班", "晚班")


def key_date_shift(key):
    """从图片 key 里取 (日期, 班次)。现场是 …/日期/班组/班次/相机面/文件，而工控机上的
    老数据是 项目/日期/班次/缺陷类型/文件 —— 不按下标数，认「8 位数字段」和「班次名」。"""
    date, shift = "", ""
    for seg in key.split("/"):
        if not date and len(seg) == 8 and seg.isdigit():
            date = seg
        elif date and seg in SHIFT_NAMES:
            shift = seg
            break
    return date, shift


def collect_shift_stats(db, item_id, lines):
    """按 (机台, 日期, 班次) 归集：采集张数 collected、已判别张数 infer、缺陷张数 defect、牌号。

    采集量来自 label_images（路径倒数第 5/3 段就是 日期/班次，与前缀多深无关）；
    判别量来自 inference_results（worker 入库时已写好 机台/日期/班次）。两边按机台名对齐。"""
    alias2line = {sched_machine_of(l): l["name"] for l in lines}
    stats = {}

    def cell(line_name, date, shift):
        return stats.setdefault((line_name, date, shift),
                                {"collected": 0, "infer": 0, "defect": 0, "brand": ""})

    for im in db.execute("SELECT line_name, src_key FROM label_images WHERE item_id=?",
                         (item_id,)).fetchall():
        date, shift = key_date_shift(im["src_key"])
        if not date:
            continue
        cell(im["line_name"], date, shift)["collected"] += 1
    for r in db.execute(
            "SELECT machine, line_name, img_date, shift, brand, COUNT(*) AS c, "
            "SUM(is_defect) AS d FROM inference_results WHERE unit_id IN "
            "(SELECT mu.id FROM model_units mu JOIN camera_faces cf ON cf.id=mu.face_id "
            "WHERE cf.item_id=?) GROUP BY machine, line_name, img_date, shift, brand",
            (item_id,)).fetchall():
        name = r["line_name"] or alias2line.get(r["machine"], r["machine"])
        c = cell(name, r["img_date"] or "", r["shift"] or "")
        c["infer"] += r["c"]
        c["defect"] += int(r["d"] or 0)
        c["brand"] = c["brand"] or (r["brand"] or "")
    # 没推理过的班次也把牌号补上，让历史表能看出当时在生产什么
    for (name, date, shift), c in stats.items():
        if c["brand"]:
            continue
        alias = next((a for a, n in alias2line.items() if n == name), name)
        c["brand"] = resolve_brand(db, alias, date, shift)
    return stats


@app.route("/collect/<tab>", methods=["GET", "POST"])
@login_required
def collect(tab):
    if tab not in dict(COLLECT_TABS):
        abort(404)
    db = get_db()
    ctx = dict(tab=tab, tabs=COLLECT_TABS, active="collect")
    lines = db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall()
    ctx["lines"] = lines
    cur_line = request.args.get("line", "all")
    ctx["cur_line"] = cur_line
    cur_item_row = cur_detect_item()   # 采集的三个页签都只看当前检测项目的数据

    if tab == "config":
        if request.method == "POST":
            f = request.form
            act = f.get("action", "")
            if act == "save_source":
                sid = f.get("source_id", type=int)
                # 只维护输入桶；out_bucket 是遗留列，界面已去掉，更新时不动它
                vals = (f.get("name", "").strip(), f.get("type", "minio"),
                        f.get("server_addr", "").strip(), f.get("username", "").strip(),
                        f.get("password", "").strip(), f.get("in_bucket", "").strip(),
                        f.get("ng_dir", "").strip())
                if sid:
                    db.execute("UPDATE data_sources SET name=?,type=?,server_addr=?,username=?,"
                               "password=?,in_bucket=?,ng_dir=? WHERE id=?", vals + (sid,))
                else:
                    sid = db.execute("INSERT INTO data_sources(name,type,server_addr,username,password,"
                                     "in_bucket,ng_dir) VALUES(?,?,?,?,?,?,?)", vals).lastrowid
                # 弹窗里勾选的产线绑到本数据源；原绑它但这次没勾的解绑
                picked = set(request.form.getlist("lines", type=int))
                db.execute("UPDATE prod_lines SET source_id=0 WHERE source_id=?", (sid,))
                for lid in picked:
                    db.execute("UPDATE prod_lines SET source_id=? WHERE id=?", (sid, lid))
                db.commit()
                rebuild_all_line_cfg(db)   # 绑定/数据源变了，重建产线缓存
                flash("数据源已保存", "success")
            elif act == "del_source":
                sid = f.get("source_id", type=int)
                db.execute("UPDATE prod_lines SET source_id=0 WHERE source_id=?", (sid,))
                db.execute("DELETE FROM data_sources WHERE id=?", (sid,))
                db.commit()
                rebuild_all_line_cfg(db)
                flash("数据源已删除", "success")
            return redirect(url_for("collect", tab="config"))
        ctx["sources"] = [dict(r) for r in db.execute(
            "SELECT * FROM data_sources ORDER BY id").fetchall()]
        # 每个数据源绑定了哪些产线（名字列表 + id列表供弹窗回显勾选）
        ref, ref_ids = {}, {}
        for l in lines:
            if l["source_id"]:
                ref.setdefault(l["source_id"], []).append(l["name"])
                ref_ids.setdefault(l["source_id"], []).append(l["id"])
        for s in ctx["sources"]:
            s["used_by"] = ref.get(s["id"], [])
            s["line_ids"] = ref_ids.get(s["id"], [])
        # 供数据源弹窗勾选产线：按 车间 → 区域 两级分组
        from collections import OrderedDict
        grouped = OrderedDict()
        for l in lines:
            ws = l["workshop"] or "未分车间"
            ar = l["area"] or "未分区域"
            grouped.setdefault(ws, OrderedDict()).setdefault(ar, []).append(
                {"id": l["id"], "name": l["name"]})
        ctx["bind_groups"] = [{"workshop": ws, "areas": [{"area": ar, "lines": ls}
                               for ar, ls in areas.items()]} for ws, areas in grouped.items()]
    elif tab == "monitor":
        # 采集监控 = 当前班次的实时情况，按机台一张卡：采了多少、判了多少、多少缺陷
        iid = cur_item_row["id"] if cur_item_row else 0
        sync_label_images(iid)
        today, shift = datetime.now().strftime("%Y%m%d"), cur_shift()
        stats = collect_shift_stats(db, iid, lines)
        ctx["cams"] = []
        for l in lines:
            if cur_line != "all" and l["name"] != cur_line:
                continue
            s = stats.get((l["name"], today, shift), {})
            wo = db.execute(
                "SELECT * FROM machine_schedule WHERE machine=? AND sched_date=? AND shift=? "
                "AND status=1", (sched_machine_of(l), today, shift)).fetchone()
            ctx["cams"].append({"name": l["name"], "brand": wo["brand"] if wo else "",
                                "order_no": (wo["order_no"] if wo else "") or "",
                                "count": s.get("collected", 0), "infer": s.get("infer", 0),
                                "defect": s.get("defect", 0)})
        ctx["cur_date"], ctx["cur_shift_name"], ctx["err"] = today, shift, None
    elif tab == "history":
        # 历史采集 = 每机台每班次的采集量，以及其中经模型判别的量（含缺陷数）
        iid = cur_item_row["id"] if cur_item_row else 0
        sync_label_images(iid)
        stats = collect_shift_stats(db, iid, lines)

        def _fmt(d):
            return d[:4] + "/" + d[4:6] + "/" + d[6:8] if len(d) == 8 and d.isdigit() else d
        all_recs = []
        for (line_name, d, sh), s in sorted(stats.items(), reverse=True):
            if cur_line != "all" and line_name != cur_line:
                continue
            col, inf = s.get("collected", 0), s.get("infer", 0)
            all_recs.append({"line": line_name, "date": _fmt(d), "shift": sh,
                             "brand": s.get("brand", ""), "img_count": col, "infer": inf,
                             "defect": s.get("defect", 0),
                             "pct": round(inf * 100.0 / col) if col else 0})
        ctx["records"], ctx["pg"] = paginate_list(all_recs, request.args.get("page"))
        ctx["show_line"] = (cur_line == "all")
    elif tab == "images" and cur_line == "all":
        ctx["all_lines"] = True
        ctx["dirs"] = [{"name": l["name"] + " 产线", "line": l["name"]} for l in lines]
        ctx["pics"] = []; ctx["total"] = 0; ctx["err"] = None
        ctx["crumbs"] = []; ctx["cur_path"] = ""; ctx["root_label"] = "全部产线"
        ctx["server_addr"] = ""; ctx["online"] = True; ctx["src"] = ""
    elif tab == "images":
        ctx["all_lines"] = False
        line_row = next((l for l in lines if l["name"] == cur_line), None)
        tcfg = line_terminal_cfg(line_row)
        scfg = line_storage_cfg(cur_line)
        ctx["src"] = "terminal" if tcfg else "minio"
        path = request.args.get("path", "").strip("/")
        ctx["cur_path"] = path
        # 新现场结构从桶根逐层下钻：车间/产线/检测项目/日期/班组/班次/相机面/文件。
        # 相机面那层的技术码（is7600C_D）显示成标准面名（正面）。
        fmap = face_display_map()
        segs = path.split("/") if path else []
        ctx["crumbs"] = [{"name": fmap.get(s, s),
                          "path": "/".join(segs[:i + 1])} for i, s in enumerate(segs)]
        ctx["root_label"] = scfg["in_bucket"] if scfg else (tcfg["ng_dir"] if tcfg else "根目录")
        ctx["server_addr"] = tcfg["sys_addr"] if tcfg else (scfg["server_addr"] if scfg else "")
        ctx["dirs"] = []; ctx["pics"] = []; ctx["total"] = 0; ctx["err"] = None; ctx["online"] = False
        try:
            if tcfg:
                import stat as _st
                root = (tcfg["ng_dir"] or "").rstrip("/")
                full = root + ("/" + path if path else "")
                t, sftp = _ws_sftp(tcfg)
                try:
                    for e in sorted(sftp.listdir_attr(full), key=lambda x: x.filename):
                        if _st.S_ISDIR(e.st_mode):
                            ctx["dirs"].append({"name": fmap.get(e.filename, e.filename), "raw": e.filename,
                                                "path": (path + "/" + e.filename).strip("/")})
                        elif e.filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                            ctx["pics"].append({"name": e.filename,
                                "url": url_for("media", src="terminal", line=cur_line, key=full + "/" + e.filename)})
                finally:
                    t.close()
            elif scfg:
                s3, cfg = get_s3(scfg)
                prefix = (path + "/") if path else ""
                r = s3.list_objects_v2(Bucket=cfg["in_bucket"], Prefix=prefix, Delimiter="/", MaxKeys=1000)
                for cp in r.get("CommonPrefixes", []):
                    name = cp["Prefix"][len(prefix):].rstrip("/")
                    ctx["dirs"].append({"name": fmap.get(name, name), "raw": name,
                                        "path": (path + "/" + name).strip("/")})
                for o in r.get("Contents", []):
                    if o["Key"] == prefix:
                        continue
                    if o["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                        ctx["pics"].append({"name": o["Key"].split("/")[-1],
                            "url": url_for("media", src="minio", line=cur_line, key=o["Key"])})
            ctx["total"] = len(ctx["pics"])
            # 图片可能上千张，分页显示（30/页）
            ctx["pics"], ctx["pg"] = paginate_list(ctx["pics"], request.args.get("page"), size=30)
            ctx["online"] = True
        except Exception as e:
            ctx["err"] = str(e)[:140]
    return render_template("collect.html", **ctx)


@app.route("/collect/wsimg")
@login_required
def collect_wsimg():
    """代理读取工控机（SFTP）上的一张图片并返回给浏览器。"""
    from flask import Response
    line = request.args.get("line", "")
    path = request.args.get("path", "")
    line_row = get_db().execute("SELECT * FROM prod_lines WHERE name=?", (line,)).fetchone()
    tcfg = line_terminal_cfg(line_row)
    if not tcfg or not path:
        abort(404)
    root = (tcfg["ng_dir"] or "").rstrip("/")
    # 安全：只允许 ng_dir 目录下、禁止路径穿越
    if ".." in path or (root and not (path == root or path.startswith(root + "/"))):
        abort(403)
    try:
        t, sftp = _ws_sftp(tcfg)
        try:
            with sftp.open(path, "rb") as f:
                data = f.read()
        finally:
            t.close()
    except Exception:
        abort(404)
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=300"})


@app.route("/media")
@login_required
def media():
    """统一图片代理：读 MinIO / 工控机原图（bmp 等）转码为 jpg 返回浏览器。"""
    from flask import Response
    import io
    src = request.args.get("src", "")
    line = request.args.get("line", "")
    key = request.args.get("key", "")
    if not key:
        abort(404)
    try:
        if src == "terminal":
            line_row = get_db().execute("SELECT * FROM prod_lines WHERE name=?", (line,)).fetchone()
            tcfg = line_terminal_cfg(line_row)
            root = (tcfg["ng_dir"] or "").rstrip("/") if tcfg else ""
            if not tcfg or ".." in key or not (key == root or key.startswith(root + "/")):
                abort(403)
            t, sftp = _ws_sftp(tcfg)
            try:
                with sftp.open(key, "rb") as f:
                    data = f.read()
            finally:
                t.close()
        else:
            scfg = line_storage_cfg(line)
            if not scfg:
                abort(404)
            s3, cfg = get_s3(scfg)
            data = s3.get_object(Bucket=cfg["in_bucket"], Key=key)["Body"].read()
    except Exception:
        abort(404)
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if ext in ("jpg", "jpeg"):
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "max-age=600"})
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        m = max(w, h)
        if m > 1600:
            img = img.resize((int(w * 1600 / m), int(h * 1600 / m)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return Response(buf.getvalue(), mimetype="image/jpeg", headers={"Cache-Control": "max-age=600"})
    except Exception:
        return Response(data, mimetype="application/octet-stream")


@app.route("/collect/test/storage", methods=["POST"])
@login_required
def collect_test_storage():
    f = request.form
    cfg = {"server_addr": f.get("server_addr", ""), "in_bucket": f.get("in_bucket", ""),
           "username": f.get("username", ""), "password": f.get("password", "")}
    if not cfg["server_addr"]:
        return jsonify({"ok": False, "msg": "请先填写服务器地址"})
    try:
        s3, _ = get_s3(cfg)
        s3.head_bucket(Bucket=cfg["in_bucket"])
        return jsonify({"ok": True, "msg": "连接成功，桶「%s」可访问" % cfg["in_bucket"]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


@app.route("/collect/test/source", methods=["POST"])
@login_required
def collect_test_source():
    """按数据源类型测试连接（用表单当前值，不依赖已保存）。"""
    f = request.form
    if f.get("type") == "ftp":
        tcfg = {"sys_addr": f.get("server_addr", ""), "ng_dir": f.get("ng_dir", "")}
        if not tcfg["sys_addr"]:
            return jsonify({"ok": False, "msg": "请先填写服务地址"})
        try:
            t, sftp = _ws_sftp(tcfg)
            try:
                n = len(sftp.listdir(tcfg["ng_dir"] or "/"))
            finally:
                t.close()
            return jsonify({"ok": True, "msg": "SFTP 连接成功，%s 下 %d 个条目" % (tcfg["ng_dir"] or "/", n)})
        except Exception as e:
            return jsonify({"ok": False, "msg": "连接失败：%s" % str(e)[:150]})
    cfg = {"server_addr": f.get("server_addr", ""), "in_bucket": f.get("in_bucket", ""),
           "username": f.get("username", ""), "password": f.get("password", "")}
    if not cfg["server_addr"]:
        return jsonify({"ok": False, "msg": "请先填写服务地址"})
    try:
        s3, _ = get_s3(cfg)
        s3.head_bucket(Bucket=cfg["in_bucket"])
        return jsonify({"ok": True, "msg": "连接成功，桶「%s」可访问" % cfg["in_bucket"]})
    except Exception as e:
        return jsonify({"ok": False, "msg": "连接失败：%s" % str(e)[:150]})


@app.route("/collect/buckets", methods=["POST"])
@login_required
def collect_buckets():
    """列对象存储上的桶名，供「编辑数据源」的输入桶下拉选择。
    手打桶名错一个字符就静默采不到图，下拉能避免。用表单当前值，不依赖已保存。"""
    f = request.form
    cfg = {"server_addr": f.get("server_addr", "").strip(),
           "username": f.get("username", "").strip(), "password": f.get("password", "").strip()}
    if not cfg["server_addr"]:
        return jsonify({"ok": False, "msg": "请先填写服务地址", "buckets": []})
    try:
        s3, _ = get_s3(cfg)
        names = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
        return jsonify({"ok": True, "msg": "读到 %d 个桶" % len(names), "buckets": sorted(names)})
    except Exception as e:
        return jsonify({"ok": False, "msg": "读取桶列表失败：%s" % str(e)[:140], "buckets": []})


@app.route("/collect/test/terminal", methods=["POST"])
@login_required
def collect_test_terminal():
    f = request.form
    tcfg = {"sys_addr": f.get("sys_addr", ""), "ng_dir": f.get("ng_dir", "")}
    if not tcfg["sys_addr"]:
        return jsonify({"ok": False, "msg": "请先填写系统地址"})
    try:
        t, sftp = _ws_sftp(tcfg)
        try:
            n = len(sftp.listdir(tcfg["ng_dir"] or "/"))
        finally:
            t.close()
        return jsonify({"ok": True, "msg": "SFTP 连接成功，%s 下有 %d 个条目" % (tcfg["ng_dir"], n)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:160]})


# ---------------------------------------------------------------- 对方 CubeStudio 对接
# 对方系统融合了 Label Studio，负责 标注→训练→部署推理。我们是它的业务客户端：
# 建项目、轮询标注进度、拉模型列表、调推理。对方没就绪时用 cubestudio_mock 顶。
def _cs_req(method, path, data=None, timeout=15):
    """调对方 CubeStudio API。基址取自集成配置 cs_url，可随时切到真地址。"""
    import urllib.request
    base = get_cfg("cs_url").rstrip("/")
    if not base:
        raise Exception("未配置 CubeStudio 地址（基础配置→系统集成）")
    headers = {"Content-Type": "application/json"}
    token = get_cfg("cs_token")
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(base + path, data=body, headers=headers)
    req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8")
        d = json.loads(txt) if txt else {}
    # cube-studio 风格：{status, result, message}，status!=0 视为失败；自动解包 result
    if isinstance(d, dict) and "status" in d and "result" in d:
        if d.get("status") not in (0, "0", None):
            raise Exception("CubeStudio %s: %s" % (path, d.get("message", "")))
        return d["result"]
    return d


def cs_ok():
    try:
        return bool((_cs_req("GET", "/health") or {}).get("ok"))
    except Exception:
        return False


def cs_ensure_project(unit, brand, face):
    """为建模单元(牌号×相机面)确保对方项目存在，返回 (project_id, embed_url)。"""
    if unit["cs_project_id"]:
        return unit["cs_project_id"], unit["cs_project_url"]
    r = _cs_req("POST", "/api/projects", {
        "unit_key": unit_key_of(get_db(), brand, face),   # 与样本上传同一稳定标识，CubeStudio 按此 upsert
        "brand": brand, "face_code": face["face_code"], "face_name": face["face_name"],
        "title": "%s · %s" % (brand, face["face_name"])})
    pid, url = r.get("project_id", ""), r.get("embed_url", "")
    if pid:
        db = get_db()
        db.execute("UPDATE model_units SET cs_project_id=?,cs_project_url=? WHERE id=?",
                   (pid, url, unit["id"]))
        db.commit()
    return pid, url


def cs_project_stats(pid):
    try:
        r = _cs_req("GET", "/api/projects/%s/stats" % pid) or {}
        return int(r.get("total", 0)), int(r.get("annotated", 0))
    except Exception as e:
        app.logger.warning("拉 CubeStudio 标注进度失败（%s）：%s", pid, e)
        return 0, 0


def cs_project_models(pid):
    try:
        r = _cs_req("GET", "/api/projects/%s/models" % pid)
        return r if isinstance(r, list) else []
    except Exception as e:
        app.logger.warning("拉 CubeStudio 模型列表失败（%s）：%s", pid, e)
        return []


# ---------------------------------------------------------------- 缺陷标注
_last_sync_ts = [0.0]


def sync_label_images(item_id=0, force=False):
    """按各机台配的实时图像目录扫描 NG 图，登记进 label_images（幂等）。
    带 60 秒节流，避免每次切换页面都重连数据源导致卡顿。

    目录来自「基础配置 → 机组/产线 → 配置目录」(prod_lines.rt_*)，再拼上检测项目的
    路径别名 —— 只扫当前检测项目那一段，不会把别的项目的图混进来。"""
    import time
    import stat
    now = time.time()
    if not force and (now - _last_sync_ts[0]) < 60:
        return
    _last_sync_ts[0] = now
    db = get_db()
    item = db.execute("SELECT * FROM detect_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        return
    existing = {r["src_key"] for r in db.execute("SELECT src_key FROM label_images").fetchall()}
    rows = []
    for l in db.execute("SELECT * FROM prod_lines WHERE rt_type<>'' AND status=1").fetchall():
        rt_type = l["rt_type"]
        line_id = l["id"]
        bucket = l["rt_bucket"] or ""
        path = line_rt_prefix(l, item)
        if rt_type == "minio":
            scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (line_id,)).fetchone()
            if not scfg or not scfg["server_addr"]:
                continue
            try:
                s3, _ = get_s3(scfg)
                tok = None
                while True:
                    kw = {"Bucket": bucket or scfg["in_bucket"], "MaxKeys": 1000,
                          "Prefix": (path + "/") if path else ""}
                    if tok:
                        kw["ContinuationToken"] = tok
                    r = s3.list_objects_v2(**kw)
                    for o in r.get("Contents", []):
                        k = o["Key"]
                        if k.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                            rows.append(("minio", k, item_id, l["name"]))
                    if r.get("IsTruncated"):
                        tok = r.get("NextContinuationToken")
                    else:
                        break
            except Exception as e:
                app.logger.warning("扫描对象存储失败（产线 %s，目录 %s）：%s", l["name"], path, e)
        else:
            # 工控机：从 ng_dir + 配置目录起 SFTP walk
            tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (line_id,)).fetchone()
            if not tcfg or not tcfg["sys_addr"]:
                continue
            root = (tcfg["ng_dir"] or "").rstrip("/") + (("/" + path) if path else "")
            try:
                t, sftp = _ws_sftp(tcfg)

                def walk(p):
                    try:
                        entries = sftp.listdir_attr(p)
                    except IOError:
                        return
                    for e in entries:
                        full = p + "/" + e.filename
                        if stat.S_ISDIR(e.st_mode):
                            walk(full)
                        elif e.filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                            rows.append(("terminal", full, item_id, l["name"]))
                try:
                    walk(root)
                finally:
                    t.close()
            except Exception as e:
                app.logger.warning("扫描工控机失败（产线 %s，目录 %s）：%s", l["name"], path, e)
    added = False
    for source, key, iid, line in rows:
        if key not in existing:
            try:
                db.execute("INSERT INTO label_images(item_id,brand,source,src_key,line_name) "
                           "VALUES(?,?,?,?,?)", (iid, "", source, key, line))
                added = True
            except IntegrityError:
                pass
    if added:
        db.commit()


def label_image_url(img):
    """生成标注图片的可访问 URL（统一走 /media 代理，bmp 自动转 jpg）。"""
    return url_for("media", src=img["source"], line=img["line_name"], key=img["src_key"])


def default_brand(db, brands):
    """默认牌号：优先选已建过建模单元的，避免落在空牌号上看着像"没数据"。"""
    r = db.execute("SELECT brand FROM model_units ORDER BY id LIMIT 1").fetchone()
    if r and any(b["spec"] == r["brand"] for b in brands):
        return r["brand"]
    return brands[0]["spec"] if brands else ""


def unit_det_id(db, u):
    """建模单元所属检测项目 id（由相机面 item_id 隐含），供操作后 redirect 保留上下文。"""
    r = db.execute("SELECT item_id FROM camera_faces WHERE id=?", (u["face_id"],)).fetchone()
    return r["item_id"] if r else 0


def mk_unit_key(proj_code, brand_code, face_code):
    """建模单元稳定标识：项目编码_牌号编码_相机面编码，统一大写，如 XB_3302101_FRONT。
    与 MinIO 目录前缀(斜杠分隔)是两码事——这里是给 CubeStudio 当项目 key 的逻辑标识。"""
    return ("%s_%s_%s" % (proj_code, brand_code, face_code)).upper()


def unit_key_of(db, brand, face):
    """由 (牌号, 相机面行) 解析出 unit_key。标注(REST)与样本上传(MQ)两条链路都用它，
    保证同一单元在 CubeStudio 只对应一个项目。face 需含 item_id、face_code、face_name。"""
    it = db.execute("SELECT code FROM detect_items WHERE id=?", (face["item_id"],)).fetchone()
    br = db.execute("SELECT code FROM brands WHERE spec=?", (brand,)).fetchone()
    pc = (it["code"] if it and it["code"] else "") or "NA"
    bc = (br["code"] if br and br["code"] else "") or brand
    fc = face["face_code"] or face["face_name"]
    return mk_unit_key(pc, bc, fc)


def detect_item_ctx(db):
    """检测项目=全局工作上下文（业界通用的工作区/项目切换器模式）：顶部下拉切换，
    存 session 跨页保持。返回 (det_items列表, 当前det_id)。检测项目由相机面 item_id 隐含。"""
    det_items = [dict(r) for r in db.execute(
        "SELECT * FROM detect_items WHERE status=1 ORDER BY id").fetchall()]
    valid = {i["id"] for i in det_items}
    # 顶栏切换会带 det → 写入 session；否则读 session；都没有才用默认
    cur = request.args.get("det", type=int)
    if cur and cur in valid:
        session["cur_det"] = cur
    else:
        cur = session.get("cur_det") if session.get("cur_det") in valid else None
    if not cur:
        r = db.execute("SELECT di.id FROM detect_items di JOIN camera_faces cf ON cf.item_id=di.id "
                       "WHERE di.status=1 AND cf.status=1 GROUP BY di.id ORDER BY di.id LIMIT 1").fetchone()
        cur = r["id"] if r else (det_items[0]["id"] if det_items else 0)
        session["cur_det"] = cur
    return det_items, cur


@app.route("/label")
@login_required
def label():
    """缺陷标注（新架构）：左侧一级=检测项目、二级=牌号，右侧该项目该牌号的相机面=建模单元。
    检测项目由相机面隐含（camera_faces.item_id），是工作上下文。"""
    db = get_db()
    det_items, cur_det = detect_item_ctx(db)
    brands = [dict(b) for b in db.execute(
        "SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    cur_brand = request.args.get("brand") or default_brand(db, brands)
    faces = [dict(f) for f in db.execute(
        "SELECT * FROM camera_faces WHERE status=1 AND item_id=? ORDER BY sort_order, id",
        (cur_det,)).fetchall()]
    cs_online = cs_ok()
    # 各相机面的样本上传统计（总张数、是否有处理中）
    cur_row = next((i for i in det_items if i["id"] == cur_det), None)
    proj_name = (cur_row["short_name"] or cur_row["name"]) if cur_row else ""
    samp = {}
    for r in db.execute("SELECT face, COALESCE(SUM(img_count),0) cnt, "
                        "SUM(status='processing') proc FROM sample_uploads "
                        "WHERE project=? AND brand=? GROUP BY face", (proj_name, cur_brand)).fetchall():
        samp[r["face"]] = {"count": int(r["cnt"] or 0), "proc": int(r["proc"] or 0)}
    # 各相机面(建模单元)绑定的缺陷分类（可多选，CubeStudio 要求一单元多分类）
    cbind = {}
    for r in db.execute("SELECT face_id,class_id,class_name FROM unit_sample_class WHERE brand=?",
                        (cur_brand,)).fetchall():
        cbind.setdefault(r["face_id"], []).append(dict(r))
    units = []
    for f in faces:
        u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?",
                       (cur_brand, f["id"])).fetchone()
        s = samp.get(f["face_name"], {"count": 0, "proc": 0})
        cb_list = cbind.get(f["id"], [])
        units.append({
            "face_id": f["id"], "face_name": f["face_name"], "face_code": f["face_code"],
            "raw_name": f["raw_name"],
            "exists": bool(u), "unit_id": u["id"] if u else 0,
            "cs_project_id": u["cs_project_id"] if u else "",
            "annotated": u["annotated"] if u else 0, "total": u["total"] if u else 0,
            "model_version": u["model_version"] if u else "",
            "sample_count": s["count"], "sample_proc": s["proc"],
            "class_ids": ",".join(str(c["class_id"]) for c in cb_list),
            "class_name": " / ".join(c["class_name"] for c in cb_list) if cb_list else "",
            "class_list": cb_list,
            "unit_key": unit_key_of(db, cur_brand, dict(f)),
        })
    units, pg = paginate_list(units, request.args.get("page"))
    # 缺陷分类候选：按当前检测项目取启用项，供行编辑弹窗选择（绑定到建模单元）。
    classes = [dict(c) for c in db.execute(
        "SELECT id, name FROM label_classes WHERE item_id=? AND status=1 ORDER BY id",
        (cur_det,)).fetchall()]
    wf_base = get_cfg("workflow_url").rstrip("/")
    return render_template("label.html", det_items=det_items, cur_det=cur_det,
                           brands=brands, cur_brand=cur_brand,
                           units=units, pg=pg, cs_online=cs_online,
                           classes=classes, wf_base=wf_base, active="label")


@app.route("/label/unit/create", methods=["POST"])
@login_required
def label_unit_create():
    """为(牌号×相机面)建立建模单元，并在对方 CubeStudio 建对应项目。"""
    db = get_db()
    brand = request.form.get("brand", "").strip()
    face_id = request.form.get("face_id", type=int)
    if not brand or not face_id:
        abort(400)
    face = db.execute("SELECT * FROM camera_faces WHERE id=?", (face_id,)).fetchone()
    if not face:
        abort(404)
    u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?", (brand, face_id)).fetchone()
    if not u:
        db.execute("INSERT INTO model_units(brand,face_id) VALUES(?,?)", (brand, face_id))
        db.commit()
        u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?", (brand, face_id)).fetchone()
    try:
        cs_ensure_project(dict(u), brand, dict(face))
        flash("已为「%s · %s」建立标注项目" % (brand, face["face_name"]), "success")
    except Exception as e:
        flash("对接 CubeStudio 失败：%s" % str(e)[:120], "error")
    return redirect(url_for("label", det=face["item_id"], brand=brand))


@app.route("/label/unit/class", methods=["POST"])
@login_required
def label_unit_class():
    """为(牌号×相机面)建模单元绑定缺陷分类。一单元一分类，该单元样本上传都用它。"""
    db = get_db()
    brand = request.form.get("brand", "").strip()
    face_id = request.form.get("face_id", type=int)
    class_ids = [int(x) for x in request.form.get("class_ids", "").split(",") if x.strip().isdigit()]
    face = db.execute("SELECT * FROM camera_faces WHERE id=?", (face_id,)).fetchone()
    if not (brand and face):
        return jsonify({"ok": False, "msg": "参数错误"}), 400
    if not class_ids:
        # 允许清空：传空字符串=取消所有绑定
        db.execute("DELETE FROM unit_sample_class WHERE brand=? AND face_id=?", (brand, face_id))
        db.commit()
        app.logger.info("建模单元清空缺陷分类 unit_key=%s by=%s",
                        unit_key_of(db, brand, dict(face)), session.get("username", ""))
        return jsonify({"ok": True, "class_ids": "", "class_name": ""})
    # 校验所有分类有效
    ph = ",".join("?" for _ in class_ids)
    rows = db.execute("SELECT id,name FROM label_classes WHERE id IN (%s) AND status=1" % ph,
                      class_ids).fetchall()
    if len(rows) != len(class_ids):
        return jsonify({"ok": False, "msg": "部分缺陷分类无效或已停用"}), 400
    unit_key = unit_key_of(db, brand, dict(face))
    # 全量替换：先删旧绑定，再插新的
    db.execute("DELETE FROM unit_sample_class WHERE brand=? AND face_id=?", (brand, face_id))
    for r in rows:
        db.execute("INSERT INTO unit_sample_class(brand,face_id,unit_key,class_id,class_name,updated_by) "
                   "VALUES(?,?,?,?,?,?)",
                   (brand, face_id, unit_key, r["id"], r["name"], session.get("username", "")))
    db.commit()
    names = " / ".join(r["name"] for r in rows)
    app.logger.info("建模单元绑定缺陷分类 unit_key=%s classes=%s by=%s",
                    unit_key, names, session.get("username", ""))
    return jsonify({"ok": True, "class_ids": ",".join(str(r["id"]) for r in rows),
                    "class_name": names})


@app.route("/label/class/add", methods=["POST"])
@login_required
def label_class_add():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        # 缺陷类别归属当前检测项目：不同检测项目的缺陷类型本就不同
        item_id = request.form.get("item_id", type=int) or 0
        if not item_id:
            cur = request.args.get("item") or ""
            r = db.execute("SELECT id FROM detect_items WHERE short_name=? OR name=?",
                           (cur, cur)).fetchone()
            item_id = r["id"] if r else 0
        db.execute("INSERT INTO label_classes(name,item_id) VALUES(?,?)", (name, item_id))
        db.commit()
        flash("分类已添加", "success")
    return redirect(url_for("label"))


@app.route("/label/class/delete/<int:cid>", methods=["POST"])
@login_required
def label_class_delete(cid):
    db = get_db()
    db.execute("DELETE FROM label_classes WHERE id=?", (cid,))
    db.commit()
    return redirect(url_for("label"))


@app.route("/label/anno/<int:unit_id>")
@login_required
def label_anno(unit_id):
    """标注入口：内嵌对方 CubeStudio 的标注界面（保留平台导航）。"""
    db = get_db()
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u:
        abort(404)
    u = dict(u)
    face = db.execute("SELECT * FROM camera_faces WHERE id=?", (u["face_id"],)).fetchone()
    title = "%s · %s" % (u["brand"], face["face_name"] if face else "?")
    try:
        pid, embed = cs_ensure_project(u, u["brand"], dict(face) if face else {})
    except Exception as e:
        flash("对接 CubeStudio 失败：%s" % str(e)[:120], "error")
        return redirect(url_for("label", brand=u["brand"]))
    # embed_url 可能是相对路径，拼成绝对地址给 iframe
    src = embed if embed.startswith("http") else get_cfg("cs_url").rstrip("/") + embed
    total, annotated = cs_project_stats(pid)
    db.execute("UPDATE model_units SET total=?,annotated=? WHERE id=?", (total, annotated, unit_id))
    db.commit()
    return render_template("label_cs.html", title=title, unit_id=unit_id, active="label",
                           cs_src=src, total=total, annotated=annotated)


@app.route("/label/workflow")
@login_required
def label_workflow():
    """内嵌视觉标注工作流，按建模单元 unit_key 携带参数。"""
    unit_key = request.args.get("unit_key", "").strip()
    base = get_cfg("workflow_url").rstrip("/")
    if not base:
        flash("未配置视觉工作流地址（基础配置 → 系统集成）", "error")
        return redirect(url_for("label"))
    url = "%s/frontend/visionWorkflow/embed/%s?embed=1" % (base, unit_key)
    app.logger.info("进入工作流 unit_key=%s url=%s by=%s", unit_key, url, session.get("username", ""))
    return render_template("label_workflow.html", wf_src=url, active="label")


# ---------------------------------------------------------------- 样本上传 + RabbitMQ
MQ_EXCHANGE = "defect.sample"
MQ_RK_REQ = "sample.upload"
MQ_RK_REPLY = "sample.upload.reply"
# v1.1：CubeStudio 一键部署推理成功后发 inference.ready，平台落库后必须回执
MQ_RK_INFER = "inference.ready"
MQ_RK_INFER_ACK = "inference.ready.reply"


def _mq_conn():
    import pika
    cred = pika.PlainCredentials(get_cfg("mq_user"), get_cfg("mq_pass"))
    return pika.BlockingConnection(pika.ConnectionParameters(
        get_cfg("mq_host"), int(get_cfg("mq_port") or 5672), "/", cred,
        heartbeat=30, blocked_connection_timeout=10))


def mq_publish_upload(payload):
    """发上传请求到 sample.upload。返回 (ok, err)。"""
    import pika
    try:
        conn = _mq_conn()
        ch = conn.channel()
        ch.exchange_declare(exchange=MQ_EXCHANGE, exchange_type="direct", durable=True)
        body = json.dumps(payload, ensure_ascii=False)
        ch.basic_publish(exchange=MQ_EXCHANGE, routing_key=MQ_RK_REQ,
                         body=body.encode("utf-8"),
                         properties=pika.BasicProperties(delivery_mode=2))
        conn.close()
        app.logger.info("MQ 发布 sample.upload body=%s", body)
        return True, ""
    except Exception as e:
        app.logger.warning("发送 MQ 上传请求失败：%s", e)
        return False, str(e)[:150]


def mq_publish(routing_key, payload):
    """往指定 routing key 发一条持久化消息。返回 (ok, err)。"""
    import pika
    try:
        conn = _mq_conn()
        ch = conn.channel()
        ch.exchange_declare(exchange=MQ_EXCHANGE, exchange_type="direct", durable=True)
        body = json.dumps(payload, ensure_ascii=False)
        ch.basic_publish(exchange=MQ_EXCHANGE, routing_key=routing_key,
                         body=body.encode("utf-8"),
                         properties=pika.BasicProperties(delivery_mode=2))
        conn.close()
        app.logger.info("MQ 发布 %s body=%s", routing_key, body)
        return True, ""
    except Exception as e:
        app.logger.warning("MQ 发布 %s 失败：%s", routing_key, e)
        return False, str(e)[:150]


def unit_by_unit_key(db, unit_key):
    """按 unit_key 反查建模单元。unit_key 是算出来的不入库，这里逐个单元算一遍比对。"""
    if not unit_key:
        return None
    want = unit_key.strip().upper()
    for u in db.execute(
            "SELECT mu.*, cf.item_id, cf.face_code, cf.face_name FROM model_units mu "
            "JOIN camera_faces cf ON cf.id=mu.face_id").fetchall():
        if unit_key_of(db, u["brand"], u) == want:
            return dict(u)
    return None


def _handle_inference_ready(raw):
    """处理 inference.ready：按 unit_key upsert 推理配置，顺手把地址绑到建模单元上，
    然后**必须**回执 inference.ready.reply（对方页面的「信息接收状态」等着它）。"""
    d = json.loads(raw)
    uk = (d.get("unit_key") or "").strip().upper()
    pid = str(d.get("project_id", "") or "")
    predict = d.get("predict_url", "") or d.get("endpoint", "")
    dbc = _DB(_connect())
    try:
        dbc.execute(
            "INSERT INTO inference_services(unit_key,project_id,project_label,service_id,"
            "service_name,model_name,model_version,model_path,model_status,endpoint,health_url,"
            "predict_url,task_type,class_names,event_id,acked) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) ON DUPLICATE KEY UPDATE "
            "project_id=VALUES(project_id),project_label=VALUES(project_label),"
            "service_id=VALUES(service_id),service_name=VALUES(service_name),"
            "model_name=VALUES(model_name),model_version=VALUES(model_version),"
            "model_path=VALUES(model_path),model_status=VALUES(model_status),"
            "endpoint=VALUES(endpoint),health_url=VALUES(health_url),"
            "predict_url=VALUES(predict_url),task_type=VALUES(task_type),"
            "class_names=VALUES(class_names),event_id=VALUES(event_id),acked=0",
            (uk, pid, d.get("project_label", ""), str(d.get("inference_service_id", "") or ""),
             d.get("service_name", ""), d.get("model_name", ""), d.get("model_version", ""),
             d.get("model_path", ""), d.get("model_status", ""), d.get("endpoint", ""),
             d.get("health_url", ""), d.get("predict_url", ""), d.get("task_type", ""),
             ",".join(d.get("class_names") or [])[:1024], d.get("event_id", "")))
        dbc.commit()
        # 绑到建模单元：推理走 predict_url，模型版本/项目号一并回填
        u = unit_by_unit_key(dbc, uk)
        if u:
            dbc.execute("UPDATE model_units SET model_endpoint=?,model_version=?,model_id=?,"
                        "cs_project_id=? WHERE id=?",
                        (predict, d.get("model_version", ""), d.get("model_name", ""),
                         pid or u["cs_project_id"], u["id"]))
            dbc.commit()
            app.logger.info("推理服务就绪并已绑定 unit_key=%s unit=%s endpoint=%s",
                            uk, u["id"], predict)
        else:
            app.logger.warning("推理就绪通知的 unit_key=%s 在平台没有对应建模单元，仅落库", uk)
        # 对方消息里带了回执 routing key 就按它发，没带才用契约默认值
        ok, err = mq_publish(d.get("reply_routing_key") or MQ_RK_INFER_ACK, {
            "event": "inference.ready.ack",
            "event_id": d.get("event_id", ""),
            "unit_key": uk,
            "project_id": pid,
            "status": "ok",
            "message": "已接收" if u else "已接收（平台暂无对应建模单元）",
            "ts": int(time.time()),
        })
        if ok:
            dbc.execute("UPDATE inference_services SET acked=1 WHERE unit_key=?", (uk,))
            dbc.commit()
        else:
            app.logger.warning("推理就绪回执发送失败 unit_key=%s：%s", uk, err)
    finally:
        dbc.close()


def _mq_reply_consumer():
    """后台常驻：消费 sample.upload.reply，按 msg_id 更新上传状态为 done/error。"""
    import pika
    while True:
        try:
            conn = _mq_conn()
            ch = conn.channel()
            ch.exchange_declare(exchange=MQ_EXCHANGE, exchange_type="direct", durable=True)
            ch.queue_declare(queue=MQ_RK_REPLY, durable=True)
            ch.queue_bind(exchange=MQ_EXCHANGE, queue=MQ_RK_REPLY, routing_key=MQ_RK_REPLY)

            def on_reply(chan, method, props, body):
                raw = body.decode("utf-8")
                app.logger.info("MQ 收到回执 sample.upload.reply body=%s", raw)
                try:
                    d = json.loads(raw)
                    mid = d.get("msg_id", "")
                    st = "done" if d.get("status") == "ok" else "error"
                    pid = str(d.get("project_id", "") or "")   # CubeStudio 首建项目时回带
                    dbc = _DB(_connect())
                    dbc.execute("UPDATE sample_uploads SET status=?,reply_msg=? WHERE msg_id=?",
                                (st, str(d.get("message", ""))[:255], mid))
                    if pid:
                        # 回填项目ID：记录本行 + 该建模单元（仅在其还没绑过项目时），统一两条链路
                        dbc.execute("UPDATE sample_uploads SET cs_project_id=? WHERE msg_id=?", (pid, mid))
                        row = dbc.execute("SELECT unit_id FROM sample_uploads WHERE msg_id=?", (mid,)).fetchone()
                        if row and row["unit_id"]:
                            dbc.execute("UPDATE model_units SET cs_project_id=? "
                                        "WHERE id=? AND (cs_project_id='' OR cs_project_id IS NULL)",
                                        (pid, row["unit_id"]))
                    dbc.commit()
                    dbc.close()
                    app.logger.info("样本上传回执 %s → %s%s", mid, st, (" cs_project=%s" % pid) if pid else "")
                except Exception as e:
                    app.logger.warning("处理 MQ 回执失败：%s", e)
                chan.basic_ack(delivery_tag=method.delivery_tag)

            # v1.1：同一条连接上再消费推理就绪通知
            ch.queue_declare(queue=MQ_RK_INFER, durable=True)
            ch.queue_bind(exchange=MQ_EXCHANGE, queue=MQ_RK_INFER, routing_key=MQ_RK_INFER)

            def on_infer_ready(chan, method, props, body):
                raw = body.decode("utf-8")
                app.logger.info("MQ 收到 inference.ready body=%s", raw)
                try:
                    _handle_inference_ready(raw)
                except Exception as e:
                    app.logger.warning("处理推理就绪通知失败：%s", e)
                chan.basic_ack(delivery_tag=method.delivery_tag)

            ch.basic_consume(queue=MQ_RK_REPLY, on_message_callback=on_reply)
            ch.basic_consume(queue=MQ_RK_INFER, on_message_callback=on_infer_ready)
            app.logger.info("MQ 消费者已启动（%s + %s）", MQ_RK_REPLY, MQ_RK_INFER)
            ch.start_consuming()
        except Exception as e:
            app.logger.warning("MQ 回执消费者断线，5秒后重连：%s", e)
            time.sleep(5)


@app.route("/label/sample/upload", methods=["POST"])
@login_required
def label_sample_upload():
    """批量上传样本：存 MinIO(项目/牌号/相机面/) → 记录 → 发 MQ 请求。返回 msg_id。"""
    db = get_db()
    project = request.form.get("project", "").strip()
    brand = request.form.get("brand", "").strip()
    face_id = request.form.get("face_id", type=int)
    files = request.files.getlist("files")
    if not (project and brand and face_id and files):
        return jsonify({"ok": False, "msg": "缺少项目/牌号/相机面或未选图片"}), 400
    face = db.execute("SELECT * FROM camera_faces WHERE id=?", (face_id,)).fetchone()
    if not face:
        return jsonify({"ok": False, "msg": "相机面不存在"}), 400
    # 缺陷分类取建模单元的绑定（可多个，在行编辑弹窗里多选）；未绑定不能上传
    cb_rows = db.execute("SELECT class_id,class_name FROM unit_sample_class WHERE brand=? AND face_id=?",
                         (brand, face_id)).fetchall()
    if not cb_rows:
        return jsonify({"ok": False, "msg": "请先为该相机面绑定缺陷分类（点行内「缺陷分类」编辑）"}), 400
    class_ids = [r["class_id"] for r in cb_rows]
    class_names = [r["class_name"] for r in cb_rows]
    class_name = " / ".join(class_names)
    scfg = db.execute("SELECT * FROM storage_config WHERE server_addr<>'' LIMIT 1").fetchone()
    if not scfg:
        return jsonify({"ok": False, "msg": "未配置对象存储数据源"}), 400
    bucket = get_cfg("sample_bucket")
    # MinIO 路径用编码（避免中文/中文括号）：项目编码/牌号编码/相机面编码/
    # 三个编码在基础数据里可维护：detect_items.code、brands.code、camera_faces.face_code
    it = db.execute("SELECT code FROM detect_items WHERE short_name=? OR name=?", (project, project)).fetchone()
    br = db.execute("SELECT code FROM brands WHERE spec=?", (brand,)).fetchone()
    proj_code = (it["code"] if it and it["code"] else "") or project
    brand_code = (br["code"] if br and br["code"] else "") or brand
    face_code = face["face_code"] or face["face_name"]
    missing = [n for n, c, raw in [("检测项目", it and it["code"], project),
               ("牌号", br and br["code"], brand), ("相机面", face["face_code"], face["face_name"])] if not c]
    prefix = "%s/%s/%s/" % (proj_code, brand_code, face_code)
    try:
        s3, _ = get_s3(scfg)
        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            s3.create_bucket(Bucket=bucket)   # 样本桶不存在自动新建
        import time as _t
        keys = []
        base = int(_t.time() * 1000)
        for i, fp in enumerate(files):
            ext = os.path.splitext(fp.filename)[1].lower() or ".jpg"
            key = prefix + "%d%s" % (base + i, ext)
            s3.upload_fileobj(fp.stream, bucket, key)
            keys.append(key)
    except Exception as e:
        app.logger.error("样本上传 MinIO 失败 project=%s brand=%s face=%s bucket=%s prefix=%s：%s",
                         project, brand, face["face_name"], bucket, prefix, e)
        return jsonify({"ok": False, "msg": "上传 MinIO 失败：%s" % str(e)[:140]}), 500
    app.logger.info("样本已上传 MinIO project=%s brand=%s face=%s class=%s count=%d path=%s/%s by=%s",
                    project, brand, face["face_name"], class_name, len(keys), bucket, prefix,
                    session.get("username", ""))

    import uuid
    msg_id = "u-%s-%s" % (datetime.now().strftime("%Y%m%d"), uuid.uuid4().hex[:8])
    # unit_key = 稳定的建模单元标识（项目编码_牌号编码_相机面编码，大写，如 XB_3302101_FRONT）。
    # CubeStudio 按此 upsert 项目，同一单元多次上传只对应一个项目；msg_id 仅做本次请求/回执关联。
    unit_key = mk_unit_key(proj_code, brand_code, face_code)
    # 若该单元此前已因标注建过 CubeStudio 项目，带上其 id，让两条链路指向同一项目
    munit = db.execute("SELECT id,cs_project_id FROM model_units WHERE brand=? AND face_id=?",
                       (brand, face_id)).fetchone()
    unit_id = munit["id"] if munit else 0
    cs_pid = (munit["cs_project_id"] if munit else "") or ""
    payload = {"msg_id": msg_id, "unit_key": unit_key, "cs_project_id": cs_pid,
               "project": project, "project_code": proj_code,
               "brand": brand, "brand_code": brand_code,
               "face": face["face_name"], "face_code": face_code,
               "class_ids": class_ids, "class_names": class_names,
               "bucket": bucket, "path": prefix, "count": len(keys), "images": keys,
               "minio": {"endpoint": "http://" + scfg["server_addr"],
                         "access_key": scfg["username"], "secret_key": scfg["password"]},
               "ts": int(datetime.now().timestamp())}
    db.execute("INSERT INTO sample_uploads(msg_id,unit_key,unit_id,cs_project_id,project,brand,face,"
               "face_code,class_id,class_name,bucket,path,img_count,status,created_by) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (msg_id, unit_key, unit_id, cs_pid, project, brand, face["face_name"],
                face["face_code"], class_ids[0] if class_ids else 0, class_name,
                bucket, prefix, len(keys), "processing",
                session.get("username", "")))
    db.commit()
    ok, err = mq_publish_upload(payload)
    if not ok:
        app.logger.error("样本上传 MQ 发送失败 msg_id=%s：%s", msg_id, err)
        db.execute("UPDATE sample_uploads SET status='error',reply_msg=? WHERE msg_id=?",
                   ("MQ发送失败：" + err, msg_id))
        db.commit()
        return jsonify({"ok": False, "msg": "已上传但消息发送失败：%s" % err, "msg_id": msg_id})
    app.logger.info("样本上传请求已发送 MQ msg_id=%s rk=%s count=%d body=%s",
                    msg_id, MQ_RK_REQ, len(keys),
                    json.dumps(payload, ensure_ascii=False))
    warn = ("（%s 未设编码，路径暂用名称，建议到基础数据补编码）" % "、".join(missing)) if missing else ""
    return jsonify({"ok": True, "msg_id": msg_id, "count": len(keys), "path": bucket + "/" + prefix, "warn": warn})


@app.route("/api/label/sample/status")
@login_required
def api_label_sample_status():
    """轮询上传状态（processing/done/error）。"""
    ids = request.args.get("ids", "")
    mids = [x for x in ids.split(",") if x.strip()]
    if not mids:
        return jsonify({})
    ph = ",".join(["?"] * len(mids))
    rows = get_db().execute("SELECT msg_id,status,reply_msg FROM sample_uploads WHERE msg_id IN (%s)" % ph,
                            mids).fetchall()
    return jsonify({r["msg_id"]: {"status": r["status"], "reply": r["reply_msg"]} for r in rows})


@app.route("/api/label/sample/history")
@login_required
def api_label_sample_history():
    """某(项目×牌号×相机面)的历次上传：数量、时间、状态，供小窗展示与自动刷新。"""
    face_id = request.args.get("face_id", type=int)
    brand = request.args.get("brand", "")
    db = get_db()
    face = db.execute("SELECT face_name FROM camera_faces WHERE id=?", (face_id,)).fetchone()
    if not face:
        return jsonify({"rows": []})
    it = db.execute("SELECT short_name,name FROM detect_items WHERE id=(SELECT item_id FROM camera_faces WHERE id=?)",
                    (face_id,)).fetchone()
    proj = (it["short_name"] or it["name"]) if it else ""
    rows = db.execute("SELECT img_count,status,reply_msg,class_name,created_at,updated_at,created_by "
                      "FROM sample_uploads WHERE project=? AND brand=? AND face=? ORDER BY id DESC LIMIT 100",
                      (proj, brand, face["face_name"])).fetchall()
    total = sum(r["img_count"] for r in rows)
    proc = sum(1 for r in rows if r["status"] == "processing")
    fmt = lambda t: t.strftime("%Y-%m-%d %H:%M:%S") if t else ""
    return jsonify({"total": total, "proc": proc, "rows": [
        {"count": r["img_count"], "status": r["status"], "reply": r["reply_msg"] or "",
         "class_name": r["class_name"] or "",
         "time": fmt(r["created_at"]),
         "reply_time": fmt(r["updated_at"]) if r["status"] != "processing" else "",
         "by": r["created_by"] or ""} for r in rows]})


@app.route("/api/label/unit/<int:unit_id>/stats")
@login_required
def api_label_unit_stats(unit_id):
    """定时轮询：从对方 CubeStudio 拉最新标注进度并刷新。"""
    db = get_db()
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u or not u["cs_project_id"]:
        return jsonify({"total": 0, "annotated": 0})
    total, annotated = cs_project_stats(u["cs_project_id"])
    db.execute("UPDATE model_units SET total=?,annotated=? WHERE id=?", (total, annotated, unit_id))
    db.commit()
    return jsonify({"total": total, "annotated": annotated})



# ---------------------------------------------------------------- 模型版本
@app.route("/model")
@login_required
def model():
    """模型管理（新架构）：按牌号列出各相机面建模单元，从对方 CubeStudio 拉训出的
    模型列表，绑定到单元供推理用。模型是为「牌号×相机面」训的。"""
    db = get_db()
    det_items, cur_det = detect_item_ctx(db)
    brands = [dict(b) for b in db.execute(
        "SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    cur_brand = request.args.get("brand") or default_brand(db, brands)
    cs_online = cs_ok()
    # 只列当前检测项目(由相机面 item_id 隐含)下、该牌号的建模单元
    all_units = [dict(u) for u in db.execute(
        """SELECT mu.*, cf.face_name, cf.face_code, cf.raw_name, cf.item_id, cf.sort_order
           FROM model_units mu
           JOIN camera_faces cf ON cf.id=mu.face_id
           WHERE mu.brand=? AND cf.item_id=? ORDER BY cf.sort_order, cf.id""",
        (cur_brand, cur_det)).fetchall()]
    # 先分页，只对当前页的单元拉对方模型列表（每行一次远程调用，不能给全部单元都调）
    page_units, pg = paginate_list(all_units, request.args.get("page"))
    rows = []
    for u in page_units:
        models = cs_project_models(u["cs_project_id"]) if (cs_online and u["cs_project_id"]) else []
        n_infer = db.execute("SELECT COUNT(*) AS c FROM inference_results WHERE unit_id=?",
                             (u["id"],)).fetchone()["c"]
        uk = unit_key_of(db, cur_brand, dict(u))
        svc = db.execute("SELECT * FROM inference_services WHERE unit_key=?", (uk,)).fetchone()
        svc_d = dict(svc) if svc else None
        if svc_d and svc_d.get("updated_at") is not None:
            svc_d["updated_at"] = str(svc_d["updated_at"])[:19]  # datetime → 可 JSON 序列化
        rows.append({"unit_key_str": uk, "svc": svc_d,
                     "unit_id": u["id"], "face_name": u["face_name"],
                     "face_raw": u["raw_name"],   # 相机面路径别名，实时目录末段用它
                     "unit_key": unit_key_of(db, cur_brand, dict(u)),
                     "bound_model": u["model_id"], "bound_version": u["model_version"],
                     "bound_endpoint": u["model_endpoint"], "models": models,
                     "infer_cnt": n_infer})
    return render_template("model.html", det_items=det_items, cur_det=cur_det,
                           brands=brands, cur_brand=cur_brand,
                           rows=rows, pg=pg, cs_online=cs_online, active="model")


@app.route("/api/infer/health")
@login_required
def api_infer_health():
    """探对方推理服务在线情况（用 inference.ready 带来的 health_url）。"""
    ok, msg = infer_health((request.args.get("key") or "").strip().upper())
    return jsonify({"ok": ok, "msg": msg})


@app.route("/model/bind", methods=["POST"])
@login_required
def model_bind():
    """把对方的某个模型绑定到建模单元，并触发部署拿到推理地址。"""
    db = get_db()
    unit_id = request.form.get("unit_id", type=int)
    model_id = request.form.get("model_id", "").strip()
    version = request.form.get("version", "").strip()
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u or not model_id:
        abort(400)
    endpoint = ""
    try:
        # 部署该模型拿推理地址（真系统里模型可能已部署，deploy 幂等返回地址）
        r = _cs_req("POST", "/api/models/%s/deploy" % model_id)
        endpoint = (r or {}).get("inference_host_url", "")
    except Exception as e:
        app.logger.error("模型部署失败 unit=%s model=%s：%s", unit_id, model_id, e)
        flash("部署模型失败：%s" % str(e)[:120], "error")
        return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))
    db.execute("UPDATE model_units SET model_id=?,model_version=?,model_endpoint=? WHERE id=?",
               (model_id, version, endpoint, unit_id))
    db.commit()
    app.logger.info("模型已绑定上线 unit=%s brand=%s model=%s version=%s endpoint=%s by=%s",
                    unit_id, u["brand"], model_id, version, endpoint, session.get("username", ""))
    flash("已绑定模型 %s 并上线推理服务" % version, "success")
    return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))


@app.route("/model/train", methods=["POST"])
@login_required
def model_train():
    """触发对方训练（占位演示：mock 立刻产出模型；真系统是异步）。"""
    db = get_db()
    unit_id = request.form.get("unit_id", type=int)
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u or not u["cs_project_id"]:
        abort(400)
    try:
        r = _cs_req("POST", "/api/projects/%s/train" % u["cs_project_id"])
        run_id = (r or {}).get("run_id", "")
        app.logger.info("已触发训练 unit=%s brand=%s cs_project=%s run=%s by=%s",
                        unit_id, u["brand"], u["cs_project_id"], run_id, session.get("username", ""))
        flash("已触发训练（run: %s）" % run_id, "success")
    except Exception as e:
        app.logger.error("触发训练失败 unit=%s cs_project=%s：%s", unit_id, u["cs_project_id"], e)
        flash("触发训练失败：%s" % str(e)[:120], "error")
    return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))


def line_rt_prefix(line, item):
    """机台 × 检测项目的实时目录前缀 = 机台上配的前缀(车间别名/机台别名) + 检测项目路径别名。
    检测项目那一段不入库，运行时按当前上下文拼，这样一台机可服务多个检测项目。"""
    base = (line["rt_path"] or "").strip("/")
    seg = ((item["path_alias"] or item["code"]) if item else "").strip("/")
    return "/".join(x for x in (base, seg) if x)


def sched_machine_of(line):
    """排程表里的机台标识：优先路径别名(现场就是用它建目录)，否则产线编码。"""
    return ((line["path_alias"] or "") or (line["code"] or "")).strip()


def cur_shift(hour=None):
    """按小时判当前班次：早班 6-14，中班 14-22，其余晚班。"""
    if hour is None:
        hour = datetime.now().hour
    if 6 <= hour < 14:
        return "早班"
    if 14 <= hour < 22:
        return "中班"
    return "晚班"


def _rtdir_check(db, rt_type, line_id, bucket, path):
    """校验实时图像目录是否可达、有无图片。返回 (ok, msg)。"""
    line = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not line:
        return False, "请选择数据源"
    path = (path or "").strip().strip("/")
    if rt_type == "minio":
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (line_id,)).fetchone()
        if not scfg or not scfg["server_addr"]:
            return False, "该数据源未配置对象存储"
        try:
            s3, _ = get_s3(scfg)
            r = s3.list_objects_v2(Bucket=(bucket or scfg["in_bucket"]),
                                   Prefix=(path + "/") if path else "", MaxKeys=50)
            n = len([o for o in r.get("Contents", [])
                     if o["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))])
            return True, "连接成功，目录下约 %d+ 张图片" % n if n else "连接成功，但目录下暂无图片"
        except Exception as e:
            return False, "连接失败：%s" % str(e)[:110]
    else:  # ftp/工控机
        tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (line_id,)).fetchone()
        if not tcfg or not tcfg["sys_addr"]:
            return False, "该数据源未配置工控机"
        try:
            import stat as _st
            root = (tcfg["ng_dir"] or "").rstrip("/")
            full = root + ("/" + path if path else "")
            t, sftp = _ws_sftp(tcfg)
            try:
                n = len([e for e in sftp.listdir_attr(full)
                         if e.filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))])
            finally:
                t.close()
            return True, "连接成功，目录下 %d 张图片" % n if n else "连接成功，但目录下暂无图片"
        except Exception as e:
            return False, "连接失败：%s" % str(e)[:110]


@app.route("/config/schedule/mock", methods=["POST"])
@login_required
def schedule_mock():
    """一键生成今天的工单：每台启用机组按 早/中/晚 三班各一张，牌号从牌号字典轮取。
    重复点击先清掉当天同机台的行再生成，不会越点越多。晚班跨零点，结束时间落到明天。"""
    db = get_db()
    lines = db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall()
    brands = [b["spec"] for b in db.execute(
        "SELECT spec FROM brands WHERE status=1 ORDER BY id").fetchall()]
    if not lines or not brands:
        flash("请先维护机组/产线和牌号", "error")
        return redirect(url_for("config", tab="schedule"))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    date = today.strftime("%Y%m%d")
    plan = [("早班", 6, 8), ("中班", 14, 8), ("晚班", 22, 8)]
    n, k = 0, 0
    for l in lines:
        machine = sched_machine_of(l)
        db.execute("DELETE FROM machine_schedule WHERE machine=? AND sched_date=?", (machine, date))
        for i, (shift, hour, span) in enumerate(plan):
            start = today + timedelta(hours=hour)
            end = start + timedelta(hours=span)
            brand = brands[k % len(brands)]
            k += 1
            db.execute("INSERT INTO machine_schedule(machine,sched_date,shift,brand,order_no,"
                       "start_at,end_at) VALUES(?,?,?,?,?,?,?)",
                       (machine, date, shift, brand,
                        "WO%s%s%d" % (date, machine.upper(), i + 1),
                        start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")))
            n += 1
    db.commit()
    app.logger.info("一键生成今日工单 %d 条 by=%s", n, session.get("username", ""))
    flash("已生成今日工单 %d 条（%d 台机组 × 3 班）" % (n, len(lines)), "success")
    return redirect(url_for("config", tab="schedule"))


@app.route("/config/lines/buckets/<int:line_id>")
@login_required
def line_buckets(line_id):
    """列该机台数据源上的桶，供实时目录弹窗选桶。凭据在服务端取，不下发到页面。"""
    db = get_db()
    scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (line_id,)).fetchone()
    if not scfg or not scfg["server_addr"]:
        return jsonify({"ok": False, "msg": "该机台未绑定对象存储数据源", "buckets": [],
                        "default": ""})
    try:
        s3, _ = get_s3(scfg)
        names = sorted(b["Name"] for b in s3.list_buckets().get("Buckets", []))
        return jsonify({"ok": True, "msg": "%s 上 %d 个桶" % (scfg["server_addr"], len(names)),
                        "buckets": names, "default": scfg["in_bucket"] or ""})
    except Exception as e:
        return jsonify({"ok": False, "msg": "读取桶列表失败：%s" % str(e)[:130],
                        "buckets": [], "default": scfg["in_bucket"] or ""})


@app.route("/config/lines/rtdir/test", methods=["POST"])
@login_required
def line_rtdir_test():
    """测试机台实时目录：路径按 前缀 + 当前检测项目别名 拼全，测的就是真实要读的目录。"""
    db = get_db()
    item = db.execute("SELECT * FROM detect_items WHERE id=?",
                      (request.form.get("item_id", type=int) or 0,)).fetchone()
    path = request.form.get("rt_path", "").strip().strip("/")
    seg = ((item["path_alias"] or item["code"]).strip("/")) if item else ""
    full = "/".join(x for x in (path, seg) if x)
    ok, msg = _rtdir_check(db, request.form.get("rt_type", ""),
                           request.form.get("line_id", type=int) or 0,
                           request.form.get("rt_bucket", ""), full)
    return jsonify({"ok": ok, "msg": msg, "path": full})


@app.route("/config/lines/rtdir/save", methods=["POST"])
@login_required
def line_rtdir_save():
    db = get_db()
    line_id = request.form.get("line_id", type=int)
    l = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not l:
        abort(404)
    db.execute("UPDATE prod_lines SET rt_type=?,rt_bucket=?,rt_path=? WHERE id=?",
               (request.form.get("rt_type", "minio"), request.form.get("rt_bucket", "").strip(),
                request.form.get("rt_path", "").strip().strip("/"), line_id))
    db.commit()
    app.logger.info("机台实时目录已保存 line=%s path=%s by=%s", l["name"],
                    request.form.get("rt_path", ""), session.get("username", ""))
    flash("实时图像目录已保存", "success")
    return redirect(url_for("config", tab="lines"))


def _rtdir_list_images(db, rt_type, line_id, bucket, path):
    """列出实时目录下的全部图片 key。返回 (keys列表, s3客户端或None, bucket, ftp信息)。
    minio 返回 (keys, s3, bucket, None)；ftp 返回 (keys, None, None, tcfg)。"""
    path = (path or "").strip("/")
    if rt_type == "minio":
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (line_id,)).fetchone()
        if not scfg:
            raise Exception("数据源未配置对象存储")
        bucket = bucket or scfg["in_bucket"]
        s3, _ = get_s3(scfg)
        keys, tok = [], None
        while True:
            kw = {"Bucket": bucket, "Prefix": (path + "/") if path else "", "MaxKeys": 1000}
            if tok:
                kw["ContinuationToken"] = tok
            r = s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in r.get("Contents", [])
                     if o["Key"].lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))]
            if not r.get("IsTruncated"):
                break
            tok = r.get("NextContinuationToken")
        return keys, s3, bucket, None
    else:  # ftp
        tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (line_id,)).fetchone()
        if not tcfg:
            raise Exception("数据源未配置工控机")
        import stat as _st
        root = (tcfg["ng_dir"] or "").rstrip("/") + ("/" + path if path else "")
        keys = []
        t, sftp = _ws_sftp(tcfg)
        try:
            def walk(p):
                for e in sftp.listdir_attr(p):
                    fp = p + "/" + e.filename
                    if _st.S_ISDIR(e.st_mode):
                        walk(fp)
                    elif e.filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                        keys.append(fp)
            walk(root)
        finally:
            t.close()
        return keys, None, None, tcfg


def _analysis_worker(line_id, item_id):
    """后台线程：按「机台 × 检测项目」扫实时目录，逐张定位建模单元后送推理入库。

    一张图的归属全部从路径和排程反查，而不是靠人配：
      目录 前缀/日期/班组/班次/相机面/文件 → 相机面查 camera_faces，
      牌号查 machine_schedule(机台+日期+班次) → 单元 = (牌号, 相机面) → 它绑的模型。
    增量：已在 inference_results 的 src_key 跳过。用独立 DB 连接（不能用 g.db）。"""
    db = _DB(_connect())

    def upd(**kw):
        cols = ",".join("%s=%%s" % k for k in kw)
        db.execute("UPDATE analysis_jobs SET " + cols.replace("%%s", "?") +
                   " WHERE line_id=? AND item_id=?", tuple(kw.values()) + (line_id, item_id))
        db.commit()

    def log(level, detail, src_key=""):
        db.execute("INSERT INTO analysis_logs(line_id,item_id,level,src_key,detail) VALUES(?,?,?,?,?)",
                   (line_id, item_id, level, src_key, detail[:500]))
        db.commit()

    def is_on():
        r = db.execute("SELECT enabled FROM analysis_jobs WHERE line_id=? AND item_id=?",
                       (line_id, item_id)).fetchone()
        return bool(r and r["enabled"])

    try:
        line = dict(db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone())
        item = dict(db.execute("SELECT * FROM detect_items WHERE id=?", (item_id,)).fetchone())
        prefix = line_rt_prefix(line, item)
        machine = sched_machine_of(line)
        log("info", "工作开启：%s / %s，目录 %s/%s" % (
            line["name"], item["short_name"] or item["name"], line["rt_bucket"] or "(默认桶)", prefix))
        # 开关式：开启后持续工作 —— 每轮扫目录处理新图，处理完歇 15 秒再扫；关闭则退出
        total_an = 0
        while is_on():
            keys, s3, bucket, _ = _rtdir_list_images(db, line["rt_type"], line_id,
                                                     line["rt_bucket"], prefix)
            # 每轮重载：中途绑了模型/改了相机面，下一轮就生效
            faces = {f["raw_name"]: dict(f) for f in db.execute(
                "SELECT * FROM camera_faces WHERE item_id=? AND status=1", (item_id,)).fetchall()}
            units = {(u["brand"], u["face_id"]): dict(u) for u in db.execute(
                "SELECT * FROM model_units WHERE model_endpoint<>''").fetchall()}
            done = {r["src_key"] for r in db.execute(
                "SELECT src_key FROM inference_results WHERE src_key<>''").fetchall()}
            upd(status="running", total=len(keys), read_cnt=0, analyzed=0, skipped=0, msg="扫描目录…")
            read = an = sk = no_face = no_sched = no_model = 0
            for key in keys:
                if not is_on():
                    break
                read += 1
                if key in done:
                    sk += 1
                else:
                    # 相对前缀的路径：日期/班组/班次/相机面/文件
                    rel = key[len(prefix):].strip("/") if prefix else key.strip("/")
                    p = rel.split("/")
                    if len(p) < 5:
                        no_face += 1
                        continue
                    date, shift, raw_face = p[0], p[2], p[3]
                    face = faces.get(raw_face)
                    if not face:
                        no_face += 1
                        continue
                    brand = resolve_brand(db, machine, date, shift, key_shot_time(key))
                    if not brand:
                        no_sched += 1
                        continue
                    unit = units.get((brand, face["id"]))
                    if not unit:
                        no_model += 1
                        continue
                    # 对方推理服务在别的机器上，进不来平台的 /media 代理，给它临时直链
                    img_url = ""
                    if s3 is not None:
                        try:
                            img_url = s3.generate_presigned_url(
                                "get_object", Params={"Bucket": bucket, "Key": key},
                                ExpiresIn=1800)
                        except Exception:
                            img_url = ""
                    res = infer_call(unit["model_endpoint"], key, img_url)
                    if res:
                        db.execute("INSERT INTO inference_results(src_key,unit_id,machine,line_name,brand,"
                                   "face_name,img_date,shift,is_defect,class_name,confidence,model_version) "
                                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (key, unit["id"], machine, line["name"], brand,
                                    face["face_name"], date, shift, res["is_defect"],
                                    res["class_name"], res["confidence"], res["model_version"]))
                        db.commit()
                        an += 1
                        total_an += 1
                        log("ok", "%s · %s → %s (置信 %.2f)" % (
                            brand, face["face_name"],
                            "正常" if res["is_defect"] == 0 else res["class_name"],
                            res["confidence"]), key)
                    else:
                        log("error", "推理调用失败，跳过", key)
                if read % 5 == 0 or read == len(keys):
                    upd(read_cnt=read, analyzed=an, skipped=sk, msg="分析中 %d/%d" % (read, len(keys)))
            if not is_on():
                break
            tip = "本轮完成(新%d/跳%d" % (an, sk)
            if no_sched:
                tip += "/无排程%d" % no_sched
            if no_model:
                tip += "/无模型%d" % no_model
            if no_face:
                tip += "/面未映射%d" % no_face
            tip += ")，累计%d，等待新图…" % total_an
            upd(status="running", read_cnt=read, analyzed=an, skipped=sk, msg=tip)
            if no_sched or no_model or no_face:
                log("warn", tip)
            # 歇一会再扫新图；期间每秒检查开关，关了立刻退
            for _ in range(15):
                if not is_on():
                    break
                time.sleep(1)
        upd(status="stopped", msg="已关闭，累计新分析 %d 张" % total_an)
        log("info", "工作关闭，累计新分析 %d 张" % total_an)
    except Exception as e:
        app.logger.exception("实时目录分析失败 line=%s item=%s", line_id, item_id)
        upd(status="error", enabled=0, msg="失败：%s" % str(e)[:180])
        try:
            log("error", "任务异常：%s" % str(e)[:400])
        except Exception:
            pass
    finally:
        db.close()


@app.route("/config/lines/analyze/toggle", methods=["POST"])
@login_required
def line_analyze_toggle():
    """工作状态开关（按 机台 × 检测项目）：开=起后台线程持续工作；关=置 enabled=0 自行退出。"""
    db = get_db()
    line_id = request.form.get("line_id", type=int)
    item_id = request.form.get("item_id", type=int) or 0
    on = request.form.get("on") == "1"
    l = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not l or not item_id:
        abort(404)
    if on:
        if not (l["rt_type"] and (l["rt_bucket"] or l["rt_path"] or l["rt_type"] == "ftp")):
            flash("该机台未配置实时图像目录", "error")
            return redirect(url_for("config", tab="lines"))
        db.execute("INSERT INTO analysis_jobs(line_id,item_id,status,enabled,msg) VALUES(?,?,?,1,?) "
                   "ON DUPLICATE KEY UPDATE enabled=1,status='running',msg=VALUES(msg)",
                   (line_id, item_id, "running", "启动中…"))
        db.commit()
        import threading
        threading.Thread(target=_analysis_worker, args=(line_id, item_id), daemon=True).start()
        app.logger.info("开启实时分析 line=%s item=%s by=%s", l["name"], item_id,
                        session.get("username", ""))
        flash("已开启工作：%s" % l["name"], "success")
    else:
        db.execute("UPDATE analysis_jobs SET enabled=0 WHERE line_id=? AND item_id=?",
                   (line_id, item_id))
        db.commit()
        flash("已关闭工作（正在停止当前轮次）", "success")
    return redirect(url_for("config", tab="lines"))


@app.route("/api/lines/analyze/status")
@login_required
def api_line_analyze_status():
    """轮询：返回当前检测项目下各机台的工作状态与分析进度。"""
    ids = request.args.get("lines", "")
    item_id = request.args.get("item", type=int) or 0
    line_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not line_ids or not item_id:
        return jsonify({})
    ph = ",".join(["?"] * len(line_ids))
    rows = get_db().execute("SELECT * FROM analysis_jobs WHERE item_id=? AND line_id IN (%s)" % ph,
                            [item_id] + line_ids).fetchall()
    return jsonify({str(r["line_id"]): {"status": r["status"], "enabled": r["enabled"],
                    "total": r["total"], "read": r["read_cnt"], "analyzed": r["analyzed"],
                    "skipped": r["skipped"], "msg": r["msg"]} for r in rows})


@app.route("/config/lines/logs/<int:line_id>")
@login_required
def line_analyze_logs(line_id):
    """审计日志：某机台在当前检测项目下的分析调用过程/结果。"""
    db = get_db()
    _, cur_det = detect_item_ctx(db)
    item_id = request.args.get("item", type=int) or cur_det
    l = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not l:
        abort(404)
    item = db.execute("SELECT * FROM detect_items WHERE id=?", (item_id,)).fetchone()
    logs, pg = paginate(db, "SELECT * FROM analysis_logs WHERE line_id=%d AND item_id=%d "
                        "ORDER BY id DESC" % (line_id, item_id),
                        page=request.args.get("page"), size=30)
    return render_template("analyze_logs.html", line=dict(l),
                           item=dict(item) if item else None,
                           logs=[dict(r) for r in logs], pg=pg, active="config")


@app.route("/config/lines/schedule/<int:line_id>")
@login_required
def line_schedule(line_id):
    """当前运行工单：该机台今天的排程 + 当前班次牌号 + 该牌号各相机面的模型绑定情况。
    把「排程 → 牌号 → 单元 → 模型」这条链一次性摊开，一眼看出现在能不能出结果。"""
    db = get_db()
    _, cur_det = detect_item_ctx(db)
    item_id = request.args.get("item", type=int) or cur_det
    l = db.execute("SELECT * FROM prod_lines WHERE id=?", (line_id,)).fetchone()
    if not l:
        abort(404)
    machine = sched_machine_of(l)
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    shift = cur_shift()
    day = [{"shift": r["shift"], "brand": r["brand"], "order_no": r["order_no"] or "",
            "start": str(r["start_at"])[:16] if r["start_at"] else "",
            "end": str(r["end_at"])[:16] if r["end_at"] else "",
            "now": bool(r["start_at"] and r["end_at"] and r["start_at"] <= now < r["end_at"])}
           for r in db.execute(
        "SELECT * FROM machine_schedule WHERE machine=? AND sched_date=? AND status=1 "
        "ORDER BY FIELD(shift,'早班','中班','晚班',''), id", (machine, today)).fetchall()]
    cur_wo = next((d for d in day if d["now"]), None)
    brand = (cur_wo["brand"] if cur_wo else "") or resolve_brand(db, machine, today, shift)
    faces = []
    if brand:
        for f in db.execute("SELECT * FROM camera_faces WHERE item_id=? AND status=1 "
                            "ORDER BY sort_order, id", (item_id,)).fetchall():
            u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?",
                           (brand, f["id"])).fetchone()
            faces.append({"face_name": f["face_name"], "raw_name": f["raw_name"],
                          "version": (u["model_version"] if u else "") or "",
                          "online": bool(u and u["model_endpoint"])})
    return jsonify({"machine": machine, "line_name": l["name"], "date": today, "shift": shift,
                    "brand": brand, "day": day, "faces": faces,
                    "order_no": (cur_wo or {}).get("order_no", ""),
                    "start": (cur_wo or {}).get("start", ""), "end": (cur_wo or {}).get("end", ""),
                    "bound": len([f for f in faces if f["online"]]), "total_faces": len(faces)})


# ---------------------------------------------------------------- 分析结果
ANALYSIS_TABS = [("shift", "当班统计"), ("history", "历史统计"), ("trend", "趋势分析")]


def _infer_base(cur_det, cur_brand):
    """推理结果的检测项目+牌号过滤。检测项目经 unit→face→item 关联。"""
    base = ("FROM inference_results WHERE unit_id IN "
            "(SELECT mu.id FROM model_units mu JOIN camera_faces cf ON cf.id=mu.face_id WHERE cf.item_id=?)")
    params = [cur_det]
    if cur_brand and cur_brand != "all":
        base += " AND brand=?"
        params.append(cur_brand)
    return base, params


@app.route("/analysis/<tab>")
@login_required
def analysis(tab):
    if tab not in dict(ANALYSIS_TABS):
        abort(404)
    db = get_db()
    det_items, cur_det = detect_item_ctx(db)
    brands = [dict(b) for b in db.execute("SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    cur_brand = request.args.get("brand", "all")
    base, params = _infer_base(cur_det, cur_brand)
    infer_total = db.execute("SELECT COUNT(*) AS c " + base, params).fetchone()["c"]
    defect_total = db.execute("SELECT COUNT(*) AS c " + base + " AND is_defect=1", params).fetchone()["c"]
    # 该检测项目下已绑定模型的单元数（可推理）
    ready = db.execute("SELECT COUNT(*) AS c FROM model_units mu JOIN camera_faces cf ON cf.id=mu.face_id "
                       "WHERE mu.model_endpoint<>'' AND cf.item_id=?", (cur_det,)).fetchone()["c"]
    return render_template("analysis.html", tab=tab, tabs=ANALYSIS_TABS,
                           det_items=det_items, cur_det=cur_det,
                           brands=brands, cur_brand=cur_brand, active="analysis",
                           infer_total=infer_total, defect_total=defect_total, ready_units=ready,
                           today=datetime.now().strftime("%Y/%m/%d"))


def _parse_ls_predict(d):
    """解析 Label Studio ML Backend 的预测响应（CubeStudio 部署的推理服务用这个协议）。
    没有任何标注框 = 没检出缺陷，记为正常，而不是丢弃。"""
    res = (d.get("results") or [{}])[0]
    items = res.get("result") or []
    ver = d.get("model_version") or res.get("model_version") or ""
    if not items:
        return {"is_defect": 0, "class_name": "正常",
                "confidence": float(res.get("score") or 0), "model_version": ver}
    top = items[0]
    val = top.get("value") or {}
    labels = (val.get("rectanglelabels") or val.get("polygonlabels")
              or val.get("choices") or val.get("labels") or [])
    return {"is_defect": 1, "class_name": labels[0] if labels else "",
            "confidence": float(top.get("score") or res.get("score") or 0),
            "model_version": ver}


def infer_call(endpoint, src_key, image_url=""):
    """调建模单元绑定的推理服务；失败返回 None（跳过，不编造）。

    endpoint 由 inference.ready 下发绑定。predict_url(含 /predict)走 Label Studio ML
    Backend 协议(图片以 URL 传给对方，对方自己拉图)；其它 endpoint 走 {src_key} 简易协议。"""
    try:
        import urllib.request
        is_ls = "/predict" in (endpoint or "")
        payload = ({"tasks": [{"id": 1, "data": {"image": image_url or src_key}}]}
                   if is_ls else {"src_key": src_key})
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        if is_ls or (isinstance(d, dict) and "results" in d):
            return _parse_ls_predict(d)
        # 兼容对方 {status,result,message} 包裹
        if isinstance(d, dict) and "result" in d and "status" in d:
            d = d["result"]
        return {"is_defect": int(d.get("is_defect", 1)), "class_name": d.get("class_name", ""),
                "confidence": float(d.get("confidence", 0)), "model_version": d.get("model_version", "")}
    except Exception as e:
        app.logger.warning("推理服务调用失败，跳过（%s）：%s", src_key, e)
        return None


def infer_health(unit_key):
    """调用前探一下推理服务是否在线（契约建议行为 3）。返回 (ok, msg)。"""
    import urllib.request
    r = get_db().execute("SELECT * FROM inference_services WHERE unit_key=?",
                         (unit_key,)).fetchone()
    if not r or not r["health_url"]:
        return False, "尚未收到该单元的推理就绪通知"
    try:
        with urllib.request.urlopen(r["health_url"], timeout=6) as resp:
            return resp.status == 200, "HTTP %d" % resp.status
    except Exception as e:
        return False, str(e)[:120]


def key_shot_time(key):
    """从图片文件名取拍摄时刻。现场文件名就是毫秒时间戳(如 1784200000024.BMP)，
    有它就能把图精确落到某张工单上；名字不是时间戳则返回 None，退回按班次匹配。"""
    name = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not (name.isdigit() and len(name) in (10, 13)):
        return None
    try:
        return datetime.fromtimestamp(int(name) / (1000.0 if len(name) == 13 else 1.0))
    except Exception:
        return None


def resolve_brand(db, machine, date, shift, shot_at=None):
    """工单反查牌号：优先按拍摄时刻落在哪张工单的起止区间内（能处理跨班/临时换牌号），
    没有时刻或工单没填时间时，退回 机台+日期+班次，再退到全天(班次为空)。"""
    if shot_at is not None:
        r = db.execute("SELECT brand FROM machine_schedule WHERE machine=? AND status=1 "
                       "AND start_at IS NOT NULL AND end_at IS NOT NULL "
                       "AND start_at<=? AND end_at>? ORDER BY start_at DESC LIMIT 1",
                       (machine, shot_at, shot_at)).fetchone()
        if r:
            return r["brand"]
    r = db.execute("SELECT brand FROM machine_schedule WHERE machine=? AND sched_date=? "
                   "AND shift=? AND status=1", (machine, date, shift)).fetchone()
    if not r:
        r = db.execute("SELECT brand FROM machine_schedule WHERE machine=? AND sched_date=? "
                       "AND shift='' AND status=1", (machine, date)).fetchone()
    return r["brand"] if r else ""


@app.route("/analysis/infer", methods=["POST"])
@login_required
def analysis_infer():
    """按新架构推理：扫数据源图 → 排程反查牌号 + 相机面 → 找单元绑定的模型 → 推理存库。"""
    db = get_db()
    scfg = db.execute("SELECT * FROM storage_config WHERE server_addr<>'' LIMIT 1").fetchone()
    if not scfg:
        flash("未配置对象存储数据源", "error")
        return redirect(request.referrer or url_for("analysis", tab="shift"))
    # 相机面映射、建模单元(含绑定 endpoint)一次性载入
    face_by_raw = {f["raw_name"]: dict(f) for f in db.execute(
        "SELECT * FROM camera_faces WHERE status=1").fetchall()}
    units = {(u["brand"], u["face_id"]): dict(u) for u in db.execute(
        "SELECT * FROM model_units WHERE model_endpoint<>''").fetchall()}
    done = {r["src_key"] for r in db.execute(
        "SELECT src_key FROM inference_results WHERE src_key<>''").fetchall()}

    s3, cfg = get_s3(scfg)
    n = skipped = no_sched = no_model = 0
    tok = None
    while True:
        kw = {"Bucket": cfg["in_bucket"], "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            key = o["Key"]
            if not key.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")) or key in done:
                continue
            # 路径：车间/机台/检测项目/日期/班组/班次/相机面/文件
            p = key.split("/")
            if len(p) < 8:
                continue
            machine, date, shift, raw_face = p[1], p[3], p[5], p[6]
            face = face_by_raw.get(raw_face)
            if not face:
                continue  # 相机面未映射，跳过
            brand = resolve_brand(db, machine, date, shift, key_shot_time(key))
            if not brand:
                no_sched += 1
                continue
            unit = units.get((brand, face["id"]))
            if not unit:
                no_model += 1
                continue
            try:
                img_url = s3.generate_presigned_url(
                    "get_object", Params={"Bucket": cfg["in_bucket"], "Key": key}, ExpiresIn=1800)
            except Exception:
                img_url = ""
            res = infer_call(unit["model_endpoint"], key, img_url)
            if res is None:
                skipped += 1
                continue
            db.execute("INSERT INTO inference_results(src_key,unit_id,machine,line_name,brand,face_name,"
                       "img_date,shift,is_defect,class_name,confidence,model_version) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, unit["id"], machine, machine, brand, face["face_name"],
                        date, shift, res["is_defect"], res["class_name"], res["confidence"],
                        res["model_version"]))
            n += 1
        if not r.get("IsTruncated"):
            break
        tok = r.get("NextContinuationToken")
    db.commit()
    app.logger.info("批量推理完成 新增=%d 未排程=%d 未绑模型=%d 调用失败=%d by=%s",
                    n, no_sched, no_model, skipped, session.get("username", ""))
    tip = "推理完成，新增 %d 条" % n
    if no_sched:
        tip += "；%d 张排程未排到牌号" % no_sched
    if no_model:
        tip += "；%d 张对应单元未绑定模型" % no_model
    if skipped:
        tip += "；%d 张推理调用失败" % skipped
    flash(tip, "success")
    return redirect(request.referrer or url_for("analysis", tab="shift"))


@app.route("/api/analysis/<tab>")
@login_required
def api_analysis(tab):
    db = get_db()
    _, cur_det = detect_item_ctx(db)
    brand = request.args.get("brand", "all")
    base, params = _infer_base(cur_det, brand)
    base += " AND is_defect=1"
    cls_expr = "COALESCE(NULLIF(class_name,''),'未分类')"

    def _fmt(d):
        return d[:4] + "/" + d[4:6] + "/" + d[6:8] if len(d) == 8 and d.isdigit() else d

    if tab == "shift":
        rows = db.execute("SELECT %s n, COUNT(*) c " % cls_expr + base +
                          " GROUP BY n ORDER BY c DESC", params).fetchall()
        return jsonify({"classes": [r["n"] for r in rows], "counts": [r["c"] for r in rows]})

    days = [r["img_date"] for r in db.execute(
        "SELECT DISTINCT img_date " + base + " AND img_date<>'' ORDER BY img_date", params).fetchall()]
    top = [r["n"] for r in db.execute(
        "SELECT %s n, COUNT(*) c " % cls_expr + base + " GROUP BY n ORDER BY c DESC", params).fetchall()][:4]
    series = []
    for cn in top:
        data = []
        for d in days:
            cnt = db.execute("SELECT COUNT(*) c " + base + " AND %s=? AND img_date=?" % cls_expr,
                             params + [cn, d]).fetchone()["c"]
            data.append(cnt)
        series.append({"name": cn, "data": data})
    return jsonify({"days": [_fmt(d) for d in days], "series": series})


# ----------------------------------------------------------------
app.logger.info("平台启动中 db=%s:%s/%s log_dir=%s", DB_HOST, DB_PORT, DB_NAME, LOG_DIR)
init_db()
app.logger.info("数据库初始化完成")

# 启动 MQ 回执消费者（守护线程，随进程存活）
try:
    import threading
    threading.Thread(target=_mq_reply_consumer, daemon=True).start()
except Exception as _e:
    app.logger.warning("MQ 回执消费者未能启动：%s", _e)

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", "9573"))
    app.logger.info("HTTP 服务监听 0.0.0.0:%d", _port)
    app.run(host="0.0.0.0", port=_port, debug=False)
