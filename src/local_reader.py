"""
本地加油票据识别（纯本地，不联网，2 秒内出结果）。

为什么单独做一套而不继续用云端视觉模型：
  云端确实准，但实测单次调用在 8~65 秒之间波动（服务端排队），
  三路采样后一单要等半分钟到一分钟，挂机时客户体验很差。
  而加油票据的版面是固定的三个数字区，完全可以本地几秒读完。

核心做法（每一步都是被实测逼出来的）：
  1. 找 LCD 面板：轮廓检测取近似四边形的大区域。
     不能靠"暗像素"——面板里有阴影和反光，全图统计一定会失败。
  2. 面板内按行投影切出数字行。
  3. 每行裁出数字区、放大 2 倍。**这一步是关键**：
     整图缩到 1400 宽送 OCR，油量 81.40 会读成 1.40、单价直接漏掉；
     裁出来放大后，同一块屏就读对了 81.40。数字占满画面才是 OCR 擅长的情况。
  4. RapidOCR 读数字文本。
  5. 【兜底的两条代数规则】OCR 对七段管的两个硬伤，靠算术补回来：
     · 小数点会被吞（700.00 读成 70000）→ 枚举小数点位置，
       选那个让「金额 ÷ 油量 = 合理单价」成立的位置
     · 单价那块屏常常是淡出状态、对比度近零 → 用 金额 ÷ 油量 反算
     最后「油量 × 单价 ≈ 金额」必须闭环，否则判定失败、转人工。

设计上刻意不做的事：不写死像素坐标。面板位置、行位置、数字位置全部现算，
所以客户换个角度重拍、或者缩放不同，都还能用。只有"三个数字区的相对关系"
是假设的（金额在上、油量在其下、单独一块是单价），这来自实际版式。
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image

import ocr_engine


# ---------------------------------------------------------------- 版面
def find_panels(gray, min_area_ratio=0.004):
    """找 LCD 面板：近似四边形的区域。返回 [(x, y, w, h, quad)]，按面积降序。"""
    H, W = gray.shape
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = H * W * min_area_ratio

    found = []
    full_frame = []          # 几乎贴满整图的四边形，见下面的说明
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

        quad = ap.reshape(4, 2).astype(np.float32)

        # 窗口边框、截图留白会凑成一个几乎贴满整图的四边形。
        #
        # 它不是屏幕，但面积最大，一定排在第一。后面的"重合去重"是按
        # 「两个框重叠超过较小者 70% 就算重复」判的——真面板整个嵌套在
        # 这个假框里，于是**所有真面板都被判成重复丢掉**。
        #
        # 实测代价很直接：微信预览窗抓的图（带一圈黑边）里，主屏 757×518
        # 和单价屏 344×183 都检出了，却只剩一个 1711×1322 的"面板"，
        # 行切分对着整张照片切成 1 行，最后只读出一个数字，订单被推去人工。
        if w > 0.9 * W and h > 0.9 * H:
            full_frame.append((area, x, y, w, h, quad))
            continue

        found.append((area, x, y, w, h, quad))

    found.sort(key=lambda t: -t[0])

    # 同一个面板的内外轮廓会被各找到一次，按重合度去重
    kept = []
    for area, x, y, w, h, quad in found:
        dup = False
        for _, kx, ky, kw, kh, _q in kept:
            ix = max(0, min(x + w, kx + kw) - max(x, kx))
            iy = max(0, min(y + h, ky + kh) - max(y, ky))
            if ix * iy > 0.7 * min(w * h, kw * kh):
                dup = True
                break
        if not dup:
            kept.append((area, x, y, w, h, quad))

    # 兜底：真面板一个都没找到时，把假框放回来。
    # 客户如果发的是「整个屏幕的截图」而不是拍机器，画面里唯一的四边形
    # 就是边框本身，这时候它确实是我们要读的东西，不能判成空。
    if not kept and full_frame:
        full_frame.sort(key=lambda t: -t[0])
        kept = [full_frame[0]]

    return [(x, y, w, h, quad) for _, x, y, w, h, quad in kept]


def order_quad(pts):
    """把四个角点排成「左上、右上、右下、左下」。"""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    rect = np.zeros((4, 2), dtype=np.float32)
    ssum = pts.sum(axis=1)
    rect[0] = pts[np.argmin(ssum)]       # 左上：x+y 最小
    rect[2] = pts[np.argmax(ssum)]       # 右下：x+y 最大
    d = np.diff(pts, axis=1).ravel()
    rect[1] = pts[np.argmin(d)]          # 右上：y-x 最小
    rect[3] = pts[np.argmax(d)]          # 左下：y-x 最大
    return rect


def warp_panel(gray, quad, pad_ratio=0.03):
    """
    把面板四边形透视矫正成正矩形，并去掉边框。

    为什么必须做：客户在加油站手持拍照，画面几乎一定有 2-6 度倾斜。
    只取外接矩形的话，裁出来的图里数字是歪的，行投影和列投影会把
    相邻数字粘在一起——实测旋转 2 度就足以把 700.00 读成 1000.00，
    而且 100 × 10 = 1000 还能自洽，算术校验根本拦不住，会直接报错价。
    """
    rect = order_quad(quad)
    tl, tr, br, bl = rect
    w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if w < 30 or h < 15:
        return None

    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                   dtype=np.float32)
    m = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(gray, m, (w, h))

    # 内缩去掉边框。矫正后边框宽度随尺寸变，按比例取。
    px = max(4, int(min(w, h) * pad_ratio))
    out = warped[px:h - px, px:w - px]
    return out if out.size else None


def binarize(roi):
    """
    面板内部二值化。

    必须做光照归一化（除以大核模糊 ≈ 估计局部背景亮度）。
    实测同一块面板里左边灰度 60、右边 190，全局阈值要么把阴影全当前景，
    要么把反光区的笔画全丢掉。
    """
    bg = cv2.GaussianBlur(roi, (0, 0), 41)
    norm = cv2.divide(roi.astype(np.float32), bg.astype(np.float32) + 1, scale=180)
    norm = np.clip(norm, 0, 255).astype(np.uint8)
    _, bw = cv2.threshold(cv2.GaussianBlur(norm, (3, 3), 0), 120, 255,
                          cv2.THRESH_BINARY_INV)
    # 反光会让笔画断裂，先连起来再切分
    return cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


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

    return merge_rows(rows)


def merge_rows(rows, gap_ratio=0.25):
    """
    把被拦腰断开的数字行合回一行。

    为什么需要：七段管的数字中间那一横常常不亮（0、7 都没有中横），
    二值化后整行会在中横位置几乎没有前景像素，行投影于是把一个数字
    切成上下两截。实测透视矫正后金额行被切成 (18,119) 和 (124,223)
    两段，中间只隔 5 像素，结果 OCR 把金额读成 1000.00。

    判据用「间隙占行高的比例」：
      · 同一行被切断 → 间隙极小（实测 5/101 = 0.05）
      · 真正的两个数字行之间 → 间隙大（实测 43/99 = 0.43）
    0.25 能干净地分开这两种情况。
    """
    if len(rows) <= 1:
        return rows

    # 基准用「各行段高度的中位数」，不能用"前一段"：
    # 前一段可能刚被合并过，高度已经翻倍，会把后面真正独立的行也吞进来。
    # 实测拿 prev_h 判断时，合并金额行后高度变 205，油量行间隙 43 < 205*0.25
    # 也被误并，两行糊成一块，结果只读出一个乱码。
    base_h = float(np.median([y1 - y0 for y0, y1 in rows]))

    out = [list(rows[0])]
    for y0, y1 in rows[1:]:
        prev = out[-1]
        gap = y0 - prev[1]
        if gap < base_h * gap_ratio:
            prev[1] = y1              # 合并
        else:
            out.append([y0, y1])
    return [tuple(r) for r in out]


def _split_cols(bw, y0, y1):
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
            if i - start >= 3:
                cols.append((start, i))
    if inb:
        cols.append((start, w))
    return cols


def digits_crop(roi_gray, bw, y0, y1, expand=0.0):
    """
    裁出一行里的数字区（去掉左侧的「金额」「油量」这类中文标签）。

    做法：先按列投影切块，再按间距分组，取总宽度最大的那一组。
    数字组一定比标签组宽得多，而且标签和数字之间隔着明显空白。

    间距阈值必须给得足够大（0.8 倍行高）：七段管的 "4" 和 "0" 常常被反光切成
    好几个碎片，碎片之间有空隙。阈值给小了会把同一个数字串断成两组，
    于是只裁到左半边——实测把 81.40 裁成 "h18"，最后一个 0 被切在外面。

    expand：裁完后上下各外扩的行高比例。见 read_row_voted 里的说明。
    """
    cols = _split_cols(bw, y0, y1)
    if not cols:
        return None

    gap_thr = (y1 - y0) * 0.8

    groups = [[cols[0]]]
    for prev, cur in zip(cols, cols[1:]):
        if cur[0] - prev[1] > gap_thr:
            groups.append([cur])
        else:
            groups[-1].append(cur)

    best = max(groups, key=lambda g: sum(c[1] - c[0] for c in g))
    x0, x1 = best[0][0], best[-1][1]
    if x1 - x0 < 12:
        return None

    e = int((y1 - y0) * expand)
    yy0 = max(0, y0 - e)
    yy1 = min(roi_gray.shape[0], y1 + e)
    return roi_gray[yy0:yy1, x0:x1]


# ---------------------------------------------------------------- OCR
def _ocr_crop(crop, scale=2, max_width=1600):
    """把裁出的一行放大后送本地 OCR，返回 [(text, score)]，按 x 排序。"""
    if crop is None or crop.size == 0:
        return []

    img = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
    if scale != 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)),
                         Image.LANCZOS)
    if img.width > max_width:
        r = max_width / float(img.width)
        img = img.resize((max_width, max(1, int(img.height * r))), Image.LANCZOS)

    try:
        lines = ocr_engine.ocr(img, max_width=None, min_score=0.12)
    except Exception:
        return []
    return [(r["text"], r["score"]) for r in lines]


# 三组裁剪参数。实测同一行在这三组下都读得对，而中间区间（外扩 0.02~0.10）
# 反而会读错——扩一点点会把上一行的底部或面板边框带进来，破坏识别。
# 所以刻意跳过中间值，不是随手取的三个数。
_VOTE_EXPANDS = (0.0, 0.15, 0.20)


def read_row_voted(roi, bw, y0, y1, expands=None, scale=2):
    """
    读一行数字，多组裁剪参数投票。

    为什么必须投票：本地 OCR 对七段管的结果对裁剪边界极其敏感。
    实测同一张图、同一行，只把上下边界挪动十几像素，结果就在
    81.40 / 0618 / 8.40 / 0h18 之间跳——单次结果不可信。
    但本地 OCR 一次只要 0.3 秒，跑三组投票的代价远低于云端一次调用，
    而可靠性从"看运气"变成"三局两胜"。

    返回 (text|None, score, votes)
    """
    expands = expands or _VOTE_EXPANDS
    votes = {}
    best_score = 0.0

    for ex in expands:
        crop = digits_crop(roi, bw, y0, y1, expand=ex)
        if crop is None:
            continue
        readings = _ocr_crop(crop, scale=scale)
        picked = _pick_number(readings)
        if not picked:
            continue
        text, score = picked
        votes[text] = votes.get(text, 0) + 1
        best_score = max(best_score, score)

        # 早停：三组参数里拿到两票就是多数了，第三组不必再跑。
        # 本地一次 OCR 约 1.2 秒，每行省一次就是省 2.4 秒——
        # 而"两组完全一致"本身已经是很强的信号（实测同一张图，
        # 意见不一致时会散成 0618 / 8.40 / 0h18 三种，不会凑巧两次相同）。
        if votes[text] >= 2:
            break

    if not votes:
        return None, 0.0, {}
    text = max(votes.items(), key=lambda kv: kv[1])[0]
    return text, best_score, votes


# ---------------------------------------------------------------- 数字解析
# OCR 对七段管常见的字符混淆。只在解析失败时逐字符尝试替换，
# 不预先全局替换——那会把 "B" 这类真字母也改掉，反而制造错误。
_CONFUSE = {"O": "0", "o": "0", "D": "0", "Q": "0",
            "l": "1", "I": "1", "|": "1", "i": "1",
            "Z": "2", "z": "2",
            "S": "5", "s": "5",
            "G": "6", "b": "6",
            "B": "8",
            "g": "9", "q": "9"}


def _clean_numeric(text):
    """把 OCR 文本清成「数字 + 最多一个小数点」。返回 str 或 None。"""
    if not text:
        return None
    s = str(text).strip()
    s = s.replace(" ", "").replace(",", ".").replace("，", ".")
    s = s.replace("。", ".")
    # 中文单位/干扰字去掉
    s = re.sub(r"[^\d.OoDQlI|iZzSsGbBgq\-]", "", s)
    if not s:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", s):
        return s

    fixed = "".join(_CONFUSE.get(ch, ch) for ch in s)
    fixed = fixed.replace("-", "")
    if re.fullmatch(r"\d+(\.\d+)?", fixed):
        return fixed
    return None


def _pick_number(readings):
    """
    从一行 OCR 结果里挑出数字。

    取"数字字符最多的那一段"，而不是简单拼接——一行里可能同时读出
    「金额」「元」这类文字，拼起来会变成垃圾。
    """
    best, best_len = None, 0
    for text, score in readings:
        num = _clean_numeric(text)
        if not num:
            continue
        ndigits = len(re.sub(r"[^\d]", "", num))
        if ndigits > best_len:
            best, best_len = (num, score), ndigits
    return best


def _decimal_candidates(raw, max_digits=9):
    """
    把一串纯数字展开成所有可能的小数点位置。

    OCR 读七段管会吞掉小数点：700.00 → "70000"。
    与其猜"加油机一般是两位小数"，不如把可能性都列出来，用算术关系筛——
    油价、油量、金额之间的乘除关系能把错的位置全部排除。
    """
    if "." in raw:
        return [raw]

    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits) > max_digits:
        return []

    out = []
    for cut in range(len(digits), -1, -1):     # cut = 整数部分位数
        if cut == 0:
            out.append("0." + digits)
        elif cut == len(digits):
            out.append(digits)
        else:
            out.append(digits[:cut] + "." + digits[cut:])
    return out


# 合理性区间。这三条是防「读错但自洽」的最后一道闸。
#
# 为什么必须收紧：算术校验只能验「金额 = 油量 × 单价」这个乘法关系，
# 如果三个数按同一比例一起错，关系依然成立。实测噪点图读出
# 1000.00 / 81.40 / 12.29，乘法自洽，但 12.29 元/升明显不是国内油价；
# 另一张读出 7000.00 / 814.00 / 8.60，814 升也不是一次加油的量。
#
# 区间按实际业务取，留了余量：
#   · 油价 4.5~11 元/升 —— 92/95 号汽油、0 号柴油都在里面，非标油品也可能略超
#   · 单次 1~600 升      —— 轿车 40~60 升，大货车双油箱 400 升上下，600 够宽松
#   · 单次 5~8000 元     —— 对应的金额范围
UNIT_PRICE_MIN, UNIT_PRICE_MAX = 4.5, 11.0
QTY_MIN, QTY_MAX = 1.0, 600.0
AMOUNT_MIN, AMOUNT_MAX = 5.0, 8000.0


def _resolve_scaling(amount_raws, qty_raws, price_raws,
                     tol_ratio=0.002, tol_min_cents=50):
    """
    定出金额和油量的真实数值。

    难点只有一个：小数点可能被吞。用两条硬关系筛：
      · 金额 ÷ 油量 必须落在合理油价区间
      · 若单价这块屏也读到了，它必须和算出来的接近
    返回 (total_cents, qty_liters, unit_price, reason)
    """
    amount_cands = []
    for raw in (amount_raws or []):
        for c in _decimal_candidates(raw):
            if c not in amount_cands:
                amount_cands.append(c)

    qty_cands = []
    for raw in (qty_raws or []):
        for c in _decimal_candidates(raw):
            if c not in qty_cands:
                qty_cands.append(c)

    price_val = None
    price_raw = (price_raws or [None])[0]
    if price_raw:
        try:
            price_val = float(price_raw)
        except ValueError:
            price_val = None
        if price_val is not None and not (UNIT_PRICE_MIN <= price_val <= UNIT_PRICE_MAX):
            # 单价读出来的值不在合理区间，多半是小数点被吞了，重新展开一次
            for c in _decimal_candidates(price_raw):
                v = float(c)
                if UNIT_PRICE_MIN <= v <= UNIT_PRICE_MAX:
                    price_val = v
                    break
            else:
                price_val = None

    best = None
    for a in amount_cands:
        av = float(a)
        if av < AMOUNT_MIN or av > AMOUNT_MAX:
            continue
        for q in (qty_cands or [None]):
            if q is None:
                # 没有油量可校验，只能接受「两位小数」这个行业默认
                if "." not in a:
                    continue
                unit = None
            else:
                qv = float(q)
                if qv < QTY_MIN or qv > QTY_MAX:
                    continue
                unit = round(av / qv, 2)
                if not (UNIT_PRICE_MIN <= unit <= UNIT_PRICE_MAX):
                    continue

            score = 0
            if unit is not None:
                score += 2
                if price_val is not None:
                    # 算出来的单价和屏显单价对得上，这是最硬的证据
                    if abs(unit - price_val) <= max(0.05, price_val * 0.02):
                        score += 3
                    else:
                        score -= 2
            if "." in a:
                score += 1                  # OCR 自己读出了小数点，优先采信

            if best is None or score > best[0]:
                best = (score, a, q, unit)

    if best is None:
        return None, None, None, "金额和油量的数值无法自洽（小数点位置都对不上合理油价）"

    score, a, q, unit = best
    total_cents = int(round(float(a) * 100))
    qty = float(q) if q else None
    return total_cents, qty, unit, ""


# ---------------------------------------------------------------- 主入口
def read(image_path, conf=None):
    """
    读一张加油票据。返回与 recognize.recognize_order 兼容的结构。

    ok=False 时 reasons 里写清是哪一步失败，上层据此决定转人工还是走云端。
    """
    result = {
        "ok": False,
        "total_cents": None,
        "qty_liters": None,
        "unit_price": None,
        "items": [],
        "shipping_cents": 0,
        "doc_discount_cents": 0,
        "confidence": None,
        "reasons": [],
        "warnings": [],
        "ocr_text": "",
        "raw_json": None,
        "degraded": False,
        "channel": "local",
        "panel_found": False,
    }

    if not os.path.isfile(image_path):
        result["reasons"].append(f"图片不存在：{image_path}")
        return result

    img = cv2.imread(image_path)
    if img is None:
        result["reasons"].append("图片读不出来（格式不支持或文件损坏）")
        return result

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    panels = find_panels(gray)
    result["warnings"].append(f"检出 {len(panels)} 个显示面板")

    if not panels:
        result["reasons"].append("没找到加油机显示屏（画面里没有四边形的数字面板）")
        result["panel_found"] = False
        return result
    result["panel_found"] = True

    # ---- 先把所有「面板 × 行 × 参数组」的裁剪都切好 ----
    #
    # 为什么先切后并行：OCR 引擎实测是线程安全的（同一进程内 4 路并发，
    # 结果与串行完全一致），而它每次调用有约 1 秒的固定开销，
    # 这些开销可以完全重叠。实测串行 4 次 4.26 秒 → 并行 1.84 秒。
    # 所以这里不做「早停」，全跑反而比串行两票停更快。
    tasks = []          # [(panel_idx, row_idx, expand, crop, panel_area)]
    for pi, (x, y, w, h, quad) in enumerate(panels[:3]):
        # 透视矫正成正矩形（而不是直接裁外接矩形），倾斜才读得准
        roi = warp_panel(gray, quad)
        if roi is None or roi.size == 0:
            continue

        try:
            bw = binarize(roi)
        except Exception:
            continue

        # 主面板（面积最大那个，装金额和油量）做三组投票；
        # 其余面板只读一组。
        #
        # 为什么不都给三组：单价那块小屏实测常年是淡出状态
        # （浅灰笔画配浅灰底，对比度近零），三组都读不出来，
        # 白烧两次 OCR。而主面板的两个数是算钱的依据，值得多读两遍。
        expands = _VOTE_EXPANDS if pi == 0 else (_VOTE_EXPANDS[0],)

        for ri, (y0, y1) in enumerate(split_rows(bw)):
            for ex in expands:
                crop = digits_crop(roi, bw, y0, y1, expand=ex)
                if crop is None:
                    continue
                tasks.append((pi, ri, ex, crop, w * h))

    if not tasks:
        result["reasons"].append("面板找到了，但里面没切出可读的数字区")
        return result

    def _one(task):
        return _ocr_crop(task[3], scale=2)

    # 先串行跑第一个任务，再并行其余。
    #
    # 这一步不是为了省事，是为了避开一次真实的退化：OCR 引擎第一次调用要加载
    # 模型（约 1.2 秒）。如果 6 个线程同时首次调用，它们会互相等着加载，
    # 实测整单从 4.4 秒涨到 8 秒——比串行还慢。
    # 先跑一个既完成加载，又正好拿到主面板第一行的第一票，一点不浪费。
    ocr_out = [_one(tasks[0])]

    if len(tasks) > 1:
        # 实测 6 路并发最优；再往上会和 onnxruntime 内部线程池抢 CPU，
        # 9 任务 8 并发反而退到 10 秒。
        workers = min(6, len(tasks) - 1)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                ocr_out += list(pool.map(_one, tasks[1:]))
        except Exception:
            # 并发出问题就退回串行，宁可慢也别整单失败
            ocr_out += [_one(t) for t in tasks[1:]]

    # ---- 按 (面板, 行) 聚合投票 ----
    votes_by_row = {}
    for (pi, ri, ex, crop, area), readings in zip(tasks, ocr_out):
        picked = _pick_number(readings)
        if not picked:
            continue
        text, score = picked
        slot = votes_by_row.setdefault((pi, ri), {
            "votes": {}, "score": 0.0, "area": area})
        slot["votes"][text] = slot["votes"].get(text, 0) + 1
        slot["score"] = max(slot["score"], score)

    rows_info = []
    texts = []
    for (pi, ri) in sorted(votes_by_row):
        slot = votes_by_row[(pi, ri)]
        detail = " / ".join(
            f"{k}×{v}" for k, v in sorted(slot["votes"].items(), key=lambda kv: -kv[1]))

        # 平票就是没有结论，不能随便挑一个。
        #
        # 实测噪点大的照片上，同一条金额行的三组裁剪参数会给出
        # 10000×1 / 70000×1 / 0000×1 三个互不相同的值。原来用 max() 取第一个，
        # 拿到 10000 → 金额变成 1000.00，而 1000 ÷ 81.4 = 12.29 元/升
        # 居然还算"合理油价"，算术校验完全拦不住，会直接把错价发到群里。
        # 平票时宁可这一行读不出、整单转人工。
        top = max(slot["votes"].values())
        winners = [k for k, v in slot["votes"].items() if v == top]
        ranked = sorted(slot["votes"].items(), key=lambda kv: -kv[1])

        if len(winners) > 1:
            # 平票不当场否决，交给后面的代数关系裁决。
            #
            # 实测变暗、轻微模糊这类图上，金额行常给出
            # 700.00×1 / 0000×1 / 70000×1 三个候选——正确的 700.00 就在里面。
            # 早先在这里直接判"无法决断"，等于把正确答案一起扔了，
            # 21 种劣化里因此白丢了好几例。而真相是：配着油量 81.40 一算，
            # 700.00 得 8.60 元/升（合理），70000 得 860，0000 得 0，
            # 唯一解一目了然。所以候选全留着，让算术来选。
            result["warnings"].append(
                f"第 {ri + 1} 行识别有分歧（{detail}），已交由算术关系裁决")
        texts.append(detail)
        rows_info.append({
            "panel": pi,
            "row": ri,
            "raw": winners[0] if len(winners) == 1 else None,
            "candidates": [t for t, _c in ranked],
            "score": slot["score"],
            "votes": slot["votes"],
            "area": slot["area"],
        })

    result["ocr_text"] = " | ".join(texts)

    if not rows_info:
        result["reasons"].append("面板找到了，但里面没读出任何数字")
        return result

    # ---- 分配角色 ----
    # 主面板（面积最大）的第一行是金额、第二行是油量；
    # 其余面板的第一行候选单价。这是这台机器固定的版式。
    main_panel = min(r["panel"] for r in rows_info)
    main_rows = sorted([r for r in rows_info if r["panel"] == main_panel],
                       key=lambda r: r["row"])
    other = [r for r in rows_info if r["panel"] != main_panel]

    def _cands(row):
        """一行能给的所有候选（投票分裂时有多个），去重保序。"""
        out = []
        if row.get("raw"):
            out.append(row["raw"])
        for t in (row.get("candidates") or []):
            if t not in out:
                out.append(t)
        return out

    amount_raws = _cands(main_rows[0]) if len(main_rows) >= 1 else []
    qty_raws = _cands(main_rows[1]) if len(main_rows) >= 2 else []
    price_raws = _cands(other[0]) if other else []

    # 只切出一行时，无法判断它是金额还是油量 —— 不猜，直接报失败。
    # 猜错的代价是报错价，比转人工贵得多。
    if amount_raws and not qty_raws:
        result["reasons"].append(
            f"只读出一个数字（{amount_raws[0]}），无法确认它是金额还是油量")
        return result

    total_cents, qty, unit, why = _resolve_scaling(amount_raws, qty_raws, price_raws)
    if total_cents is None:
        result["reasons"].append(why or "数值无法自洽")
        return result

    # ---- 闭环校验：油量 × 单价 ≈ 金额 ----
    tol = max(50, int(total_cents * 0.002))
    if qty and unit:
        calc = int(round(qty * unit * 100))
        diff = abs(calc - total_cents)
        if diff > tol:
            result["reasons"].append(
                f"算术不闭环：{qty:.2f} 升 × {unit:.2f} 元/升 = "
                f"{calc / 100:.2f} 元，与屏显金额 {total_cents / 100:.2f} 元 "
                f"差 {diff / 100:.2f} 元（容差 {tol / 100:.2f} 元）")
            return result

    scores = [r["score"] for r in rows_info]
    result["confidence"] = min(scores) if scores else None
    result["total_cents"] = total_cents
    result["qty_liters"] = qty
    result["unit_price"] = unit
    result["items"] = [{
        "name": "加油",
        "spec": "升",
        "qty": qty,
        "unit_price": unit,
        "subtotal": total_cents / 100.0,
    }]
    result["raw_json"] = {
        "amount_raw": amount_raws,
        "qty_raw": qty_raws,
        "price_raw": price_raws,
        "total": total_cents / 100.0,
        "qty": qty,
        "unit_price": unit,
        "rows": rows_info,
    }
    result["ok"] = True

    if not price_raws:
        result["warnings"].append("单价那块屏没读出来（对比度太低），已用 金额÷油量 反算")
    return result


if __name__ == "__main__":
    import sys
    import time

    path = sys.argv[1] if len(sys.argv) > 1 else "data/samples/fuel_01.jpg"
    t0 = time.time()
    r = read(path)
    el = time.time() - t0

    print(f"耗时 {el:.2f}s")
    print("通过  :", r["ok"])
    print("金额  :", (f"{r['total_cents'] / 100:.2f} 元" if r["total_cents"] else "-"))
    print("油量  :", f"{r['qty_liters']:.2f} 升" if r["qty_liters"] else "-")
    print("单价  :", f"{r['unit_price']:.2f} 元/升" if r["unit_price"] else "-")
    print("置信度:", r["confidence"])
    print("原因  :", r["reasons"])
    print("提示  :", r["warnings"])
    print("原文  :", r["ocr_text"][:200])
