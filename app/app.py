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
    "ls_url": os.environ.get("LS_URL", "http://127.0.0.1:8080"),
    "ls_token": os.environ.get("LS_TOKEN", "cigarette-label-studio-token-2026"),
    "ls_user": os.environ.get("LS_USER", "admin@cigarette.local"),
    "ls_webhook_url": os.environ.get("LS_WEBHOOK_URL", ""),  # 本平台回调地址，注册进 LS
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
    ("ls_url", "Label Studio 地址", "浏览器与平台都要能访问，故必须用 IP 不能用 127.0.0.1"),
    ("ls_token", "Label Studio API Token", "LS 1.23+ 需在组织设置里开启 legacy token 才可用"),
    ("ls_user", "Label Studio 登录账号", "仅用于标注页提示，不参与鉴权"),
    ("ls_webhook_url", "标注回调地址(Webhook)", "留空则用本机地址推算；LS 标注后回写进度到此"),
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
        # 检测项目升级为一等公民：自己挂数据源目录与外部系统项目 id
        ("detect_items", "src_prefix", "VARCHAR(128) DEFAULT '' COMMENT '数据源顶层目录(如 小包外观)'"),
        ("detect_items", "ls_project_id", "INT DEFAULT 0 COMMENT '对应 Label Studio 项目ID, 0=未建'"),
        ("detect_items", "cs_project_id", "VARCHAR(64) DEFAULT '' COMMENT '对应 CubeStudio 项目组(预留)'"),
        # 缺陷类别与标注图片归属到检测项目
        ("label_classes", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("label_images", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("label_tasks", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("inference_results", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        ("model_versions", "item_id", "INT DEFAULT 0 COMMENT '所属检测项目(detect_items.id)'"),
        # 新架构推理：结果不再来自 label_images，直接记 机台/相机面/单元/对象key
        ("inference_results", "machine", "VARCHAR(64) DEFAULT '' COMMENT '机台/产线'"),
        ("inference_results", "face_name", "VARCHAR(64) DEFAULT '' COMMENT '相机面'"),
        ("inference_results", "unit_id", "INT DEFAULT 0 COMMENT '建模单元(model_units.id)'"),
        ("inference_results", "src_key", "VARCHAR(512) DEFAULT '' COMMENT '对象key'"),
        # 实时图像目录：绑定相机采集图片的持续输入目录，job 服务定时从此读图送推理
        ("model_units", "rt_type", "VARCHAR(16) DEFAULT '' COMMENT '实时目录类型: minio/ftp'"),
        ("model_units", "rt_line_id", "INT DEFAULT 0 COMMENT '复用哪条产线的数据源凭据(prod_lines.id)'"),
        ("model_units", "rt_bucket", "VARCHAR(128) DEFAULT '' COMMENT '桶名(minio)'"),
        ("model_units", "rt_path", "VARCHAR(512) DEFAULT '' COMMENT '目录路径'"),
        # 数据源独立化：产线引用某个数据源（多产线可共用）。storage_config/terminal_config
        # 退化为由数据源派生的按产线缓存（rebuild_line_cfg 重建），现有读代码不用改。
        ("prod_lines", "source_id", "INT DEFAULT 0 COMMENT '引用的数据源(data_sources.id)'"),
        ("analysis_jobs", "enabled", "INT DEFAULT 0 COMMENT '工作状态开关: 1开启 0关闭'"),
        # 样本上传：msg_id 只做消息关联，项目归属改用稳定的 unit_key（CubeStudio 按此 upsert，
        # 避免同一建模单元多次上传被建成多个项目）。
        ("sample_uploads", "unit_key", "VARCHAR(160) DEFAULT '' COMMENT '建模单元稳定标识=项目编码_牌号编码_相机面编码(大写)'"),
        ("sample_uploads", "unit_id", "INT DEFAULT 0 COMMENT '建模单元(model_units.id), 0=尚未建单元'"),
        ("sample_uploads", "cs_project_id", "VARCHAR(64) DEFAULT '' COMMENT 'CubeStudio项目ID(回执回填)'"),
        ("sample_uploads", "class_id", "INT DEFAULT 0 COMMENT '缺陷分类ID(label_classes.id), 上传必选'"),
        ("sample_uploads", "class_name", "VARCHAR(64) DEFAULT '' COMMENT '缺陷分类名称, 随MQ消息发给CubeStudio'"),
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

    # 老数据按牌号存（牌号是 BRAND_MAP 编出来的），归到第一个配了数据源目录的检测
    # 项目。只在刚加列时回填一次，避免误伤之后 item_id 合法为 0 的行。
    if "model_versions" in added:
        it = db.execute("SELECT id FROM detect_items WHERE src_prefix<>'' ORDER BY id LIMIT 1").fetchone()
        if it:
            db.execute("UPDATE model_versions SET item_id=? WHERE item_id=0", (it["id"],))
            db.commit()

    # 一次性引导：把既有数据归到「小包CCD」项目下（此前 BRAND_MAP 硬编码 小包外观→玉溪（硬））
    row = db.execute("SELECT COUNT(*) AS c FROM detect_items WHERE src_prefix<>''").fetchone()
    if not row["c"]:
        it = db.execute("SELECT id FROM detect_items WHERE short_name=? OR name=?",
                        ("小包CCD", "小包CCD检测")).fetchone()
        if it:
            db.execute("UPDATE detect_items SET src_prefix=? WHERE id=?", ("小包外观", it["id"]))
            db.execute("UPDATE label_classes SET item_id=? WHERE item_id=0", (it["id"],))
            db.execute("UPDATE label_images SET item_id=? WHERE item_id=0", (it["id"],))
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
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='检测项目表'""",
        """CREATE TABLE IF NOT EXISTS prod_lines(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            code VARCHAR(64) NOT NULL COMMENT '产线编码',
            name VARCHAR(64) NOT NULL COMMENT '产线名称',
            workshop VARCHAR(64) DEFAULT '' COMMENT '所属车间',
            area VARCHAR(64) DEFAULT '' COMMENT '所属区域',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='机组/产线表'""",
        """CREATE TABLE IF NOT EXISTS brands(
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
            unit_id INT PRIMARY KEY COMMENT '建模单元(model_units.id), 一单元一当前任务',
            status VARCHAR(16) DEFAULT 'idle' COMMENT 'idle/running/done/error/stopped',
            enabled INT DEFAULT 0 COMMENT '工作状态开关: 1开启 0关闭',
            total INT DEFAULT 0 COMMENT '目录总张数',
            read_cnt INT DEFAULT 0 COMMENT '已读取张数',
            analyzed INT DEFAULT 0 COMMENT '已分析(新推理入库)',
            skipped INT DEFAULT 0 COMMENT '已跳过(之前分析过)',
            msg VARCHAR(255) DEFAULT '' COMMENT '最新消息',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='实时目录分析任务(进度)'""",
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
            class_name VARCHAR(64) DEFAULT '' COMMENT '缺陷分类名称, 随MQ消息发给CubeStudio',
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
            UNIQUE KEY uq_brand_face (brand, face_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='建模单元的缺陷分类绑定(一单元一分类)'""",
        """CREATE TABLE IF NOT EXISTS analysis_logs(
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            unit_id INT NOT NULL COMMENT '建模单元(model_units.id)',
            ts DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
            level VARCHAR(8) DEFAULT 'info' COMMENT 'info/ok/warn/error',
            src_key VARCHAR(512) DEFAULT '' COMMENT '图片key',
            detail VARCHAR(512) DEFAULT '' COMMENT '调用过程/结果',
            KEY idx_unit_ts (unit_id, id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='分析调用审计日志'""",
        """CREATE TABLE IF NOT EXISTS machine_schedule(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            machine VARCHAR(64) NOT NULL COMMENT '机台/产线(如 a01)',
            sched_date VARCHAR(16) NOT NULL COMMENT '日期 YYYYMMDD',
            shift VARCHAR(16) DEFAULT '' COMMENT '班次(空=全天)',
            brand VARCHAR(64) NOT NULL COMMENT '该时段生产的牌号',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            UNIQUE KEY uq_sched (machine, sched_date, shift)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='机台排程(机台×日期×班次→牌号), 推理时反查牌号'""",
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
            raw_name VARCHAR(128) NOT NULL COMMENT 'MinIO 里的原始相机面目录名, 如 is7600C_D',
            face_name VARCHAR(64) NOT NULL COMMENT '标准面名(界面显示), 如 正面',
            face_code VARCHAR(32) NOT NULL COMMENT '标准面编码(传对方模型接口), 如 front',
            machine_model VARCHAR(64) DEFAULT '' COMMENT '机型, 如 is7600C',
            sort_order INT DEFAULT 0 COMMENT '面序号 1-6',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用',
            UNIQUE KEY uq_raw (raw_name)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='相机面映射表(原始目录名→标准面)'""",
        """CREATE TABLE IF NOT EXISTS label_tasks(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            brand VARCHAR(64) NOT NULL COMMENT '牌号',
            total INT COMMENT '图片总数',
            labeling INT COMMENT '标注中数量',
            unlabeled INT COMMENT '未标注数量',
            exported INT DEFAULT 0 COMMENT '是否已导出: 1是 0否'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='缺陷标注任务表'""",
        """CREATE TABLE IF NOT EXISTS model_versions(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            brand VARCHAR(64) NOT NULL COMMENT '牌号',
            version VARCHAR(32) NOT NULL COMMENT '版本号',
            pub_date VARCHAR(32) COMMENT '发布日期',
            note VARCHAR(255) DEFAULT '' COMMENT '备注说明',
            status VARCHAR(16) DEFAULT '停用' COMMENT '状态: 测试/推理/停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='模型版本表'""",
        # collect_history 表已废弃：历史采集页直接按 label_images 实时聚合，
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
        """CREATE TABLE IF NOT EXISTS integration_config(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            cfg_key VARCHAR(64) NOT NULL COMMENT '配置项键',
            cfg_value TEXT COMMENT '配置项值',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
            UNIQUE KEY uq_cfg_key (cfg_key)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='外部系统集成配置表(Label Studio/推理服务/CubeStudio)'""",
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
        # 不灌模型版本 / 标注进度 / 历史采集的种子数据：
        # 这些表只应由真实动作写入 —— 模型版本来自「发布样本集」，标注进度来自
        # Label Studio 回写，历史采集来自实际扫描数据源。编造的演示数据混在里面
        # 分不清真假，比空表更糟。
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
            "SELECT id,name,short_name,src_prefix FROM detect_items WHERE status=1 ORDER BY id").fetchall()]
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
                             "ORDER BY machine_model, sort_order, id", (cur_det,),
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
    if tab == "items":  # 补每个检测项目已采图片数
        for r in data:
            r["img_count"] = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE item_id=?",
                                        (r["id"],)).fetchone()["c"]
    workshops = [dict(w) for w in db.execute("SELECT * FROM workshops WHERE status=1 ORDER BY id").fetchall()]
    areas = [dict(a) for a in db.execute("SELECT * FROM areas WHERE status=1 ORDER BY id").fetchall()]
    sched_brands = [dict(b) for b in db.execute("SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    return render_template("config.html", tab=tab, tabs=CONFIG_TABS, rows=data, pg=pg,
                           workshops=workshops, areas=areas, sched_brands=sched_brands, active="config")


@app.route("/config/items/sources")
@login_required
def config_item_sources():
    """探测各产线数据源里实际存在的顶层目录，供「数据源目录」下拉选择。
    手填字符串打错一个字就静默采不到图，且完全看不出哪里错了。"""
    db = get_db()
    found, errs = {}, []
    for l in db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall():
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
        tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
        try:
            if scfg and scfg["server_addr"]:
                s3, cfg = get_s3(scfg)
                r = s3.list_objects_v2(Bucket=cfg["in_bucket"], Delimiter="/", MaxKeys=1000)
                for cp in r.get("CommonPrefixes", []):
                    found.setdefault(cp["Prefix"].rstrip("/"), []).append(l["name"])
            elif tcfg and tcfg["sys_addr"]:
                import stat as _st
                t, sftp = _ws_sftp(tcfg)
                try:
                    for e in sftp.listdir_attr((tcfg["ng_dir"] or "").rstrip("/")):
                        if _st.S_ISDIR(e.st_mode):
                            found.setdefault(e.filename, []).append(l["name"])
                finally:
                    t.close()
        except Exception as e:
            errs.append("%s: %s" % (l["name"], str(e)[:60]))
            app.logger.warning("探测数据源目录失败（产线 %s）：%s", l["name"], e)
    used = {r["src_prefix"]: (r["short_name"] or r["name"]) for r in
            db.execute("SELECT * FROM detect_items WHERE src_prefix<>''").fetchall()}
    return jsonify({"dirs": [{"name": d, "lines": ls, "used_by": used.get(d, "")}
                             for d, ls in sorted(found.items())], "errors": errs})


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
    if what == "ls":
        url = request.form.get("ls_url", "").strip().rstrip("/")
        token = request.form.get("ls_token", "").strip()
        try:
            req = urllib.request.Request(url + "/api/projects?page_size=1",
                                         headers={"Authorization": "Token " + token})
            with urllib.request.urlopen(req, timeout=8) as r:
                n = json.loads(r.read().decode()).get("count", 0)
            ver = (_ls_get("/api/version") or {}).get("release", "")
            return jsonify({"ok": True, "msg": "连接成功，Label Studio %s，%d 个项目" % (ver or "?", n)})
        except Exception as e:
            hint = ""
            if "401" in str(e):
                hint = "（Token 无效，或 LS 1.23+ 未开启 legacy token）"
            return jsonify({"ok": False, "msg": "连接失败：%s%s" % (str(e)[:100], hint)})
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
            db.execute("UPDATE detect_items SET code=?,name=?,short_name=?,src_prefix=? WHERE id=?",
                       (f["code"], f["name"], f.get("short_name", ""),
                        f.get("src_prefix", "").strip().strip("/"), rid))
        else:
            db.execute("INSERT INTO detect_items(code,name,short_name,src_prefix) VALUES(?,?,?,?)",
                       (f["code"], f["name"], f.get("short_name", ""),
                        f.get("src_prefix", "").strip().strip("/")))
    elif tab == "lines":
        if rid:
            db.execute("UPDATE prod_lines SET code=?,name=?,workshop=?,area=? WHERE id=?",
                       (f["code"], f["name"], f.get("workshop", ""), f.get("area", ""), rid))
        else:
            db.execute("INSERT INTO prod_lines(code,name,workshop,area) VALUES(?,?,?,?)",
                       (f["code"], f["name"], f.get("workshop", ""), f.get("area", "")))
    elif tab == "brands":
        if rid:
            db.execute("UPDATE brands SET code=?,spec=? WHERE id=?", (f["code"], f["spec"], rid))
        else:
            db.execute("INSERT INTO brands(code,spec) VALUES(?,?)", (f["code"], f["spec"]))
    elif tab == "workshops":
        if rid:
            db.execute("UPDATE workshops SET name=? WHERE id=?", (f["name"], rid))
        else:
            db.execute("INSERT INTO workshops(name) VALUES(?)", (f["name"],))
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
        vals = (f["machine"].strip(), f["sched_date"].strip(), f.get("shift", "").strip(), f["brand"].strip())
        if rid:
            db.execute("UPDATE machine_schedule SET machine=?,sched_date=?,shift=?,brand=? WHERE id=?",
                       vals + (rid,))
        else:
            db.execute("INSERT INTO machine_schedule(machine,sched_date,shift,brand) VALUES(?,?,?,?) "
                       "ON DUPLICATE KEY UPDATE brand=VALUES(brand)", vals)
    elif tab == "faces":
        det = f.get("item_id", type=int) or 0
        vals = (det, f["raw_name"].strip(), f["face_name"].strip(), f.get("face_code", "").strip(),
                f.get("machine_model", "").strip(), f.get("sort_order", 0) or 0)
        if rid:
            db.execute("UPDATE camera_faces SET item_id=?,raw_name=?,face_name=?,face_code=?,"
                       "machine_model=?,sort_order=? WHERE id=?", vals + (rid,))
        else:
            # 自动发现里「一键映射」也走这里，同项目同名目录已存在则更新，避免唯一键冲突
            db.execute("INSERT INTO camera_faces(item_id,raw_name,face_name,face_code,machine_model,sort_order) "
                       "VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE "
                       "face_name=VALUES(face_name),face_code=VALUES(face_code),"
                       "machine_model=VALUES(machine_model),sort_order=VALUES(sort_order)", vals)
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
    """{原始相机面目录名: 标准面名}，用于把 is7600C_D 这类技术码显示成「正面」。"""
    return {r["raw_name"]: r["face_name"] for r in
            get_db().execute("SELECT raw_name,face_name FROM camera_faces WHERE status=1").fetchall()}


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
                vals = (f.get("name", "").strip(), f.get("type", "minio"),
                        f.get("server_addr", "").strip(), f.get("username", "").strip(),
                        f.get("password", "").strip(), f.get("in_bucket", "").strip(),
                        f.get("out_bucket", "").strip(), f.get("ng_dir", "").strip())
                if sid:
                    db.execute("UPDATE data_sources SET name=?,type=?,server_addr=?,username=?,"
                               "password=?,in_bucket=?,out_bucket=?,ng_dir=? WHERE id=?", vals + (sid,))
                else:
                    sid = db.execute("INSERT INTO data_sources(name,type,server_addr,username,password,"
                                     "in_bucket,out_bucket,ng_dir) VALUES(?,?,?,?,?,?,?,?)", vals).lastrowid
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
        sync_label_images()
        ctx["cams"] = []; ctx["err"] = None
        ctx["by_line"] = (cur_line == "all")
        iid = cur_item_row["id"] if cur_item_row else 0
        if cur_line == "all":
            for l in lines:
                c = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE line_name=? AND item_id=?",
                               (l["name"], iid)).fetchone()["c"]
                ctx["cams"].append({"name": l["name"], "count": c})
        else:
            from collections import Counter
            cnt = Counter()
            for im in db.execute("SELECT src_key FROM label_images WHERE line_name=? AND item_id=?",
                                 (cur_line, iid)).fetchall():
                parts = im["src_key"].split("/")
                cnt[parts[-2] if len(parts) >= 2 else "?"] += 1
            ctx["cams"] = [{"name": k, "count": v} for k, v in sorted(cnt.items())]
    elif tab == "history":
        sync_label_images()
        from collections import defaultdict
        # 按检测项目聚合，不按牌号：图片路径里只有检测项目，没有牌号信息
        # （此前显示的牌号是 BRAND_MAP 硬编码映射出来的，并非真实数据）
        items = {r["id"]: (r["short_name"] or r["name"]) for r in
                 db.execute("SELECT id,name,short_name FROM detect_items").fetchall()}
        agg = defaultdict(int)
        q = "SELECT item_id, src_key, line_name FROM label_images WHERE item_id=?"
        params = (cur_item_row["id"] if cur_item_row else 0,)
        if cur_line != "all":
            q += " AND line_name=?"
            params += (cur_line,)
        for im in db.execute(q, params).fetchall():
            parts = im["src_key"].split("/")
            if len(parts) < 4:
                continue
            agg[(im["line_name"], parts[-4], parts[-3],
                 items.get(im["item_id"], ""))] += 1  # (产线, 日期, 班次, 检测项目)

        def _fmt(d):
            return d[:4] + "/" + d[4:6] + "/" + d[6:8] if len(d) == 8 and d.isdigit() else d
        all_recs = [{"line": ln, "date": _fmt(d), "shift": s, "brand": b, "img_count": c}
                    for (ln, d, s, b), c in sorted(agg.items(), reverse=True)]
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


# ---------------------------------------------------------------- Label Studio 对接
def _ls_headers():
    return {"Authorization": "Token " + get_cfg("ls_token"),
            "Content-Type": "application/json"}


def _ls_get(path):
    """GET Label Studio API，失败返回空 dict（调用方据此降级）。"""
    import urllib.request
    try:
        req = urllib.request.Request(get_cfg("ls_url").rstrip("/") + path, headers=_ls_headers())
        req.get_method = lambda: "GET"
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _ls_post(path, data, method="POST"):
    import urllib.request
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(get_cfg("ls_url").rstrip("/") + path, data=body,
                                 headers=_ls_headers())
    req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else {}


def ls_label_config(classes):
    """按缺陷类别字典生成 LS 的标注界面配置。"""
    cfg = ['<View>', '  <Image name="image" value="$image"/>',
           '  <RectangleLabels name="tag" toName="image">']
    for c in classes:
        bg = "#%02x%02x%02x" % tuple((int(c["id"]) * 47 * (i + 1) + 40) % 200 + 30 for i in range(3))
        cfg.append('    <Label value="%s" background="%s"/>' % (c["name"], bg))
    cfg += ['  </RectangleLabels>', '</View>']
    return "\n".join(cfg)


def ls_ensure_project(item, classes):
    """为某检测项目确保 Label Studio 标注项目存在且缺陷类别是最新的，返回 project_id。
    优先用 detect_items.ls_project_id 直接命中，避免改名后重复建项目。"""
    db = get_db()
    brand = item["short_name"] or item["name"]
    want = ls_label_config(classes)
    pid = item["ls_project_id"] or 0
    p = _ls_get("/api/projects/%d" % pid) if pid else {}
    if not (p or {}).get("id"):
        pid, p = None, {}
        r = _ls_get("/api/projects?page_size=200") or {}
        for x in r.get("results", []):
            if x.get("title") == brand:
                pid, p = x["id"], x
                break
    if not pid:
        p = _ls_post("/api/projects", {"title": brand, "label_config": want,
                     "description": "卷烟厂缺陷标注 · %s · %d类缺陷" % (brand, len(classes))})
        pid = p.get("id")
        if not pid:
            return None
    elif classes and (p.get("label_config") or "").strip() != want.strip():
        # 类别字典改过就把 LS 的标注界面同步过去 —— 否则平台里新增的缺陷类型
        # 在 LS 标注时根本选不到，删掉的还一直留着。
        try:
            _ls_post("/api/projects/%d" % pid, {"label_config": want}, method="PATCH")
            app.logger.info("已同步「%s」的缺陷类别到 Label Studio（%d 类）", brand, len(classes))
        except Exception as e:
            # 已有标注用到了将被删除的类别时 LS 会拒绝，属预期，保留旧配置
            app.logger.warning("同步缺陷类别到 Label Studio 失败（%s）：%s", brand, e)
    db.execute("UPDATE detect_items SET ls_project_id=? WHERE id=?", (pid, item["id"]))
    db.commit()
    return pid


def ls_ensure_s3_storage(pid, scfg, prefix=""):
    """给 LS 项目挂 MinIO 源存储（幂等）。LS 自己列桶建任务、每次访问现签 URL，
    平台不再枚举图片。新建时触发一次同步，返回 (storage_id, 是否新建)。
    prefix 限定只同步本检测项目自己的目录，否则多个项目会互相灌入对方的图。"""
    r = _ls_get("/api/storages/s3?project=%d" % pid)
    for s in (r if isinstance(r, list) else r.get("results", []) if isinstance(r, dict) else []):
        if s.get("bucket") == scfg["in_bucket"] and (s.get("prefix") or "") == prefix:
            return s.get("id"), False
    s = _ls_post("/api/storages/s3", {
        "project": pid,
        "title": "MinIO %s/%s" % (scfg["in_bucket"], prefix or "*"),
        "bucket": scfg["in_bucket"],
        "prefix": prefix,
        "s3_endpoint": "http://" + scfg["server_addr"],
        "aws_access_key_id": scfg["username"],
        "aws_secret_access_key": scfg["password"],
        "region_name": "us-east-1",
        "use_blob_urls": True,   # 每个对象=一个任务，data.image=s3://…
        "recursive_scan": True,  # key 是 检测项目/日期/班次/缺陷类型/文件名 多级嵌套
        "presign": True,         # LS 按需现签，URL 不会过期
        "presign_ttl": 60,
    })
    sid = s.get("id")
    if sid:
        _ls_post("/api/storages/s3/%d/sync" % sid, {})
    return sid, True


def ls_webhook_url():
    """LS 回调本平台的地址。优先用集成配置里手填的；没填则按当前请求推算。
    注意这个地址是 LS 容器去访问的，不能是 127.0.0.1。"""
    u = get_cfg("ls_webhook_url")
    if u:
        return u
    try:
        return url_for("label_webhook", _external=True)
    except Exception:
        return ""


def ls_ensure_webhook(pid):
    """把本平台的回调地址注册进 Label Studio（幂等）。
    没有这一步，标注完不会回写平台 —— 端点白写。"""
    url = ls_webhook_url()
    if not url or "127.0.0.1" in url or "localhost" in url:
        return False  # LS 容器访问不到，注册了也是死链
    existing = _ls_get("/api/webhooks/")
    for w in (existing if isinstance(existing, list) else []):
        if w.get("url") == url:
            return True
    _ls_post("/api/webhooks/", {
        "url": url,
        "project": pid,
        "send_payload": True,
        "send_for_all_actions": False,
        "actions": ["ANNOTATION_CREATED", "ANNOTATION_UPDATED", "ANNOTATIONS_DELETED",
                    "TASKS_CREATED", "TASKS_DELETED"],
        "headers": {"Authorization": "Token " + get_cfg("ls_token")},
        "is_active": True,
    })
    return True


def ls_task_counts(pid):
    """读 LS 项目的标注进度。"""
    p = _ls_get("/api/projects/%d" % pid) or {}
    total = p.get("task_number") or 0
    done = p.get("finished_task_number") or 0
    return {"total": total, "done": done, "unlabeled": max(0, total - done)}


# ---------------------------------------------------------------- 缺陷标注
_last_sync_ts = [0.0]


def sync_label_images(force=False):
    """扫描所有数据源，把 NG 图按顶层目录(牌号)登记进 label_images（幂等）。
    带 60 秒节流，避免每次切换页面都重连数据源导致卡顿。"""
    import time
    import stat
    now = time.time()
    if not force and (now - _last_sync_ts[0]) < 60:
        return
    _last_sync_ts[0] = now
    db = get_db()
    existing = {r["src_key"] for r in db.execute("SELECT src_key FROM label_images").fetchall()}
    # 数据源顶层目录 → 检测项目。此前是硬编码 BRAND_MAP={"小包外观":"玉溪（硬）"}，
    # 既写死了映射、又把检测项目冒充成牌号；现在由 detect_items.src_prefix 配置驱动。
    prefix_map = {r["src_prefix"]: r["id"] for r in db.execute(
        "SELECT id, src_prefix FROM detect_items WHERE src_prefix<>''").fetchall()}
    rows = []
    for l in db.execute("SELECT * FROM prod_lines").fetchall():
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
        tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
        if scfg and scfg["server_addr"]:
            # 对象存储产线：分页列全部对象，顶层目录=检测项目
            try:
                s3, _ = get_s3(scfg)
                tok = None
                while True:
                    kw = {"Bucket": scfg["in_bucket"], "MaxKeys": 1000}
                    if tok:
                        kw["ContinuationToken"] = tok
                    r = s3.list_objects_v2(**kw)
                    for o in r.get("Contents", []):
                        k = o["Key"]
                        if "/" in k and k.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                            iid = prefix_map.get(k.split("/")[0])
                            if iid:
                                rows.append(("minio", k, iid, l["name"]))
                    if r.get("IsTruncated"):
                        tok = r.get("NextContinuationToken")
                    else:
                        break
            except Exception as e:
                app.logger.warning("扫描对象存储失败（产线 %s）：%s", l["name"], e)
        elif tcfg:
            # 工控机产线：SFTP 全量 walk，相对路径顶层=检测项目
            root = (tcfg["ng_dir"] or "").rstrip("/")
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
                            rel = full[len(root):].lstrip("/")
                            iid = prefix_map.get(rel.split("/")[0]) if "/" in rel else None
                            if iid:
                                rows.append(("terminal", full, iid, l["name"]))
                try:
                    walk(root)
                finally:
                    t.close()
            except Exception as e:
                app.logger.warning("扫描工控机失败（产线 %s）：%s", l["name"], e)
    added = False
    for source, key, item_id, line in rows:
        if key not in existing:
            try:
                db.execute("INSERT INTO label_images(item_id,brand,source,src_key,line_name) "
                           "VALUES(?,?,?,?,?)", (item_id, "", source, key, line))
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
    # 各相机面(建模单元)绑定的缺陷分类
    cbind = {r["face_id"]: dict(r) for r in db.execute(
        "SELECT face_id,class_id,class_name FROM unit_sample_class WHERE brand=?", (cur_brand,)).fetchall()}
    units = []
    for f in faces:
        u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?",
                       (cur_brand, f["id"])).fetchone()
        s = samp.get(f["face_name"], {"count": 0, "proc": 0})
        cb = cbind.get(f["id"], {})
        units.append({
            "face_id": f["id"], "face_name": f["face_name"], "face_code": f["face_code"],
            "raw_name": f["raw_name"],
            "exists": bool(u), "unit_id": u["id"] if u else 0,
            "cs_project_id": u["cs_project_id"] if u else "",
            "annotated": u["annotated"] if u else 0, "total": u["total"] if u else 0,
            "model_version": u["model_version"] if u else "",
            "sample_count": s["count"], "sample_proc": s["proc"],
            "class_id": cb.get("class_id") or 0, "class_name": cb.get("class_name") or "",
            "unit_key": unit_key_of(db, cur_brand, dict(f)),
        })
    units, pg = paginate_list(units, request.args.get("page"))
    # 缺陷分类候选：按当前检测项目取启用项，供行编辑弹窗选择（绑定到建模单元）。
    classes = [dict(c) for c in db.execute(
        "SELECT id, name FROM label_classes WHERE item_id=? AND status=1 ORDER BY id",
        (cur_det,)).fetchall()]
    return render_template("label.html", det_items=det_items, cur_det=cur_det,
                           brands=brands, cur_brand=cur_brand,
                           units=units, pg=pg, cs_online=cs_online,
                           classes=classes, active="label")


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
    class_id = request.form.get("class_id", type=int)
    face = db.execute("SELECT * FROM camera_faces WHERE id=?", (face_id,)).fetchone()
    if not (brand and face):
        return jsonify({"ok": False, "msg": "参数错误"}), 400
    cls = db.execute("SELECT id,name FROM label_classes WHERE id=? AND status=1", (class_id,)).fetchone() \
        if class_id else None
    if not cls:
        return jsonify({"ok": False, "msg": "请选择有效的缺陷分类"}), 400
    unit_key = unit_key_of(db, brand, dict(face))
    db.execute("INSERT INTO unit_sample_class(brand,face_id,unit_key,class_id,class_name,updated_by) "
               "VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE "
               "class_id=VALUES(class_id),class_name=VALUES(class_name),"
               "unit_key=VALUES(unit_key),updated_by=VALUES(updated_by)",
               (brand, face_id, unit_key, cls["id"], cls["name"], session.get("username", "")))
    db.commit()
    app.logger.info("建模单元绑定缺陷分类 unit_key=%s class=%s by=%s",
                    unit_key, cls["name"], session.get("username", ""))
    return jsonify({"ok": True, "class_id": cls["id"], "class_name": cls["name"]})


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

            ch.basic_consume(queue=MQ_RK_REPLY, on_message_callback=on_reply)
            app.logger.info("MQ 回执消费者已启动")
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
    # 缺陷分类取建模单元的绑定（在行编辑弹窗里设置）；未绑定不能上传
    cb = db.execute("SELECT class_id,class_name FROM unit_sample_class WHERE brand=? AND face_id=?",
                    (brand, face_id)).fetchone()
    if not cb or not cb["class_id"]:
        return jsonify({"ok": False, "msg": "请先为该相机面绑定缺陷分类（点行内「缺陷分类」编辑）"}), 400
    class_id, class_name = cb["class_id"], cb["class_name"]
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
               "class_id": class_id, "class_name": class_name,
               "bucket": bucket, "path": prefix, "count": len(keys), "images": keys,
               "minio": {"endpoint": "http://" + scfg["server_addr"],
                         "access_key": scfg["username"], "secret_key": scfg["password"]},
               "ts": int(datetime.now().timestamp())}
    db.execute("INSERT INTO sample_uploads(msg_id,unit_key,unit_id,cs_project_id,project,brand,face,"
               "face_code,class_id,class_name,bucket,path,img_count,status,created_by) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (msg_id, unit_key, unit_id, cs_pid, project, brand, face["face_name"],
                face["face_code"], class_id, class_name, bucket, prefix, len(keys), "processing",
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


# LS 实际发出的动作名。曾错写成 ANNOTATION_DELETED / TASK_CREATED / TASK_DELETED
# （少了复数 S），这三种事件会被静默忽略。以 /api/webhooks/info/ 返回的为准。
LS_WEBHOOK_ACTIONS = ["ANNOTATION_CREATED", "ANNOTATIONS_CREATED", "ANNOTATION_UPDATED",
                      "ANNOTATIONS_DELETED", "TASKS_CREATED", "TASKS_DELETED"]


def cache_label_progress(db, item_id, name, cnt):
    """把标注进度写进 label_tasks 当缓存，首页据此免去逐个项目调 LS。"""
    n = db.execute("SELECT COUNT(*) AS c FROM label_tasks WHERE item_id=?", (item_id,)).fetchone()["c"]
    if n:
        db.execute("UPDATE label_tasks SET brand=?,total=?,labeling=?,unlabeled=? WHERE item_id=?",
                   (name, cnt["total"], cnt["done"], cnt["unlabeled"], item_id))
    else:
        db.execute("INSERT INTO label_tasks(item_id,brand,total,labeling,unlabeled) VALUES(?,?,?,?,?)",
                   (item_id, name, cnt["total"], cnt["done"], cnt["unlabeled"]))
    db.commit()


@app.route("/label/reset/<int:item_id>", methods=["POST"])
@login_required
def label_reset(item_id):
    """删除该检测项目的 Label Studio 标注项目，下次打开标注页会重建。
    LS 里的标注会一并没掉，故前端需二次确认。原图在 MinIO，不受影响。"""
    db = get_db()
    item = db.execute("SELECT * FROM detect_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    name = item["short_name"] or item["name"]
    pid = item["ls_project_id"] or 0
    if not pid:
        flash("检测项目「%s」没有关联的 Label Studio 项目" % name, "error")
        return redirect(url_for("label", item=name))
    try:
        cnt = ls_task_counts(pid)
        _ls_post("/api/projects/%d" % pid, {}, method="DELETE")
        db.execute("UPDATE detect_items SET ls_project_id=0 WHERE id=?", (item_id,))
        db.execute("DELETE FROM label_tasks WHERE item_id=?", (item_id,))
        db.commit()
        flash("已删除「%s」的标注项目（含 %d 条标注）。再次打开标注页会重新建立并同步图片。"
              % (name, cnt["done"]), "success")
    except Exception as e:
        flash("删除失败：%s" % str(e)[:120], "error")
    return redirect(url_for("label", item=name))


@app.route("/label/webhook", methods=["POST"])
def label_webhook():
    """Label Studio 标注事件回调 —— 刷新平台侧的标注进度缓存。
    LS 侧的注册见 ls_ensure_webhook()；鉴权靠注册时带上的 Authorization 头。"""
    if request.headers.get("Authorization", "") != "Token " + get_cfg("ls_token"):
        return jsonify({"error": "unauthorized"}), 403
    try:
        data = request.get_json(force=True) or {}
        if data.get("action", "") in LS_WEBHOOK_ACTIONS:
            pid = (data.get("project") or {}).get("id")
            if pid:
                db = get_db()
                # 按 ls_project_id 反查检测项目，比拿 LS 项目标题去匹配可靠（改名不会断）
                it = db.execute("SELECT id,name,short_name FROM detect_items WHERE ls_project_id=?",
                                (pid,)).fetchone()
                if it:
                    cache_label_progress(db, it["id"], it["short_name"] or it["name"],
                                         ls_task_counts(pid))
    except Exception as e:
        app.logger.warning("Label Studio webhook 处理失败：%s", e)
    return jsonify({"ok": True})


def ls_export_coco(pid, classes):
    """从 Label Studio 拉已标注任务并转成 COCO，返回 (coco, 图片key列表)。
    注意 LS 的 x/y/width/height 是相对原图的百分比，必须按 original_width/height
    换算成像素，COCO 的 bbox 要的是绝对像素。"""
    tasks = _ls_get("/api/projects/%d/export?exportType=JSON&download_all_tasks=false" % pid)
    if not isinstance(tasks, list):
        return None, []
    cat_id = {c["name"]: c["id"] for c in classes}
    coco = {"images": [], "annotations": [],
            "categories": [{"id": c["id"], "name": c["name"]} for c in classes]}
    keys, aid = [], 1
    for t in tasks:
        uri = ((t.get("data") or {}).get("image") or "")
        if not uri:
            continue
        key = uri.split("/", 3)[-1] if uri.startswith("s3://") else uri  # s3://桶/键 → 键
        boxes, W, H = [], 0, 0
        for a in (t.get("annotations") or []):
            for r in (a.get("result") or []):
                if r.get("type") != "rectanglelabels":
                    continue
                W = r.get("original_width") or W
                H = r.get("original_height") or H
                v = r.get("value") or {}
                names = v.get("rectanglelabels") or []
                if names:
                    boxes.append((names[0], v))
        if not boxes or not W or not H:
            continue
        img_id = len(coco["images"]) + 1
        coco["images"].append({"id": img_id, "file_name": key, "width": W, "height": H})
        keys.append(key)
        for name, v in boxes:
            x, y = v.get("x", 0) / 100.0 * W, v.get("y", 0) / 100.0 * H
            w, h = v.get("width", 0) / 100.0 * W, v.get("height", 0) / 100.0 * H
            coco["annotations"].append({
                "id": aid, "image_id": img_id, "category_id": cat_id.get(name),
                "bbox": [round(x, 2), round(y, 2), round(w, 2), round(h, 2)],
                "area": round(w * h, 2), "iscrowd": 0, "segmentation": []})
            aid += 1
    return coco, keys


@app.route("/label/export/<int:item_id>", methods=["POST"])
@login_required
def label_export(item_id):
    """把 Label Studio 里的标注导出为 COCO 样本集，发布到 defect-datasets 桶。
    标注数据在 LS 里，不再读平台自带标注器的 annotations 表（那张表已不再写入）。"""
    db = get_db()
    item = db.execute("SELECT * FROM detect_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    item = dict(item)
    name = item["short_name"] or item["name"]
    pid = item["ls_project_id"] or 0
    if not pid:
        flash("检测项目「%s」尚未建立 Label Studio 项目" % name, "error")
        return redirect(url_for("label"))
    classes = [dict(c) for c in db.execute(
        "SELECT * FROM label_classes WHERE item_id=? ORDER BY id", (item_id,)).fetchall()]
    try:
        coco, keys = ls_export_coco(pid, classes)
    except Exception as e:
        flash("从 Label Studio 拉取标注失败：%s" % str(e)[:120], "error")
        return redirect(url_for("label"))
    if not coco or not coco["images"]:
        flash("检测项目「%s」在 Label Studio 中暂无已标注样本" % name, "error")
        return redirect(url_for("label"))

    ver_n = db.execute("SELECT COUNT(*) c FROM model_versions WHERE brand=?", (name,)).fetchone()["c"] + 1
    version = "v%d" % ver_n
    prefix = "%s/%s/" % (name, version)
    try:
        scfg = db.execute("SELECT * FROM storage_config LIMIT 1").fetchone()
        if not scfg:
            flash("未找到对象存储配置", "error")
            return redirect(url_for("label"))
        s3, _ = get_s3(scfg)
        dst = "defect-datasets"
        body = json.dumps(coco, ensure_ascii=False, indent=2).encode("utf-8")
        s3.put_object(Bucket=dst, Key=prefix + "coco.json", Body=body, ContentType="application/json")
        for k in keys:
            s3.copy_object(Bucket=dst, Key=prefix + k,
                           CopySource={"Bucket": scfg["in_bucket"], "Key": k})
        db.execute("INSERT INTO model_versions(item_id,brand,version,pub_date,note,status) "
                   "VALUES(?,?,?,?,?,?)",
                   (item_id, name, version, datetime.now().strftime("%Y-%m-%d"),
                    "从 Label Studio 导出 %d 张标注图 / %d 个标注框" % (
                        len(coco["images"]), len(coco["annotations"])), "测试"))
        db.commit()
        app.logger.info("样本集已发布 project=%s version=%s images=%d boxes=%d dst=%s by=%s",
                        name, version, len(coco["images"]), len(coco["annotations"]),
                        dst + "/" + prefix, session.get("username", ""))
        flash("样本集 %s%s 已发布到 defect-datasets：%d 张图、%d 个标注框" % (
            name, version, len(coco["images"]), len(coco["annotations"])), "success")
    except Exception as e:
        app.logger.error("样本集导出发布失败 project=%s：%s", name, e)
        flash("导出失败: %s" % str(e)[:120], "error")
    return redirect(url_for("label"))


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
        """SELECT mu.*, cf.face_name, cf.sort_order FROM model_units mu
           JOIN camera_faces cf ON cf.id=mu.face_id
           WHERE mu.brand=? AND cf.item_id=? ORDER BY cf.sort_order, cf.id""",
        (cur_brand, cur_det)).fetchall()]
    # 先分页，只对当前页的单元拉对方模型列表（每行一次远程调用，不能给全部单元都调）
    page_units, pg = paginate_list(all_units, request.args.get("page"))
    rows = []
    for u in page_units:
        models = cs_project_models(u["cs_project_id"]) if (cs_online and u["cs_project_id"]) else []
        job = db.execute("SELECT * FROM analysis_jobs WHERE unit_id=?", (u["id"],)).fetchone()
        rows.append({"unit_id": u["id"], "face_name": u["face_name"],
                     "bound_model": u["model_id"], "bound_version": u["model_version"],
                     "bound_endpoint": u["model_endpoint"], "models": models,
                     "rt_type": u.get("rt_type", ""), "rt_line_id": u.get("rt_line_id", 0),
                     "rt_bucket": u.get("rt_bucket", ""), "rt_path": u.get("rt_path", ""),
                     "job": dict(job) if job else None})
    # 已配置的数据源（供实时目录选择复用凭据）：minio 用 storage_config，ftp 用 terminal_config
    sources = []
    for l in db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall():
        sc = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
        tc = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
        if sc and sc["server_addr"]:
            sources.append({"line_id": l["id"], "line": l["name"], "type": "minio",
                            "addr": sc["server_addr"], "bucket": sc["in_bucket"]})
        elif tc and tc["sys_addr"]:
            sources.append({"line_id": l["id"], "line": l["name"], "type": "ftp",
                            "addr": tc["sys_addr"], "dir": tc["ng_dir"]})
    return render_template("model.html", det_items=det_items, cur_det=cur_det,
                           brands=brands, cur_brand=cur_brand,
                           rows=rows, pg=pg, cs_online=cs_online, sources=sources, active="model")


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


@app.route("/model/rtdir/test", methods=["POST"])
@login_required
def model_rtdir_test():
    ok, msg = _rtdir_check(get_db(), request.form.get("rt_type", ""),
                           request.form.get("rt_line_id", type=int) or 0,
                           request.form.get("rt_bucket", ""), request.form.get("rt_path", ""))
    return jsonify({"ok": ok, "msg": msg})


@app.route("/model/rtdir/save", methods=["POST"])
@login_required
def model_rtdir_save():
    db = get_db()
    unit_id = request.form.get("unit_id", type=int)
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u:
        abort(404)
    db.execute("UPDATE model_units SET rt_type=?,rt_line_id=?,rt_bucket=?,rt_path=? WHERE id=?",
               (request.form.get("rt_type", ""), request.form.get("rt_line_id", type=int) or 0,
                request.form.get("rt_bucket", "").strip(),
                request.form.get("rt_path", "").strip().strip("/"), unit_id))
    db.commit()
    flash("实时图像目录已保存", "success")
    return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))


def _rtdir_list_images(db, u):
    """列出单元实时目录下的全部图片 key。返回 (keys列表, s3客户端或None, bucket, ftp信息)。
    minio 返回 (keys, s3, bucket, None)；ftp 返回 (keys, None, None, tcfg)。"""
    line_id, path = u["rt_line_id"], (u["rt_path"] or "").strip("/")
    if u["rt_type"] == "minio":
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (line_id,)).fetchone()
        if not scfg:
            raise Exception("数据源未配置对象存储")
        bucket = u["rt_bucket"] or scfg["in_bucket"]
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


def _analysis_worker(unit_id):
    """后台线程：从单元实时目录抓图，逐张送绑定模型推理入库，更新进度。
    增量：已在 inference_results 的 src_key 跳过。用独立 DB 连接（不能用 g.db）。"""
    db = _DB(_connect())

    def upd(**kw):
        cols = ",".join("%s=%%s" % k for k in kw)
        db.execute("UPDATE analysis_jobs SET " + cols.replace("%%s", "?") + " WHERE unit_id=?",
                   tuple(kw.values()) + (unit_id,))
        db.commit()

    def log(level, detail, src_key=""):
        db.execute("INSERT INTO analysis_logs(unit_id,level,src_key,detail) VALUES(?,?,?,?)",
                   (unit_id, level, src_key, detail[:500]))
        db.commit()

    def is_on():
        r = db.execute("SELECT enabled FROM analysis_jobs WHERE unit_id=?", (unit_id,)).fetchone()
        return bool(r and r["enabled"])

    try:
        u = dict(db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone())
        face = db.execute("SELECT face_name FROM camera_faces WHERE id=?", (u["face_id"],)).fetchone()
        face_name = face["face_name"] if face else ""
        endpoint = u["model_endpoint"]
        log("info", "工作开启，推理服务 %s" % endpoint)
        # 开关式：开启后持续工作 —— 每轮扫目录处理新图，处理完歇 15 秒再扫；关闭则退出
        total_an = 0
        while is_on():
            keys, _, _, _ = _rtdir_list_images(db, u)
            done = {r["src_key"] for r in db.execute(
                "SELECT src_key FROM inference_results WHERE unit_id=? AND src_key<>''", (unit_id,)).fetchall()}
            upd(status="running", total=len(keys), read_cnt=0, analyzed=0, skipped=0, msg="扫描目录…")
            read = an = sk = 0
            for key in keys:
                if not is_on():
                    break
                read += 1
                if key in done:
                    sk += 1
                else:
                    res = infer_call(endpoint, key)
                    if res:
                        p = key.split("/")
                        date = p[-4] if len(p) >= 4 else ""
                        shift = p[-3] if len(p) >= 3 else ""
                        db.execute("INSERT INTO inference_results(src_key,unit_id,machine,line_name,brand,"
                                   "face_name,img_date,shift,is_defect,class_name,confidence,model_version) "
                                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (key, unit_id, "", "", u["brand"], face_name, date, shift,
                                    res["is_defect"], res["class_name"], res["confidence"], res["model_version"]))
                        db.commit()
                        an += 1
                        total_an += 1
                        log("ok", "%s → %s (置信 %.2f)" % (
                            "正常" if res["is_defect"] == 0 else res["class_name"], res["class_name"],
                            res["confidence"]), key)
                    else:
                        log("error", "推理调用失败，跳过", key)
                if read % 5 == 0 or read == len(keys):
                    upd(read_cnt=read, analyzed=an, skipped=sk, msg="分析中 %d/%d" % (read, len(keys)))
            if not is_on():
                break
            upd(status="running", read_cnt=read, analyzed=an, skipped=sk,
                msg="本轮完成(新%d/跳%d)，累计%d，等待新图…" % (an, sk, total_an))
            # 歇一会再扫新图；期间每秒检查开关，关了立刻退
            for _ in range(15):
                if not is_on():
                    break
                time.sleep(1)
        upd(status="stopped", msg="已关闭，累计新分析 %d 张" % total_an)
        log("info", "工作关闭，累计新分析 %d 张" % total_an)
    except Exception as e:
        app.logger.exception("实时目录分析失败 unit=%s", unit_id)
        upd(status="error", enabled=0, msg="失败：%s" % str(e)[:180])
        try:
            log("error", "任务异常：%s" % str(e)[:400])
        except Exception:
            pass
    finally:
        db.close()


@app.route("/model/analyze/toggle", methods=["POST"])
@login_required
def model_analyze_toggle():
    """工作状态开关：开=启动后台分析线程持续工作；关=置 enabled=0，worker 自行退出。"""
    db = get_db()
    unit_id = request.form.get("unit_id", type=int)
    on = request.form.get("on") == "1"
    u = db.execute("SELECT * FROM model_units WHERE id=?", (unit_id,)).fetchone()
    if not u:
        abort(404)
    if on:
        if not u["model_endpoint"]:
            flash("该单元未绑定推理模型，无法开启", "error")
            return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))
        if not (u["rt_type"] and (u["rt_bucket"] or u["rt_path"] or u["rt_type"] == "ftp")):
            flash("该单元未配置实时图像目录", "error")
            return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))
        db.execute("INSERT INTO analysis_jobs(unit_id,status,enabled,msg) VALUES(?,?,1,?) "
                   "ON DUPLICATE KEY UPDATE enabled=1,status='running',msg=VALUES(msg)",
                   (unit_id, "running", "启动中…"))
        db.commit()
        import threading
        threading.Thread(target=_analysis_worker, args=(unit_id,), daemon=True).start()
        flash("已开启工作", "success")
    else:
        db.execute("UPDATE analysis_jobs SET enabled=0 WHERE unit_id=?", (unit_id,))
        db.commit()
        flash("已关闭工作（正在停止当前轮次）", "success")
    return redirect(url_for("model", det=unit_det_id(db, u), brand=u["brand"]))


@app.route("/api/model/analyze/status")
@login_required
def api_model_analyze_status():
    """轮询：返回各单元的工作状态与分析进度。"""
    ids = request.args.get("units", "")
    unit_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not unit_ids:
        return jsonify({})
    ph = ",".join(["?"] * len(unit_ids))
    rows = get_db().execute("SELECT * FROM analysis_jobs WHERE unit_id IN (%s)" % ph, unit_ids).fetchall()
    return jsonify({str(r["unit_id"]): {"status": r["status"], "enabled": r["enabled"],
                    "total": r["total"], "read": r["read_cnt"], "analyzed": r["analyzed"],
                    "skipped": r["skipped"], "msg": r["msg"]} for r in rows})


@app.route("/model/analyze/logs/<int:unit_id>")
@login_required
def model_analyze_logs(unit_id):
    """审计日志：某单元的分析调用过程/结果。"""
    db = get_db()
    u = db.execute("SELECT mu.*, cf.face_name FROM model_units mu JOIN camera_faces cf ON cf.id=mu.face_id "
                   "WHERE mu.id=?", (unit_id,)).fetchone()
    if not u:
        abort(404)
    logs, pg = paginate(db, "SELECT * FROM analysis_logs WHERE unit_id=%d ORDER BY id DESC" % unit_id,
                        page=request.args.get("page"), size=30)
    return render_template("analyze_logs.html", u=dict(u), logs=[dict(r) for r in logs], pg=pg,
                           active="model")


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


def infer_call(endpoint, src_key):
    """调建模单元绑定的推理服务；失败返回 None（跳过，不编造）。"""
    try:
        import urllib.request
        body = json.dumps({"src_key": src_key}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        # 兼容对方 {status,result,message} 包裹
        if isinstance(d, dict) and "result" in d and "status" in d:
            d = d["result"]
        return {"is_defect": int(d.get("is_defect", 1)), "class_name": d.get("class_name", ""),
                "confidence": float(d.get("confidence", 0)), "model_version": d.get("model_version", "")}
    except Exception as e:
        app.logger.warning("推理服务调用失败，跳过（%s）：%s", src_key, e)
        return None


def resolve_brand(db, machine, date, shift):
    """机台排程反查牌号：先精确(机台+日期+班次)，再退到全天(班次为空)。"""
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
            brand = resolve_brand(db, machine, date, shift)
            if not brand:
                no_sched += 1
                continue
            unit = units.get((brand, face["id"]))
            if not unit:
                no_model += 1
                continue
            res = infer_call(unit["model_endpoint"], key)
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
