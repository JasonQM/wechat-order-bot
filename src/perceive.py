"""
感知解析：把 OCR 结果 + 画面像素，解析成「气泡」列表。

这是整个项目里最依赖实机标定的一块。下面所有阈值都是初始估计，
必须在真实微信截图上看一遍再调。集中放在 THRESHOLDS 里，方便一处改。

输出结构
--------
每段气泡是一个 dict：
    type    "text" | "image" | "divider"
    side    "in"（对方）| "out"（我方绿底）
    sender  群里发送者昵称，认不出为 None
    text    文字内容（image 为空）
    ...
"""

import re

import numpy as np

# 聊天区里的系统提示／时间戳，不是消息，必须过滤掉
TIME_RE = re.compile(r"^(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)$")
SYSTEM_WORDS = {
    "昨天", "前天", "今天", "星期一", "星期二", "星期三", "星期四",
    "星期五", "星期六", "星期日", "查看更多消息", "重新编辑", "发送",
    "以下为新消息", "以下是新消息", "对方正在输入...",
}

# 群里常见的系统提示，不是客户消息，必须过滤
SYSTEM_PREFIXES = (
    "你修改群名为", "修改群名为", "你邀请", "你撤回了一条消息",
    "加入了群聊", "退出了群聊", "移出了群聊", "邀请.*加入了群聊",
    "开启了群聊邀请确认", "关闭了群聊邀请确认", "群公告",
    "以上是打招呼的内容", "你已添加了", "该消息已过期",
)


def _is_system_text(t):
    if not t:
        return False
    for p in SYSTEM_PREFIXES:
        if t.startswith(p):
            return True
    return False

# 全部阈值集中在这里，实机标定时只改这里
THRESHOLDS = {
    # 图片区块检测
    "block": 16,             # 网格块边长（像素）
    "block_fill": 0.35,      # 块内「非气泡底色」像素占比超过此值算有内容
    "block_std": 9.0,        # 备用：灰度标准差阈值（旧算法保留）
    "text_pad": 6,           # 文字框外扩，避免文字块被当成图片
    "img_min_side": 72,      # 图片气泡最小边长
    "img_max_aspect": 5.0,   # 宽高比上限（超过视为横幅/卡片，不是订单图）
    "img_min_aspect": 0.2,
    "img_min_area": 9000,
    "bg_diff": 10,            # 与聊天背景色相差多少算「有内容」（白底图靠这条被看见）
    "img_dilate": 1,          # 膨胀半径（块），把分散的二维码/文字连成整张图
    "img_denoise": 2,          # 8 邻域里至少还有几个块才算真内容（去孤立噪点）
    "img_min_std": 12.0,      # 矩形内部灰度标准差下限，排除纯白空块
    "img_expand": 0.08,       # 框向外扩的比例，补偿抓不到的纯白边缘

    # 气泡合并
    "merge_gap_y": 40,       # 同侧文字行垂直间距小于此值，合并为同一气泡

    # 发送者昵称行
    "name_max_h": 22,        # 昵称行高度上限（会再和正文中位数比较）
    "name_h_factor": 1.2,    # 允许昵称行比正文中位高度高出的倍数
    "name_max_len": 16,      # 昵称最长字数（超过就不像昵称）
    "name_left_off": 70,     # 昵称行相对聊天区左边界的最大偏移（最关键的特征）
    "name_to_bubble_gap": 90,  # 昵称行到下方气泡的最大间距
    "name_min_w": 14,

    # 绿底判定（我方气泡）
    "green_dr": 40,          # 绿通道要比红通道高出这么多
    "green_db": 55,          # 绿通道要比蓝通道高出这么多
    "green_g_min": 190,      # 绿通道下限
    "green_r_min": 105,      # 红通道下限（照片里的深绿红通道很低，靠这条排除）
    "green_r_max": 205,
    "green_b_min": 55,
    "green_b_max": 175,
    "green_pixels": 15,
    "green_ratio": 0.10,     # 绿色像素占采样区的比例下限
    "green_min": 45,         # 旧字段，保留兼容
}

DIVIDER_WORDS = ("以下为新消息", "以下是新消息", "以下為新消息")
HEADER_SKIP = {
    "最小化", "最大化", "关闭", "设置", "聊天信息", "搜索", "表情", "发送",
    "文件", "截图", "语音聊天", "视频聊天", "…", "...", "口", "-", "x", "X",
}


# ---------------------------------------------------------------- 顶栏
def read_header(lines, layout):
    """读顶栏群名。只取顶栏区域内、聊天区左侧的候选行，按 x 排序拼接。"""
    csx, hh = layout["chat_start_x"], layout["header_h"]
    total_w = layout["total_w_px"]

    cands = []
    for r in lines:
        if r["cy"] > hh:
            continue
        if r["cx"] < csx - 40:
            continue
        if r["cx"] > csx + 0.45 * (total_w - csx):
            continue
        if r["text"] in HEADER_SKIP:
            continue
        if len(r["text"]) <= 1 and not r["text"].isdigit():
            continue
        cands.append(r)

    cands.sort(key=lambda r: r["cx"])
    return " ".join(r["text"] for r in cands).strip()


def header_region(layout):
    return (0, 0, layout["total_w_px"], int(layout["header_h"] + 12))


# ---------------------------------------------------------------- 分界线
def find_divider(lines, layout):
    """找「以下为新消息」分界线，返回其 y 中心；没有则 None。"""
    csx = layout["chat_start_x"]
    for r in lines:
        if r["cx"] <= csx:
            continue
        if any(w in r["text"] for w in DIVIDER_WORDS):
            return r["cy"]
    return None


# ---------------------------------------------------------------- 几何辅助
def is_in_chat(r, layout):
    csx = layout["chat_start_x"]
    top = layout["header_h"] + 12
    bottom = layout["total_h_px"] - int(layout["input_h"] * 0.70)
    return r["cx"] > csx + 12 and top < r["cy"] < bottom


def _patch(arr, x1, y1, x2, y2):
    h, w = arr.shape[:2]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(w, int(x2)), min(h, int(y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return arr[y1:y2, x1:x2]


def is_green_bubble(arr, r, th=None):
    """
    采样文字框外扩区域，判断是不是我方绿色气泡。

    必须收紧到「微信气泡绿」这一档，不能用「只要是绿色就算」——
    实测踩过：加油机照片顶部有一条绿色横幅「提枪加油」，
    宽松判绿会把它认成我方气泡，进而误判「我已经回过了」，整单丢失。

    微信绿大约 RGB(149,236,105)；照片里常见的绿是 RGB(76,175,80) 这类，
    红通道明显更低，用红通道下限就能分开。
    """
    t = th or THRESHOLDS
    pad_x, pad_y = 18, 8
    patch = _patch(arr, r["x1"] - pad_x, r["y1"] - pad_y,
                   r["x2"] + pad_x, r["y2"] + pad_y)
    if patch is None or patch.size == 0:
        return False

    pr = patch[:, :, 0].astype(int)
    pg = patch[:, :, 1].astype(int)
    pb = patch[:, :, 2].astype(int)

    mask = ((pg > t["green_g_min"]) & (pg > pr + t["green_dr"])
            & (pg > pb + t["green_db"])
            & (pr > t["green_r_min"]) & (pr < t["green_r_max"])
            & (pb > t["green_b_min"]) & (pb < t["green_b_max"]))

    hit = int(mask.sum())
    if hit < t["green_pixels"]:
        return False
    # 还要占足够比例，防止照片里一小块绿就触发
    ratio = hit / float(pr.size)
    return ratio >= t["green_ratio"]


# ---------------------------------------------------------------- 图片区块
def _dilate(mask, k):
    """二值图膨胀（方形核），用来把分散的内容块连成一片。"""
    out = mask.copy()
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
            out |= shifted
    return out


def _split_tall_box(gray, box, x0, y0, min_gap_ratio=0.03, blank_std=9.0):
    """
    把「几张图被膨胀连成一片」的框，按内部水平空白带切开。

    为什么需要：同一个人在群里连发两张照片时，两张之间只隔几十像素，
    二值化后的膨胀会把它们连成一个连通域。实测两张加油机照片（各约 220 高）
    被合成一个 y=245~704 的框（高 459），于是只会点开其中一张，
    另一张永远处理不到——不报错，就是漏了。

    只在「框高 > 宽 × 1.3」时才尝试切分：
    聊天里的照片多是方形或偏宽，高度超过宽度 1.3 倍的，几乎只能是竖向拼接。
    这样订单截图（宽大于高）不会被误切。
    """
    px1, py1 = int(box["x1"]), int(box["y1"])
    px2, py2 = int(box["x2"]), int(box["y2"])
    w, h = px2 - px1, py2 - py1
    if w <= 0 or h <= 0 or h <= w * 1.3:
        return [box]

    sub = gray[max(0, py1 - y0):py2 - y0, max(0, px1 - x0):px2 - x0]
    if sub.size == 0 or sub.shape[0] < 20:
        return [box]

    # 行方向标准差低 = 这一行几乎是纯色，可以当作两张图之间的空隙
    row_std = sub.std(axis=1)
    blank = row_std < blank_std

    gaps, inb, start = [], False, 0
    for i, v in enumerate(blank):
        if v and not inb:
            inb, start = True, i
        elif not v and inb:
            inb = False
            if i - start >= max(4, h * min_gap_ratio):
                gaps.append((start, i))
    if inb and len(blank) - start >= max(4, h * min_gap_ratio):
        gaps.append((start, len(blank)))

    if not gaps:
        return [box]

    cuts, prev = [], 0
    for g0, g1 in gaps:
        if g0 - prev > h * 0.12:
            cuts.append((prev, g0))
        prev = g1
    if h - prev > h * 0.12:
        cuts.append((prev, h))

    if len(cuts) <= 1:
        return [box]

    out = []
    for c0, c1 in cuts:
        out.append({"x1": float(px1), "y1": float(py1 + c0),
                    "x2": float(px2), "y2": float(py1 + c1),
                    "w": float(w), "h": float(c1 - c0)})
    return out


def detect_image_boxes(img, lines, layout, th=None):
    """
    在聊天区里找图片气泡。

    算法演进（两版都是踩坑换来的）：

    第一版用灰度标准差：绿气泡里的文字对比度也高，会把「绿气泡+图片」
    连成一大片，框出双倍高度。

    第二版改成「排除白底和绿底，剩下的算内容」：解决了绿气泡，但
    **白底的订单截图全废了**——截图大面积是白的，被当成气泡底色排掉，
    只剩二维码/文字那一小块，面积不够直接被过滤。
    真实订单截图恰恰绝大多数是白底，所以这一版等于对真实场景失效。

    现在这版换基准：先估计【聊天区背景色】，再找出「明显偏离背景」的区域。
    背景是浅灰（实测 ≈242），图片白是 255，差 13 —— 能分开；
    绿气泡单独排除；然后做膨胀把二维码、文字这些分散内容连成整张图。
    """
    t = th or THRESHOLDS
    arr = np.asarray(img.convert("RGB"))
    csx, hh = layout["chat_start_x"], layout["header_h"]
    # 下边界取聊天区真实底边（总高 − 输入区高）。
    # 之前用 total_h − input_h*0.70 算，得出 547，比真实底边 504 低了 43 像素：
    # 那 43 像素是输入框上方的分隔线与留白，会被当成「内容」并和图片连成一片，
    # 于是框一路拉到聊天区底部（实测框高 455，聊天区才 423）。
    bottom = int(layout.get("chat_box", (0, 0, 0,
                 layout["total_h_px"] - layout["input_h"]))[3])

    x0, y0 = int(csx), int(hh + 10)
    # 右边让出 16 像素：那里是聊天区的滚动条/窗口边框，每一行都有内容，
    # 会形成一条贯穿全高的竖直“脊梁”，把所有分散的内容连成一个大连通域，
    # 结果框从上边缘一直拉到下边缘（实测 241x413，图片其实只有 240x230）。
    x1, y1 = int(layout["total_w_px"]) - 16, int(bottom)
    if x1 <= x0 or y1 <= y0:
        return []

    sub = arr[y0:y1, x0:x1].astype(int)
    r, g, b = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]

    # 排除我方绿色气泡（微信那种亮绿）：整块绿底对比度高，不排掉会被当成图片
    near_green = ((g > 195) & (r < 200) & (b < 175)
                  & (g > r + 20) & (g > b + 20))
    gray = sub.mean(axis=2)
    gray = np.where(near_green, 250.0, gray)     # 绿底压成背景灰

    bs = t["block"]
    gh, gw = gray.shape[0] // bs, gray.shape[1] // bs
    if gh < 2 or gw < 2:
        return []

    # 判据用【块内灰度标准差】而不是「与背景色的差」。
    # 差的版本对背景估计太敏感：实测背景中位数 250，图片白 255 只差 5，
    # 根本分不开，结果只有二维码和文字被标出来，四周白底全丢。
    # 标准差的区分度大得多：图片区域实测 65，空白背景 16，一眼分得开。
    tiles = gray[:gh * bs, :gw * bs].reshape(gh, bs, gw, bs)
    mask = tiles.std(axis=(1, 3)) > t["block_std"]

    # 抹掉文字所在块（含少量外扩）
    pad = t["text_pad"]
    for rr in lines:
        if rr["cy"] < hh or rr["cy"] > bottom:
            continue
        bx1 = int((rr["x1"] - pad - x0) // bs)
        bx2 = int((rr["x2"] + pad - x0) // bs)
        by1 = int((rr["y1"] - pad - y0) // bs)
        by2 = int((rr["y2"] + pad - y0) // bs)
        bx1, by1 = max(0, bx1), max(0, by1)
        bx2, by2 = min(gw - 1, bx2), min(gh - 1, by2)
        if bx2 >= bx1 and by2 >= by1:
            mask[by1:by2 + 1, bx1:bx2 + 1] = False

    # 去噪：先去掉孤立的零星块。
    #
    # 为什么必须有这一步：聊天区里散落着时间戳、头像边缘、气泡描边这类
    # 只有一两个块的小点。它们本身无害，但下一步膨胀会把它们和图片连成一片，
    # 于是在图片上方凭空多出上百像素的空白（实测框从 y=95 一直拉到 y=543）。
    # 判据：一个块周围 8 邻域里至少还有 k 个块，才算「真的属于某块内容」。
    if t.get("img_denoise", 0):
        nb = np.zeros_like(mask, dtype=int)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                nb += np.roll(np.roll(mask, dy, axis=0), dx, axis=1).astype(int)
        mask = mask & (nb >= int(t["img_denoise"]))

    # 膨胀：图片里的二维码、文字、色块是分散的，不连起来会被切成碎片
    mask = _dilate(mask, int(t["img_dilate"]))

    visited = np.zeros_like(mask, dtype=bool)
    boxes = []
    gray = sub.mean(axis=2)

    for sy in range(gh):
        for sx in range(gw):
            if not mask[sy, sx] or visited[sy, sx]:
                continue
            stack = [(sy, sx)]
            visited[sy, sx] = True
            minx = maxx = sx
            miny = maxy = sy
            while stack:
                cy, cx = stack.pop()
                minx = min(minx, cx); maxx = max(maxx, cx)
                miny = min(miny, cy); maxy = max(maxy, cy)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < gh and 0 <= nx < gw and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))

            px1 = x0 + minx * bs
            py1 = y0 + miny * bs
            px2 = x0 + (maxx + 1) * bs
            py2 = y0 + (maxy + 1) * bs
            w, h = px2 - px1, py2 - py1

            if w < t["img_min_side"] or h < t["img_min_side"]:
                continue
            if w * h < t["img_min_area"]:
                continue
            aspect = w / float(h)
            if aspect > t["img_max_aspect"] or aspect < t["img_min_aspect"]:
                continue

            # 矩形内部要真的有内容。纯白空块（比如气泡里的留白）灰度几乎不变，
            # 拿来当图片去点开只会白折腾一次。
            patch = gray[max(0, py1 - y0):py2 - y0, max(0, px1 - x0):px2 - x0]
            if patch.size and patch.std() < t["img_min_std"]:
                continue

            # 向外放一点：检测抓的是「有颜色的内容」，图片四周的纯白边
            # （白 255 vs 背景 250，差 5 太小）抓不到，不放大会裁掉内容。
            ex = int(w * t.get("img_expand", 0.08))
            ey = int(h * t.get("img_expand", 0.08))
            px1 = max(x0, px1 - ex); py1 = max(y0, py1 - ey)
            px2 = min(x1, px2 + ex); py2 = min(y1, py2 + ey)
            w, h = px2 - px1, py2 - py1

            box = {"x1": float(px1), "y1": float(py1),
                   "x2": float(px2), "y2": float(py2),
                   "w": float(w), "h": float(h)}
            # 竖着连成一片的，按内部空白切开（同一人连发多张图的情况）
            for part in _split_tall_box(gray, box, x0, y0):
                if part["w"] >= t["img_min_side"] and part["h"] >= t["img_min_side"]:
                    boxes.append(part)

    return boxes


# ---------------------------------------------------------------- 昵称行
def detect_name_lines(lines, layout, th=None):
    """
    找群里的发送者昵称行。

    关键特征其实是【位置】：昵称贴在头像上方，x1 紧靠聊天区左边界；
    而消息正文都在更右边。字号靠不住——小窗口下昵称和正文一样高（实测 17px），
    按「昵称一定更小」去卡会把昵称全漏掉。

    所以判据改成：位置（x 偏移）为主 + 长度限制 + 高度只做宽松上限 + 排除绿色气泡。
    """
    t = th or THRESHOLDS
    csx = layout["chat_start_x"]

    in_chat = [r for r in lines if is_in_chat(r, layout)]
    if not in_chat:
        return []

    median_h = float(np.median([r["h"] for r in in_chat]))
    limit_h = min(t["name_max_h"] * 1.5,
                  max(median_h * t["name_h_factor"], 14.0))

    cands = []
    for r in in_chat:
        if r["h"] > limit_h:
            continue
        # 位置是最强特征
        if r["x1"] - csx > t["name_left_off"]:
            continue
        if r["w"] < t["name_min_w"]:
            continue
        if len(r["text"]) > t["name_max_len"]:
            continue
        if TIME_RE.match(r["text"].strip()):
            continue
        if _is_system_text(r["text"]):
            continue
        cands.append(r)

    return cands


def _nearest_name(name_lines, y):
    best = None
    for n in name_lines:
        if n["cy"] <= y and (best is None or n["cy"] > best["cy"]):
            best = n
    return best


# ---------------------------------------------------------------- 主解析
def parse_chat(img, lines, layout, th=None):
    """
    把画面解析成气泡序列（文字 + 图片 + 分界线），按 y 排序。

    返回 (bubbles, divider_y)
    """
    t = th or THRESHOLDS
    arr = np.asarray(img.convert("RGB"))

    divider_y = find_divider(lines, layout)
    names = detect_name_lines(lines, layout, t)

    bubbles = []

    if divider_y is not None:
        bubbles.append({"type": "divider", "side": None, "sender": None,
                        "text": "以下为新消息", "y_top": divider_y - 6,
                        "y_bottom": divider_y + 6, "x1": 0, "x2": 0})

    # 图片气泡先算出来，用来过滤「照片内部的文字」。
    # 实测踩过：加油机照片里有「金额」「油量」「提枪加油」，OCR 会把它们
    # 当成聊天消息，其中绿色横幅还会被误判成我方气泡，导致整单被跳过。
    img_boxes = detect_image_boxes(img, lines, layout, t)

    def _inside_image(r):
        for b in img_boxes:
            if (b["x1"] - 4 <= r["cx"] <= b["x2"] + 4
                    and b["y1"] - 4 <= r["cy"] <= b["y2"] + 4):
                return True
        return False

    # 文字行（排除顶栏、分界线、昵称行、图片内部文字）
    name_ids = {id(n) for n in names}
    text_rows = []
    for r in lines:
        if not is_in_chat(r, layout):
            continue
        if id(r) in name_ids:
            continue
        if _inside_image(r):
            continue
        if any(w in r["text"] for w in DIVIDER_WORDS):
            continue
        if r["text"] in SYSTEM_WORDS:
            continue
        if _is_system_text(r["text"]):
            continue
        if TIME_RE.match(r["text"].strip()):
            continue
        if any(r["text"].startswith(p) for p in
               ("[转账]", "微信转账", "已收款", "已被接收", "你撤回了一条消息")):
            continue
        if "拍了拍" in r["text"]:
            continue
        text_rows.append(r)

    text_rows.sort(key=lambda r: r["cy"])

    # 同侧相邻行合并成一个气泡
    merged = []
    for r in text_rows:
        side = "out" if is_green_bubble(arr, r, t) else "in"
        if (merged and merged[-1]["side"] == side
                and abs(r["cy"] - merged[-1]["y_bottom"]) < t["merge_gap_y"]):
            m = merged[-1]
            m["text"] = m["text"] + "，" + r["text"]
            m["y_bottom"] = r["y2"]
            m["x1"] = min(m["x1"], r["x1"])
            m["x2"] = max(m["x2"], r["x2"])
        else:
            merged.append({
                "type": "text", "side": side, "sender": None,
                "text": r["text"],
                "y_top": r["y1"], "y_bottom": r["y2"],
                "x1": r["x1"], "x2": r["x2"],
            })

    bubbles.extend(merged)

    # 图片气泡（复用上面已经算好的 img_boxes，避免重复计算）
    for b in img_boxes:
        csx = layout["chat_start_x"]
        side = "in" if b["x1"] < csx + 0.55 * (layout["total_w_px"] - csx) else "out"
        bubbles.append({
            "type": "image", "side": side, "sender": None, "text": "",
            "y_top": b["y1"], "y_bottom": b["y2"],
            "x1": b["x1"], "x2": b["x2"],
            "box": (int(b["x1"]), int(b["y1"]), int(b["x2"]), int(b["y2"])),
        })

    bubbles.sort(key=lambda b: b["y_top"])

    # 绑定发送者：对方气泡归属上方最近的昵称行
    for b in bubbles:
        if b["type"] == "text" and b["side"] == "out":
            continue
        if b["type"] == "divider":
            continue
        n = _nearest_name(names, b["y_top"])
        if n is not None and (b["y_top"] - n["y2"]) <= t["name_to_bubble_gap"]:
            b["sender"] = n["text"].strip() or None

    return bubbles, divider_y


def replied_image_boxes(bubbles, hints=None):
    """
    找出「下面已经跟着我方报价」的图片，返回这些图片的 box 集合。

    这是点开前的最后一道判重，判据是【语义】而不是图像相似度：
    我们给订单图的回复是一段含「订单合计 / 折后」的文本，它紧跟在被回复的图下面。

    ⚠ 为什么只在「一图一回复」严格交错时才算数：

    直觉写法是"图 A 下面（到下一张图之前）有我的报价，就算回复过"。这个写法会漏单。
    反例是用户连发两张、我们只回了第一张：
        图A → 图B → 「订单合计 700.00 …」
    那段回复夹在图 A、图 B 之间，按直觉写法会同时把**图 B** 也算成已回复，
    于是图 B 永远不会被处理——不报错、不打日志，就是少了一单。

    真实归属无法从画面判断（回复里没有"回复给谁"的信息），所以这里采取
    只会更保守的策略：只认 I R I R … 这一种严格交错模式。
    只要中间出现"两张图之间没有回复"，整屏直接放弃语义判定，
    交给缩略图指纹去逐张判——慢一点，但绝不会把没回复的图判成已回复。

    返回空集合表示"语义判定不可用"，调用方必须退回指纹判据。
    """
    if not hints:
        return set()

    seq = sorted(bubbles, key=lambda b: (b.get("y_top") or 0))

    done = set()
    expect_reply = False      # 刚吃过一张图，正在等它的回复
    last_img = None

    for b in seq:
        typ, side = b.get("type"), b.get("side")

        if typ == "image" and side == "in":
            if expect_reply:
                # 上一张图还没等到回复就来了下一张 —— 归属无法确定，整体放弃
                return set()
            expect_reply = True
            last_img = b
            continue

        if typ != "text" or side != "out":
            continue

        if not expect_reply:
            # 我方发的东西出现在"没有待回复图片"的位置（比如收款码前后的寒暄），
            # 与本次判定无关，跳过即可
            continue

        t = b.get("text") or ""
        if any(h and h in t for h in hints):
            done.add(last_img["box"])
        # 无论这条是不是报价，这张图的"待回复"窗口到此结束：
        # 后面再出现的回复属于更下面的消息，不能回头算给它
        expect_reply = False

    return done


def pending_from(bubbles, divider_y=None, last_cursor_y=None):
    """
    取出「需要处理的对方消息」。

    优先用分界线：分界线以下的全是未读。
    没有分界线时退回 last_cursor_y：游标以下算新。
    两者都没有时，取最后一个我方气泡之后的所有对方气泡。
    """
    if divider_y is not None:
        cand = [b for b in bubbles
                if b["type"] != "divider" and b["y_top"] >= divider_y - 4]
    elif last_cursor_y is not None:
        cand = [b for b in bubbles if b["y_top"] > last_cursor_y]
    else:
        last_out = -1
        for i, b in enumerate(bubbles):
            if b["type"] == "text" and b["side"] == "out":
                last_out = i
        cand = bubbles[last_out + 1:]

    return [b for b in cand if b.get("side") == "in"]


# ---------------------------------------------------------------- 自检
def _selfcheck():
    """用合成数据验证几何逻辑不崩，并给出关键判定结果。"""
    layout = {"chat_start_x": 320, "header_h": 85, "input_h": 160,
              "total_w_px": 1100, "total_h_px": 750}

    lines = [
        {"text": "五常大米下单群(128)", "score": .99, "x1": 340, "y1": 30,
         "x2": 520, "y2": 50, "cx": 430, "cy": 40, "w": 180, "h": 20},
        {"text": "以下为新消息", "score": .95, "x1": 500, "y1": 200,
         "x2": 620, "y2": 218, "cx": 560, "cy": 209, "w": 120, "h": 18},
        {"text": "张三", "score": .9, "x1": 332, "y1": 240,
         "x2": 356, "y2": 254, "cx": 344, "cy": 247, "w": 24, "h": 14},
        {"text": "合计 119.80", "score": .97, "x1": 350, "y1": 270,
         "x2": 470, "y2": 292, "cx": 410, "cy": 281, "w": 120, "h": 22},
    ]

    title = read_header(lines, layout)
    div = find_divider(lines, layout)
    names = detect_name_lines(lines, layout)

    ok = 0
    fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        ok += bool(cond)
        fail += (not cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name:34s} {detail}")

    chk("顶栏识别", title == "五常大米下单群(128)", f"-> {title!r}")
    chk("分界线定位", div is not None and abs(div - 209) < 1, f"-> {div}")
    chk("昵称行识别", len(names) == 1 and names[0]["text"] == "张三",
        f"-> {[n['text'] for n in names]}")

    # 合成一张纯色图，验证图片检测在无内容时不误报
    from PIL import Image
    flat = Image.new("RGB", (1100, 750), (245, 245, 245))
    boxes = detect_image_boxes(flat, lines, layout)
    chk("空白画面不误判为图片", len(boxes) == 0, f"-> {len(boxes)} 个")

    # 合成一张有内容块的图，应能检出
    noisy = Image.new("RGB", (1100, 750), (245, 245, 245))
    px = noisy.load()
    import random
    rnd = random.Random(42)
    for y in range(300, 600):
        for x in range(380, 700):
            v = rnd.randint(0, 255)
            px[x, y] = (v, v, v)
    boxes2 = detect_image_boxes(noisy, lines, layout)
    chk("有内容块能检出图片", len(boxes2) >= 1, f"-> {len(boxes2)} 个 {boxes2[:1]}")

    print(f"\n{ok} 通过, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(_selfcheck())

def sender_above(lines, layout, y_top, max_gap=90):
    """
    兜底：在给定 y 之上找一个"像昵称"的行，作为发送者。

    正常路径由 parse_chat 绑定昵称行，但它依赖全局的字号中位数等统计，
    画面里文字少的时候会失手（实测同一张图有时能读到 .M，有时读不到）。
    这里在单条气泡附近再找一次，条件放宽：
      · 位于气泡上方 max_gap 像素内
      · 水平位置靠左（贴近头像）
      · 长度短（昵称一般不超过 16 字）
    找不到就返回 None，不硬猜。
    """
    csx = layout["chat_start_x"]
    best = None
    for r in lines:
        if r["y2"] > y_top + 2:
            continue
        if y_top - r["y2"] > max_gap:
            continue
        if r["x1"] - csx > 90:
            continue
        if len(r["text"]) > 16:
            continue
        if TIME_RE.match(r["text"].strip()):
            continue
        if _is_system_text(r["text"]):
            continue
        if best is None or r["y2"] > best["y2"]:
            best = r
    return best["text"].strip() if best else None
