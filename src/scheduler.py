"""
调度器：把「看画面 → 取图 → 识别 → 算价 → 发送 → 落库」串成闭环。

几个关键取舍
------------
1. 用「缩略图感知哈希」做幂等，不用滚动位置的 y 坐标，也不用抓图后的 sha256
   （后者每次渲染都不一样，同一张订单会被记成多条）。
   坐标会随窗口大小、字体、滚动而变，哈希不会。同一个群同一张图永远只处理一次。

2. 每处理一张图都重新拍快照。
   因为发送消息会让微信自动滚回底部，屏幕状态被打断是常态，
   与其维护滚动游标，不如每次重拍 + 哈希去重。慢一点，但不会错。

3. 找不到「以下为新消息」分界线时才往上翻。
   分界线是微信自带的未读边界，比找自己上一条发言可靠得多。

4. 并发是被禁止的。翻页、切群、发送都要求独占界面状态。
"""

import hashlib
import json
import os
import time
from datetime import datetime

from PIL import Image

import config
import desktop
import groups as g_mod
import imgcap
import ocr_engine
import perceive
import pricing
import recognize
import sender
import single_instance
import store
import window as W


def log(msg, path=None):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        if path is None:
            p = config.load()["paths"].get("logs", "logs")
            base = config.path_of(p)
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, f"bot-{datetime.now():%Y-%m-%d}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _log_image_info(path):
    """
    打印固定图片的实际信息（路径 / 大小 / 指纹 / 修改时间），并返回指纹。

    为什么要打：固定图片被换掉后如果出问题，外部看起来只有一种现象——
    「发的还是旧图」。但原因至少有三种：配置没指过去、上传没落盘、
    或者字节没变（上传的本来就是同一张）。不把指纹打出来就只能猜。
    指纹同时会写进订单记录，事后能查这一单到底发了哪张图。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            fp = hashlib.md5(f.read()).hexdigest()[:8]
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
        log(f"    固定图片：{os.path.basename(path)}  {size / 1024:.0f}KB  "
            f"指纹={fp}  改于={mtime}")
        return fp
    except Exception as e:
        log(f"    固定图片：读取信息失败（{e}）")
        return ""


def _box_overlap(a, b):
    """两个矩形的交并比（IoU）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / float(ua) if ua > 0 else 0.0


def content_key_image(img, size=16):
    """
    图片内容指纹（dHash 差异哈希，16×16 = 240 位）。

    为什么不用 sha256：同一张图两次抓下来，字节数都会差几个
    （实测 1491912 vs 1491910），sha256 必然不同，去重直接失效，同一单被记 3 条。

    为什么不用 8×8 均值哈希：太粗。订单一类的图（大片白底 + 中间内容）
    很容易撞车，两张不同的订单算出同一个指纹，就会漏单。

    dHash 比的是「相邻像素谁更亮」，对亮度变化不敏感、对结构变化敏感，
    区分度和稳定性兼顾。
    """
    g = img.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
    px = list(g.getdata())
    bits = []
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits.append("1" if px[base + col] > px[base + col + 1] else "0")
    return f"{int(''.join(bits), 2):060x}"


def content_key_file(path, size=16):
    from PIL import Image
    with Image.open(path) as im:
        return content_key_image(im, size)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


THUMB_CROP_SIDE = 128


def thumb_key(img, box, size=16):
    """
    用聊天窗口里那张缩略图算 dHash，作为「点开之前」的判重键。

    两个设计要点，都是踩坑换来的：

    1) 必须和 content_key_image 用同一种算法（dHash 16×16 十六进制）。
       早先这里用「12×12 灰度 + 8 级量化 + md5」，键空间和库里存的 dHash
       完全不兼容，拿它去查库永远查不到，于是每张图都只能先点开、抓图、
       再判重——这正是"图片被反复打开"的根因。

    2) 只取【中心正方形】，不裁整个框。图片框是靠像素标准差检测出来的，
       白底照片和聊天背景只差几个灰阶，同一张图两次检测的边界能差二三十像素
       （实测同一个位置一次 259 高、一次 292 高）。整框裁下来 dHash 就整个变了，
       判重随之失效。取中心固定大小的正方形，边缘抖动被挡在外面。
    """
    from PIL import Image

    x1, y1, x2, y2 = [int(v) for v in box]
    w, h = img.size
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    bw, bh = x2 - x1, y2 - y1
    if bw < 16 or bh < 16:
        return ""

    side = int(min(bw, bh) * 0.80)
    if side < 12:
        return ""
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

    # 裁一块【固定像素】的正方形，边长不跟着检测框走。
    #
    # 这一条是判重能不能生效的分水岭。图片框是像素检测出来的，同一张白底照片
    # 两次检测的高度实测能差 13~33 像素（168 / 181 / 172）。如果裁剪边长
    # 跟着框高变化，同一张图算出来的就不是同一块区域，dHash 直接南辕北辙
    # （实测三张同一张照片算出 3a20…、1cff…、38fe… 三个完全不同的值）。
    # 固定边长后，框抖动只让取样窗口整体平移十几像素，dHash 只动两三位。
    side = min(THUMB_CROP_SIDE, bw, bh)
    hx = side // 2

    crop = img.crop((cx - hx, cy - hx, cx - hx + side, cy - hx + side))
    crop = crop.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
    px = list(crop.getdata())
    bits = []
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits.append("1" if px[base + col] > px[base + col + 1] else "0")

    val = int("".join(bits), 2)

    # 全 0 或全 1 说明这块区域几乎是纯色（白墙、黑边、纯背景）。
    #
    # dHash 比的是"相邻像素谁更亮"，纯色区里左右一样亮，全部算 0，
    # 于是**任何两张纯色图都会得到同一个键**，判重就变成
    # "任意两张白底截图都是同一单"。实测某张 2025×1227 的截图中心
    # 正好落在纯白区域（灰度 min=max=255），算出 60 个 0。
    #
    # 这种键不携带任何信息，返回空串表示"不可用作判重依据"：
    # 调用方会照常点开、按内容哈希兜底，只是少省一次点击，不会误判。
    if val == 0 or val == (1 << (size * size)) - 1:
        return ""
    return f"{val:060x}"


class Bot:
    def __init__(self, override_dry_run=None, only_group=None, sweep=False, deep=False):
        self.conf = config.load()
        self.groups_conf = g_mod.load_conf()
        self.dry_run = (self.conf["safety"]["dry_run"]
                        if override_dry_run is None else override_dry_run)
        self.only_group = only_group
        self.sweep = sweep
        self.deep = deep
        self.last_full_scan = 0.0
        self.force_full_scan = False
        self.last_stats = {}
        # 像素哨兵：上一轮看到的画面指纹，以及上次「强制完整扫描」的时间
        self.last_view_sig = None
        self.last_force_full = 0.0
        self.handled = {}         # dHash -> 订单号。本次运行内抓过图的，避免重复抓
        self.thumb_attempts = {}  # 缩略图 dHash -> 已尝试次数（进程内累积，防死循环）
        self.last_send_ts = 0.0
        self.group_send_times = {}

        store.init_db()

        self.img_dir = config.path_of(self.conf["paths"]["images"])
        os.makedirs(self.img_dir, exist_ok=True)

    # ------------------------------------------------------------ 限速
    def _wait_rate_limit(self, group_name):
        s = self.conf["safety"]
        gap = float(s.get("send_interval_sec", 8))
        jitter = float(s.get("send_interval_jitter", 3))
        import random
        need = gap + random.uniform(-jitter, jitter)
        elapsed = time.time() - self.last_send_ts
        if elapsed < need:
            time.sleep(need - elapsed)

        limit = int(s.get("per_group_hourly_limit", 30))
        now = time.time()
        lst = [t for t in self.group_send_times.get(group_name, []) if now - t < 3600]
        if len(lst) >= limit:
            raise RuntimeError(
                f"群「{group_name}」本小时已发送 {len(lst)} 条，达到上限 {limit}，暂停")
        self.group_send_times[group_name] = lst

    def _mark_sent(self, group_name):
        self.last_send_ts = time.time()
        self.group_send_times.setdefault(group_name, []).append(self.last_send_ts)

    # ------------------------------------------------------------ 整屏快照
    def _snapshot(self, hwnd):
        """
        抓一屏并解析成气泡。整窗 OCR 是这一层最贵的操作（1.5~2.5 秒），
        所以结果要被本屏所有图片共用，不能每张图都重来一次。

        返回 None 表示抓图失败（窗口被遮挡/最小化），调用方必须停下。
        """
        try:
            img, _, _ = W.capture(hwnd)
            if img is None:
                return None
            lay = W.compute_layout(*img.size)
            lines = ocr_engine.ocr(
                img, max_width=self.conf["ocr"]["window_max_width"])
            bubbles, divider_y = perceive.parse_chat(img, lines, lay)
            return {"img": img, "lay": lay, "lines": lines,
                    "bubbles": bubbles, "divider_y": divider_y}
        except Exception as e:
            log(f"  ⚠ 整屏解析失败：{e}")
            return None

    def _view_restored(self, hwnd, snap, tol=4):
        """
        点开图片、又关掉预览窗之后，判断聊天区是不是回到了原样。

        判据用【图片框的位置集合】，不用全像素 md5。
        md5 会因为鼠标悬停高亮、输入框光标闪烁这类无意义的变化而抖动，
        一抖就判定"画面变了"→ 重新整窗 OCR → 白花两秒，
        而实际上坐标根本没动。用框位置（容差 4px）既够灵敏又不会被噪声骗。
        """
        try:
            img, _, _ = W.capture(hwnd)
            if img is None or img.size != snap["img"].size:
                return False

            boxes = perceive.detect_image_boxes(img, snap["lines"], snap["lay"])
            base = [b["box"] for b in snap["bubbles"] if b["type"] == "image"]
            if len(boxes) != len(base):
                return False

            cur = sorted((int(b["x1"]), int(b["y1"]), int(b["x2"]), int(b["y2"]))
                         for b in boxes)
            old = sorted((int(b[0]), int(b[1]), int(b[2]), int(b[3]))
                         for b in base)
            for a, b in zip(cur, old):
                if max(abs(a[i] - b[i]) for i in range(4)) > tol:
                    return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 单张图
    def _process_one_image(self, hwnd, layout, rect, group_name, bubble, tkey=None):
        """
        处理一张图片气泡：抓图 → 去重 → 识别 → 算价 → 发送 → 落库。

        tkey 是缩略图的感知哈希，用作幂等键。

        为什么不用抓下来那张图的 sha256 做幂等键：
        每次抓同一张预览图，抗锯齿和渲染会有细微差别，sha256 每次都不同，
        结果同一张订单被记成好几条（实测产生过 #46/#47/#48 三条重复）。
        感知哈希只看画面内容，12×12 量化后细微差异被抹平，才稳定。
        """
        t0 = time.time()
        day = datetime.now().strftime("%Y-%m-%d")
        out_dir = os.path.join(self.img_dir, day)
        tmp_name = f"tmp_{int(time.time() * 1000)}.png"
        tmp_path = os.path.join(out_dir, tmp_name)

        grab = imgcap.grab_image(hwnd, bubble["box"], tmp_path)
        if not grab["ok"]:
            log(f"  ✗ 取图失败：{grab['note']}")
            return False, "grab_failed"

        # 指纹在【抓到大图之后】算，用 dHash 而不是 sha256：
        # 同一张图两次抓下来字节数都会差几个，sha256 必然不同，去重会失效。
        try:
            dedupe_key = content_key_file(tmp_path)
        except Exception:
            dedupe_key = tkey or sha256_file(tmp_path)

        if dedupe_key in self.handled:
            # 本次运行已经抓过这张图。顺手把它这处缩略图记下来，
            # 这样下一轮（甚至下一次翻页）就能在【点开之前】判出来。
            oid = self.handled.get(dedupe_key)
            if tkey:
                store.add_seen_thumb(group_name, tkey, oid)
            os.remove(tmp_path)
            return False, "dup_session"

        existed = store.find_by_sha(group_name, dedupe_key)
        if existed:
            st = existed["status"]
            # 记下这处观察。同一张照片可能被连发好几次、占好几个位置，
            # 每处算出的缩略图指纹都不同，所以这里按"位置"记多行，
            # 而不是往订单上覆盖一个值。
            if tkey:
                store.add_seen_thumb(group_name, tkey, existed["id"])
            # 已发送 / 待人工：跳过，绝不重复
            if st in ("auto_sent", "manual_sent", "manual_pending"):
                os.remove(tmp_path)
                self.handled[dedupe_key] = existed["id"]
                log(f"  · 这张图处理过（订单 #{existed['id']}，{st}），跳过")
                return False, "dup_db"
            # processing：上次识别到一半断了（异常、断电、被强杀）。
            # 这条是"僵尸单"，删掉它重新走一遍，否则金额永远空着、也没人认领。
            if st == "processing":
                log(f"  · 订单 #{existed['id']} 停在「识别中」，重建这一单")
                store.delete_order(existed["id"])
                existed = None

            # ready：识别已完成、只是还没发出去 —— 接着发，不重新建单
            if existed and st == "ready" and existed["final_cents"]:
                os.remove(tmp_path)
                self.handled[dedupe_key] = existed["id"]
                log(f"  · 订单 #{existed['id']} 已识别待发送，续跑发送")
                return True, self._reply_order(
                    hwnd, layout, rect, group_name, existed["id"],
                    existed["sender"], existed["final_cents"])

        final_path = os.path.join(out_dir, f"{dedupe_key}.png")
        os.replace(tmp_path, final_path)

        sender_name = bubble.get("sender")
        rel_path = os.path.relpath(final_path, os.path.join(config.ROOT, "data"))
        log(f"  → 取到图 {os.path.basename(final_path)}（{grab['mode']}）发送者={sender_name}")

        # ---------- 第一阶段：先落单 ----------
        # 先把"这笔订单存在过"记下来，后面识别成不成功都有据可查。
        # 中途崩溃也不会凭空消失，下次还能接着处理。
        order_id = store.create_order(
            group_name=group_name,
            sender=sender_name,
            image_path=rel_path,
            image_sha256=dedupe_key,
            note="已抓图，等待识别",
        )
        self.handled[dedupe_key] = order_id
        if tkey:
            store.add_seen_thumb(group_name, tkey, order_id)
        log(f"  · 已建单 #{order_id}（待识别）")

        # ---------- 第二阶段：识别 ----------
        #
        # 必须包 try：识别会调云端模型，超时、限流、返回格式异常都可能抛错。
        # 不包的话异常一路冒到最外层，订单就永远停在 processing（"识别中"），
        # 而它的指纹又已经在 self.handled 里，下一轮只会说"处理过"直接跳过——
        # 从用户角度看就是这一单凭空消失了，日志里只有一条笼统的本轮异常。
        try:
            rec = recognize.recognize_order(final_path, self.conf)
        except Exception as e:
            reason = f"识别异常：{type(e).__name__}: {e}"
            store.update_order(
                order_id,
                status="manual_pending",
                at_result="skipped",
                elapsed_ms=int((time.time() - t0) * 1000),
                detail_check="fail",
                note=reason[:500],
            )
            self.handled.pop(dedupe_key, None)  # 放出去，允许下一轮重试
            log(f"  ✗ 订单 #{order_id} {reason}")
            return True, "recognize_error"

        if not rec["ok"]:
            reason = "；".join(rec["reasons"]) or "识别未通过"
            if rec.get("warnings"):
                reason += "｜提示：" + "；".join(rec["warnings"])
            store.update_order(
                order_id,
                ocr_total_cents=rec["total_cents"],
                status="manual_pending",
                at_result="skipped",
                elapsed_ms=int((time.time() - t0) * 1000),
                detail_check="fail",
                min_confidence=rec["confidence"],
                ocr_text=rec["ocr_text"][:8000],
                model_json=str(rec["raw_json"])[:8000],
                note=reason[:500],
            )
            log(f"  ⚠ 订单 #{order_id} 转人工：{reason}")
            return True, "manual_pending"

        total_cents = rec["total_cents"]
        final_cents = pricing.discount_cents(total_cents, self.conf["discount_percent"])
        reply_text = pricing.build_reply(sender_name, total_cents, final_cents, self.conf)

        # 识别成功：先把金额写进去，此时还没回复
        store.update_order(
            order_id,
            ocr_total_cents=total_cents,
            discount_rate=self.conf["discount_percent"] / 100.0,
            final_cents=final_cents,
            min_confidence=rec["confidence"],
            detail_check="pass",
            ocr_text=rec["ocr_text"][:8000],
            model_json=str(rec["raw_json"])[:8000],
            elapsed_ms=int((time.time() - t0) * 1000),
            status="ready",
            note="识别完成，待发送",
        )
        log(f"  · 订单 #{order_id} 识别完成：{pricing.format_cents(total_cents)} → "
            f"{pricing.format_cents(final_cents)} 元")

        # ---------- 第三阶段：回复 ----------
        return True, self._reply_order(hwnd, layout, rect, group_name,
                                       order_id, sender_name, final_cents)

    def _reply_order(self, hwnd, layout, rect, group_name, order_id,
                     sender_name, final_cents):
        """
        给一笔已识别完的订单发回复。发送成功标记 replied=1。

        抽成独立方法，是因为它可以被两条路径调用：
          · 刚识别完，直接发
          · 上次识别完但没发成（status=ready），这次补发
        """
        if self.dry_run:
            store.mark_status(order_id, "ready", "dry_run：只识别不发送")
            log("  · dry_run，未发送")
            return "ready"

        conf = self.conf
        # 文案里的「合计」用订单里存的原始金额，不用折扣反推（反推会有分位误差）
        row = store.fetch_order(order_id)
        reply_text = pricing.build_reply(sender_name, row["ocr_total_cents"],
                                         final_cents, conf)

        at_result = "skipped"
        status = "auto_sent"
        note = ""
        reply_image_fp = ""

        # 记住用户当前在用的窗口，发完还回去。
        # 发消息必须把微信拿到前台（要模拟键盘输入），但没理由霸占着不放。
        # 参考项目也是这么做的（发完 SetForegroundWindow(原窗口)）。
        try:
            prev_foreground = W.user32.GetForegroundWindow()
        except Exception:
            prev_foreground = 0

        try:
            self._wait_rate_limit(group_name)

            if conf["reply"].get("mention") and sender_name:
                ok_at = sender.mention(hwnd, layout, sender_name)
                at_result = "ok" if ok_at else "degraded"

            r1 = sender.send_text(hwnd, layout, reply_text,
                                  group_name=group_name, force=True)
            if not r1.get("ok"):
                status = "failed"
                note = r1.get("note", "")
            else:
                time.sleep(0.6)
                img_path = config.path_of(conf["reply"].get("image", ""))
                if img_path and os.path.isfile(img_path):
                    # 把「实际发的是哪个文件」写进日志。
                    # 这一步不是调试用的花活：固定图片被换掉时，如果只在代码里
                    # 拼路径、不打日志，出问题就只能靠猜——是配置没生效、
                    # 上传没落盘、还是发了缓存的旧图，三者在外部看起来一模一样。
                    reply_image_fp = _log_image_info(img_path)
                    r2 = sender.send_image(hwnd, layout, img_path,
                                           group_name=group_name, force=True,
                                           rect=rect)
                    if not r2.get("ok"):
                        note = "文本已发，图片失败：" + r2.get("note", "")
                        reply_image_fp = ""     # 没发成，别把这单记成「发过这张图」
                    elif r2.get("verified") is False:
                        note = "文本已发；图片未检出增量，请人工确认"
                else:
                    note = "未配置固定图片，只发了文本"
                self._mark_sent(group_name)
        except RuntimeError as e:
            status = "manual_pending"
            note = str(e)
        except Exception as e:
            status = "failed"
            note = f"发送异常：{e}"
        finally:
            # 无论成功失败，都把前台还给用户原来的窗口
            try:
                if prev_foreground and prev_foreground != hwnd:
                    W.user32.SetForegroundWindow(prev_foreground)
            except Exception:
                pass

        store.update_order(order_id, at_result=at_result,
                           reply_image_fp=reply_image_fp or None)
        if status in ("auto_sent", "manual_sent"):
            store.mark_replied(order_id, status, note[:500] or None)
            log(f"  ✓ 订单 #{order_id} 已回复 {pricing.format_cents(final_cents)} 元 {note}")
        else:
            store.mark_status(order_id, status, note[:500] or None)
            log(f"  ✗ 订单 #{order_id} {status}：{note}")

        return status

    # ------------------------------------------------------------ 单个群
    def process_group(self, hwnd, layout, rect, entry, skip_switch=False):
        """
        处理一个群。返回本群处理的单数。

        skip_switch=True 时不去点侧边栏，直接处理当前已打开的群。
        用于「当前顶栏就是这个群」的情况——选中态是绿底白字，
        侧边栏 OCR 常常读不出群名，光靠列表会把它漏掉。
        """
        # entry 可以是侧边栏条目（dict），也可以直接是群名（str）。
        # 推荐传白名单里的【完整群名】：侧边栏宽度有限，长群名会被微信
        # 截断成「A0000某某加油1510…」，拿这串去定位条目、点击、
        # 再用顶栏复核，全程对不上，结果就是"切群失败（顶栏读作「」）"，
        # 第二个群永远进不去 —— 客户在那边发图自然一直没反应。
        if isinstance(entry, dict):
            group_name = entry.get("text") or ""
        else:
            group_name = entry or ""

        if skip_switch:
            title, img0, lay0 = desktop.read_current_title(hwnd)
            if not title:
                return 0
            verdict = g_mod.match(title, self.groups_conf)
            if not verdict["allowed"]:
                return 0
            group_name = verdict["canonical"] or title
            log(f"\n▶ 当前已打开「{group_name}」，直接处理")
        else:
            log(f"\n▶ 进入群「{group_name}」")
            ok, title = desktop.switch_to_group_by_name(
                hwnd, layout, rect, group_name, conf_groups=self.groups_conf)
            if not ok:
                log(f"  ✗ 切群失败（顶栏读作「{title}」），跳过")
                return 0

            # 校验这个群是否在白名单里（侧边栏 OCR 可能认错字）
            verdict = g_mod.match(title, self.groups_conf)
            if not verdict["allowed"]:
                log(f"  · 顶栏「{title}」不在白名单（{verdict['reason']}），跳过")
                return 0
            group_name = verdict["canonical"] or title

        canonical = group_name

        # 进群先确认视图在最底部。
        # 如果停在上面的历史位置，新消息根本不在屏幕上，会被当成"没有新消息"。
        try:
            if not desktop.scroll_to_bottom(hwnd, layout, rect):
                log("  ⚠ 未能确认滚到最底部，本轮可能看不到最新消息")
        except Exception as e:
            log(f"  ⚠ 滚到底部时出错：{e}")
        max_screens = int(self.conf["safety"].get("max_scroll_screens", 5))
        count = 0

        # 回复文案里用来认出「这条是我发的报价」的关键词。
        # 判据见 perceive.replied_image_boxes：图片下面跟着我方报价文本，
        # 就说明这张图处理过了——不看屏幕坐标、不算图像相似度、根本不用点开。
        hints = self.conf["reply"].get("detect_keywords") or ["订单合计", "折后"]

        max_attempt = int(self.conf["safety"].get("max_grab_attempts", 3))
        max_grabs = int(self.conf["safety"].get("max_grabs_per_group", 8))

        # 整个群这一轮内试过的缩略图指纹。跨屏保留：
        # 往上翻页后同一张图会落在不同 y 坐标，但指纹不变，不该再点一次。
        attempted = set()
        done_boxes = []
        clean_screens = 0     # 连续多少屏"一张要处理的图都没有"
        did_scroll = False    # 本轮是否真的往上翻过页（决定收尾要不要滚回底部）

        try:
            for page in range(max_screens + 1):
                page_actionable = False
                grabs = 0

                # 整屏只做一次 capture + OCR，供本屏所有图复用。
                #
                # 这是"慢"的主因：原来每处理一张图（哪怕只是判定"这张处理过"）
                # 都要重新抓图并整窗 OCR 一遍，一次 1.5~2.5 秒。屏幕上挂着 8 张旧图时，
                # 光确认它们都处理过就要二十秒，一轮下来三四十秒全是空转。
                snap = self._snapshot(hwnd)
                if snap is None:
                    break

                for _round in range(max_grabs + 2):
                    if grabs >= max_grabs:
                        log(f"  · 本屏已处理 {grabs} 张，达到上限，先收工")
                        break

                    bubbles = snap["bubbles"]
                    img = snap["img"]

                    imgs = [b for b in bubbles
                            if b["type"] == "image" and b.get("side") == "in"]
                    imgs.sort(key=lambda b: b["y_top"])

                    # 下方已经有我方报价的图：这是最可靠的"已处理"判据
                    replied = perceive.replied_image_boxes(bubbles, hints)

                    target = None
                    target_key = None

                    for b in imgs:
                        k = thumb_key(img, b["box"])
                        if not k:
                            continue
                        if k in attempted:
                            continue
                        if self.thumb_attempts.get(k, 0) >= max_attempt:
                            continue
                        if any(_box_overlap(b["box"], d) > 0.4 for d in done_boxes):
                            attempted.add(k)
                            continue

                        # ① 语义判据：图下面跟着我的报价 → 处理过了，绝不点开
                        if b["box"] in replied:
                            attempted.add(k)
                            log("  · 该图下方已有我的报价，跳过（未点开）")
                            continue

                        # ② 指纹判据：这处缩略图已经对应过订单
                        rec = store.find_by_thumb(canonical, k)
                        if rec and rec["status"] in (
                                "auto_sent", "manual_sent", "manual_pending"):
                            attempted.add(k)
                            log(f"  · 该图已建单（订单 #{rec['order_id']}，"
                                f"{rec['status']}），跳过（未点开）")
                            continue

                        target, target_key = b, k
                        break

                    if target is None:
                        break

                    attempted.add(target_key)
                    self.thumb_attempts[target_key] = \
                        self.thumb_attempts.get(target_key, 0) + 1
                    done_boxes.append(target["box"])
                    if len(done_boxes) > 40:
                        done_boxes.pop(0)

                    grabs += 1
                    handled, status = self._process_one_image(
                        hwnd, snap["lay"], rect, canonical, target,
                        tkey=target_key)

                    if handled:
                        count += 1
                        page_actionable = True
                    else:
                        log(f"  · 本张跳过（{status}），继续下一张")

                    # 发过消息 → 微信自动滚回底部，这一屏的坐标全部作废
                    if status in ("auto_sent", "manual_sent"):
                        time.sleep(0.2)
                        snap = self._snapshot(hwnd)
                        if snap is None:
                            break
                        continue

                    # 只是点开又关掉 → 画面应当复原。复原就继续用同一份解析结果，
                    # 省掉一次整窗 OCR；没复原说明有残留窗口或滚动，必须重拍。
                    if self._view_restored(hwnd, snap):
                        continue

                    snap = self._snapshot(hwnd)
                    if snap is None:
                        break

                # ---------------- 翻页 ----------------
                #
                # 这里必须往上翻，不能"本屏没活干就收工"。
                # 反例就是最常见的稳态：客户发图 → 我回报价文本 → 我发收款码。
                # 滚到最底部时，屏幕上全是**我的**收款码，客户那张订单图在上一屏。
                # 如果首屏没看到要处理的图就退出，等于永远看不见订单。
                #
                # 但又不能无脑翻满 5 屏：空群也要花 5 次整窗 OCR（十几秒）白跑。
                # 折中——连续两屏都"一张要处理的图都没有"才停。
                # 这样首屏 + 上一屏（订单图所在的那屏）都会被看过，
                # 再往上就确实是处理过的历史了。
                if page_actionable:
                    clean_screens = 0
                else:
                    clean_screens += 1
                    if clean_screens >= 2 and not self.deep:
                        break

                if snap is None:
                    break
                if snap["divider_y"] is not None:
                    break       # 已经到未读起点，不用再往上翻

                moved, _ = desktop.scroll_chat(hwnd, snap["lay"], rect,
                                               up=True, notches=5)
                if not moved:
                    break
                did_scroll = True
        finally:
            # 只有真的翻过页才需要滚回底部。
            # 没翻页时画面本来就停在最新位置，再滚一遍等于白花一次
            # "按 PageDown + 等两拍确认到底"（实测 1 秒），
            # 而这是每轮都要付的固定成本。
            if did_scroll:
                # 无论如何都滚回底部，否则下次会从历史位置开始，大面积误读
                try:
                    desktop.scroll_to_bottom(hwnd, layout, rect)
                except Exception:
                    pass

        log(f"◀ 离开群「{group_name}」，处理 {count} 单")
        return count

    # ------------------------------------------------------------ 单轮
    def run_once(self):
        """扫一遍所有群。返回本轮处理的单数。"""
        # 每轮重新读配置：这样在 Web 面板改了固定图片、文案、折扣、群名单，
        # 下一轮就生效，不需要重启机器人。
        self.conf = config.load()
        self.groups_conf = g_mod.load_conf()

        win = W.pick_main_window()
        if not win:
            # 这里必须抛异常，不能 return 0。
            # 返回 0 会被上层当成「本轮没活干」，不计入失败次数，
            # 于是连续失败退避永远触发不了 —— 实测微信被关掉之后，
            # 这里每秒写一条「没找到微信窗口」，一晚上写了 3.6 万条、2.8MB 日志。
            raise RuntimeError("没找到微信窗口，请确认微信已启动并登录")

        hwnd = win["hwnd"]
        img, _, _ = W.capture(hwnd)
        if img is None:
            raise RuntimeError("抓图失败（PrintWindow 拿不到画面，窗口可能被遮挡或正在最小化）")

        layout = W.compute_layout(*img.size)
        rect = win["rect"]

        # ---------------------------------------------------------------
        # 像素哨兵：画面没变就什么都不做，直接返回
        #
        # 为什么这一步最关键：一轮完整扫描要 OCR 9 次、约 14 秒，
        # 但绝大多数轮次里屏幕上根本没发生任何变化——那 14 秒纯属空转。
        # 抓图一次只要 12ms，对两块区域各算一个 md5 也就几毫秒，
        # 所以「没变化」这一轮的代价从 16 秒降到几十毫秒。
        #
        # 副作用必须兜住：画面不变不代表没有待办（比如上次取图失败的图要重试）。
        # 所以加一个强制周期，到点无论画面有没有变都完整跑一轮。
        # ---------------------------------------------------------------
        now = time.time()
        force_interval = float(self.conf["safety"].get("force_full_interval_sec", 60))
        forced = (now - self.last_force_full) >= force_interval

        try:
            view_sig = (desktop.sidebar_signature(img, layout),
                        desktop.chat_signature(img, layout))
        except Exception:
            view_sig = None

        if (not forced and view_sig is not None
                and view_sig == self.last_view_sig):
            self.last_stats = {"skipped": True}
            return 0

        self.last_view_sig = view_sig
        if forced:
            self.last_force_full = now

        total = 0

        # 先处理「当前已打开的这个群」。
        # 原因：微信把选中的会话画成绿底白字，侧边栏 OCR 经常读不出它的群名
        # （实测第一个条目被读成「[图片]」而不是「测试加油」）。
        # 如果白名单群恰好是当前打开的那个，只靠列表就会整个漏掉。
        cur_title, _, _ = desktop.read_current_title(hwnd)
        done_group = None
        if cur_title:
            cur_verdict = g_mod.match(cur_title, self.groups_conf)
            if cur_verdict["allowed"]:
                if not self.only_group or (
                        g_mod.normalize(cur_title) == g_mod.normalize(self.only_group)):
                    done_group = g_mod.normalize(cur_verdict["canonical"] or cur_title)
                    log(f"当前顶栏是白名单群「{cur_verdict['canonical'] or cur_title}」")
                    total += self.process_group(hwnd, layout, rect, None,
                                                skip_switch=True)

        # 会话列表：分级扫描
        #   平时只扫当前可见的一屏（快，约 2 秒）
        #   每隔 full_scan_interval_sec 做一次滚动全量扫描（60+ 会话要约 25 秒），
        #   用来兜住免打扰群 —— 它们有新消息也不会浮到顶部，只会安静地沉下去。
        #
        # scan_always：白名单群是否无条件巡检（不看红点）。默认开，见下面的说明。
        scan_always = bool(self.conf["safety"].get("scan_allowed_always", True))
        now = time.time()
        interval = float(self.conf["safety"].get("full_scan_interval_sec", 300))
        do_full = (self.deep or self.sweep or self.force_full_scan
                   or (now - self.last_full_scan) >= interval)

        if do_full:
            log("· 全量扫描会话列表（含滚动）…")
            want = [g.get("name") for g in g_mod.enabled_groups(self.groups_conf)]
            collected = desktop.collect_sidebar(hwnd, layout, rect,
                                                conf_groups=self.groups_conf,
                                                want_names=want)
            self.last_full_scan = now
            log(f"会话列表共 {len(collected)} 个（已含滚动扫描）")
        else:
            lines = ocr_engine.ocr(img, max_width=self.conf["ocr"]["window_max_width"])
            entries = desktop.sidebar_entries(img, lines, layout)
            collected = {}
            for e in entries:
                k = g_mod.normalize(e["text"], self.groups_conf)
                if k:
                    collected[k] = e
            log(f"会话列表当前可见 {len(collected)} 个（未做滚动扫描）")

        # 记一下「白名单群有几个在这次扫描范围内」。
        # 这个数字很关键：快速扫描只覆盖首屏那几个会话，白名单群如果在下面，
        # 就得等下一次全量扫描，实时性从 10 秒变成几分钟。
        # 面板上把它显示出来，用户才知道自己该不该把群置顶。
        allowed_total = len([g for g in (self.groups_conf.get("groups") or [])
                             if g.get("enabled", True)])
        allowed_visible = 0
        for _key, _e in collected.items():
            _v = g_mod.match_sidebar(_e["text"], _e.get("truncated", False),
                                     self.groups_conf)
            if _v["allowed"]:
                allowed_visible += 1
        self.last_stats = {
            "scan_mode": "full" if do_full else "quick",
            "sessions_seen": len(collected),
            "allowed_total": allowed_total,
            "allowed_visible": allowed_visible,
        }

        for key, e in collected.items():
            # 候选筛选用宽松匹配（容忍侧边栏截断）；最终由顶栏把关
            verdict = g_mod.match_sidebar(e["text"], e.get("truncated", False),
                                          self.groups_conf)
            if not verdict["allowed"]:
                continue
            if self.only_group and g_mod.normalize(e["text"]) != g_mod.normalize(self.only_group):
                continue

            # 刚才已经按「当前打开的群」处理过了，不要重复进
            if done_group and g_mod.normalize(e["text"]) == done_group:
                continue

            # 白名单群默认【无条件巡检】，不看红点。
            #
            # 红点在本项目里三处都靠不住：
            #   · 免打扰群根本不显示红点；
            #   · 你在微信里点开看一眼（或者机器人自己点进去），红点就没了，
            #     但机器人可能还没处理过里面的图；
            #   · 微信有时压根不刷新未读数。
            #
            # 实测后果很直接：15100 群从 00:58 之后再没被巡检过一次，
            # 客户在那边发了图，机器人压根没去看 —— 表现就是"最新发的图不识别"。
            #
            # 白名单通常只有一两个群，全扫一遍每群约 7 秒，
            # 代价远小于漏一单。群多到十条以上、嫌慢，
            # 把 safety.scan_allowed_always 设成 false 退回旧行为。
            if e["unread"] == "none" and not self.sweep and not scan_always:
                continue

            # 注意：这里不预先定位。切群时由 switch_to_group_by_name 每次重新定位，
            # 避免拿过期 y 坐标点到隔壁群。
            #
            # 传白名单的完整群名，不传 e["text"] —— 侧边栏会截断长群名，
            # 用截断的名字切群会一直失败（顶栏读作空），那个群就永远巡检不到。
            total += self.process_group(
                hwnd, layout, rect, verdict.get("canonical") or e["text"])

        # 收尾：处理完重新采样一次画面指纹，作为下一轮的比较基准。
        #
        # 不这么做就会自激振荡：处理过程本身（点开图片、关闭预览、滚动）
        # 改变了画面，下一轮哨兵一比较「和上轮不同」→ 再完整处理一遍 →
        # 又把画面改一遍…… 于是同一个群被反复扫描，每轮白烧十几秒。
        self._resample_view(hwnd)

        return total

    def _resample_view(self, hwnd):
        """
        处理结束后重新记录画面指纹，作为下一轮的比较基准。

        要点：必须等画面【稳定】再采样。
        处理过程结束时微信往往还在播动画（滚动惯性、气泡弹出、悬停高亮），
        立刻抓一张会采到中间态：下一轮一比较"和上轮不同"，又完整处理一遍，
        然后又播一遍动画…… 形成每 30 秒进一次群、反复点图的循环。
        这里连续抓两次，两次指纹一致才认为画面定下来了。
        """
        try:
            prev = None
            for _ in range(4):
                time.sleep(0.35)
                img, _, _ = W.capture(hwnd)
                if img is None:
                    continue
                lay = W.compute_layout(*img.size)
                sig = (desktop.sidebar_signature(img, lay),
                       desktop.chat_signature(img, lay))
                if sig == prev and prev is not None:
                    self.last_view_sig = sig       # 连续两次一致 → 稳定了
                    return
                prev = sig
            if prev is not None:
                self.last_view_sig = prev
        except Exception:
            pass

    # ------------------------------------------------------------ 常驻
    def _heartbeat_path(self):
        return os.path.join(config.ROOT, "data", "heartbeat.json")

    def _pause_path(self):
        """
        暂停标志文件。

        为什么用文件而不是进程内开关：面板和机器人是两个独立进程，
        面板没法直接改机器人内存里的变量。写一个约定文件，
        机器人每轮自己检查——够简单，而且进程重启后状态还在（不会一重启就自动跑起来）。
        """
        return os.path.join(config.ROOT, "data", "pause.flag")

    def _in_quiet_hours(self):
        """
        现在是不是夜间静默时段。

        为什么要有这个：凌晨反复扫会话列表、切群、发模板化消息，
        在服务端看来就是明显的异常行为。客户凌晨发加油订单本来就极少，
        静默的收益（降低被风控的概率）远大于损失（那几单延迟到早上处理）。

        跨零点也支持，比如 "23:30-07:00"。
        """
        spec = str(self.conf["safety"].get("quiet_hours") or "").strip()
        if not spec or "-" not in spec:
            return False
        try:
            a, b = spec.split("-", 1)
            h1, m1 = [int(x) for x in a.strip().split(":")]
            h2, m2 = [int(x) for x in b.strip().split(":")]
        except Exception:
            return False

        now = datetime.now()
        cur = now.hour * 60 + now.minute
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end        # 跨零点

    def _is_paused(self):
        return os.path.isfile(self._pause_path())

    def _stop_path(self):
        """
        「请退出」标志文件。和 pause.flag 一个思路，但语义完全不同：

          pause  = 进程留着、不扫描。恢复是瞬时的，也不用重新加载 OCR 模型。
          stop   = 进程真的退出。锁随之释放，下次点「启动」才是全新的一轮。

        为什么不用杀进程：
        面板拿到的只是 instance.json 里的一个 pid，那个文件在进程被强杀时
        会残留。照着旧 pid 去 taskkill，很可能杀掉一个复用了该 pid 的无关进程。
        写标志让机器人自己退，它还能顺手把心跳、锁、标志清理干净。

        代价是要等它从 sleep 里醒过来，所以下面把长睡眠切成了小段。
        """
        return os.path.join(config.ROOT, "data", "stop.flag")

    def _should_stop(self):
        return os.path.isfile(self._stop_path())

    def _clear_stop_flag(self):
        """启动时清一次：否则上一次的停止指令会让新进程一起来就自杀。"""
        try:
            os.remove(self._stop_path())
        except Exception:
            pass

    def _clear_pause_flag(self):
        try:
            os.remove(self._pause_path())
        except Exception:
            pass

    def _sleep_checkable(self, seconds):
        """
        可被打断的 sleep。返回 True 表示收到停止指令、应当退出。

        原来是一句 time.sleep(wait)。夜间 wait 是 25 秒，
        面板点了「停止」要干等半分钟才有反应，用户会以为按钮没生效、
        于是再点一次，甚至去任务管理器强杀——那才是最糟的路径。
        切成一秒一段，最多一秒就能响应。
        """
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if self._should_stop():
                return True
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
        return self._should_stop()

    def _write_heartbeat(self, **fields):
        """
        心跳文件。

        挂机最怕的不是报错，是「看起来在跑、其实早停了」。
        写一个带时间戳的文件，面板就能算出「多久没心跳」，
        一眼看出是活着、卡住、还是已经退出。
        """
        p = self._heartbeat_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = {}
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            data.update(fields)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception:
            pass

    def _cleanup_logs(self, keep_days=14):
        """
        删掉太旧的日志。

        挂机是长期行为：日志每天一个文件，跑几个月就是几百个。
        不清会慢慢把盘占满，而占满的那天往往是「写不进日志 → 整个进程崩」，
        属于平时看不见、坏起来很难查的那类问题。
        """
        try:
            log_dir = config.path_of(self.conf["paths"].get("logs", "logs"))
            if not os.path.isdir(log_dir):
                return
            cutoff = time.time() - keep_days * 86400
            removed = 0
            for name in os.listdir(log_dir):
                if not name.startswith("bot-") or not name.endswith(".log"):
                    continue
                p = os.path.join(log_dir, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except Exception:
                    pass
            if removed:
                log(f"清理了 {removed} 个超过 {keep_days} 天的日志")
        except Exception:
            pass

    def run_forever(self, interval=3.0):
        """
        常驻主循环。

        三条设计原则，都是被实际故障逼出来的：
          1. 单轮异常不退出 —— 一次抓图失败不该让挂机整体停摆
          2. 连续失败要退避 —— 否则微信最小化时每 3 秒打一条错误日志，刷爆磁盘
          3. 每轮写心跳 —— 让「它到底还活着吗」有据可查，而不是靠猜
        """
        started = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        log(f"启动常驻循环，间隔 {interval}s，dry_run={self.dry_run}")

        # 上一次「停止」留下的标志必须先清掉，否则新进程一进来就自己退出，
        # 用户看到的又是"点了启动没反应"。
        self._clear_stop_flag()
        self._cleanup_logs()

        cycles = 0
        fails = 0

        self._write_heartbeat(
            state="running", started_at=started, pid=os.getpid(),
            dry_run=bool(self.dry_run), interval=interval,
            cycles=0, last_tick=started,
            last_duration_sec=0, last_error=None, consecutive_errors=0,
        )

        while True:
            # 停止指令：面板点了「停止」。清干净再走，别留下标志让下次启动立刻自杀。
            if self._should_stop():
                log("收到停止指令，退出")
                self._write_heartbeat(
                    state="stopped",
                    last_tick=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    cycles=cycles,
                )
                self._clear_stop_flag()
                self._clear_pause_flag()
                single_instance.clear_owner()
                break

            # 暂停：心跳照写（让人知道进程活着、只是没干活），但不扫描不回复
            if self._is_paused():
                self._write_heartbeat(
                    state="paused",
                    last_tick=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    cycles=cycles,
                )
                self._sleep_checkable(max(1.0, interval))
                continue

            # 夜间低频：照常扫描，只是把轮询间隔拉长。
            #
            # 早先这里是"完全跳过"，但晚上照样有车加油，停掉就是漏单。
            # 又不能不处理——凌晨高频翻会话列表是明显的异常行为特征。
            # 折中：继续工作，把间隔从 1 秒拉到 quiet_interval_sec（默认 25 秒）。
            # 这样发现延迟最多几十秒，不影响夜间成交，同时把操作频次降到 1/25。
            quiet = self._in_quiet_hours()

            t0 = time.time()
            try:
                n = self.run_once()
                dur = time.time() - t0
                cycles += 1
                fails = 0
                if n:
                    log(f"本轮处理 {n} 单（{dur:.1f}s）")
                self._write_heartbeat(
                    state=("quiet" if quiet else "running"),
                    last_tick=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    last_duration_sec=round(dur, 2),
                    cycles=cycles, last_error=None, consecutive_errors=0,
                    quiet=quiet,
                    **(self.last_stats or {}),
                )
            except KeyboardInterrupt:
                log("收到中断，退出")
                self._write_heartbeat(
                    state="stopped",
                    last_tick=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    cycles=cycles,
                )
                self._clear_pause_flag()
                single_instance.clear_owner()
                break
            except Exception as e:
                fails += 1
                dur = time.time() - t0
                log(f"✗ 本轮异常（连续 {fails} 次）：{e}")
                self._write_heartbeat(
                    state="error",
                    last_tick=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    last_duration_sec=round(dur, 2),
                    cycles=cycles,
                    last_error=f"{type(e).__name__}: {e}"[:300],
                    consecutive_errors=fails,
                )

            wait = interval
            if fails >= 3:
                wait = min(60.0, interval * (2 ** min(fails - 3, 4)))
                log(f"  连续异常 {fails} 次，等待 {wait:.0f}s 再试")
            elif quiet:
                # 夜间低频：间隔拉到 quiet_interval_sec，操作频次降到 1/25，
                # 但仍然持续扫描，晚上来的订单不会漏
                wait = max(interval, float(self.conf["safety"].get("quiet_interval_sec", 25)))
            self._sleep_checkable(wait)
