"""
发送层：文本、图片、@ 发起人。

所有发送都走「剪贴板 + Ctrl+V + Enter」这条最贴近真人的路径，
并沿用参考仓库的安全框架：
    · 发送前复核顶栏，确认还在目标群
    · 击键前强校验前台窗口，不是微信就中止（防串窗）
    · 发完做差分验收，确认真的长出了新气泡

@ 的实现是分层降级：任何一层失败都退回「软 @」（正文里写昵称），
绝不因为 @ 不成而不发消息。
"""

import ctypes
import hashlib
import os
import struct
import time
from ctypes import wintypes

import numpy as np

import config
import ocr_engine
import perceive
import window as W

user32 = W.user32
kernel32 = W.kernel32

# 64 位下指针必须显式声明，否则会被截断成 32 位
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p

CF_DIB = 8
GMEM_MOVEABLE = 0x0002


# ---------------------------------------------------------------- 剪贴板
def copy_text(text, retries=3, delay=0.04):
    import pyperclip
    for i in range(retries):
        try:
            pyperclip.copy(text)
            time.sleep(delay)
            return True
        except Exception:
            time.sleep(delay * (i + 1))
    return False


def _pil_to_dib(img):
    """
    PIL 图 → CF_DIB 字节流（BITMAPINFOHEADER + 24位 BGR 自下而上 + 4字节对齐）。
    注意 BMP 是自下而上存的，行序要反过来。
    """
    img = img.convert("RGB")
    w, h = img.size
    rgb = img.tobytes()

    stride = ((w * 3 + 3) // 4) * 4
    pad = b"\x00" * (stride - w * 3)

    rows = []
    for y in range(h - 1, -1, -1):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        bgr = bytearray(row)
        bgr[0::3] = row[2::3]
        bgr[2::3] = row[0::3]
        rows.append(bytes(bgr) + pad)

    pixels = b"".join(rows)
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    return header + pixels


def copy_image(path, retries=3):
    """把图片文件写进剪贴板（CF_DIB），这样粘贴出来是图片而不是文件。"""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    data = _pil_to_dib(img)

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(0.08)
            continue
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)

            # SetClipboardData 成功后内存归系统所有，不能再 free
            if not user32.SetClipboardData(CF_DIB, handle):
                return False
            return True
        finally:
            user32.CloseClipboard()

    return False


# ---------------------------------------------------------------- 画面签名
def _sig(img, layout, which="chat"):
    if img is None:
        return ""
    x1, y1, x2, y2 = layout["chat_box"] if which == "chat" else layout["input_box"]
    crop = np.asarray(img.convert("RGB"))[int(y1):int(y2), int(x1):int(x2)]
    if crop.size == 0:
        return ""
    return hashlib.md5(crop.tobytes()).hexdigest()


# ---------------------------------------------------------------- 键盘
def _focus_input(hwnd, layout):
    """点一下输入框让它获得焦点。"""
    x = int(0.55 * layout["total_w_px"])
    y = int(layout["total_h_px"] - layout["input_h"] * 0.55)
    W.post_click(hwnd, x, y)
    time.sleep(0.05)


def _press(vk, mods=()):
    for m in mods:
        user32.keybd_event(m, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 0x0002, 0)
    for m in reversed(mods):
        user32.keybd_event(m, 0, 0x0002, 0)


def _paste_and_enter(hwnd, layout):
    """Ctrl+V 然后 Enter。返回是否成功把焦点留在微信。"""
    if not W.activate(hwnd):
        return False
    if user32.GetForegroundWindow() != hwnd:
        return False

    _focus_input(hwnd, layout)
    if user32.GetForegroundWindow() != hwnd:
        return False

    _press(0x56, mods=(0x11,))   # Ctrl+V
    time.sleep(0.06)
    _press(0x0D)                 # Enter
    time.sleep(0.05)
    return True


# ---------------------------------------------------------------- 校验顶栏
def _verify_target(hwnd, group_name):
    """发送前确认当前还在目标群。"""
    import groups as g_mod

    img, _, _ = W.capture(hwnd)
    if img is None:
        return False, "抓图失败"
    layout = W.compute_layout(*img.size)
    lines = ocr_engine.ocr(img, max_width=1400)
    title = perceive.read_header(lines, layout)

    if g_mod.normalize(title) == g_mod.normalize(group_name):
        return True, title
    return False, title


# ---------------------------------------------------------------- @
def mention(hwnd, layout, name, wait=0.5):
    """
    真 @ 发起人。分层降级，全部失败返回 False（调用方改走软 @）。

    步骤：聚焦输入框 → 输入 @ → 等弹窗 → OCR 找昵称行 → 点它。
    """
    if not name:
        return False

    img, _, _ = W.capture(hwnd)
    if img is None:
        return False
    before = _sig(img, layout, "chat")

    if not W.activate(hwnd):
        return False

    _focus_input(hwnd, layout)
    if user32.GetForegroundWindow() != hwnd:
        return False

    # 输入 @ 用 Shift+2（比粘贴更可靠地触发成员列表）
    _press(0x32, mods=(0x10,))
    time.sleep(wait)

    img2, _, _ = W.capture(hwnd)
    if img2 is None:
        return False
    if _sig(img2, layout, "chat") == before:
        return False  # 弹窗没出现

    # 弹窗浮在输入框上方，OCR 一下找目标昵称
    lines = ocr_engine.ocr(img2, max_width=1400)
    target = None
    for r in lines:
        if r["text"].strip() == name.strip():
            target = r
            break

    if target is None:
        # 找不到就撤掉 "@"，交给软 @
        _press(0x08)  # Backspace
        return False

    W.post_click(hwnd, int(target["cx"]), int(target["cy"]))
    time.sleep(0.2)
    return True


# ---------------------------------------------------------------- 发送
def send_text(hwnd, layout, text, group_name=None, force=False, verify=True):
    """
    发一条文本。

    返回 dict: ok / sent / skipped / note
    """
    conf = config.load()
    if conf["safety"]["dry_run"] and not force:
        return {"ok": True, "sent": False, "skipped": True,
                "note": "dry_run 模式，未实际发送"}

    if group_name:
        ok, title = _verify_target(hwnd, group_name)

        # 顶栏不是目标群时，先试着切回去一次。
        #
        # 为什么需要：发文本和发图之间有 0.6 秒间隔，这期间用户可能点了
        # 侧边栏别的会话（实测发完文字后用户切到"微信支付"看收款，
        # 图片就发不出去了）。原来只做校验不做补救，等于用户一碰微信就断。
        if not ok and rect:
            try:
                import desktop
                switched, t2 = desktop.switch_to_group_by_name(
                    hwnd, layout, rect, group_name)
                if switched:
                    ok, title = _verify_target(hwnd, group_name)
            except Exception:
                pass

        if not ok:
            return {"ok": False, "sent": False, "skipped": False,
                    "note": f"顶栏不是目标群（当前「{title}」），中止发送"}

    # 抓图带重试。
    # 实测发送文本之后紧接着发图，微信可能正在重绘/弹动画，
    # 那一瞬间 PrintWindow 三种方式都可能返回空。
    # 原来只抓一次，抓到空就直接放弃，结果就是"文本发出去了、图片没发"。
    img_pre = None
    for _ in range(3):
        img_pre, _, _ = W.capture(hwnd)
        if img_pre is not None:
            break
        time.sleep(0.35)
    if img_pre is None:
        return {"ok": False, "sent": False, "skipped": False,
                "note": "抓图失败（连试 3 次都拿不到画面）"}

    if not copy_text(text):
        return {"ok": False, "sent": False, "skipped": False, "note": "剪贴板写入失败"}

    if not _paste_and_enter(hwnd, layout):
        return {"ok": False, "sent": False, "skipped": False,
                "note": "前台焦点未拿到，中止发送（防串窗）"}

    if not verify:
        return {"ok": True, "sent": True, "skipped": False, "note": "已发送（未验证）"}

    time.sleep(0.35)
    img_post, _, _ = W.capture(hwnd)
    if img_post is None:
        return {"ok": True, "sent": True, "skipped": False,
                "note": "已发送但无法验证（抓图失败）"}

    changed = _sig(img_pre, layout) != _sig(img_post, layout)
    return {"ok": True, "sent": True, "skipped": False,
            "note": "已发送并验证" if changed else "已发送但未检出气泡增量",
            "verified": changed}


def send_image(hwnd, layout, image_path, group_name=None, force=False,
               verify=True, rect=None):
    """
    发一张图片（CF_DIB 剪贴板 + Ctrl+V）。

    关键：不能"粘完直接回车"。
    实测发完文字后紧接着贴图，微信还没清空输入框 / 焦点还没回来，
    Ctrl+V 会落空，但代码以为成功了（回车照样按下去，什么也没发出去）。
    所以必须【贴完先确认输入框真的有内容】再按回车，否则重试。
    """
    conf = config.load()
    if conf["safety"]["dry_run"] and not force:
        return {"ok": True, "sent": False, "skipped": True,
                "note": "dry_run 模式，未实际发送"}

    if not image_path or not os.path.isfile(image_path):
        return {"ok": False, "sent": False, "skipped": False,
                "note": f"图片不存在：{image_path}"}

    if group_name:
        ok, title = _verify_target(hwnd, group_name)

        # 顶栏不是目标群时，先试着切回去一次。
        #
        # 为什么需要：发文本和发图之间有 0.6 秒间隔，这期间用户可能点了
        # 侧边栏别的会话（实测发完文字后用户切到"微信支付"看收款，
        # 图片就发不出去了）。原来只做校验不做补救，等于用户一碰微信就断。
        if not ok and rect:
            try:
                import desktop
                switched, t2 = desktop.switch_to_group_by_name(
                    hwnd, layout, rect, group_name)
                if switched:
                    ok, title = _verify_target(hwnd, group_name)
            except Exception:
                pass

        if not ok:
            return {"ok": False, "sent": False, "skipped": False,
                    "note": f"顶栏不是目标群（当前「{title}」），中止发送"}

    # 抓图带重试。
    # 实测发送文本之后紧接着发图，微信可能正在重绘/弹动画，
    # 那一瞬间 PrintWindow 三种方式都可能返回空。
    # 原来只抓一次，抓到空就直接放弃，结果就是"文本发出去了、图片没发"。
    img_pre = None
    for _ in range(3):
        img_pre, _, _ = W.capture(hwnd)
        if img_pre is not None:
            break
        time.sleep(0.35)
    if img_pre is None:
        return {"ok": False, "sent": False, "skipped": False,
                "note": "抓图失败（连试 3 次都拿不到画面）"}

    # ---- 粘贴 + 确认插入 ----
    inserted = False
    for attempt in range(2):
        if not copy_image(image_path):
            time.sleep(0.3)
            continue

        if not W.activate(hwnd):
            time.sleep(0.3)
            continue
        if user32.GetForegroundWindow() != hwnd:
            time.sleep(0.3)
            continue

        _focus_input(hwnd, layout)
        time.sleep(0.15)

        # 粘贴前先清空输入框。
        #
        # 为什么必须有这一步：第一次粘贴其实可能已经成功，但输入框签名比对
        # 因为图片渲染延迟判成"没插进去"，于是走第二轮重试——输入框里就叠了
        # 两张图，按一次回车一起发出去。实测群里出现了两条相同的收款码。
        # 同一时刻连发两条一模一样的图片，对风控来说是最典型的机器特征。
        # 先 Ctrl+A + Delete 清干净，重试也只会有一张。
        _press(0x41, mods=(0x11,))     # Ctrl+A
        time.sleep(0.06)
        _press(0x2E)                   # Delete
        time.sleep(0.12)

        img_a, _, _ = W.capture(hwnd)
        pre_sig = _sig(img_a, layout, "input") if img_a is not None else ""

        _press(0x56, mods=(0x11,))     # Ctrl+V
        time.sleep(0.9)                # 给微信解码图片的时间

        img_b, _, _ = W.capture(hwnd)
        post_sig = _sig(img_b, layout, "input") if img_b is not None else ""

        if post_sig and post_sig != pre_sig:
            inserted = True
            break
        time.sleep(0.4)

    if not inserted:
        return {"ok": False, "sent": False, "skipped": False,
                "note": "图片没能粘进输入框（可能焦点丢失或剪贴板被占用），已放弃，未发送空白消息"}

    _press(0x0D)                       # Enter 发送
    time.sleep(0.2)

    if not verify:
        return {"ok": True, "sent": True, "skipped": False, "note": "已发送（未验证）"}

    time.sleep(0.5)
    img_post, _, _ = W.capture(hwnd)
    if img_post is None:
        return {"ok": True, "sent": True, "skipped": False,
                "note": "已发送但无法验证"}

    changed = _sig(img_pre, layout) != _sig(img_post, layout)
    return {"ok": True, "sent": True, "skipped": False,
            "verified": changed,
            "note": "图片已发送并验证" if changed else "图片已发出但未检出气泡增量"}


# ---------------------------------------------------------------- 自检
def _selfcheck():
    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        ok += bool(cond)
        fail += (not cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name:32s} {detail}")

    from PIL import Image
    img = Image.new("RGB", (10, 8), (255, 0, 0))
    px = img.load()
    px[0, 0] = (0, 0, 255)  # 左上角放个蓝点，用来验证行序

    dib = _pil_to_dib(img)
    header = struct.unpack("<IiiHHIIiiII", dib[:40])
    chk("DIB 头 width/height", header[1] == 10 and header[2] == 8, f"-> {header[1]}x{header[2]}")
    chk("DIB 位深 24", header[4] == 24, f"-> {header[4]}")

    stride = ((10 * 3 + 3) // 4) * 4   # 32
    chk("行对齐到4字节", len(dib) - 40 == stride * 8, f"-> {len(dib) - 40}")

    # DIB 自下而上：最后一行是图像顶部，左上像素应是蓝色(BGR 里是 B=255)
    last_row_start = 40 + stride * 7
    b, g, r = dib[last_row_start], dib[last_row_start + 1], dib[last_row_start + 2]
    chk("像素 BGR 顺序正确", (b, g, r) == (255, 0, 0), f"-> BGR={(b, g, r)}")

    print(f"\n{ok} 通过, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selfcheck())
