"""
本地 OCR 封装（通道 B）。

用途：证明「这个数字真的出现在图上」，而不是大模型编的。
所以它不追求排版理解，只要求：原文、坐标、置信度，都拿到。

关于分辨率
----------
整窗扫描可以降采样（默认 1400px 宽）提速，坐标会自动映射回原图；
但【订单截图必须走原分辨率】，降采样会让小字号金额糊掉。
ocr() 的 max_width 参数就是干这个的，订单图传 None。

关于依赖
--------
rapidocr 装不上时不会在 import 阶段炸，只在真正调用时报错并给出安装命令。
"""

import os
import sys

import numpy as np

import config

_ENGINE = None
_ENGINE_ERR = None


def _load_engine():
    global _ENGINE, _ENGINE_ERR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERR is not None:
        raise RuntimeError(_ENGINE_ERR)

    try:
        from rapidocr import RapidOCR  # 新版包名
        _ENGINE = RapidOCR()
        return _ENGINE
    except ImportError:
        pass

    try:
        from rapidocr_onnxruntime import RapidOCR  # 旧版包名，兼容
        _ENGINE = RapidOCR()
        return _ENGINE
    except ImportError:
        _ENGINE_ERR = (
            "未安装 OCR 引擎。任选其一安装：\n"
            "  python -m pip install rapidocr onnxruntime opencv-python-headless\n"
            "  python -m pip install rapidocr-onnxruntime"
        )
        raise RuntimeError(_ENGINE_ERR)


def _normalize(raw):
    """
    把不同版本 rapidocr 的返回统一成 [(box, text, score), ...]。

    兼容三种形态：
      · 旧版: ( [ [box, text, score], ... ], elapse )
      · 3.x : 对象，含 .boxes / .txts / .scores
      · 字典: {'boxes':..., 'txts':..., 'scores':...}
    """
    if raw is None:
        return []

    # 旧版：元组，第一项是列表
    if isinstance(raw, tuple):
        payload = raw[0] if raw else None
        if isinstance(payload, list):
            out = []
            for it in payload:
                if isinstance(it, (list, tuple)) and len(it) >= 3:
                    out.append((it[0], it[1], float(it[2])))
            return out
        return _normalize(payload)

    # 3.x 对象
    boxes = getattr(raw, "boxes", None)
    txts = getattr(raw, "txts", None)
    scores = getattr(raw, "scores", None)
    if boxes is not None and txts is not None:
        scores = scores if scores is not None else [1.0] * len(txts)
        return [(b, t, float(s)) for b, t, s in zip(boxes, txts, scores)]

    # 字典
    if isinstance(raw, dict):
        boxes = raw.get("boxes")
        txts = raw.get("txts")
        scores = raw.get("scores") or [1.0] * (len(txts) if txts else 0)
        if boxes is not None and txts is not None:
            return [(b, t, float(s)) for b, t, s in zip(boxes, txts, scores)]

    # 已是标准列表
    if isinstance(raw, list):
        out = []
        for it in raw:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                out.append((it[0], it[1], float(it[2])))
        return out

    return []


def _to_line(box, text, score, scale):
    """
    box: 4 点 [[x,y],...]（可能是 numpy）；scale: 降采样比例，用于还原到原图坐标。
    返回带几何信息的 dict。
    """
    pts = np.asarray(box, dtype=float).reshape(-1, 2)
    if scale and scale != 1.0:
        pts = pts / scale

    xs, ys = pts[:, 0], pts[:, 1]
    x1, x2 = float(xs.min()), float(xs.max())
    y1, y2 = float(ys.min()), float(ys.max())

    return {
        "text": str(text).strip(),
        "score": float(score),
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "cx": (x1 + x2) / 2.0,
        "cy": (y1 + y2) / 2.0,
        "w": x2 - x1,
        "h": y2 - y1,
    }


def ocr(img, max_width=None, min_score=None, region=None):
    """
    对 PIL.Image 做 OCR。

    max_width  给定时先等比缩小到该宽度再识别，坐标还原到原图坐标系。
               整窗扫描建议 1400；订单截图必须传 None（用原分辨率）。
    min_score  低于该置信度的行直接丢弃。
    region     只识别其中一块区域 (x1, y1, x2, y2)，坐标仍是【原图坐标系】。
               侧边栏这种小区域用了能快好几倍——整窗 OCR 会把聊天区
               几百行文字也读一遍，而那些字对扫描会话列表毫无用处。

    返回按 y 排序的行列表，每行含 text/score/坐标。
    """
    conf = config.load()
    if min_score is None:
        min_score = conf["ocr"].get("min_score", 0.5)

    engine = _load_engine()
    scale = 1.0
    work = img.convert("RGB")

    off_x = off_y = 0
    if region:
        rx1, ry1, rx2, ry2 = [int(v) for v in region]
        rx1, ry1 = max(0, rx1), max(0, ry1)
        rx2, ry2 = min(work.width, rx2), min(work.height, ry2)
        if rx2 - rx1 < 8 or ry2 - ry1 < 8:
            return []
        work = work.crop((rx1, ry1, rx2, ry2))
        off_x, off_y = rx1, ry1

    if max_width and work.width > max_width:
        scale = max_width / float(work.width)
        new_h = max(1, int(round(work.height * scale)))
        work = work.resize((max_width, new_h))

    arr = np.asarray(work)
    raw = engine(arr)
    rows = _normalize(raw)

    lines = []
    for box, text, score in rows:
        if not str(text).strip():
            continue
        if score < min_score:
            continue
        line = _to_line(box, text, score, scale)
        if off_x or off_y:
            for k in ("x1", "x2", "cx"):
                line[k] += off_x
            for k in ("y1", "y2", "cy"):
                line[k] += off_y
        lines.append(line)

    lines.sort(key=lambda r: (r["cy"], r["cx"]))
    return lines


def text_of(lines, joiner=" "):
    return joiner.join(r["text"] for r in lines)


def find_text(lines, needle):
    """在 OCR 结果里找包含指定子串的行。"""
    return [r for r in lines if needle in r["text"]]


def ready():
    """OCR 是否可用（不抛异常）。"""
    try:
        _load_engine()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("OCR 引擎可用:", ready())
    if not ready():
        try:
            _load_engine()
        except RuntimeError as e:
            print(e)
        sys.exit(1)
