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

# 内嵌四步向导界面（自包含，纯前端驱动，每步真能操作往下走）
EMBED_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;margin:0;background:#1e1e2e;color:#cdd6f4}
.top{background:#181825;padding:12px 20px;border-bottom:1px solid #313244;font-size:15px}
.top b{color:#89b4fa}.tag{font-size:12px;color:#6c7086;margin-left:8px}
.steps{display:flex;gap:8px;padding:16px 20px;background:#181825}
.step{flex:1;padding:10px;text-align:center;background:#313244;border-radius:8px;font-size:13px;color:#a6adc8;position:relative}
.step.on{background:#89b4fa;color:#1e1e2e;font-weight:600}
.step.done{background:#40a02b;color:#fff}
.step.done::after{content:" ✓"}
.wrap{padding:24px;max-width:720px;margin:0 auto}
.panel{background:#313244;border-radius:10px;padding:24px;min-height:220px}
h3{margin:0 0 6px}.desc{color:#a6adc8;font-size:13px;margin-bottom:18px}
button{background:#89b4fa;color:#1e1e2e;border:0;padding:9px 20px;border-radius:6px;font-weight:600;cursor:pointer;font-size:14px}
button:disabled{background:#45475a;color:#6c7086;cursor:not-allowed}
button.ghost{background:transparent;color:#89b4fa;border:1px solid #45475a}
.bar{height:10px;background:#45475a;border-radius:5px;overflow:hidden;margin:14px 0}
.bar>i{display:block;height:100%;background:#89b4fa;width:0;transition:width .3s}
.row{display:flex;align-items:center;gap:12px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
.thumb{background:#45475a;border-radius:6px;height:64px;display:flex;align-items:center;justify-content:center;font-size:11px;color:#a6adc8;position:relative;overflow:hidden}
.thumb.boxed{outline:2px solid #f9e2af;outline-offset:-2px}
.thumb .lb{position:absolute;left:2px;top:2px;background:#f9e2af;color:#1e1e2e;font-size:9px;padding:0 3px;border-radius:2px}
.metric{display:inline-block;background:#1e1e2e;border-radius:6px;padding:8px 14px;margin:4px 8px 4px 0;font-size:13px}
.metric b{color:#a6e3a1;font-size:16px}
select{background:#1e1e2e;color:#cdd6f4;border:1px solid #45475a;border-radius:6px;padding:7px 10px;font-size:13px}
.ok{color:#a6e3a1}.hint{font-size:12px;color:#6c7086;margin-top:14px}
code{background:#1e1e2e;padding:2px 7px;border-radius:4px;color:#89b4fa;font-size:12px}
</style></head><body>
<div class="top"><b>CubeStudio</b> 标注 · 训练 · 部署 <span class="tag">项目 __TITLE__ (MOCK)</span></div>
<div class="steps">
  <div class="step" id="s1">① 上传图片</div>
  <div class="step" id="s2">② 标注</div>
  <div class="step" id="s3">③ 训练模型</div>
  <div class="step" id="s4">④ 部署推理</div>
</div>
<div class="wrap"><div class="panel" id="panel"></div></div>
<script>
var PID = "__PID__";
var st = {step:1, total:30, uploaded:0, annotated:0, model:null, endpoint:null, preAnno:false};

function setSteps(){
  for(var i=1;i<=4;i++){
    var e=document.getElementById('s'+i); e.className='step';
    if(i<st.step)e.className='step done'; else if(i===st.step)e.className='step on';
  }
}
function go(n){ st.step=n; setSteps(); render(); }

function render(){
  setSteps();
  var p=document.getElementById('panel');
  if(st.step===1) p.innerHTML = viewUpload();
  else if(st.step===2) p.innerHTML = viewAnno();
  else if(st.step===3) p.innerHTML = viewTrain();
  else p.innerHTML = viewDeploy();
}

/* ① 上传 */
function viewUpload(){
  return '<h3>① 从数据源上传图片</h3>'
   +'<div class="desc">从 MinIO 数据源选取该「牌号×相机面」的图片导入标注项目。</div>'
   +'<div class="row"><span>导入数量</span><select id="cnt"><option>30</option><option>60</option><option>100</option></select>'
   +'<button onclick="doUpload()" id="upBtn">开始导入</button></div>'
   +'<div class="bar"><i id="upBar"></i></div>'
   +'<div id="upMsg" class="hint">尚未导入。</div>';
}
function doUpload(){
  st.total=parseInt(document.getElementById('cnt').value);
  document.getElementById('upBtn').disabled=true;
  var done=0, bar=document.getElementById('upBar'), msg=document.getElementById('upMsg');
  var t=setInterval(function(){
    done+=Math.ceil(st.total/12);
    if(done>=st.total){done=st.total;clearInterval(t);
      st.uploaded=st.total; msg.innerHTML='<span class="ok">✓ 已导入 '+st.total+' 张，可进入标注</span>';
      setTimeout(function(){go(2);},700);}
    bar.style.width=(100*done/st.total)+'%';
    msg.textContent='导入中… '+done+'/'+st.total;
  },120);
}

/* ② 标注（可选预标注） */
function viewAnno(){
  var thumbs='';
  for(var i=0;i<Math.min(st.total,12);i++){
    var boxed = i<st.annoShown ? ' boxed':'';
    var lb = i<st.annoShown ? '<span class="lb">'+DEF[i%DEF.length]+'</span>':'';
    thumbs+='<div class="thumb'+boxed+'">'+lb+'图'+(i+1)+'</div>';
  }
  st.annoShown = st.annoShown||0;
  return '<h3>② 标注</h3>'
   +'<div class="desc">人工画框标注缺陷；也可先用预标注模型自动标，再人工修正。</div>'
   +'<div class="row"><label><input type="checkbox" id="pre" '+(st.preAnno?'checked':'')+' onchange="st.preAnno=this.checked"> 使用预标注模型</label>'
   +'<button class="ghost" onclick="doPre()">预标注全部</button>'
   +'<button onclick="doAnno()">标注下一张</button></div>'
   +'<div class="grid">'+thumbs+'</div>'
   +'<div class="row"><div class="bar" style="flex:1"><i id="anBar" style="width:'+(100*st.annotated/st.total)+'%"></i></div>'
   +'<span id="anTxt">'+st.annotated+' / '+st.total+'</span></div>'
   +'<button onclick="go(3)" '+(st.annotated<3?'disabled':'')+'>标注完成，去训练 →</button>'
   +'<div class="hint">进度会通过 /api/projects/{id}/stats 回给平台（平台每10秒轮询）。</div>';
}
var DEF=["侧面翘边","商标歪斜","商标错牙","小盒触皱","顶部内衬破损"];
function doAnno(){
  if(st.annotated<st.total){st.annotated++; st.annoShown=Math.min(st.total,st.annotated);}
  syncStats(); render();
}
function doPre(){
  st.preAnno=true; st.annotated=st.total; st.annoShown=Math.min(st.total,12);
  syncStats(); render();
}
function syncStats(){
  fetch('/embed/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project:PID,action:'annotate',annotated:st.annotated,total:st.total})});
}

/* ③ 训练 */
function viewTrain(){
  return '<h3>③ 训练模型</h3>'
   +'<div class="desc">用已标注的 '+st.annotated+' 张样本训练缺陷检测模型。</div>'
   +'<div class="row"><span>基础网络</span><select><option>YOLOv8</option><option>ResNet50</option><option>Faster R-CNN</option></select>'
   +'<span>迭代</span><select><option>100</option><option>200</option></select>'
   +'<button onclick="doTrain()" id="trBtn">开始训练</button></div>'
   +'<div class="bar"><i id="trBar"></i></div>'
   +'<div id="trMsg" class="hint">尚未训练。</div>'
   +'<div id="trDone"></div>';
}
function doTrain(){
  document.getElementById('trBtn').disabled=true;
  var e=0,bar=document.getElementById('trBar'),msg=document.getElementById('trMsg');
  var t=setInterval(function(){
    e+=8; if(e>=100){e=100;clearInterval(t);
      fetch('/api/projects/'+PID+'/train',{method:'POST'})
        .then(function(r){return r.json();}).then(function(d){
          st.model=d.result;
          var mp=(st.model.metrics&&st.model.metrics.map)||'-', pr=(st.model.metrics&&st.model.metrics.precision)||'-';
          document.getElementById('trDone').innerHTML=
            '<div style="margin-top:12px"><span class="ok">✓ 训练完成，产出 '+st.model.model_version+'</span>'
            +'<div style="margin:10px 0"><span class="metric">mAP <b>'+mp+'</b></span>'
            +'<span class="metric">precision <b>'+pr+'</b></span></div>'
            +'<button onclick="go(4)">部署上线 →</button></div>';
        });
      msg.textContent='训练完成';
    } else msg.textContent='训练中… epoch '+Math.ceil(e/8)+'  loss '+(2/(e/10+1)).toFixed(3);
    bar.style.width=e+'%';
  },200);
}

/* ④ 部署 */
function viewDeploy(){
  if(st.endpoint) return deployedView();
  return '<h3>④ 部署推理服务</h3>'
   +'<div class="desc">把训好的 '+(st.model?st.model.model_version:'模型')+' 部署为在线推理服务。</div>'
   +'<div class="row"><span>推理框架</span><select><option>TorchServe</option><option>Triton</option></select>'
   +'<button onclick="doDeploy()" id="dpBtn">一键部署</button></div>'
   +'<div class="bar"><i id="dpBar"></i></div><div id="dpMsg" class="hint">尚未部署。</div>';
}
function doDeploy(){
  document.getElementById('dpBtn').disabled=true;
  var e=0,bar=document.getElementById('dpBar'),msg=document.getElementById('dpMsg');
  var t=setInterval(function(){
    e+=10; if(e>=100){e=100;clearInterval(t);
      fetch('/api/models/'+st.model.model_id+'/deploy',{method:'POST'})
        .then(function(r){return r.json();}).then(function(d){
          st.endpoint=d.result.inference_host_url; render();
        });
    } else msg.textContent='拉起推理服务 pod… '+e+'%';
    bar.style.width=e+'%';
  },180);
}
function deployedView(){
  return '<h3>④ 部署推理服务</h3>'
   +'<div style="margin:20px 0"><span class="ok" style="font-size:16px">✓ 推理服务已上线</span></div>'
   +'<div class="row"><span>模型</span><b>'+st.model.model_version+'</b></div>'
   +'<div class="row"><span>推理地址</span><code>'+st.endpoint+'</code></div>'
   +'<div class="hint">回到平台「③模型管理」即可把该模型绑定到建模单元，供④推理统计调用。</div>'
   +'<div style="margin-top:16px"><button class="ghost" onclick="go(1)">重新走一遍</button></div>';
}

render();
</script></body></html>"""


def _pid():
    _seq["p"] += 1
    return "cs-proj-%d" % _seq["p"]


def get_or_make(pid):
    """按需取/补建项目。mock 是内存态，重启后旧 PID 会丢；平台建模单元里存着这些
    PID，重启后再访问就 404。补建让重启后旧 PID 照样能用（真系统项目是持久化的）。"""
    p = PROJECTS.get(pid)
    if not p:
        p = PROJECTS[pid] = {"brand": "", "face_code": "", "total": 30,
                             "annotated": 0, "created": time.time()}
    return p


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
            return self._send(EMBED_HTML.replace("__PID__", pid).replace("__TITLE__", title),
                              ctype="text/html")

        # GET /api/projects/{id}/stats
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "stats":
            p = get_or_make(parts[2])
            # 若内嵌向导手动操作过（manual），以其为准；否则按时间自增模拟
            if not p.get("manual"):
                elapsed = int(time.time() - p["created"])
                p["annotated"] = min(p["total"], elapsed // 5)
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

        # POST /embed/action  内嵌向导的操作回写（标注等），让平台轮询能拿到真实进度
        if u.path == "/embed/action":
            p = get_or_make(b.get("project", ""))
            if b.get("action") == "annotate":
                p["manual"] = True
                p["total"] = int(b.get("total", p["total"]))
                p["annotated"] = min(p["total"], int(b.get("annotated", 0)))
            return self._ok({"total": p.get("total", 0), "annotated": p.get("annotated", 0)})

        # POST /api/projects  建项目
        if u.path == "/api/projects":
            pid = _pid()
            PROJECTS[pid] = {"brand": b.get("brand", ""), "face_code": b.get("face_code", ""),
                             "total": int(b.get("total", 30) or 30), "annotated": 0,
                             "created": time.time()}
            return self._ok({"project_id": pid, "embed_url": "/embed?project=%s" % pid})

        # POST /api/projects/{id}/train  触发训练
        if len(parts) == 4 and parts[1] == "projects" and parts[3] == "train":
            p = get_or_make(parts[2])
            mid = _mid()  # 立刻产出"训练完成"模型（真实系统异步）
            m = {"project_id": parts[2],
                 "model_name": "%s-%s" % (p["brand"], p["face_code"]),
                 "model_version": "v%d" % (len(MODELS) + 1),
                 "model_status": "test", "inference_host_url": "",
                 "metrics": {"map": round(random.uniform(0.75, 0.95), 3),
                             "precision": round(random.uniform(0.8, 0.97), 3)}}
            MODELS[mid] = m
            # 返回训练结果 + 模型详情（向导要 model_version/metrics 显示，deploy 要 model_id）
            return self._ok({"run_id": "run-%s" % mid, "model_id": mid,
                             "model_version": m["model_version"], "metrics": m["metrics"]})

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
