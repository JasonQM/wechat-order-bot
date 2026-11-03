"""
桌面操作层：扫侧边栏、切群、上下翻页、读当前群名。

与 window.py 的分工：
    window.py  只管「怎么抓、怎么点」这一层原语
    desktop.py 负责「业务上的动作」：我要切到某个群、我要往上翻一屏

所有"动作"都带验证：切群后必须复核顶栏，翻页后必须确认画面真的变了。
不做验证的动作等于赌博，而这里是不能赌的地方（发错群是不可逆事故）。
"""

import hashlib
import re
import time

import numpy as np
from PIL import Image

import ocr_engine
import perceive
import window as W

# 红点判定阈值
RED_STRONG = 15   # 正常未读（带数字）
RED_WEAK = 3      # 免打扰小圆点（无数字）

# 侧边栏里群名右边会跟着时间，OCR 常把两者粘成一行：「测试加油11:32」
TRAIL_TIME = re.compile(
    r"(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|昨天|前天|星期[一二三四五六日]|星期[一二三四五六日天])\s*$"
)
ELLIPSIS = ("...", "…", "..")
# 群名前可能带未读条数提示：「[2条]我观故自在」
UNREAD_PREFIX = re.compile(r"^\[\s*\d+\s*条\s*\]\s*")
# 消息预览的开头特征，用来避免把预览当成群名
PREVIEW_PREFIX = ("[图片]", "[视频]", "[文件]", "[语音]", "[动画表情]", "[链接]",
                  "[小程序]", "[位置]", "[名片]", "[转账]", "[红包]", "[聊天记录]")

# 实测（微信 4.1.15）：
#   · 会话列表：PostMessage 滚轮【有效】
#   · 聊天区：  PostMessage 滚轮【无效】，必须真的按 PageUp/PageDown
# 聊天区因此干脆不再尝试滚轮，见 scroll_chat。
CHAT_SCROLL_BY_KEY = True


# ---------------------------------------------------------------- 侧边栏
def _red_pixels(arr, name_line, layout):
    """数名字左边（头像右上角）红点区域里的红色像素数。"""
    x1 = max(0, int(name_line["x1"] - 36))
    x2 = max(0, int(name_line["x1"] - 3))
    y1 = max(0, int(name_line["y1"] - 20))
    y2 = min(layout["total_h_px"], int(name_line["y1"] + 20))
    if x2 <= x1 or y2 <= y1:
        return 0

    patch = arr[y1:y2, x1:x2]
    if patch.size == 0:
        return 0

    pr = patch[:, :, 0].astype(int)
    pg = patch[:, :, 1].astype(int)
    pb = patch[:, :, 2].astype(int)
    mask = (pr >= 200) & (pg <= 110) & (pb <= 110) & (pr > pg + 65)
    return int(mask.sum())


def sidebar_entries(img, lines, layout):
    """
    解析侧边栏会话列表。

    返回按 y 排序的列表，每项：
        text   会话名（尽量取该行最像名字的那一行）
        y      点击用 y 中心
        red    红色像素数（判断未读）
        unread "none" | "dot"（免打扰小圆点）| "count"（带数字）
    """
    arr = np.asarray(img.convert("RGB"))
    csx = layout["chat_start_x"]
    sbx = layout.get("sidebar_w", 0)
    top = layout["header_h"]
    bottom = layout["total_h_px"] - 24

    # 会话列表在「图标栏右边界」到「聊天区左边界」之间，
    # 图标栏本身没有文字，但为稳妥还是显式排除掉。
    rows = [r for r in lines
            if sbx < r["cx"] < csx and r["cy"] > top and r["cy"] < bottom]
    rows.sort(key=lambda r: r["cy"])
    if not rows:
        return []

    # 按垂直间距聚类成一个「会话项」
    clusters = []
    for r in rows:
        if clusters and (r["cy"] - clusters[-1][-1]["cy"]) < 34:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    entries = []
    for cl in clusters:
        # 会话项里字号最大的那一行通常是名字，下面一行是消息预览。
        # 但如果群名没被 OCR 读到（选中态是绿底白字，经常读不出来），
        # 簇里可能只剩预览行（[图片]、[2条]xxx）。这时要优先跳过预览样式的行。
        def _is_preview(t):
            t = (t or "").strip()
            return (any(t.startswith(p) for p in PREVIEW_PREFIX)
                    or re.match(r"^\[\s*\d+\s*条\s*\]", t) is not None)

        cands = [r for r in cl if not _is_preview(r["text"])]
        unreliable = not cands
        name_line = max(cands or cl, key=lambda r: (r["h"], -r["cy"]))

        red = _red_pixels(arr, name_line, layout)
        if red >= RED_STRONG:
            unread = "count"
        elif red >= RED_WEAK:
            unread = "dot"
        else:
            unread = "none"

        raw_text = name_line["text"].strip()
        clean, truncated = clean_sidebar_text(raw_text)

        # 点击点取「名字行」与「最后一行（消息预览）」的垂直中点，
        # 落在条目正中更保险；只点名字行容易贴着条目上边缘。
        y_click = (name_line["cy"] + max(r["cy"] for r in cl)) / 2.0

        entries.append({
            "text": clean,          # 已去掉尾部时间
            "text_raw": raw_text,   # 原始 OCR 文本，便于排查
            "truncated": truncated,  # 是否被微信截断（带省略号）
            "unreliable": unreliable,  # 群名没读到，只有预览行
            "y": float(name_line["cy"]),
            "y_click": float(y_click),
            "red": red,
            "unread": unread,
            "x1": name_line["x1"],
        })

    return entries


def clean_sidebar_text(text):
    """
    清理侧边栏 OCR 文本。

    微信会话项里，群名右边紧跟着时间，OCR 经常把两者识别成同一个文本块，
    于是拿到的是「测试加油11:32」。不剥掉时间，白名单就永远匹配不上，
    结果是整个群被静默跳过 —— 这是最隐蔽的漏单方式。

    返回 (cleaned, truncated)
    """
    s = str(text or "").strip()
    truncated = s.endswith(ELLIPSIS)

    # 反复剥：先剥未读条数前缀「[2条]」，再剥时间，可能露了省略号还要再剥
    for _ in range(3):
        before = s
        s = UNREAD_PREFIX.sub("", s)
        s = TRAIL_TIME.sub("", s).strip()
        for e in ELLIPSIS:
            if s.endswith(e):
                s = s[:-len(e)].strip()
                truncated = True
        # OCR 把省略号读成单个句点时也要剥掉，否则同一个群会生成两个 key
        s2 = re.sub(r"[.\u3002]+$", "", s).strip()
        if s2 != s:
            truncated = True
            s = s2
        if s == before:
            break

    return s, truncated


def find_entry(entries, title, conf_groups=None):
    """在侧边栏里找和给定群名等价的那一项（归一化后比较，人数差异不影响）。"""
    import groups as g_mod
    want = g_mod.normalize(title, conf_groups)
    if not want:
        return None
    for e in entries:
        if g_mod.normalize(e["text"], conf_groups) == want:
            return e
    return None


# ---------------------------------------------------------------- 画面签名
def chat_signature(img, layout):
    """聊天区的像素指纹，用于判断「画面变了没有」。"""
    x1, y1, x2, y2 = layout["chat_box"]
    crop = np.asarray(img.convert("RGB"))[int(y1):int(y2), int(x1):int(x2)]
    if crop.size == 0:
        return ""
    return hashlib.md5(crop.tobytes()).hexdigest()


# ---------------------------------------------------------------- 切群
def switch_to_group(hwnd, layout, entry, max_retry=2):
    """
    点侧边栏某一项，切过去，并用顶栏复核。

    返回 (ok, title)
    """
    import groups as g_mod

    click_x = int(layout["sidebar_w"] + 0.45 * (layout["chat_start_x"] - layout["sidebar_w"]))
    want = g_mod.normalize(entry["text"])
    click_y = int(entry.get("y_click", entry["y"]))
    title = ""

    for _ in range(max_retry + 1):
        W.post_click(hwnd, click_x, click_y)
        time.sleep(0.35)  # 等 Qt 渲染

        img, _, _ = W.capture(hwnd)
        if img is None:
            time.sleep(0.2)
            continue

        cur_layout = W.compute_layout(*img.size)
        lines = ocr_engine.ocr(img, max_width=1400)
        title = perceive.read_header(lines, cur_layout)

        got = g_mod.normalize(title)
        if got and (want in got or got in want):
            return True, title

    return False, title


def switch_to_group_by_name(hwnd, layout, rect, name, tries=3, conf_groups=None):
    """
    按群名切换：每一轮都重新在列表里定位，再点击、再复核顶栏。

    为什么不复用一开始拿到的 y：会话列表会因为新消息不断重排，
    滚过列表之后旧坐标就失效了 —— 拿旧坐标重试等于拿错坐标重试。
    每次都重新定位，才能保证点到的是当前屏幕上那个群。
    """
    last_title = ""
    for _ in range(tries):
        entry = locate_entry(hwnd, layout, rect, name, conf_groups=conf_groups)
        if entry is None:
            return False, last_title
        ok, last_title = switch_to_group(hwnd, layout, entry, max_retry=0)
        if ok:
            return True, last_title
    return False, last_title


def read_current_title(hwnd):
    """抓一张图，读当前群名。"""
    img, _, _ = W.capture(hwnd)
    if img is None:
        return None, None, None
    layout = W.compute_layout(*img.size)
    lines = ocr_engine.ocr(img, max_width=1400)
    return perceive.read_header(lines, layout), img, layout


# ---------------------------------------------------------------- 翻页
def _send_page_key(hwnd, layout, up=True):
    """
    在聊天区按 PageUp/PageDown 翻页。

    用 PostMessage 投递按键，【不把微信窗口抢到前台】。
    原来这里调 W.activate()，后果是机器人每翻一页就把微信顶到最前面，
    用户正在用别的软件时鼠标焦点被抢走——实测反馈"鼠标经常被切到微信"。
    扫描阶段本来就不需要窗口在前台：实测 PostMessage 的
    WM_KEYDOWN/WM_KEYUP 能让聊天区正常滚动，画面签名确实变了。
    只有真正发送消息（要模拟键盘输入）才需要 activate。
    """
    x = int(layout["chat_start_x"] + 0.5 * (layout["total_w_px"] - layout["chat_start_x"]))
    y = int(layout["header_h"] + 0.5 * (layout["total_h_px"] - layout["header_h"] - layout["input_h"]))
    W.post_click(hwnd, x, y)
    time.sleep(0.08)

    vk = 0x21 if up else 0x22  # PageUp / PageDown
    lp = (y << 16) | (x & 0xFFFF)
    W.user32.PostMessageW(hwnd, 0x0100, vk, lp)      # WM_KEYDOWN
    time.sleep(0.02)
    W.user32.PostMessageW(hwnd, 0x0101, vk, lp)      # WM_KEYUP
    time.sleep(0.30)
    return True


def _content_sig(img, layout, size=16, levels=8):
    """
    聊天区「内容」指纹：缩到 16×16 灰度再量化成 8 级。

    为什么不用全像素 md5：那个太灵敏。鼠标悬停在气泡上会让它变亮，
    输入框光标闪烁、消息里的动图、甚至亚像素抗锯齿，每一样都能让 md5 变化，
    于是"画面变没变"这个判断永远为"变了"——

      · scroll_chat 里滚轮明明无效，却被判成"滚动成功"，键盘兜底永不执行；
      · scroll_to_bottom 里则永远等不到"稳定"，白按满 10 次。
    两个症状合起来就是：机器人停在哪一屏就永远待在哪一屏。

    缩到 16×16 之后，局部高亮被平均掉，而"翻过一屏"这种整体位移一定会变。
    """
    x1, y1, x2, y2 = layout["chat_box"]
    crop = img.convert("L").crop((int(x1), int(y1), int(x2), int(y2)))
    if crop.size[0] < 4 or crop.size[1] < 4:
        return ""
    crop = crop.resize((size, size), Image.Resampling.BILINEAR)
    step = 256 // levels
    q = bytes(min(levels - 1, p // step) for p in crop.getdata())
    return hashlib.md5(q).hexdigest()


def scroll_chat(hwnd, layout, rect, up=True, notches=5, verify=True):
    """
    向上/向下翻一屏聊天记录。

    只用键盘 PageUp/PageDown，不再尝试滚轮。
    理由：PostMessage 投递的滚轮消息微信聊天区根本不响应（实测多次），
    而校验用的是内容指纹，滚轮"没响应但被判成功"会让键盘兜底永远轮不到执行。
    侧边栏另有一套（scroll_sidebar），那边滚轮是有效的，互不影响。
    """
    before = None
    if verify:
        img, _, _ = W.capture(hwnd)
        if img is not None:
            before = _content_sig(img, layout)

    _send_page_key(hwnd, layout, up=up)
    time.sleep(0.35)

    if verify:
        img3, _, _ = W.capture(hwnd)
        if img3 is None:
            return False, "pagekey"
        return _content_sig(img3, layout) != before, "pagekey"

    return True, "pagekey"


# 滚动结果三态。
#
# 早先只有 True/False，而 False 的判据是"画面从没变过"——
# 这样一来"滚到了底"和"滚不动了但没到头"（比如窗口被遮挡、
# PostMessage 没送达）会混成同一个 True，于是这一轮会被当成
# "扫描完整"，实际上新消息根本不在屏幕上。
# 只有确认过才能算完整，其余一律当不确定，让上层去打日志。
SCROLL_CONFIRMED = "confirmed"    # 画面连续不动，确认已到底
SCROLL_UNCERTAIN = "uncertain"    # 按满次数仍不稳定，不知道到没到底
SCROLL_FAILED = "failed"          # 画面压根没动过，滚动没生效


def scroll_to_bottom(hwnd, layout, rect, times=10):
    """
    强行滚到最底部。

    判据：连续两次 PageDown 之后聊天区像素签名没变 → 到底了。

    这里踩过一个很贵的坑：原来用 OCR 找「跳到最新消息」浮标来判断是否到底，
    找不到就认为"已经在底部"。但**微信 PC 版根本没有这个浮标**
    （那是手机版/Mac 版的），于是判断永远返回"已到底"，
    机器人从来没真正滚到底过。

    后果不是"慢"而是**致命**：机器人整天停在几屏之前的历史位置，
    最新的聊天记录压根不在屏幕上，客户新发的加油机照片永远看不见。
    外部表现就是"群里发了图，机器人一直没反应"。

    现在改成看画面是不是真的不动了：底部时 PageDown 不产生任何变化。
    不用滚轮（聊天区对 PostMessage 滚轮无响应），不用浮标（PC 版没有）。
    全程 PostMessage，不抢前台焦点。
    """
    prev = None
    still = 0

    for _ in range(times):
        img, _, _ = W.capture(hwnd)
        if img is None:
            return False
        lay = W.compute_layout(*img.size)
        sig = _content_sig(img, lay)

        if sig == prev:
            still += 1
            if still >= 2:
                return SCROLL_CONFIRMED
        else:
            still = 0
        prev = sig

        _send_page_key(hwnd, lay, up=False)

    # 到了次数上限还没稳定：只能说"翻动过"，不能说"翻到底了"。
    # 画面从没变过则是滚动完全没生效。
    return SCROLL_UNCERTAIN if prev else SCROLL_FAILED


# ---------------------------------------------------------------- 顶部分界线
def sidebar_signature(img, layout):
    """会话列表区域的像素指纹。"""
    x1, y1, x2, y2 = layout["sidebar_box"]
    crop = np.asarray(img.convert("RGB"))[int(y1):int(y2), int(x1):int(x2)]
    if crop.size == 0:
        return ""
    return hashlib.md5(crop.tobytes()).hexdigest()


def scroll_sidebar(hwnd, layout, rect, down=True, notches=3):
    """
    滚动会话列表。这是「群多到超出一屏」时唯一的解决办法。

    注意：滚的是会话列表区域，不是聊天区。
    """
    before = None
    img, _, _ = W.capture(hwnd)
    if img is not None:
        before = sidebar_signature(img, layout)

    cx = int((layout["sidebar_w"] + layout["chat_start_x"]) / 2)
    cy = int(layout["header_h"] + (layout["total_h_px"] - layout["header_h"]) / 2)

    W.post_wheel(hwnd, cx, cy, notches=(-notches if down else notches))
    time.sleep(0.30)

    img2, _, _ = W.capture(hwnd)
    if img2 is None:
        return False
    return sidebar_signature(img2, layout) != before


def collect_sidebar(hwnd, layout, rect, max_steps=8, conf_groups=None,
                    want_names=None):
    """
    把整个会话列表扫一遍（含滚动），返回 {归一化名: 条目}。

    先往上滚到顶，再一路往下收集。
    只靠当前可见的那 8-9 个会话是不够的——实测这台机器有 66 个会话，
    一屏只显示 9 个，其余不滚动就永远扫不到。

    want_names 给定时，一旦这些群都已被扫到就提前收工。
    为什么值得做：完整滚完列表要 16 帧、每帧一次 OCR（约 2 秒），总共 33 秒。
    但真正要看的往往就一两个群，它们通常在前几帧就出现了。
    提前退出把全量扫描从 33 秒压到 6-8 秒，跟踪延迟跟着一起降。
    """
    import groups as g_mod

    collected = {}
    want = [str(n) for n in (want_names or []) if n]

    def all_wanted_found():
        """白名单群是不是都已经扫到了（容忍侧边栏截断）。"""
        if not want:
            return False
        for name in want:
            hit = False
            for e in collected.values():
                v = g_mod.match_sidebar(e["text"], e.get("truncated", False),
                                        conf_groups)
                if v["allowed"] and g_mod.normalize(v["canonical"] or "", conf_groups) \
                        == g_mod.normalize(name, conf_groups):
                    hit = True
                    break
            if not hit:
                return False
        return True

    def take():
        img, _, _ = W.capture(hwnd)
        if img is None:
            return None, [], ""
        lay = W.compute_layout(*img.size)
        # 这里坚持用整窗 OCR，不用 region 裁侧边栏。
        # 实测裁剪只快 1.2 倍（RapidOCR 的耗时主要是模型推理的固定开销，
        # 跟像素多少关系不大），但识别结果会漂：同一次采样里
        # 「公众号」被裁成了「八月」、「小鲸出海1群（广」变成了「小鲸出海1群」。
        # 会话名是白名单匹配的输入，识别不稳比慢一点危险得多。
        lines = ocr_engine.ocr(img, max_width=1400)
        return lay, sidebar_entries(img, lines, lay), sidebar_signature(img, lay)

    # 注意：两个方向必须各自维护 seen_views。
    # 否则「往上滚到顶」的最后一帧和「开始往下滚」的第一帧签名相同，
    # 会被当成「画面没变」直接 break，结果只收集到第一屏（踩过）。
    for direction_down in (False, True):
        seen_views = set()
        for _ in range(max_steps):
            lay, entries, sig = take()
            if lay is None:
                break
            if sig in seen_views:
                break
            seen_views.add(sig)

            for e in entries:
                k = g_mod.normalize(e["text"], conf_groups)
                if not k:
                    continue
                # 同一个群出现多次时，保留有未读标记的那一份
                if k not in collected or (e["unread"] != "none"
                                          and collected[k]["unread"] == "none"):
                    collected[k] = e

            if all_wanted_found():
                return collected

            if not scroll_sidebar(hwnd, lay, rect, down=direction_down):
                break

    # 收尾：滚回列表顶部，让每次扫描结束时停在同一个位置。
    #
    # 这不是强迫症。扫描是「先上滚到顶、再下扫到底」，结束时停在底部；
    # 而像素哨兵靠「两轮画面指纹相同」判断无事可做。
    # 不回顶的话，每轮结束时侧边栏位置都不一样 → 指纹必然变 →
    # 哨兵永远判「有变化」→ 每轮都完整扫一遍，优化等于没做（踩过）。
    for _ in range(12):
        if not scroll_sidebar(hwnd, lay, rect, down=False):
            break

    return collected


def locate_entry(hwnd, layout, rect, name, max_steps=8, conf_groups=None):
    """
    在当前会话列表里定位某个群，必要时滚动查找，返回带【当前 y 坐标】的条目。

    不能拿之前收集到的 y 去点——滚动之后 y 全变了，会点到别的群。
    """
    import groups as g_mod
    want = g_mod.normalize(name, conf_groups)
    if not want:
        return None

    for direction_down in (False, True):
        for _ in range(max_steps):
            img, _, _ = W.capture(hwnd)
            if img is None:
                return None
            lay = W.compute_layout(*img.size)
            lines = ocr_engine.ocr(img, max_width=1400)
            for e in sidebar_entries(img, lines, lay):
                # 必须用 match_sidebar，不能用 normalize 全等比较。
                # 会话列表里的长群名会被微信截断成「A0000某某加油1510…」，
                # 全等永远对不上，结果就是群明明在列表里却"找不到"，
                # 切群、发图全部失败（踩过：发完文字后发不出图，就是因为这里）。
                v = g_mod.match_sidebar(e["text"], e.get("truncated", False),
                                        conf_groups)
                if not v["allowed"]:
                    continue
                # 再用 canonical 确认命中的确实是目标群，而不是别的同前缀群
                if g_mod.normalize(v.get("canonical") or "", conf_groups) == want:
                    return e
            if not scroll_sidebar(hwnd, lay, rect, down=direction_down):
                break

    return None


def snapshot_with_divider(hwnd, layout, rect, max_screens=5):
    """
    拍一张「能看到未读分界线」的快照。

    先在当前画面找「以下为新消息」；找不到就往上翻屏，直到找到或翻够次数。

    关键：返回的是【同一次抓图】里的图、OCR 结果、布局、分界线坐标。
    跨越多张截图拼坐标是错的——滚动之后 y 全变了。

    返回 (img, lines, layout, divider_y, screens)；失败时 img 为 None。
    """
    img = img_layout = None
    lines = []
    divider_y = None
    used = 0

    for i in range(max_screens + 1):
        img, _, _ = W.capture(hwnd)
        if img is None:
            return None, [], layout, None, i

        img_layout = W.compute_layout(*img.size)
        lines = ocr_engine.ocr(img, max_width=1400)
        divider_y = perceive.find_divider(lines, img_layout)
        used = i

        if divider_y is not None:
            break
        if i < max_screens:
            scroll_chat(hwnd, img_layout, rect, up=True, notches=5)

    return img, lines, img_layout, divider_y, used
