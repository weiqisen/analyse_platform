# -*- coding: utf-8 -*-
"""缺陷图片分析平台 — Flask 后端"""
import os
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
            id INT PRIMARY KEY COMMENT '主键(固定为1，单条配置)',
            server_addr VARCHAR(128) COMMENT '存储服务地址',
            in_bucket VARCHAR(64) COMMENT '输入桶名',
            username VARCHAR(64) COMMENT '存储服务用户名',
            password VARCHAR(128) COMMENT '存储服务密码',
            out_bucket VARCHAR(64) COMMENT '输出桶名'
        ) DEFAULT CHARSET=utf8mb4 COMMENT='存储服务配置表'""",
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
            name VARCHAR(64) NOT NULL COMMENT '缺陷分类名称'
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
                           ("3302101", "玉溪（软）"), ("3302102", "玉溪（创客）"),
                           ("3302103", "玉溪（缤果爆）"), ("3303001", "云烟（紫）")]:
            db.execute("INSERT INTO brands(code,spec) VALUES(?,?)", (code, spec))
        # 对象存储配置（模式一）：指向 10.10.96.65 上的 MinIO
        db.execute("INSERT INTO storage_config(id,server_addr,in_bucket,username,password,out_bucket) "
                   "VALUES(1,?,?,?,?,?)",
                   ("10.10.96.65:9000", "ng-yzwg", "minioadmin", "minioadmin123", "ng-yzwg-out"))
        # 终端配置（模式二）：A02->10.10.96.66，A03->10.10.96.67 工控机 NG 目录
        db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (line_ids["A02"], "10.10.96.66:29", "/data/ng/NG_IMG", "YYYYMMDD", "1,8",
                    "早、中、晚", 3, "1#,2#,3#", "中华（软）"))
        db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (line_ids["A03"], "10.10.96.67:29", "/data/ng/NG_IMG", "YYYYMMDD", "1,8",
                    "早、中、晚", 2, "1#,2#", "云烟（紫）"))
        for c in ["污渍", "翘边", "封签歪斜", "印刷错误", "刺破", "缺支"]:
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
    return render_template("index.html", active="home")


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
CONFIG_TABS = [("items", "检测项目"), ("lines", "机组/产线"), ("brands", "牌号/品规")]


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
    }[tab]
    return render_template("config.html", tab=tab, tabs=CONFIG_TABS,
                           rows=data, active="config")


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
    db.commit()
    flash("保存成功", "success")
    return redirect(url_for("config", tab=tab))


@app.route("/config/<tab>/delete/<int:rid>", methods=["POST"])
@login_required
def config_delete(tab, rid):
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands"}.get(tab)
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
    table = {"items": "detect_items", "lines": "prod_lines", "brands": "brands"}.get(tab)
    if not table:
        abort(404)
    db = get_db()
    db.execute("UPDATE %s SET status=1-status WHERE id=?" % table, (rid,))
    db.commit()
    return redirect(url_for("config", tab=tab))


# ---------------------------------------------------------------- 图像采集
COLLECT_TABS = [("mode", "采集模式"), ("storage", "服务配置"), ("terminal", "终端配置"),
                ("monitor", "采集监控"), ("history", "历史采集"), ("images", "图片查询")]


def get_s3(cfg=None):
    """按 storage_config 构建 MinIO/S3 客户端。"""
    import boto3
    from botocore.client import Config
    if cfg is None:
        cfg = get_db().execute("SELECT * FROM storage_config WHERE id=1").fetchone()
    s3 = boto3.client("s3", endpoint_url="http://" + (cfg["server_addr"] or ""),
                      aws_access_key_id=cfg["username"] or "",
                      aws_secret_access_key=cfg["password"] or "",
                      region_name="us-east-1",
                      config=Config(signature_version="s3v4", connect_timeout=4,
                                    read_timeout=8, retries={"max_attempts": 1}))
    return s3, cfg


def line_terminal_cfg(line_row):
    """产线为工控机采集模式则返回其 terminal_config，否则 None（对象存储模式）。"""
    if not line_row:
        return None
    return get_db().execute("SELECT * FROM terminal_config WHERE line_id=?",
                            (line_row["id"],)).fetchone()


@app.route("/collect/<tab>", methods=["GET", "POST"])
@login_required
def collect(tab):
    if tab not in dict(COLLECT_TABS):
        abort(404)
    db = get_db()
    ctx = dict(tab=tab, tabs=COLLECT_TABS, active="collect")
    lines = db.execute("SELECT * FROM prod_lines WHERE status=1 ORDER BY id").fetchall()
    ctx["lines"] = lines
    cur_line = request.args.get("line") or (lines[0]["name"] if lines else "A01")
    ctx["cur_line"] = cur_line

    if tab == "storage":
        if request.method == "POST":
            f = request.form
            db.execute("UPDATE storage_config SET server_addr=?,in_bucket=?,username=?,password=?,out_bucket=? WHERE id=1",
                       (f["server_addr"], f["in_bucket"], f["username"], f["password"], f["out_bucket"]))
            db.commit()
            flash("服务配置已保存", "success")
            return redirect(url_for("collect", tab="storage"))
        ctx["cfg"] = db.execute("SELECT * FROM storage_config WHERE id=1").fetchone()
        try:
            s3, cfg = get_s3(ctx["cfg"])
            s3.head_bucket(Bucket=cfg["in_bucket"])
            ctx["conn"] = ("ok", "连接正常，输入桶「%s」可访问" % cfg["in_bucket"])
        except Exception as e:
            ctx["conn"] = ("fail", str(e)[:140])
    elif tab == "terminal":
        if request.method == "POST":
            f = request.form
            db.execute("DELETE FROM terminal_config WHERE line_id=?", (f["line_id"],))
            db.execute("INSERT INTO terminal_config(line_id,sys_addr,ng_dir,date_dir,str_pos,shift_dir,cam_count,cam_dirs,brand_dirs) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (f["line_id"], f["sys_addr"], f["ng_dir"], f["date_dir"], f["str_pos"],
                        f["shift_dir"], f["cam_count"], f["cam_dirs"], f["brand_dirs"]))
            db.commit()
            flash("终端配置已保存", "success")
            return redirect(url_for("collect", tab="terminal", line=cur_line))
        line_row = next((l for l in lines if l["name"] == cur_line), None)
        ctx["line_row"] = line_row
        ctx["tcfg"] = db.execute("SELECT * FROM terminal_config WHERE line_id=?",
                                 (line_row["id"] if line_row else -1,)).fetchone()
    elif tab == "monitor":
        line_row = next((l for l in lines if l["name"] == cur_line), None)
        tcfg = line_terminal_cfg(line_row)
        ctx["src"] = "terminal" if tcfg else "minio"
        ctx["tcfg"] = tcfg
        ctx["cams"] = []
        if not tcfg:
            try:
                s3, cfg = get_s3()
                objs = s3.list_objects_v2(Bucket=cfg["in_bucket"], MaxKeys=2000).get("Contents", [])
                from collections import Counter
                cnt = Counter()
                for o in objs:
                    parts = o["Key"].split("/")
                    cnt[parts[3] if len(parts) >= 4 else "未分类"] += 1
                ctx["cams"] = [{"name": k, "count": v} for k, v in sorted(cnt.items())]
                ctx["err"] = None
            except Exception as e:
                ctx["err"] = str(e)[:140]
    elif tab == "history":
        ctx["records"] = db.execute(
            "SELECT * FROM collect_history WHERE line=? ORDER BY date DESC, shift", (cur_line,)).fetchall() \
            or db.execute("SELECT * FROM collect_history ORDER BY date DESC LIMIT 20").fetchall()
    elif tab == "images":
        line_row = next((l for l in lines if l["name"] == cur_line), None)
        tcfg = line_terminal_cfg(line_row)
        ctx["src"] = "terminal" if tcfg else "minio"
        ctx["tcfg"] = tcfg
        ctx["pics"] = []; ctx["total"] = 0
        if not tcfg:
            try:
                s3, cfg = get_s3()
                objs = s3.list_objects_v2(Bucket=cfg["in_bucket"], MaxKeys=500).get("Contents", [])
                pics = []
                for o in objs:
                    url = s3.generate_presigned_url(
                        "get_object", Params={"Bucket": cfg["in_bucket"], "Key": o["Key"]}, ExpiresIn=7200)
                    pics.append({"key": o["Key"], "name": o["Key"].split("/")[-1], "url": url})
                ctx["pics"] = pics; ctx["total"] = len(pics); ctx["err"] = None
            except Exception as e:
                ctx["err"] = str(e)[:140]
    return render_template("collect.html", **ctx)


# ---------------------------------------------------------------- 缺陷标注
@app.route("/label")
@login_required
def label():
    db = get_db()
    tasks = db.execute("SELECT * FROM label_tasks ORDER BY id").fetchall()
    classes = db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()
    return render_template("label.html", tasks=tasks, classes=classes, active="label")


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


@app.route("/label/anno/<int:tid>")
@login_required
def label_anno(tid):
    db = get_db()
    task = db.execute("SELECT * FROM label_tasks WHERE id=?", (tid,)).fetchone()
    classes = db.execute("SELECT * FROM label_classes ORDER BY id").fetchall()
    if not task:
        abort(404)
    return render_template("label_anno.html", task=task, classes=classes, active="label")


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
    cur_line = request.args.get("line") or (lines[0]["name"] if lines else "A01")
    return render_template("analysis.html", tab=tab, tabs=ANALYSIS_TABS,
                           lines=lines, cur_line=cur_line, active="analysis",
                           today=datetime.now().strftime("%Y/%m/%d"))


@app.route("/api/analysis/<tab>")
@login_required
def api_analysis(tab):
    line = request.args.get("line", "A01")
    rnd = random.Random(hash(line + tab) & 0xffff)
    classes = ["污渍", "翘边", "封签歪斜", "印刷错误", "刺破", "缺支"]
    if tab == "shift":
        return jsonify({
            "classes": classes,
            "counts": [rnd.randint(5, 120) for _ in classes],
        })
    if tab == "history":
        days = [(datetime.now() - timedelta(days=d)).strftime("%m/%d") for d in range(6, -1, -1)]
        return jsonify({
            "days": days,
            "series": [{"name": c, "data": [rnd.randint(2, 60) for _ in days]} for c in classes[:4]],
        })
    days = [(datetime.now() - timedelta(days=d)).strftime("%m/%d") for d in range(13, -1, -1)]
    return jsonify({
        "days": days,
        "series": [{"name": c, "data": [rnd.randint(10, 90) for _ in days]} for c in classes[:4]],
    })


# ----------------------------------------------------------------
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "9573")), debug=False)
