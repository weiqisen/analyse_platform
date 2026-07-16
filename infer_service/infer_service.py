# -*- coding: utf-8 -*-
"""缺陷分类推理服务 —— ResNet18(ImageNet) 特征 + k-NN 最近邻投票。

为什么是这个方案：
  defect-raw 桶内按「检测项目/日期/班次/缺陷类型/文件名」组织，目录第 4 级即缺陷类型，
  34 类共 166 张、每类约 5 张。这个量训不出深度模型，故用 ImageNet 预训练骨干提特征 +
  最近邻投票 —— 少样本场景的标准做法。真实前向计算，无随机数。

  桶内 35 个目录 = 34 类缺陷 + 「正常」(5 张)，故 is_defect 由预测类别决定：
  命中「正常」为 0，其余为 1。

算法同事接入：把 embed() 换成自己的模型前向，或直接重写 /infer 的实现，
HTTP 契约（见 infer() 的 docstring）保持不变即可，平台侧无需改动。
"""
import io
import os
import json
import logging

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from flask import Flask, request, jsonify
import boto3
from botocore.client import Config
from torchvision import transforms
from torchvision.models import resnet18

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("infer")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://10.10.96.65:9000")
MINIO_KEY = os.environ.get("MINIO_KEY", "minioadmin")
MINIO_SECRET = os.environ.get("MINIO_SECRET", "minioadmin123")
BUCKET = os.environ.get("MINIO_BUCKET", "defect-raw")
CACHE = os.environ.get("GALLERY_CACHE", "/data/gallery.npz")
TOPK = int(os.environ.get("TOPK", "5"))
NORMAL_CLASS = os.environ.get("NORMAL_CLASS", "正常")  # 该目录名不算缺陷
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")
# 随结果返回，让每条推理结果能追溯到具体是哪个模型算的
MODEL_VERSION = os.environ.get("MODEL_VERSION", "resnet18-knn-1.0")

app = Flask(__name__)
torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "4")))

# ---------------------------------------------------------------- 模型
_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])


def build_model():
    """ResNet18 去掉分类头，输出 512 维特征。权重离线预置在镜像内。"""
    m = resnet18(weights=None)
    sd = torch.load(os.environ.get("RESNET18_WEIGHTS", "/weights/resnet18-f37072fd.pth"),
                    map_location="cpu")
    m.load_state_dict(sd)
    m.fc = nn.Identity()
    m.eval()
    return m


MODEL = build_model()


def s3():
    return boto3.client("s3", endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_SECRET,
                        region_name="us-east-1",
                        config=Config(signature_version="s3v4", connect_timeout=8, read_timeout=30))


def read_image(key):
    o = s3().get_object(Bucket=BUCKET, Key=key)
    return Image.open(io.BytesIO(o["Body"].read())).convert("RGB")


@torch.no_grad()
def embed(img):
    """图 → L2 归一化的 512 维特征（归一化后点积即余弦相似度）。"""
    v = MODEL(_tf(img).unsqueeze(0)).squeeze(0).numpy()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def label_of(key):
    """key = 检测项目/日期/班次/缺陷类型/文件名 —— 倒数第二级是缺陷类型。"""
    parts = key.split("/")
    return parts[-2] if len(parts) >= 2 else ""


# ---------------------------------------------------------------- 底库
GALLERY = {"keys": [], "labels": [], "feats": None}


def list_keys():
    c, tok, out = s3(), None, []
    while True:
        kw = {"Bucket": BUCKET, "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = c.list_objects_v2(**kw)
        out += [o["Key"] for o in r.get("Contents", []) if o["Key"].lower().endswith(IMG_EXT)]
        if not r.get("IsTruncated"):
            return sorted(out)
        tok = r.get("NextContinuationToken")


def build_gallery():
    """给桶内每张图算特征建底库；按 key 集合缓存，桶没变则直接读缓存。"""
    keys = list_keys()
    sig = str(hash(tuple(keys)))
    if os.path.exists(CACHE):
        try:
            z = np.load(CACHE, allow_pickle=True)
            if str(z["sig"]) == sig:
                GALLERY.update(keys=list(z["keys"]), labels=list(z["labels"]), feats=z["feats"])
                log.info("底库读缓存：%d 张 / %d 类", len(GALLERY["keys"]), len(set(GALLERY["labels"])))
                return
        except Exception as e:
            log.warning("缓存读取失败，重建：%s", e)
    log.info("建底库中（%d 张，CPU 前向，约需 1-2 分钟）…", len(keys))
    feats, ok_keys, labels = [], [], []
    for i, k in enumerate(keys, 1):
        try:
            feats.append(embed(read_image(k)))
            ok_keys.append(k)
            labels.append(label_of(k))
        except Exception as e:
            log.warning("跳过 %s：%s", k, e)
        if i % 25 == 0:
            log.info("  %d/%d", i, len(keys))
    GALLERY.update(keys=ok_keys, labels=labels, feats=np.stack(feats) if feats else None)
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        np.savez(CACHE, keys=np.array(ok_keys), labels=np.array(labels),
                 feats=GALLERY["feats"], sig=sig)
    except Exception as e:
        log.warning("缓存写入失败：%s", e)
    log.info("底库就绪：%d 张 / %d 类", len(ok_keys), len(set(labels)))


def classify(key, feat):
    """k-NN 投票。排除 key 自身 —— 底库和待推理是同一批图，
    不排除的话最近邻永远是自己（相似度 1.0），准确率虚高到 100%，结果没有意义。"""
    if GALLERY["feats"] is None or not len(GALLERY["keys"]):
        return None, 0.0
    sims = GALLERY["feats"] @ feat
    mask = np.array([k != key for k in GALLERY["keys"]])
    if not mask.any():
        return None, 0.0
    sims = np.where(mask, sims, -1.0)
    idx = np.argsort(-sims)[:TOPK]
    votes = {}
    for i in idx:
        if sims[i] <= -1.0:
            continue
        votes[GALLERY["labels"][i]] = votes.get(GALLERY["labels"][i], 0.0) + float(sims[i])
    if not votes:
        return None, 0.0
    name = max(votes, key=votes.get)
    # 置信度 = 胜出类得票占比，反映近邻的一致程度
    return name, round(votes[name] / sum(votes.values()), 3)


# ---------------------------------------------------------------- HTTP
@app.route("/health")
def health():
    return jsonify({"ok": True, "gallery": len(GALLERY["keys"]),
                    "classes": len(set(GALLERY["labels"])), "topk": TOPK,
                    "model_version": MODEL_VERSION})


@app.route("/infer", methods=["POST"])
def infer():
    """平台契约：
       入 {"image_id":int, "src_key":str, "line":str, "brand":str}
       出 {"is_defect":1, "class_name":str, "confidence":float}
       class_id 由平台按 class_name 查字典映射，本服务不关心平台的库表 id。"""
    d = request.get_json(force=True) or {}
    key = d.get("src_key", "")
    if not key:
        return jsonify({"error": "src_key required"}), 400
    try:
        name, conf = classify(key, embed(read_image(key)))
    except Exception as e:
        log.exception("推理失败 %s", key)
        return jsonify({"error": str(e)}), 500
    if not name:
        return jsonify({"error": "gallery empty"}), 503
    return jsonify({"is_defect": 0 if name == NORMAL_CLASS else 1,
                    "class_name": name, "confidence": conf,
                    "model_version": MODEL_VERSION})


build_gallery()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "9600")))
