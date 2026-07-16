# -*- coding: utf-8 -*-
"""缺陷图片分析平台 — Flask 后端"""
import os
import json
import hashlib
import functools
import random
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
    "infer_url": os.environ.get("INFER_URL", ""),          # 算法推理服务，为空则用内置模拟
    "ls_url": os.environ.get("LS_URL", "http://127.0.0.1:8080"),
    "ls_token": os.environ.get("LS_TOKEN", "cigarette-label-studio-token-2026"),
    "ls_user": os.environ.get("LS_USER", "admin@cigarette.local"),
    "ls_webhook_url": os.environ.get("LS_WEBHOOK_URL", ""),  # 本平台回调地址，注册进 LS
    "cs_url": os.environ.get("CS_URL", ""),                 # CubeStudio（预留）
    "cs_token": os.environ.get("CS_TOKEN", ""),
}
CFG_LABELS = [
    ("ls_url", "Label Studio 地址", "浏览器与平台都要能访问，故必须用 IP 不能用 127.0.0.1"),
    ("ls_token", "Label Studio API Token", "LS 1.23+ 需在组织设置里开启 legacy token 才可用"),
    ("ls_user", "Label Studio 登录账号", "仅用于标注页提示，不参与鉴权"),
    ("ls_webhook_url", "标注回调地址(Webhook)", "留空则用本机地址推算；LS 标注后回写进度到此"),
    ("infer_url", "推理服务地址", "留空则用内置模拟判定（仅演示，不可用于生产统计）"),
    ("cs_url", "CubeStudio 地址", "预留，本机资源不足未部署"),
    ("cs_token", "CubeStudio Token", "预留"),
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
    ]
    added = set()
    for table, col, ddl in adds:
        if not _has_column(db, table, col):
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
            added.add(table)
    if added:
        db.commit()

    # 相机面映射种子：现场用 is7600C_x 技术码，首次预置这 6 面（is7600C 机型）
    if not db.execute("SELECT COUNT(*) AS c FROM camera_faces").fetchone()["c"]:
        seed_faces = [
            ("is7600C_D", "正面", "front", 1), ("is7600C_U", "反面", "back", 2),
            ("is7600C_L", "前部", "left", 3), ("is7600C_R", "尾部", "right", 4),
            ("is7600C_zuo", "六面相机左", "six_left", 5),
            ("is7600C_you", "六面相机右", "six_right", 6),
        ]
        for raw, name, code, order in seed_faces:
            db.execute("INSERT INTO camera_faces(raw_name,face_name,face_code,machine_model,sort_order) "
                       "VALUES(?,?,?,?,?)", (raw, name, code, "is7600C", order))
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
        """CREATE TABLE IF NOT EXISTS label_classes(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            name VARCHAR(64) NOT NULL COMMENT '缺陷分类名称',
            status INT DEFAULT 1 COMMENT '状态: 1启用 0停用'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='缺陷标注分类表'""",
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
        """CREATE TABLE IF NOT EXISTS annotations(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            image_id INT NOT NULL COMMENT '关联label_images.id',
            class_id INT COMMENT '缺陷分类ID(label_classes.id)',
            class_name VARCHAR(64) COMMENT '缺陷分类名称',
            shape VARCHAR(16) DEFAULT 'rect' COMMENT '形状: rect矩形 / polygon多边形',
            bbox_x DOUBLE DEFAULT 0 COMMENT '外接框左上x(像素)',
            bbox_y DOUBLE DEFAULT 0 COMMENT '外接框左上y(像素)',
            bbox_w DOUBLE DEFAULT 0 COMMENT '外接框宽(像素)',
            bbox_h DOUBLE DEFAULT 0 COMMENT '外接框高(像素)',
            points TEXT COMMENT '多边形顶点[[x,y],...] JSON(像素)',
            created_by VARCHAR(64) DEFAULT '' COMMENT '标注人',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '标注时间',
            KEY idx_image (image_id)
        ) DEFAULT CHARSET=utf8mb4 COMMENT='标注框表'""",
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
    """顶栏检测项目下拉。读 detect_items 表 —— 此前是硬编码列表，
    与 /config/items 的增删改查完全脱节，新建的项目根本进不了导航。"""
    items = []
    try:
        items = [dict(r) for r in get_db().execute(
            "SELECT id,name,short_name,src_prefix FROM detect_items "
            "WHERE status=1 ORDER BY id").fetchall()]
    except Exception:
        pass  # 建表前或登录页连不上库时不阻塞页面渲染
    names = [(i["short_name"] or i["name"]) for i in items]
    cur_row = cur_detect_item()   # 与各路由取的是同一个，避免顶栏显示与实际数据不一致
    cur = (cur_row["short_name"] or cur_row["name"]) if cur_row else ""
    return dict(DETECT_ITEMS=names, DETECT_ITEM_ROWS=items, cur_item=cur,
                cur_item_id=(cur_row["id"] if cur_row else 0))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row and row["password"] == md5(password):
            if not row["status"]:
                flash("账号已停用，请联系管理员", "error")
            else:
                session["uid"] = row["id"]
                session["username"] = row["username"]
                session["realname"] = row["realname"] or row["username"]
                session["role"] = row["role"]
                return redirect(request.args.get("next") or url_for("index"))
        else:
            flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- 首页
@app.route("/")
@login_required
def index():
    db = get_db()
    sync_label_images()
    lines = db.execute("SELECT name FROM prod_lines WHERE status=1 ORDER BY id").fetchall()
    line_stats = [{"name": l["name"],
                   "count": db.execute("SELECT COUNT(*) AS c FROM label_images WHERE line_name=?",
                                       (l["name"],)).fetchone()["c"]} for l in lines]
    total_imgs = db.execute("SELECT COUNT(*) AS c FROM label_images").fetchone()["c"]
    infer = db.execute("SELECT COUNT(*) AS c FROM model_versions WHERE status='推理'").fetchone()["c"]
    classes = db.execute("SELECT COUNT(*) AS c FROM label_classes").fetchone()["c"]
    # 标注进度按检测项目，读 label_tasks 缓存（由 LS webhook 与 /label 刷新），
    # 首页因此不必逐个项目调 LS。label_images.annotated 是内置标注器时代的字段，
    # 换 LS 后不再写入，读它只会恒为 0。
    brand_prog = []
    annotated = 0
    for it in db.execute("SELECT * FROM detect_items WHERE status=1 ORDER BY id").fetchall():
        n_img = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE item_id=?",
                           (it["id"],)).fetchone()["c"] or 0
        if not n_img:
            continue
        c = db.execute("SELECT total,labeling FROM label_tasks WHERE item_id=?",
                       (it["id"],)).fetchone()
        done = c["labeling"] if c else 0
        annotated += done
        brand_prog.append({"brand": it["short_name"] or it["name"],
                           "total": (c["total"] if c else 0) or n_img, "done": done})
    max_count = max([s["count"] for s in line_stats] + [1])
    return render_template("index.html", active="home", line_stats=line_stats,
                           total_imgs=total_imgs, annotated=annotated, infer=infer,
                           classes=classes, lines_n=len(lines), brand_prog=brand_prog,
                           max_count=max_count)


# ---------------------------------------------------------------- 用户管理
@app.route("/users")
@login_required
@admin_required
def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return render_template("users.html", rows=rows, active="users")


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
CONFIG_TABS = [("items", "检测项目"), ("lines", "机组/产线"), ("brands", "牌号/品规"),
               ("workshops", "车间"), ("areas", "区域"), ("defects", "缺陷分类"),
               ("faces", "相机面映射"), ("integration", "系统集成")]


@app.route("/config/<tab>")
@login_required
def config(tab):
    if tab not in dict(CONFIG_TABS):
        abort(404)
    db = get_db()
    if tab == "integration":
        rows = [{"key": k, "label": lab, "hint": hint, "value": get_cfg(k),
                 "from_db": k in _cfg_cache["d"] and bool(_cfg_cache["d"][k])}
                for k, lab, hint in CFG_LABELS]
        return render_template("config_integration.html", tab=tab, tabs=CONFIG_TABS,
                               rows=rows, active="config",
                               default_webhook=url_for("label_webhook", _external=True))
    data = {
        "items": [dict(r, img_count=db.execute(
            "SELECT COUNT(*) AS c FROM label_images WHERE item_id=?", (r["id"],)).fetchone()["c"])
            for r in db.execute("SELECT * FROM detect_items ORDER BY id").fetchall()],
        "lines": db.execute("SELECT * FROM prod_lines ORDER BY id").fetchall(),
        "brands": db.execute("SELECT * FROM brands ORDER BY id").fetchall(),
        "workshops": db.execute("SELECT * FROM workshops ORDER BY id").fetchall(),
        "areas": db.execute("SELECT * FROM areas ORDER BY id").fetchall(),
        "defects": db.execute("SELECT * FROM label_classes ORDER BY id").fetchall(),
        "faces": db.execute("SELECT * FROM camera_faces ORDER BY machine_model, sort_order, id").fetchall(),
    }[tab]
    if tab == "faces":
        return render_template("config_faces.html", tab=tab, tabs=CONFIG_TABS,
                               rows=[dict(r) for r in data], active="config")
    workshops = [dict(w) for w in db.execute("SELECT * FROM workshops WHERE status=1 ORDER BY id").fetchall()]
    areas = [dict(a) for a in db.execute("SELECT * FROM areas WHERE status=1 ORDER BY id").fetchall()]
    return render_template("config.html", tab=tab, tabs=CONFIG_TABS,
                           rows=data, workshops=workshops, areas=areas, active="config")


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


@app.route("/config/faces/discover")
@login_required
def config_faces_discover():
    """扫数据源里第 7 层(相机面)的实际目录名，标出哪些已映射、哪些待映射。
    现场路径：车间/产线/检测项目/日期/班组/班次/相机面/文件，相机面是倒数第二级。"""
    db = get_db()
    raw_faces, errs = {}, []
    for l in db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall():
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
        if not (scfg and scfg["server_addr"]):
            continue
        try:
            s3, cfg = get_s3(scfg)
            # 列一批对象，取倒数第二级作为相机面（比逐层下钻省事且够用）
            tok = None
            seen = 0
            while seen < 3000:
                kw = {"Bucket": cfg["in_bucket"], "MaxKeys": 1000}
                if tok:
                    kw["ContinuationToken"] = tok
                r = s3.list_objects_v2(**kw)
                for o in r.get("Contents", []):
                    parts = o["Key"].split("/")
                    if len(parts) >= 2 and "." in parts[-1]:
                        raw_faces.setdefault(parts[-2], set()).add(l["name"])
                    seen += 1
                if not r.get("IsTruncated"):
                    break
                tok = r.get("NextContinuationToken")
        except Exception as e:
            errs.append("%s: %s" % (l["name"], str(e)[:60]))
            app.logger.warning("探测相机面失败（产线 %s）：%s", l["name"], e)
    mapped = {r["raw_name"]: (r["face_name"], r["face_code"]) for r in
              db.execute("SELECT * FROM camera_faces").fetchall()}
    out = []
    for raw in sorted(raw_faces):
        m = mapped.get(raw)
        out.append({"raw_name": raw, "lines": sorted(raw_faces[raw]),
                    "face_name": m[0] if m else "", "face_code": m[1] if m else "",
                    "mapped": bool(m)})
    return jsonify({"faces": out, "errors": errs})


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
    if what == "infer":
        url = request.form.get("infer_url", "").strip()
        if not url:
            return jsonify({"ok": False, "msg": "未填写地址；留空将使用内置模拟判定"})
        try:
            health = url.rsplit("/", 1)[0] + "/health"
            with urllib.request.urlopen(health, timeout=8) as r:
                d = json.loads(r.read().decode())
            return jsonify({"ok": True, "msg": "连接成功，底库 %s 张 / %s 类"
                            % (d.get("gallery", "?"), d.get("classes", "?"))})
        except Exception as e:
            return jsonify({"ok": False, "msg": "连接失败：%s" % str(e)[:110]})
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
    elif tab == "faces":
        vals = (f["raw_name"].strip(), f["face_name"].strip(), f.get("face_code", "").strip(),
                f.get("machine_model", "").strip(), f.get("sort_order", 0) or 0)
        if rid:
            db.execute("UPDATE camera_faces SET raw_name=?,face_name=?,face_code=?,"
                       "machine_model=?,sort_order=? WHERE id=?", vals + (rid,))
        else:
            # 自动发现里「一键映射」也走这里，同名目录已存在则更新，避免唯一键冲突
            db.execute("INSERT INTO camera_faces(raw_name,face_name,face_code,machine_model,sort_order) "
                       "VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE "
                       "face_name=VALUES(face_name),face_code=VALUES(face_code),"
                       "machine_model=VALUES(machine_model),sort_order=VALUES(sort_order)", vals)
    db.commit()
    flash("保存成功", "success")
    return redirect(url_for("config", tab=tab))


@app.route("/config/<tab>/delete/<int:rid>", methods=["POST"])
@login_required
def config_delete(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands",
             "workshops": "workshops", "areas": "areas", "defects": "label_classes",
             "faces": "camera_faces"}.get(tab)
    if not table:
        abort(404)
    db = get_db()
    db.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
    db.commit()
    flash("已删除", "success")
    return redirect(url_for("config", tab=tab))


@app.route("/config/<tab>/toggle/<int:rid>", methods=["POST"])
@login_required
def config_toggle(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands",
             "workshops": "workshops", "areas": "areas", "defects": "label_classes",
             "faces": "camera_faces"}.get(tab)
    if not table:
        abort(404)
    db = get_db()
    db.execute("UPDATE %s SET status=1-status WHERE id=?" % table, (rid,))
    db.commit()
    return redirect(url_for("config", tab=tab))


# ---------------------------------------------------------------- 图像采集
COLLECT_TABS = [("config", "采集配置"), ("monitor", "采集监控"),
                ("history", "历史采集"), ("images", "图片查询")]


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
            lid = f.get("line_id")
            if lid:
                db.execute("DELETE FROM storage_config WHERE line_id=?", (lid,))
                db.execute("DELETE FROM terminal_config WHERE line_id=?", (lid,))
                if f.get("mode") == "terminal":
                    db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                               "VALUES(?,?,?,?,?,?,?,?,?)",
                               (lid, f.get("sys_addr", ""), f.get("ng_dir", ""), f.get("date_dir", "YYYYMMDD"),
                                f.get("str_pos", "1,8"), f.get("shift_dir", "早、中、晚"),
                                f.get("cam_count", 4) or 4, f.get("cam_dirs", ""), f.get("brand_dirs", "")))
                else:
                    db.execute("INSERT INTO storage_config(line_id,server_addr,in_bucket,username,password,out_bucket) "
                               "VALUES(?,?,?,?,?,?)",
                               (lid, f.get("server_addr", ""), f.get("in_bucket", ""), f.get("username", ""),
                                f.get("password", ""), f.get("out_bucket", "")))
                db.commit()
                flash("产线采集来源已保存", "success")
            return redirect(url_for("collect", tab="config"))
        line_cfgs = []
        for l in lines:
            scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
            tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
            host = ((scfg["server_addr"] or "").split(":")[0]) if scfg else ""
            line_cfgs.append({"line": l, "mode": "terminal" if tcfg else "minio",
                              "scfg": scfg, "tcfg": tcfg,
                              "console_url": ("http://%s:9001" % host) if host else "#"})
        ctx["line_cfgs"] = line_cfgs
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
        ctx["records"] = [{"line": ln, "date": _fmt(d), "shift": s, "brand": b, "img_count": c}
                          for (ln, d, s, b), c in sorted(agg.items(), reverse=True)]
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


@app.route("/label")
@login_required
def label():
    """缺陷标注（新架构）：左侧牌号下拉，右侧该牌号的相机面=建模单元。
    每个单元对接对方 CubeStudio（建项目→内嵌标注→轮询进度）。"""
    db = get_db()
    brands = [dict(b) for b in db.execute(
        "SELECT * FROM brands WHERE status=1 ORDER BY id").fetchall()]
    cur_brand = request.args.get("brand") or (brands[0]["spec"] if brands else "")
    faces = [dict(f) for f in db.execute(
        "SELECT * FROM camera_faces WHERE status=1 ORDER BY sort_order, id").fetchall()]
    cs_online = cs_ok()
    units = []
    for f in faces:
        u = db.execute("SELECT * FROM model_units WHERE brand=? AND face_id=?",
                       (cur_brand, f["id"])).fetchone()
        units.append({
            "face_id": f["id"], "face_name": f["face_name"], "face_code": f["face_code"],
            "raw_name": f["raw_name"],
            "exists": bool(u), "unit_id": u["id"] if u else 0,
            "cs_project_id": u["cs_project_id"] if u else "",
            "annotated": u["annotated"] if u else 0, "total": u["total"] if u else 0,
            "model_version": u["model_version"] if u else "",
        })
    return render_template("label.html", brands=brands, cur_brand=cur_brand,
                           units=units, cs_online=cs_online, active="label")


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
    return redirect(url_for("label", brand=brand))


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


@app.route("/label/save/<int:image_id>", methods=["POST"])
@login_required
def label_save(image_id):
    db = get_db()
    data = request.get_json(force=True) or {}
    items = data.get("annos", [])
    db.execute("UPDATE label_images SET width=?,height=?,annotated=? WHERE id=?",
               (int(data.get("width", 0)), int(data.get("height", 0)), 1 if items else 0, image_id))
    db.execute("DELETE FROM annotations WHERE image_id=?", (image_id,))
    for a in items:
        bx = a.get("bbox", [0, 0, 0, 0])
        db.execute("INSERT INTO annotations(image_id,class_id,class_name,shape,"
                   "bbox_x,bbox_y,bbox_w,bbox_h,points,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (image_id, a.get("class_id"), a.get("class_name", ""), a.get("shape", "rect"),
                    bx[0], bx[1], bx[2], bx[3], json.dumps(a.get("points", [])),
                    session.get("username", "")))
    db.commit()
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
        flash("样本集 %s%s 已发布到 defect-datasets：%d 张图、%d 个标注框" % (
            name, version, len(coco["images"]), len(coco["annotations"])), "success")
    except Exception as e:
        flash("导出失败: %s" % str(e)[:120], "error")
    return redirect(url_for("label"))


# ---------------------------------------------------------------- 模型版本
@app.route("/model")
@login_required
def model():
    """模型版本按检测项目看 —— 模型是为某个检测项目训的，与牌号无关。
    此前按牌号切换，且查不到时会静默列出全部版本（等于过滤条件失效）。"""
    db = get_db()
    it = cur_detect_item()
    rows = db.execute("SELECT * FROM model_versions WHERE item_id=? ORDER BY id DESC",
                      (it["id"] if it else 0,)).fetchall()
    return render_template("model.html", rows=rows, active="model",
                           cur_brand=(it["short_name"] or it["name"]) if it else "")


@app.route("/model/status/<int:mid>", methods=["POST"])
@login_required
def model_status(mid):
    st = request.form.get("status", "停用")
    db = get_db()
    db.execute("UPDATE model_versions SET status=? WHERE id=?", (st, mid))
    db.commit()
    return redirect(request.referrer or url_for("model"))


# ---------------------------------------------------------------- 分析结果
ANALYSIS_TABS = [("shift", "当班统计"), ("history", "历史统计"), ("trend", "趋势分析")]


@app.route("/analysis/<tab>")
@login_required
def analysis(tab):
    if tab not in dict(ANALYSIS_TABS):
        abort(404)
    db = get_db()
    lines = db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall()
    cur_line = request.args.get("line", "all")
    it = cur_detect_item()
    infer_total = db.execute("SELECT COUNT(*) AS c FROM inference_results WHERE item_id=?",
                             (it["id"] if it else 0,)).fetchone()["c"]
    return render_template("analysis.html", tab=tab, tabs=ANALYSIS_TABS,
                           lines=lines, cur_line=cur_line, active="analysis",
                           infer_total=infer_total, using_mock=(not get_cfg("infer_url")),
                           today=datetime.now().strftime("%Y/%m/%d"))


def run_inference_one(img, infer_url, classes):
    """调算法服务对一张 NG 图推理，返回判定 dict；本张失败返回 None（跳过）。
    没有内置模拟兜底 —— 推理结果会进分析库当统计依据，编造的数据一旦混进去
    就再也分不清真假，宁可少一条也不能假一条。"""
    try:
        import urllib.request
        body = json.dumps({"image_id": img["id"], "src_key": img["src_key"],
                           "line": img["line_name"], "brand": img["brand"]}).encode("utf-8")
        req = urllib.request.Request(infer_url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        cname = d.get("class_name", "")
        cid = d.get("class_id")
        if cid is None and cname:  # 算法服务只回类名，平台自己查字典映射 id
            cid = next((c["id"] for c in classes if c["name"] == cname), None)
        return {"is_defect": int(d.get("is_defect", 1)), "class_id": cid,
                "class_name": cname, "confidence": float(d.get("confidence", 0)),
                "model_version": d.get("model_version", "")}
    except Exception as e:
        app.logger.warning("推理服务调用失败，跳过（%s）：%s", img["src_key"], e)
        return None


@app.route("/analysis/infer", methods=["POST"])
@login_required
def analysis_infer():
    db = get_db()
    infer_url = get_cfg("infer_url")
    if not infer_url:
        flash("未配置推理服务地址，无法推理。请到「基础配置 → 系统集成」填写。", "error")
        return redirect(request.referrer or url_for("analysis", tab="shift"))
    sync_label_images(force=True)
    classes = [dict(c) for c in db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()]
    done = {r["image_id"] for r in db.execute("SELECT image_id FROM inference_results").fetchall()}
    n = 0
    skipped = 0
    for img in db.execute("SELECT * FROM label_images").fetchall():
        if img["id"] in done:
            continue
        parts = img["src_key"].split("/")
        date = parts[-4] if len(parts) >= 4 else ""
        shift = parts[-3] if len(parts) >= 3 else ""
        res = run_inference_one(img, infer_url, classes)
        if res is None:
            skipped += 1
            continue
        db.execute("INSERT INTO inference_results(image_id,item_id,line_name,brand,img_date,shift,"
                   "is_defect,class_id,class_name,confidence,model_version) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (img["id"], img["item_id"], img["line_name"], img["brand"], date, shift,
                    res["is_defect"], res["class_id"], res["class_name"], res["confidence"],
                    res["model_version"]))  # 版本取自算法服务自报，能追溯到具体模型
        n += 1
    db.commit()
    tip = "推理完成，本次新增 %d 条结果" % n
    if skipped:
        tip += "；%d 张推理服务读不到已跳过" % skipped
    flash(tip, "success")
    return redirect(request.referrer or url_for("analysis", tab="shift"))


@app.route("/api/analysis/<tab>")
@login_required
def api_analysis(tab):
    db = get_db()
    line = request.args.get("line", "all")
    it = cur_detect_item()
    base = "FROM inference_results WHERE is_defect=1 AND item_id=?"
    params = [it["id"] if it else 0]
    if line != "all":
        base += " AND line_name=?"
        params.append(line)
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
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "9573")), debug=False)
