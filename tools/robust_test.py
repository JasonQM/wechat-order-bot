"""
本地识别鲁棒性测试：模拟真实拍照条件下的各种劣化，看方案在哪里失效。

为什么必须做这件事：
  目前只有 1 张样本图，3.8 秒全对是在理想条件下测的。
  但客户在加油站现场拍照，会遇到：光线暗、逆光、手持倾斜、对焦不准、
  ISO 噪点、手机裁切、离得远导致屏占画面小……
  与其等症状出现了再猜原因，不如主动把这些条件造出来，看边界在哪。

变换都是按真实场景设定的，不是为了刁难算法：
  · 亮度 ±35%      —— 夜间补光不足 / 白天反光过曝
  · 旋转 ±2/4/6°   —— 手持角度
  · 缩放 0.5~1.6x  —— 站得远 / 离得近
  · 高斯模糊        —— 对焦不准
  · 噪声            —— 夜间高 ISO
  · 裁切            —— 客户发图前随手裁过
  · 组合            —— 真实情况通常是多种叠加

输出：每项变异的识别结果，以及失败原因。
"""

import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import config          # noqa: E402
import recognize       # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "data", "samples", "fuel_01.jpg")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "probe_out", "robust")

# 正确答案（人工核对过）
TRUTH = {"total": 70000, "qty": 81.40, "unit": 8.60}


def rot(img, deg):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def brightness(img, factor):
    """factor > 1 变亮，< 1 变暗。"""
    return np.clip(img.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def scale(img, f):
    h, w = img.shape[:2]
    r = cv2.resize(img, (int(w * f), int(h * f)),
                   interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
    return r


def blur(img, k):
    return cv2.GaussianBlur(img, (k, k), 0)


def noise(img, sigma):
    g = np.random.default_rng(42).normal(0, sigma, img.shape)
    return np.clip(img.astype(np.float32) + g, 0, 255).astype(np.uint8)


def crop_pct(img, pct):
    """四边各裁掉 pct（0~0.2）比例，模拟客户裁图。"""
    h, w = img.shape[:2]
    dy, dx = int(h * pct), int(w * pct)
    return img[dy:h - dy, dx:w - dx]


def pad_zoom_out(img):
    """把图缩小后放到更大的黑底上：模拟「站得远，屏只占画面一小块」。"""
    h, w = img.shape[:2]
    small = cv2.resize(img, (int(w * 0.6), int(h * 0.6)), interpolation=cv2.INTER_AREA)
    canvas = np.full((h, w, 3), 40, dtype=np.uint8)
    y0, x0 = (h - small.shape[0]) // 2, (w - small.shape[1]) // 2
    canvas[y0:y0 + small.shape[0], x0:x0 + small.shape[1]] = small
    return canvas


CASES = [
    ("原图", lambda im: im),
    ("变暗 35%", lambda im: brightness(im, 0.65)),
    ("变暗 50%", lambda im: brightness(im, 0.50)),
    ("变亮 35%", lambda im: brightness(im, 1.35)),
    ("旋转 2°", lambda im: rot(im, 2)),
    ("旋转 4°", lambda im: rot(im, 4)),
    ("旋转 6°", lambda im: rot(im, 6)),
    ("旋转 -4°", lambda im: rot(im, -4)),
    ("缩小 0.75x", lambda im: scale(im, 0.75)),
    ("缩小 0.5x", lambda im: scale(im, 0.5)),
    ("放大 1.4x", lambda im: scale(im, 1.4)),
    ("模糊 k=3", lambda im: blur(im, 3)),
    ("模糊 k=5", lambda im: blur(im, 5)),
    ("噪点 σ=12", lambda im: noise(im, 12)),
    ("噪点 σ=25", lambda im: noise(im, 25)),
    ("裁边 3%", lambda im: crop_pct(im, 0.03)),
    ("裁边 6%", lambda im: crop_pct(im, 0.06)),
    ("远景(缩到画布60%)", pad_zoom_out),
    ("暗+斜+糊", lambda im: blur(rot(brightness(im, 0.7), 3), 3)),
    ("暗+噪点", lambda im: noise(brightness(im, 0.65), 15)),
    ("斜+裁边", lambda im: crop_pct(rot(im, 4), 0.03)),
]


def verdict(r):
    if not r.get("ok"):
        return False, "未通过"
    t = r.get("total_cents")
    q = r.get("qty_liters")
    u = r.get("unit_price")
    if t != TRUTH["total"]:
        return False, f"金额错({t})"
    if q is not None and abs(q - TRUTH["qty"]) > 0.02:
        return False, f"油量错({q})"
    if u is not None and abs(u - TRUTH["unit"]) > 0.05:
        return False, f"单价错({u:.2f})"
    return True, "正确"


def main():
    os.makedirs(OUT, exist_ok=True)
    conf = config.load()
    print(f"识别模式：{conf['recognition']['mode']}")
    img = cv2.imread(SRC)
    if img is None:
        print(f"读不到样本图：{SRC}")
        return 1

    ok_n = 0
    fails = []
    print(f"{'场景':<20} {'结果':<8} {'耗时':>6}  金额/油量/单价")
    print("-" * 78)

    for idx, (name, fn) in enumerate(CASES, 1):
        try:
            var = fn(img.copy())
        except Exception as e:
            print(f"{name:<20} 变换失败 {e}")
            continue

        # 文件名必须用 ASCII：Windows + OpenCV 对中文路径支持不稳，
        # 实测 cv2.imwrite 写不出来，紧接着 os.path.isfile 返回 False，
        # 整轮全部「未通过」，看起来像算法全崩，其实是文件根本没落地。
        path = os.path.join(OUT, f"case_{idx:02d}.png")
        if not cv2.imwrite(path, var):
            print(f"{name:<20} 写文件失败，跳过")
            continue

        t0 = time.time()
        try:
            r = recognize.recognize_order(path, conf)
        except Exception as e:
            print(f"{name:<20} 异常 {type(e).__name__}: {e}")
            fails.append((name, f"异常 {type(e).__name__}"))
            continue
        el = time.time() - t0

        good, why = verdict(r)
        ok_n += good
        if not good:
            fails.append((name, why))

        t = f"{r['total_cents'] / 100:.2f}" if r.get("total_cents") else "-"
        q = f"{r['qty_liters']:.2f}" if r.get("qty_liters") else "-"
        u = f"{r['unit_price']:.2f}" if r.get("unit_price") else "-"
        mark = "OK  " if good else "FAIL"
        print(f"{name:<20} {mark:<8} {el:5.1f}s  {t:>8} / {q:>7} / {u:>5}"
              + ("" if good else f"   ← {why}"))

    total = len(CASES)
    print("-" * 78)
    print(f"通过 {ok_n}/{total}（{100 * ok_n / total:.0f}%）")
    if fails:
        print("\n失败场景：")
        for n, w in fails:
            print(f"  · {n}：{w}")
        print(f"\n变异图已存到 {OUT}")
    return 0 if ok_n == total else 1


if __name__ == "__main__":
    sys.exit(main())
