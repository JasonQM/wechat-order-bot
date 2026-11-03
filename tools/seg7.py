"""
七段数码管本地解码器（实验版）。

为什么不用通用 OCR：
  加油机的数字是七段 LED/LCD，笔画结构固定。通用 OCR 看的是"字形像不像某个字"，
  遇到反光、倾斜、笔画断裂就会乱猜——实测把 81.40 读成 1.40、8.60 直接漏掉。
  而七段管只需要判断「七个位置各自亮不亮」，这是个 7 位二进制的问题，
  容错空间比字形匹配大一个量级，而且错了一定能被段码表识别出来（不存在的组合）。

流程：
  1. 找 LCD 面板（轮廓检测，取近似四边形的大区域）
  2. 光照归一化 + 二值化（面板里有阴影和反光，全局阈值一定会失败）
  3. 行投影切数字行
  4. 行内列投影切单个数字
  5. 每个数字归一化后按七段位置采样 → 段码 → 数字
  全部数字用「油量 × 单价 ≈ 金额」做代数校验。
"""

import cv2
import numpy as np


# 七段编码：a=顶横 b=右上竖 c=右下竖 d=底横 e=左下竖 f=左上竖 g=中横
SEGMENTS = {
    "0": set("abcdef"),
    "1": set("bc"),
    "2": set("abged"),
    "3": set("abgcd"),
    "4": set("fgbc"),
    "5": set("afgcd"),
    "6": set("afgecd"),
    "7": set("abc"),
    "8": set("abcdefg"),
    "9": set("abcdfg"),
}


def find_panels(gray, min_area_ratio=0.004):
    """找 LCD 面板：近似四边形的亮边框区域。返回 [(x, y, w, h)]，按面积降序。"""
    H, W = gray.shape
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = H * W * min_area_ratio

    found = []
    for c in cnts:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        peri = cv2.arcLength(c, True)
        ap = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(ap) != 4 or not cv2.isContourConvex(ap):
            continue
        x, y, w, h = cv2.boundingRect(ap)
        if w < 40 or h < 20:
            continue
        ar = w / float(h)
        if not (0.8 < ar < 8.0):
            continue
        found.append((area, x, y, w, h))

    found.sort(key=lambda t: -t[0])

    # 去掉高度重合的重复框（同一个面板会被内外轮廓各找到一次）
    kept = []
    for area, x, y, w, h in found:
        dup = False
        for _, kx, ky, kw, kh in kept:
            ix = max(0, min(x + w, kx + kw) - max(x, kx))
            iy = max(0, min(y + h, ky + kh) - max(y, ky))
            inter = ix * iy
            if inter > 0.7 * min(w * h, kw * kh):
                dup = True
                break
        if not dup:
            kept.append((area, x, y, w, h))
    return [(x, y, w, h) for _, x, y, w, h in kept]


def binarize(roi):
    """
    面板内部二值化。

    必须做光照归一化：面板里有反光和阴影，实测同一块区域左边灰度 60、右边 190，
    全局阈值要么把阴影全当前景，要么把反光区的笔画全丢掉。
    做法是除以大核模糊（相当于估计局部背景亮度），再固定阈值。
    """
    bg = cv2.GaussianBlur(roi, (0, 0), 41)
    norm = cv2.divide(roi.astype(np.float32), bg.astype(np.float32) + 1, scale=180)
    norm = np.clip(norm, 0, 255).astype(np.uint8)

    _, bw = cv2.threshold(cv2.GaussianBlur(norm, (3, 3), 0), 120, 255,
                          cv2.THRESH_BINARY_INV)
    # 闭运算：反光会导致笔画断裂，先连起来再判断
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return bw


def split_rows(bw, min_ratio=0.06):
    """按行投影切出数字行。"""
    h, w = bw.shape
    rs = (bw > 0).sum(axis=1)
    thr = max(3, int(w * 0.02))

    rows, inb, start = [], False, 0
    for i, v in enumerate(rs):
        if v > thr and not inb:
            inb, start = True, i
        elif v <= thr and inb:
            inb = False
            if i - start > h * min_ratio:
                rows.append((start, i))
    if inb and len(rs) - start > h * min_ratio:
        rows.append((start, len(rs)))
    return rows


def split_digits(bw, y0, y1, min_w_ratio=0.015):
    """在给定行内按列投影切出单个数字块。"""
    band = bw[y0:y1, :]
    h, w = band.shape
    cs = (band > 0).sum(axis=0)
    thr = max(2, int(h * 0.05))

    cols, inb, start = [], False, 0
    for i, v in enumerate(cs):
        if v > thr and not inb:
            inb, start = True, i
        elif v <= thr and inb:
            inb = False
            if i - start >= max(3, w * min_w_ratio * 0.3):
                cols.append((start, i))
    if inb:
        cols.append((start, w))
    return cols


def read_segments(patch):
    """
    对单个数字块采样七段。

    patch 是已归一化到 (W, H) 的二值图（255=笔画）。
    七段的采样框都取在"段中心的一小块"，避开段与段之间的连接处——
    那里亮/不亮都可能，采到会误判。
    """
    H, W = patch.shape
    t = 0.20          # 横段厚度（相对高度）
    s = 0.22          # 竖段宽度（相对宽度）
    m = 0.34          # 采样区在长度方向上的收缩，避开连接处

    def frac(x1, y1, x2, y2):
        x1, x2 = int(max(0, x1)), int(min(W, x2))
        y1, y2 = int(max(0, y1)), int(min(H, y2))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        sub = patch[y1:y2, x1:x2]
        return float((sub > 0).mean())

    mid_y1, mid_y2 = int(H * (0.5 - t / 2)), int(H * (0.5 + t / 2))
    spans = {
        # 横段：取中间那一段（避左右连接）
        "a": (W * m, 0, W * (1 - m), H * t),
        "g": (W * m, mid_y1, W * (1 - m), mid_y2),
        "d": (W * m, H * (1 - t), W * (1 - m), H),
        # 竖段：取上下两截的中间部分
        "f": (0, H * 0.18, W * s, H * 0.42),
        "b": (W * (1 - s), H * 0.18, W, H * 0.42),
        "e": (0, H * 0.58, W * s, H * 0.82),
        "c": (W * (1 - s), H * 0.58, W, H * 0.82),
    }
    scores = {k: frac(*v) for k, v in spans.items()}
    return scores


def decode_segments(scores, thr=0.30):
    """段占比 → 数字。返回 (数字|None, 最佳距离, 命中段集合)。"""
    lit = {k for k, v in scores.items() if v >= thr}
    best, best_d = None, 99
    for digit, segs in SEGMENTS.items():
        d = len(segs ^ lit)
        if d < best_d:
            best, best_d = digit, d
    return best, best_d, lit


def normalize_patch(bw, x0, x1):
    """
    取一个数字块并归一化。

    只做垂直方向对齐（去掉行内上下留白），水平方向保留原形状——
    七段管的宽高比有自己的规矩，强行拉成正方形会让竖段变得过粗。
    """
    h, w = bw.shape
    sub = bw[:, x0:x1]
    rows = np.where((sub > 0).any(axis=1))[0]
    if len(rows) == 0:
        return None
    sub = sub[rows.min():rows.max() + 1, :]
    if sub.shape[0] < 8 or sub.shape[1] < 3:
        return None
    return cv2.resize(sub, (40, 64), interpolation=cv2.INTER_NEAREST)


def read_panel(roi_gray, debug_tag=None):
    """
    读一个面板里的所有数字行。

    返回形如 [ [ (digit, conf, is_dot), ... ], ... ]（每行一个列表）
    """
    bw = binarize(roi_gray)
    rows = split_rows(bw)
    out = []
    for y0, y1 in rows:
        band_digits = []
        for x0, x1 in split_digits(bw, y0, y1):
            w = x1 - x0
            if w < 4:
                continue
            # 小数点：很窄且贴底部
            col = bw[y0:y1, x0:x1]
            if w <= (y1 - y0) * 0.14:
                ys = np.where((col > 0).any(axis=1))[0]
                if len(ys) and ys.mean() > (y1 - y0) * 0.55:
                    band_digits.append((".", 1.0, True))
                    continue
            patch = normalize_patch(bw[y0:y1, :], x0, x1)
            if patch is None:
                continue
            scores = read_segments(patch)
            digit, dist, lit = decode_segments(scores)
            band_digits.append((digit, dist, False))
        if band_digits:
            out.append(band_digits)
    return out


if __name__ == "__main__":
    img = cv2.imread("data/samples/fuel_01.jpg")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    panels = find_panels(gray)
    print("面板:", panels)
    for i, (x, y, w, h) in enumerate(panels[:3]):
        pad = 12
        roi = gray[y + pad:y + h - pad, x + pad:x + w - pad]
        rows = read_panel(roi)
        print(f"\n--- 面板 {i} ({x},{y},{w},{h}) 共 {len(rows)} 行 ---")
        for band in rows:
            txt = "".join(d if d else "?" for d, _, _ in band)
            dists = [c for _, c, _ in band]
            print(f"  读出: {txt!r}   段距: {dists}")
