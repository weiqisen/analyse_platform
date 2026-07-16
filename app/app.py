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

# 算法推理服务地址（HTTP）。为空时使用内置模拟判定，接入算法平台后配置此项即可
INFER_URL = os.environ.get("INFER_URL", "")

# Label Studio 对接（标注引擎）
LS_URL = os.environ.get("LS_URL", "http://127.0.0.1:8080")
LS_TOKEN = os.environ.get("LS_TOKEN", "cigarette-label-studio-token-2026")

app = Flask(__name__)
app.secret_key = "yancao-analyse-platform-secret-2026"

# 检测项目（顶部导航）
DETECT_ITEMS = ["烟支外观", "五轮成像", "小包CCD", "散包检测", "条外观"]


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
        """CREATE TABLE IF NOT EXISTS collect_history(
            id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
            line VARCHAR(32) COMMENT '产线名称',
            date VARCHAR(32) COMMENT '采集日期',
            shift VARCHAR(16) COMMENT '班次',
            brand VARCHAR(64) COMMENT '牌号',
            img_count INT COMMENT '采集图片数量'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='历史采集记录表'""",
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
    ]
    for stmt in ddl:
        db.execute(stmt)
    db.commit()
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
        for b in ["中华（软）", "玉溪（软）", "玉溪（创客）", "云烟（紫）"]:
            db.execute("INSERT INTO label_tasks(brand,total,labeling,unlabeled) VALUES(?,?,?,?)",
                       (b, 98, 59, 39))
        seed_models = [
            ("V1.3", "2026-06-30", "在前一版本基础上调整学习率与数据增强", "测试"),
            ("V1.2", "2026-05-31", "在前一版本基础上补充翘边样本", "推理"),
            ("V1.1", "2026-04-30", "在前一版本基础上调整置信度阈值", "停用"),
            ("V1.0", "2026-03-31", "在前一版本基础上扩充训练集", "停用"),
            ("V0.5", "2026-02-28", "在前一版本基础上调整骨干网络", "停用"),
            ("V0.1", "2026-01-31", "对10种缺陷识别准确率90%", "停用"),
        ]
        for v, d, n, s in seed_models:
            db.execute("INSERT INTO model_versions(brand,version,pub_date,note,status) VALUES(?,?,?,?,?)",
                       ("中华（软）", v, d, n, s))
        # 历史采集记录
        rnd = random.Random(42)
        brands = ["中华（软）", "玉溪（软）", "玉溪（创客）", "云烟（紫）"]
        today = datetime.now()
        for d in range(14):
            day = (today - timedelta(days=d)).strftime("%Y/%m/%d")
            for shift in ["早班", "中班", "晚班"]:
                db.execute("INSERT INTO collect_history(line,date,shift,brand,img_count) VALUES(?,?,?,?,?)",
                           ("A%02d" % rnd.randint(1, 3), day, shift,
                            rnd.choice(brands), rnd.randint(80, 420)))
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


@app.context_processor
def inject_globals():
    return dict(DETECT_ITEMS=DETECT_ITEMS,
                cur_item=request.args.get("item", DETECT_ITEMS[0]))


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
    annotated = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE annotated=1").fetchone()["c"]
    infer = db.execute("SELECT COUNT(*) AS c FROM model_versions WHERE status='推理'").fetchone()["c"]
    classes = db.execute("SELECT COUNT(*) AS c FROM label_classes").fetchone()["c"]
    brand_prog = []
    for b in db.execute("SELECT DISTINCT brand FROM label_images ORDER BY brand").fetchall():
        r = db.execute("SELECT COUNT(*) AS t, COALESCE(SUM(annotated),0) AS d "
                       "FROM label_images WHERE brand=?", (b["brand"],)).fetchone()
        brand_prog.append({"brand": b["brand"], "total": r["t"] or 0, "done": int(r["d"] or 0)})
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
               ("workshops", "车间"), ("areas", "区域"), ("defects", "缺陷分类")]


@app.route("/config/<tab>")
@login_required
def config(tab):
    if tab not in dict(CONFIG_TABS):
        abort(404)
    db = get_db()
    data = {
        "items": db.execute("SELECT * FROM detect_items ORDER BY id").fetchall(),
        "lines": db.execute("SELECT * FROM prod_lines ORDER BY id").fetchall(),
        "brands": db.execute("SELECT * FROM brands ORDER BY id").fetchall(),
        "workshops": db.execute("SELECT * FROM workshops ORDER BY id").fetchall(),
        "areas": db.execute("SELECT * FROM areas ORDER BY id").fetchall(),
        "defects": db.execute("SELECT * FROM label_classes ORDER BY id").fetchall(),
    }[tab]
    workshops = [dict(w) for w in db.execute("SELECT * FROM workshops WHERE status=1 ORDER BY id").fetchall()]
    areas = [dict(a) for a in db.execute("SELECT * FROM areas WHERE status=1 ORDER BY id").fetchall()]
    return render_template("config.html", tab=tab, tabs=CONFIG_TABS,
                           rows=data, workshops=workshops, areas=areas, active="config")


@app.route("/config/<tab>/save", methods=["POST"])
@login_required
def config_save(tab):
    db = get_db()
    f = request.form
    rid = f.get("id")
    if tab == "items":
        if rid:
            db.execute("UPDATE detect_items SET code=?,name=?,short_name=? WHERE id=?",
                       (f["code"], f["name"], f.get("short_name", ""), rid))
        else:
            db.execute("INSERT INTO detect_items(code,name,short_name) VALUES(?,?,?)",
                       (f["code"], f["name"], f.get("short_name", "")))
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
    db.commit()
    flash("保存成功", "success")
    return redirect(url_for("config", tab=tab))


@app.route("/config/<tab>/delete/<int:rid>", methods=["POST"])
@login_required
def config_delete(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands",
             "workshops": "workshops", "areas": "areas", "defects": "label_classes"}.get(tab)
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
             "workshops": "workshops", "areas": "areas", "defects": "label_classes"}.get(tab)
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
        if cur_line == "all":
            for l in lines:
                c = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE line_name=?",
                               (l["name"],)).fetchone()["c"]
                ctx["cams"].append({"name": l["name"], "count": c})
        else:
            from collections import Counter
            cnt = Counter()
            for im in db.execute("SELECT src_key FROM label_images WHERE line_name=?",
                                 (cur_line,)).fetchall():
                parts = im["src_key"].split("/")
                cnt[parts[-2] if len(parts) >= 2 else "?"] += 1
            ctx["cams"] = [{"name": k, "count": v} for k, v in sorted(cnt.items())]
    elif tab == "history":
        sync_label_images()
        from collections import defaultdict
        agg = defaultdict(int)
        q = "SELECT brand, src_key, line_name FROM label_images"
        params = ()
        if cur_line != "all":
            q += " WHERE line_name=?"
            params = (cur_line,)
        for im in db.execute(q, params).fetchall():
            parts = im["src_key"].split("/")
            if len(parts) < 4:
                continue
            agg[(im["line_name"], parts[-4], parts[-3], im["brand"])] += 1  # (产线, 日期, 班次, 牌号)

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
        ctx["crumbs"] = path.split("/") if path else []
        ctx["root_label"] = tcfg["ng_dir"] if tcfg else (scfg["in_bucket"] if scfg else "根目录")
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
                            ctx["dirs"].append({"name": e.filename,
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
                    ctx["dirs"].append({"name": name, "path": (prefix + name).strip("/")})
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


# ---------------------------------------------------------------- Label Studio 对接
def _ls_req(method, path, data=None):
    """调 Label Studio REST API。"""
    import urllib.request
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(LS_URL + path, data=body,
                                 headers={"Authorization": "Token " + LS_TOKEN,
                                          "Content-Type": "application/json"})
    req.get_method = lambda: method.upper()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 204:
                return {}
            return json.loads(r.read().decode("utf-8")) if r.status not in (200, 201) else _ls_req._resp or {}
    except urllib.request.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise Exception("LS %s %s → %d: %s" % (method, path, e.code, body))


def _ls_get(path):
    try:
        r = __import__("urllib.request").request.Request(
            LS_URL + path, headers={"Authorization": "Token " + LS_TOKEN})
        r.get_method = lambda: "GET"
        with __import__("urllib.request").request.urlopen(r, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _ls_post(path, data):
    import urllib.request
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(LS_URL + path, data=body,
                                 headers={"Authorization": "Token " + LS_TOKEN,
                                          "Content-Type": "application/json"})
    req.get_method = lambda: "POST"
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def ls_ensure_project(brand, classes, scfg):
    """为某牌号确保 Label Studio 标注项目存在，返回 (project_id, task_count)。"""
    r = _ls_get("/api/projects?page_size=200") or {}
    pid = None
    for p in r.get("results", []):
        if p.get("title") == brand:
            pid = p["id"]
            break
    if not pid:
        labels = [{"value": c["name"], "background": "#%02x%02x%02x" %
                   tuple((int(c["id"]) * 47 * (i + 1) + 40) % 200 + 30 for i in range(3))} for c in classes]
        label_config = '<View>\n  <Image name="image" value="$image"/>\n  <RectangleLabels name="tag" toName="image">\n'
        for l in labels:
            label_config += '    <Label value="%s" background="%s"/>\n' % (l["value"], l["background"])
        label_config += '  </RectangleLabels>\n</View>'
        p = _ls_post("/api/projects", {"title": brand, "label_config": label_config,
                     "description": "卷烟厂缺陷标注 · %s · %d类缺陷" % (brand, len(classes))})
        pid = p.get("id")
    if not pid:
        return None, 0
    total = (_ls_get("/api/projects/%d" % pid) or {}).get("task_number", 0)
    return pid, total


def ls_import_images(pid, brand, scfg):
    """从 MinIO 导入某牌号「正常」图片 presigned URL 到 Label Studio 项目（幂等）。"""
    import boto3
    from botocore.client import Config
    s3 = boto3.client("s3", endpoint_url="http://" + scfg["server_addr"],
                      aws_access_key_id=scfg["username"], aws_secret_access_key=scfg["password"],
                      region_name="us-east-1",
                      config=Config(signature_version="s3v4", connect_timeout=8, read_timeout=20))
    # 列出桶里已有任务，避免重复导入
    existing = set()
    try:
        r = _ls_get("/api/projects/%d/tasks?page_size=5000" % pid) or {}
        for t in r.get("results", []):
            url = (t.get("data") or {}).get("image", "")
            if url:
                existing.add(url.split("?")[0].rsplit("/", 1)[-1])
    except Exception:
        pass

    tasks = []
    tok = None
    while True:
        kw = {"Bucket": scfg["in_bucket"], "Prefix": brand + "/", "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            k = o["Key"]
            if "/正常/" not in k or not k.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                continue
            if k.rsplit("/", 1)[-1] in existing:
                continue
            url = s3.generate_presigned_url("get_object", Params={"Bucket": scfg["in_bucket"], "Key": k}, ExpiresIn=86400)
            tasks.append({"data": {"image": url}})
        if r.get("IsTruncated"):
            tok = r.get("NextContinuationToken")
        else:
            break
    if not tasks:
        return 0
    for i in range(0, len(tasks), 100):
        _ls_post("/api/projects/%d/import" % pid, tasks[i:i + 100])
    return len(tasks)


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
    BRAND_MAP = {"小包外观": "玉溪（硬）"}
    rows = []
    for l in db.execute("SELECT * FROM prod_lines").fetchall():
        scfg = db.execute("SELECT * FROM storage_config WHERE line_id=?", (l["id"],)).fetchone()
        tcfg = db.execute("SELECT * FROM terminal_config WHERE line_id=?", (l["id"],)).fetchone()
        if scfg and scfg["server_addr"]:
            # 对象存储产线：分页列全部对象，顶层目录=牌号
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
                            b = BRAND_MAP.get(k.split("/")[0], k.split("/")[0])
                            rows.append(("minio", k, b, l["name"]))
                    if r.get("IsTruncated"):
                        tok = r.get("NextContinuationToken")
                    else:
                        break
            except Exception:
                pass
        elif tcfg:
            # 工控机产线：SFTP 全量 walk，相对路径顶层=牌号
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
                            b = rel.split("/")[0] if "/" in rel else ""
                            brand = BRAND_MAP.get(b, b)
                            rows.append(("terminal", full, brand, l["name"]))
                try:
                    walk(root)
                finally:
                    t.close()
            except Exception:
                pass
    added = False
    for source, key, brand, line in rows:
        if brand and key not in existing:
            try:
                db.execute("INSERT INTO label_images(brand,source,src_key,line_name) VALUES(?,?,?,?)",
                           (brand, source, key, line))
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
    db = get_db()
    sync_label_images()
    classes = [dict(c) for c in db.execute("SELECT * FROM label_classes WHERE status=1 ORDER BY id").fetchall()]
    scfg = db.execute("SELECT * FROM storage_config LIMIT 1").fetchone()
    ls_ok = False
    try:
        r = _ls_get("/api/version")
        if r and isinstance(r, dict) and "label-studio" in str(r).lower():
            ls_ok = True
    except Exception:
        pass
    tasks = []
    for b in db.execute("SELECT spec FROM brands WHERE status=1 ORDER BY id").fetchall():
        brand = b["spec"]
        total_db = db.execute("SELECT COUNT(*) AS c FROM label_images WHERE brand=?",
                              (brand,)).fetchone()["c"] or 0
        pid = None
        ls_total = ls_done = 0
        if ls_ok and scfg and total_db:
            try:
                pid, _ = ls_ensure_project(brand, classes, scfg)
                if pid:
                    ls_import_images(pid, brand, scfg)
                    cnt = ls_task_counts(pid)
                    ls_total, ls_done = cnt["total"], cnt["done"]
            except Exception:
                pass
        tasks.append({"brand": brand, "total": ls_total or total_db,
                      "labeling": ls_done, "unlabeled": max(0, (ls_total or total_db) - ls_done),
                      "exported": ls_done, "ls_pid": pid, "ls_ok": ls_ok})
    has_ls = ls_ok
    return render_template("label.html", tasks=tasks, classes=classes, active="label",
                           LS_URL=LS_URL, has_ls=has_ls)


@app.route("/label/class/add", methods=["POST"])
@login_required
def label_class_add():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO label_classes(name) VALUES(?)", (name,))
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


@app.route("/label/anno/<brand>")
@login_required
def label_anno(brand):
    """标注入口：Label Studio 可用时跳转其项目页，否则用平台内置标注器。"""
    db = get_db()
    try:
        r = _ls_get("/api/projects?page_size=200") or {}
        for p in r.get("results", []):
            if p.get("title") == brand:
                return redirect(LS_URL.rstrip("/") + "/projects/%d/data?tab=1" % p["id"])
    except Exception:
        pass
    imgs = db.execute("SELECT * FROM label_images WHERE brand=? AND src_key LIKE ? ORDER BY id", (brand, "%/正常/%")).fetchall()
    if not imgs:
        sync_label_images()
        imgs = db.execute("SELECT * FROM label_images WHERE brand=? AND src_key LIKE ? ORDER BY id", (brand, "%/正常/%")).fetchall()
    if not imgs:
        flash("牌号「%s」在数据源中未找到图片" % brand, "error")
        return redirect(url_for("label"))
    idx = request.args.get("i", type=int) or 0
    idx = max(0, min(idx, len(imgs) - 1))
    img = imgs[idx]
    classes = [dict(c) for c in db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()]
    annos = db.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY id", (img["id"],)).fetchall()
    return render_template("label_anno.html", brand=brand, img=dict(img), img_url=label_image_url(img),
                           idx=idx, total=len(imgs), classes=classes,
                           annos=[dict(a) for a in annos], active="label")


@app.route("/label/webhook", methods=["POST"])
def label_webhook():
    """Label Studio 标注事件回写 —— 更新图片标注计数（免登 token 验证）。"""
    hdr = request.headers.get("Authorization", "")
    if hdr != "Token " + LS_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    try:
        data = request.get_json(force=True) or {}
        action = data.get("action", "")
        if action in ("ANNOTATION_CREATED", "ANNOTATION_UPDATED", "ANNOTATION_DELETED",
                      "TASK_CREATED", "TASK_DELETED"):
            pid = data.get("project", {}).get("id")
            if pid:
                cnt = ls_task_counts(pid)
                db = get_db()
                p = _ls_get("/api/projects/%d" % pid) or {}
                brand = p.get("title", "")
                if brand:
                    db.execute("UPDATE label_tasks SET total=?,labeling=?,unlabeled=? WHERE brand=?",
                               (cnt["total"], cnt["done"], cnt["unlabeled"], brand))
                    db.commit()
    except Exception:
        pass
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


@app.route("/label/export/<brand>", methods=["POST"])
@login_required
def label_export(brand):
    """持久化导出标注样本集到 defect-datasets 桶（算法训练流水线直接读取）。"""
    db = get_db()
    imgs = db.execute("SELECT * FROM label_images WHERE brand=? AND annotated=1 AND src_key LIKE ? ORDER BY id",
                      (brand, "%/正常/%")).fetchall()
    if not imgs:
        flash("该牌号暂无可导出的已标注样本", "error")
        return redirect(url_for("label"))
    cats = db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()
    # 版本号：查询当前牌号已有版本数 +1
    ver_n = db.execute("SELECT COUNT(*) c FROM model_versions WHERE brand=?", (brand,)).fetchone()["c"] + 1
    version = "v%d" % ver_n
    prefix = "%s/%s/" % (brand, version)

    # 组装 COCO
    coco = {"images": [], "annotations": [],
            "categories": [{"id": c["id"], "name": c["name"]} for c in cats]}
    aid = 1
    for im in imgs:
        coco["images"].append({"id": im["id"], "file_name": im["src_key"],
                               "width": im["width"], "height": im["height"]})
        for a in db.execute("SELECT * FROM annotations WHERE image_id=?", (im["id"],)).fetchall():
            seg = []
            if a["shape"] == "polygon" and a["points"]:
                pts = json.loads(a["points"])
                seg = [[c for p in pts for c in p]]
            coco["annotations"].append({
                "id": aid, "image_id": im["id"], "category_id": a["class_id"],
                "bbox": [a["bbox_x"], a["bbox_y"], a["bbox_w"], a["bbox_h"]],
                "area": a["bbox_w"] * a["bbox_h"], "iscrowd": 0, "segmentation": seg})
            aid += 1

    # 写入 defect-datasets 桶（COCO json + 复制对应图片）
    try:
        scfg = db.execute("SELECT * FROM storage_config LIMIT 1").fetchone()
        if not scfg:
            flash("未找到对象存储配置", "error")
            return redirect(url_for("label"))
        import boto3
        from botocore.client import Config
        s3 = boto3.client("s3", endpoint_url="http://" + scfg["server_addr"],
                          aws_access_key_id=scfg["username"], aws_secret_access_key=scfg["password"],
                          region_name="us-east-1",
                          config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=30))
        dst_bucket = "defect-datasets"
        # COCO json
        body = json.dumps(coco, ensure_ascii=False, indent=2).encode("utf-8")
        s3.put_object(Bucket=dst_bucket, Key=prefix + "coco.json", Body=body, ContentType="application/json")
        # 复制原图（从 defect-raw）
        for im in imgs:
            s3.copy_object(Bucket=dst_bucket, Key=prefix + im["src_key"],
                           CopySource={"Bucket": "defect-raw", "Key": im["src_key"]})
        # 注册模型版本
        db.execute("INSERT INTO model_versions(brand,version,pub_date,note,status) VALUES(?,?,?,?,?)",
                   (brand, version, datetime.now().strftime("%Y-%m-%d"),
                    "从 %d 张已标注样本导出" % len(imgs), "测试"))
        db.commit()
        flash("样本集 %s%s 已发布到 defect-datasets，含 %d 张标注图" % (brand, version, len(imgs)), "success")
    except Exception as e:
        flash("导出失败: %s" % str(e)[:120], "error")
    return redirect(url_for("label"))


# ---------------------------------------------------------------- 模型版本
@app.route("/model")
@login_required
def model():
    db = get_db()
    brands = [r["spec"] for r in db.execute("SELECT spec FROM brands WHERE status=1").fetchall()]
    cur_brand = request.args.get("brand") or (brands[0] if brands else "")
    rows = db.execute("SELECT * FROM model_versions WHERE brand=? ORDER BY id DESC", (cur_brand,)).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM model_versions ORDER BY id DESC").fetchall()
    return render_template("model.html", brands=brands, cur_brand=cur_brand,
                           rows=rows, active="model")


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
    infer_total = db.execute("SELECT COUNT(*) AS c FROM inference_results").fetchone()["c"]
    return render_template("analysis.html", tab=tab, tabs=ANALYSIS_TABS,
                           lines=lines, cur_line=cur_line, active="analysis",
                           infer_total=infer_total, using_mock=(not INFER_URL),
                           today=datetime.now().strftime("%Y/%m/%d"))


def run_inference_one(img, classes):
    """对一张 NG 图推理，返回判定 dict。INFER_URL 有则调算法服务，否则内置模拟。"""
    if INFER_URL:
        try:
            import urllib.request
            body = json.dumps({"image_id": img["id"], "src_key": img["src_key"],
                               "line": img["line_name"], "brand": img["brand"]}).encode("utf-8")
            req = urllib.request.Request(INFER_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
            return {"is_defect": int(d.get("is_defect", 1)), "class_id": d.get("class_id"),
                    "class_name": d.get("class_name", ""), "confidence": float(d.get("confidence", 0))}
        except Exception:
            pass
    h = int(hashlib.md5(img["src_key"].encode("utf-8")).hexdigest(), 16)
    cls = classes[h % len(classes)] if classes else None
    return {"is_defect": 0 if h % 5 == 0 else 1,
            "class_id": cls["id"] if cls else None,
            "class_name": cls["name"] if cls else "",
            "confidence": round(0.6 + (h % 40) / 100.0, 3)}


@app.route("/analysis/infer", methods=["POST"])
@login_required
def analysis_infer():
    db = get_db()
    sync_label_images(force=True)
    classes = [dict(c) for c in db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()]
    done = {r["image_id"] for r in db.execute("SELECT image_id FROM inference_results").fetchall()}
    v = db.execute("SELECT version FROM model_versions WHERE status='推理' ORDER BY id DESC LIMIT 1").fetchone()
    ver = v["version"] if v else ""
    n = 0
    for img in db.execute("SELECT * FROM label_images").fetchall():
        if img["id"] in done:
            continue
        parts = img["src_key"].split("/")
        date = parts[-4] if len(parts) >= 4 else ""
        shift = parts[-3] if len(parts) >= 3 else ""
        res = run_inference_one(img, classes)
        db.execute("INSERT INTO inference_results(image_id,line_name,brand,img_date,shift,"
                   "is_defect,class_id,class_name,confidence,model_version) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (img["id"], img["line_name"], img["brand"], date, shift,
                    res["is_defect"], res["class_id"], res["class_name"], res["confidence"], ver))
        n += 1
    db.commit()
    tip = "推理完成，本次新增 %d 条结果" % n + ("（内置模拟判定）" if not INFER_URL else "")
    flash(tip, "success")
    return redirect(request.referrer or url_for("analysis", tab="shift"))


@app.route("/api/analysis/<tab>")
@login_required
def api_analysis(tab):
    db = get_db()
    line = request.args.get("line", "all")
    base = "FROM inference_results WHERE is_defect=1"
    params = []
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
