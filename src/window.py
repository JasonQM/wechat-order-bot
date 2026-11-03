"""
窗口层：微信主窗口的枚举、抓图、点击、激活、布局计算。

来源与改动
----------
基础来自参考仓库 Luofeng-Cloud/WeChat-AI-AutoReply 的 wechat_ai_bot_dev.py
（get_wechat_hwnd / grab_wechat_window / activate_wechat / post_click_client_point /
compute_wechat_layout），做了三处修正：

1. 窗口匹配改按【进程名】而非窗口类名。
   实测：只按 `Qt*` 类名匹配会命中 EA Desktop（EADesktop.exe）等无关 Qt 应用。
2. 布局比例外置到 config/layout.json，便于在微信 4.x 上实机标定，不必改代码。
3. 抓图返回 PIL.Image，并保留 raw 像素供差分使用。

本模块只做"看"和"点"，不处理业务。
"""

import ctypes
import json
import os
import time
from ctypes import wintypes

import numpy as np
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WECHAT_PROCS = {"wechat.exe", "weixin.exe"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
LAYOUT_CONF = os.path.join(ROOT, "config", "layout.json")

DEFAULT_LAYOUT = {
    "sidebar_w_ratio": 0.055,
    "sidebar_w_min": 55,
    "chat_start_x_ratio": 0.285,
    "chat_start_x_min": 320,
    "header_h_ratio": 0.110,
    "header_h_min": 85,
    "input_h_ratio": 0.160,
    "input_h_min": 140,
    "input_h_max": 220,
}


# ---------------------------------------------------------------- DPI
def init_dpi():
    """每显示器 DPI 感知。必须在任何坐标计算之前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale():
    try:
        hdc = user32.GetDC(0)
        log_w = gdi32.GetDeviceCaps(hdc, 8)     # HORZRES
        phys_w = gdi32.GetDeviceCaps(hdc, 118)  # DESKTOPHORZRES
        user32.ReleaseDC(0, hdc)
        s = phys_w / float(log_w)
        return s if s > 0 else 1.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 窗口枚举
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)


def _proc_name(pid):
    try:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return "?"
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return os.path.basename(buf.value) if ok else "?"
    except Exception:
        return "?"


def find_wechat_windows(min_w=400, min_h=300, include_hidden=False):
    """
    返回属于微信进程的所有窗口（含空壳窗口，由调用方筛选）。

    include_hidden=True 时把最小化/隐藏的窗口也带出来。
    为什么需要：微信被最小化后 IsWindowVisible 为假，原来的实现直接跳过，
    于是挂机中途用户点了最小化 → 机器人报「没找到微信窗口」→ 整个停摆。
    最小化是挂机时最正常的操作之一，不能当异常处理。
    """
    out = []

    def cb(hwnd, _lp):
        visible = bool(user32.IsWindowVisible(hwnd))
        if not visible and not include_hidden:
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = _proc_name(pid.value)
        if pname.lower() not in WECHAT_PROCS:
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top

        minimized = bool(user32.IsIconic(hwnd)) or not visible
        if not minimized and (w < min_w or h < min_h):
            return True
        if minimized:
            # 最小化时 GetWindowRect 给的是 (-32000,-32000)，尺寸不可信，
            # 先用恢复后的常规尺寸代替，真正恢复后外面会重新取。
            w, h = max(w, min_w), max(h, min_h)

        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)

        cname, tname = cls.value, title.value

        # 只认 Qt 的普通窗口类。
        #
        # 微信进程里挂着几十个系统窗口：输入法（IME/SoPY/SoGou）、
        # 电源消息、USB 热插拔、ICC 配置、托盘图标消息窗……
        # 其中 WxTrayIconMessageWindow 最坑：它会被撑到和屏幕一样大
        # （实测 2580x1029），如果按「面积最大」挑，正好挑中它。
        # 那窗口永远是空白的，于是表现为「抓图成功但 OCR 读到 0 行」。
        if "QWindowIcon" not in cname and "QWindowToolSaveBits" not in cname:
            return True

        is_shell = (tname == "Weixin") or ("TrayIcon" in cname) or ("TrayIcon" in tname)

        out.append({
            "hwnd": hwnd,
            "class": cname,
            "title": tname,
            "pid": pid.value,
            "proc": pname,
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "size": (w, h),
            "minimized": minimized,
            "is_shell": is_shell,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _restore_window(hwnd, settle=0.45):
    """把最小化的窗口恢复出来。"""
    try:
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        time.sleep(settle)
        return True
    except Exception:
        return False


def pick_main_window(windows=None, auto_restore=True):
    """
    挑真实主窗口：优先非空壳、面积最大。找不到返回 None。

    如果微信被最小化（挂机时用户随手点一下很正常），这里会把它恢复出来。
    不恢复就没法干活：最小化状态下 PrintWindow 抓不到画面，点击也无效。
    """
    wins = windows if windows is not None else find_wechat_windows()
    if not wins:
        wins = find_wechat_windows(include_hidden=True)

    if not wins:
        return None

    real = [w for w in wins if not w["is_shell"]]
    pool = real or wins
    best = max(pool, key=lambda w: w["size"][0] * w["size"][1])

    if auto_restore and best.get("minimized"):
        _restore_window(best["hwnd"])
        again = find_wechat_windows()
        if again:
            real2 = [w for w in again if not w["is_shell"]]
            pool2 = real2 or again
            best = max(pool2, key=lambda w: w["size"][0] * w["size"][1])

    return best


# ---------------------------------------------------------------- 抓图
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def capture(hwnd, flags=(2, 0)):
    """
    PrintWindow 抓图，按 flags 顺序尝试，返回第一个"有内容"的结果。

    返回 (PIL.Image, (l,t,r,b), dpi_scale)；全失败返回 (None, None, scale)。
    """
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None, None, dpi_scale()

    scale = dpi_scale()
    last = None

    for flag in flags:
        hwnd_dc = user32.GetWindowDC(hwnd)
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        gdi32.SelectObject(mfc_dc, bmp)

        ok = user32.PrintWindow(hwnd, mfc_dc, flag)

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mfc_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        if not ok or not got:
            continue

        img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
        arr = np.asarray(img)
        if arr.size and int(arr.max()) > 10:
            return img, (rect.left, rect.top, rect.right, rect.bottom), scale
        last = img

    return last, (rect.left, rect.top, rect.right, rect.bottom), scale


# ---------------------------------------------------------------- 布局
def load_layout_conf():
    conf = dict(DEFAULT_LAYOUT)
    if os.path.isfile(LAYOUT_CONF):
        try:
            with open(LAYOUT_CONF, "r", encoding="utf-8") as f:
                conf.update(json.load(f) or {})
        except Exception:
            pass
    return conf


def compute_layout(W, H, conf=None):
    """
    按窗口宽高计算关键区域（客户区坐标系，与抓图一致）。

    sidebar_w     左侧会话列表宽度
    chat_start_x  聊天区左边界
    header_h      顶部标题栏高度
    input_h       底部输入区高度
    """
    c = conf or load_layout_conf()
    sidebar_w = max(c["sidebar_w_min"], int(c["sidebar_w_ratio"] * W))
    chat_start_x = max(c["chat_start_x_min"], int(c["chat_start_x_ratio"] * W))
    header_h = max(c["header_h_min"], int(c["header_h_ratio"] * H))
    input_h = min(c["input_h_max"], max(c["input_h_min"], int(c["input_h_ratio"] * H)))

    return {
        "sidebar_w": sidebar_w,
        "chat_start_x": chat_start_x,
        "header_h": header_h,
        "input_h": input_h,
        "total_w_px": W,
        "total_h_px": H,
        # 派生区域
        # 注意：sidebar_w 是左侧「图标栏」宽度（头像/聊天/通讯录那一竖条），
        # 真正的会话列表在 sidebar_w → chat_start_x 之间。
        # 实机标定（微信 4.1.15，896x648）：图标栏≈55，会话列表 55→320，顶栏≈85，输入区≈140。
        "chat_box": (chat_start_x, header_h, W, H - input_h),
        "sidebar_box": (sidebar_w, header_h, chat_start_x, H - 20),
        "input_box": (chat_start_x, H - input_h, W, H),
    }


# ---------------------------------------------------------------- 动作
def activate(hwnd, settle=0.03):
    """把微信提到前台（发键盘事件前必须）。"""
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.05)

        cur = user32.GetForegroundWindow()
        cur_thread = user32.GetWindowThreadProcessId(cur, None) if cur else 0
        wx_thread = user32.GetWindowThreadProcessId(hwnd, None)
        my_thread = kernel32.GetCurrentThreadId()

        a1 = a2 = False
        try:
            if cur_thread and cur_thread != wx_thread:
                a1 = bool(user32.AttachThreadInput(cur_thread, wx_thread, True))
            if my_thread and my_thread != wx_thread:
                a2 = bool(user32.AttachThreadInput(my_thread, wx_thread, True))

            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003 | 0x0040)  # TOPMOST, 不改尺寸位置
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)          # NOTOPMOST
        finally:
            if a1:
                user32.AttachThreadInput(cur_thread, wx_thread, False)
            if a2:
                user32.AttachThreadInput(my_thread, wx_thread, False)

        time.sleep(settle)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def post_click(hwnd, client_x, client_y):
    """
    向窗口投递鼠标消息，物理鼠标一动不动。
    client_x/y 是客户区坐标（与抓图坐标一致）。
    """
    try:
        lp = (int(client_y) << 16) | (int(client_x) & 0xFFFF)
        user32.PostMessageW(hwnd, 0x0200, 0x0000, lp)   # WM_MOUSEMOVE
        time.sleep(0.01)
        user32.PostMessageW(hwnd, 0x0201, 0x0001, lp)   # WM_LBUTTONDOWN
        time.sleep(0.02)
        user32.PostMessageW(hwnd, 0x0202, 0x0000, lp)   # WM_LBUTTONUP
        time.sleep(0.03)
    except Exception:
        pass


def client_to_screen(hwnd, client_x, client_y):
    """
    客户区坐标 → 屏幕坐标。

    不能简单用「窗口外框左上角 + 客户区偏移」：
    窗口外框左上角不等于客户区左上角，中间差着标题栏和边框（通常 30px 左右）。
    必须让系统换算。
    """
    pt = wintypes.POINT(int(client_x), int(client_y))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def post_wheel(hwnd, client_x, client_y, notches=-1):
    """
    投递滚轮消息。

    坑：WM_MOUSEWHEEL 的 lParam 是【屏幕坐标】，而 WM_LBUTTONDOWN 是【客户区坐标】。
    同一个 PostMessage，鼠标消息和滚轮消息用的坐标系不一样，混用会滚到别的地方去。

    向上翻历史用负 delta（每格 120）。
    """
    try:
        sx, sy = client_to_screen(hwnd, client_x, client_y)
        delta = (int(notches) * 120) & 0xFFFF
        wp = (delta << 16) | 0x0000
        lp = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
        user32.PostMessageW(hwnd, 0x020A, wp, lp)       # WM_MOUSEWHEEL
        time.sleep(0.03)
    except Exception:
        pass
