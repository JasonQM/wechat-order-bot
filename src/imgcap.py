"""
图片抓取：从聊天窗口里把订单截图「原样」拿出来。

为什么不能直接截聊天区那块
--------------------------
聊天窗口里的图是缩略图，通常只有 200-300px 宽，订单截图上的金额数字必然糊。
所以流程是：点开图片 → 微信弹出大图预览窗 → 抓预览窗 → 关掉。

三条纪律
--------
1. 任何异常都必须走关闭逻辑（try/finally）。预览窗不关，后面所有 OCR
   都会读到错误界面，会连锁误判。
2. 关完要【验证真的关了】。Esc 偶尔会被别的窗口吃掉。
3. 开不出预览窗时不硬失败，退回「直接裁聊天区」并标记 degraded，
   让下游知道这张图质量差 —— 比整条链路卡死强。
"""

import os
import time

import numpy as np

import window as W

MIN_NEW_WIN = 120      # 预览窗最小宽（过滤托盘气泡之类）
OPEN_TIMEOUT = 2.0
CLOSE_TIMEOUT = 1.2


def _win_set(min_w=MIN_NEW_WIN, min_h=MIN_NEW_WIN):
    wins = W.find_wechat_windows(min_w=min_w, min_h=min_h)
    return {w["hwnd"]: w for w in wins}


def _area(w):
    return w["size"][0] * w["size"][1]


def _close_window(hwnd):
    """
    关闭预览窗。

    用 WM_CLOSE 而不是 Esc：Esc 要求窗口在前台，会先把微信抢到最前，
    用户正在用别的程序时焦点会被夺走（实测反馈过）。
    WM_CLOSE 直接投递给目标窗口，不需要任何焦点变化。
    """
    # 两步都走 PostMessage，全程不碰焦点：
    #   1) WM_CLOSE —— 通用关窗，但微信的图片预览窗实测不响应这个
    #   2) WM_KEYDOWN/UP + VK_ESCAPE —— 投递 Esc 给它自己处理（不需要它在前台）
    # 原来只发 WM_CLOSE，关不掉就退回"激活窗口 + 真按键"，于是又抢一次焦点。
    try:
        W.user32.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE
        time.sleep(0.08)
        W.user32.PostMessageW(hwnd, 0x0100, 0x1B, 0)       # WM_KEYDOWN Esc
        time.sleep(0.02)
        W.user32.PostMessageW(hwnd, 0x0101, 0x1B, 0)       # WM_KEYUP Esc
    except Exception:
        pass


def _close_window_by_esc(hwnd):
    """兜底：WM_CLOSE 没关掉时才用 Esc（这一步确实需要前台）。"""
    W.activate(hwnd)
    time.sleep(0.05)
    W.user32.keybd_event(0x1B, 0, 0, 0)
    time.sleep(0.03)
    W.user32.keybd_event(0x1B, 0, 0x0002, 0)


def _close_preview(main_hwnd, preview_hwnd):
    """关预览窗并验证消失；必要时重试一次，最后把焦点还给主窗。"""
    deadline = time.time() + CLOSE_TIMEOUT
    attempt = 0

    while time.time() < deadline and attempt < 2:
        _close_window(preview_hwnd)
        time.sleep(0.15)
        if preview_hwnd not in _win_set():
            return True                 # 关掉了，不需要碰焦点
        attempt += 1

    # WM_CLOSE 关不掉时才退回 Esc（会短暂抢一次前台）
    _close_window_by_esc(preview_hwnd)
    time.sleep(0.2)
    return preview_hwnd not in _win_set()


def _validate(img):
    """图片质量校验：太小、全黑、比例离谱都算不合格。"""
    if img is None:
        return False, "抓图失败"
    w, h = img.size
    if max(w, h) < 300:
        return False, f"尺寸过小 {w}x{h}"
    arr = np.asarray(img.convert("RGB"))
    if int(arr.max()) <= 12:
        return False, "画面全黑"
    if min(w, h) > 0 and not (0.12 < w / float(h) < 8.0):
        return False, f"宽高比异常 {w}x{h}"
    return True, "ok"


def grab_image(hwnd, box, save_path):
    """
    点开指定位置的图片，抓预览窗存盘，然后关掉。

    box        (x1,y1,x2,y2) 图片气泡在客户区坐标系里的矩形
    save_path  保存路径（PNG）

    返回 dict:
        ok       bool
        path     存盘路径
        mode     "preview"（大图预览窗）| "crop"（退化：直接裁聊天区）
        note     人话说明
    """
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    x1, y1, x2, y2 = [int(v) for v in box]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

    before = set(_win_set().keys())
    preview_hwnd = None
    img = None
    mode = "crop"
    note = ""

    try:
        # 单击图片即可打开预览
        W.post_click(hwnd, cx, cy)

        # 等新窗口出现
        deadline = time.time() + OPEN_TIMEOUT
        while time.time() < deadline:
            now = _win_set()
            added = [h for h in now if h not in before]
            if added:
                preview_hwnd = max(added, key=lambda h: _area(now[h]))
                break
            time.sleep(0.08)

        if preview_hwnd is not None:
            time.sleep(0.25)  # 等大图解码渲染
            pimg, _, _ = W.capture(preview_hwnd)
            ok, why = _validate(pimg)
            if ok:
                img = pimg
                mode = "preview"
            else:
                note = f"预览窗抓图不合格（{why}），退回裁切"
        else:
            note = "未检测到预览窗"
    finally:
        if preview_hwnd is not None:
            closed = _close_preview(hwnd, preview_hwnd)
            if not closed:
                note = (note + "；" if note else "") + "预览窗未能关闭，建议中止本群"

    # 退化路径：直接裁聊天区那块
    if img is None:
        main_img, _, _ = W.capture(hwnd)
        if main_img is not None:
            w, h = main_img.size
            crop = main_img.crop((max(0, x1), max(0, y1),
                                  min(w, x2), min(h, y2)))
            ok, why = _validate(crop)
            if ok:
                img = crop
                note = (note + "；" if note else "") + "使用聊天区裁切，分辨率偏低"
            else:
                note = (note + "；" if note else "") + f"裁切也不合格（{why}）"

    if img is None:
        return {"ok": False, "path": None, "mode": mode, "note": note or "抓图失败"}

    img.save(save_path, "PNG")
    return {"ok": True, "path": save_path, "mode": mode,
            "note": note or ("大图预览" if mode == "preview" else "聊天区裁切")}
