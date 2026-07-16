# -*- coding: utf-8 -*-
"""对方 CubeStudio（融合 Label Studio）的 MOCK 服务。

目的：对方系统还在开发，我们先按约定的接口契约做一个假实现，
      把平台侧的菜单②③④ 全链路打通。等对方部署好，把平台的
      CS_URL 指向真地址即可，平台代码不用改。

接口契约（贴近真实 cube-studio 风格：响应统一 {status, result, message}，
status=0 成功；推理服务字段用 model_name/model_version/inference_host_url/model_status）：
  POST /api/projects              建标注/训练项目(牌号×相机面) → result:{project_id, embed_url}
  GET  /api/projects/{id}/stats   标注进度(平台定时轮询)        → result:{total, annotated}
  GET  /api/projects/{id}/models  该项目训出的模型列表          → result:[{model_id, model_name, model_version, model_status, metrics, inference_host_url}]
  POST /api/projects/{id}/train   触发训练(占位)                → result:{run_id, model_id}
  POST /api/models/{mid}/deploy   部署为推理服务                → result:{inference_host_url, model_status}
  POST /infer                     推理标准接口 {image_url|src_key} → result:{class_name, confidence, is_defect}
  GET  /embed?project={id}        被平台 iframe 内嵌的标注界面(HTML)

真实 cube-studio 部署出的是 TorchServe/Triton（入参二进制图、出参 logits），
不符合本契约。对接时需对方在标准接口后包一层，或我方加适配器。/infer 这里
按我们的标准契约模拟，接真系统时以此为准跟对方对齐。
"""
import json
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 9700

# 简单内存态：项目、模型
PROJECTS = {}   # id -> {brand, face_code, total, annotated, created}
MODELS = {}     # model_id -> {project_id, version, status, metrics, endpoint}
_seq = {"p": 0, "m": 0}

DEFECTS = ["侧面翘边", "商标歪斜", "商标错牙", "小盒触皱", "顶部内衬破损", "封签破损", "正常"]


def _pid():
    _seq["p"] += 1
    return "cs-proj-%d" % _seq["p"]


def _mid():
    _seq["m"] += 1
    return "cs-model-%d" % _seq["m"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, result, code=200):
        """cube-studio 风格：{status:0, result:..., message:'success'}"""
        return self._send({"status": 0, "result": result, "message": "success"}, code)

    def _err(self, msg, code=404):
        return self._send({"status": 1, "result": None, "message": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]

        if u.path == "/health":
            return self._ok({"ok": True, "service": "cubestudio-mock",
                             "projects": len(PROJECTS), "models": len(MODELS)})

        if u.path == "/embed":
            pid = (q.get("project") or [""])[0]
            p = PROJECTS.get(pid, {})
            title = "%s · %s" % (p.get("brand", "?"), p.get("face_code", "?"))
            # 模拟对方的标注界面
            html = """<!doctype html><html><head><meta charset="utf-8">
<style>body{font-family:sans-serif;margin:0;background:#1e1e2e;color:#cdd6f4}
.top{background:#181825;padding:12px 20px;border-bottom:1px solid #313244}
.wrap{padding:24px}.card{background:#313244;border-radius:8px;padding:20px;margin-bottom:14px}
.step{display:inline-block;padding:6px 14px;background:#45475a;border-radius:20px;margin-right:8px;font-size:13px}
.on{background:#89b4fa;color:#1e1e2e;font-weight:600}
button{background:#89b4fa;color:#1e1e2e;border:0;padding:8px 18px;border-radius:6px;font-weight:600;cursor:pointer}</style>
</head><body><div class="top"><b>CubeStudio</b> · 标注/训练 <span style="color:#89b4fa">%s</span> (MOCK)</div>
<div class="wrap">
<div class="card"><span class="step on">①上传图片</span><span class="step">②标注</span><span class="step">③训练</span><span class="step">④部署推理</span></div>
<div class="card"><h3>标注工作台</h3><p>这是对方 CubeStudio 的内嵌界面占位。真实系统会在这里完成：从数据源选图上传 → 人工/预标注 → 一键训练 → 一键部署推理服务。</p>
<p>当前项目：<b>%s</b></p><button onclick="alert('对方系统实现，标注结果通过 /api/projects/{id}/stats 回给平台')">模拟标注一张</button></div>
</div></body></html>""" % (title, title)
            return self._send(html, ctype="text/html")

        # GET /api/projects/{id}/stats
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "stats":
            p = PROJECTS.get(parts[2])
            if not p:
                return self._err("project not found")
            # 模拟标注进度随时间增长
            elapsed = int(time.time() - p["created"])
            p["annotated"] = min(p["total"], elapsed // 5)  # 每 5 秒标一张
            return self._ok({"total": p["total"], "annotated": p["annotated"]})

        # GET /api/projects/{id}/models
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "models":
            ms = [dict(m, model_id=mid) for mid, m in MODELS.items() if m["project_id"] == parts[2]]
            return self._ok(ms)

        return self._err("unknown endpoint")

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        b = self._body()

        # POST /api/projects  建项目
        if u.path == "/api/projects":
            pid = _pid()
            PROJECTS[pid] = {"brand": b.get("brand", ""), "face_code": b.get("face_code", ""),
                             "total": int(b.get("total", 30) or 30), "annotated": 0,
                             "created": time.time()}
            return self._ok({"project_id": pid, "embed_url": "/embed?project=%s" % pid})

        # POST /api/projects/{id}/train  触发训练
        if len(parts) == 4 and parts[1] == "projects" and parts[3] == "train":
            p = PROJECTS.get(parts[2])
            if not p:
                return self._err("project not found")
            mid = _mid()  # 立刻产出"训练完成"模型（真实系统异步）
            MODELS[mid] = {"project_id": parts[2],
                           "model_name": "%s-%s" % (p["brand"], p["face_code"]),
                           "model_version": "v%d" % (len(MODELS) + 1),
                           "model_status": "test", "inference_host_url": "",
                           "metrics": {"map": round(random.uniform(0.75, 0.95), 3),
                                       "precision": round(random.uniform(0.8, 0.97), 3)}}
            return self._ok({"run_id": "run-%s" % mid, "model_id": mid})

        # POST /api/models/{mid}/deploy  部署推理
        if len(parts) == 4 and parts[1] == "models" and parts[3] == "deploy":
            m = MODELS.get(parts[2])
            if not m:
                return self._err("model not found")
            m["model_status"] = "online"
            m["inference_host_url"] = "http://127.0.0.1:%d/infer?model=%s" % (PORT, parts[2])
            return self._ok({"inference_host_url": m["inference_host_url"], "model_status": "online"})

        # POST /infer  推理标准接口
        if u.path == "/infer":
            key = b.get("src_key") or b.get("image_url") or ""
            h = abs(hash(key))
            name = DEFECTS[h % len(DEFECTS)]
            return self._ok({"is_defect": 0 if name == "正常" else 1,
                             "class_name": name,
                             "confidence": round(0.7 + (h % 30) / 100.0, 3),
                             "model_version": "cs-mock"})

        return self._err("unknown endpoint")


if __name__ == "__main__":
    print("CubeStudio MOCK 服务启动于 :%d" % PORT)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
