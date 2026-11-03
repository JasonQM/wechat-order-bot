#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信窗口抓图可行性探针（零第三方依赖，仅用标准库 + ctypes）

用途：
  验证「零注入 / 纯外部感知」路线在你的机器和微信版本上是否成立。
  核心问题只有一个：PrintWindow 能不能抓到微信的真实画面。
  抓不到 = 这条路走不通，无需继续。

不做任何修改微信的操作：
  不注入、不点击、不按键、不读写微信文件，只做窗口枚举与画面抓取。

用法：
  python tools/probe_wechat.py
  python tools/probe_wechat.py --keep-open    # 抓图前不尝试前置窗口

输出：
  probe_out/win_<hwnd>_flag<2|0>.bmp  抓图结果
  probe_out/report.txt                文字报告
"""

import ctypes
import os
import struct
import sys
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
version_dll = ctypes.WinDLL("version")  # 版本资源 API 在 version.dll，不在 kernel32

# 微信主窗口所属进程（3.x = WeChat.exe，4.x = Weixin.exe）
WECHAT_PROCS = {"wechat.exe", "weixin.exe"}

# 每显示器 DPI 感知，避免坐标被系统虚拟化
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "probe_out")
OUT_DIR = os.path.abspath(OUT_DIR)

REPORT = []


def say(line=""):
    print(line)
    REPORT.append(line)


# ---------------------------------------------------------------- 版本探测
def probe_version():
    """从注册表读微信安装路径与版本号，判断是否为 4.x。"""
    try:
        import winreg
    except ImportError:
        say("[版本] 无法导入 winreg，跳过")
        return None

    found = []
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat", "InstallPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\WeChat", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat", "InstallPath"),
    ]
    for hive, key_path, value_name in candidates:
        try:
            with winreg.OpenKey(hive, key_path) as k:
                path, _ = winreg.QueryValueEx(k, value_name)
                found.append((key_path, path))
        except OSError:
            continue

    if not found:
        say("[版本] 注册表未找到微信安装信息（可能为绿色版/未安装）")
        return None

    for key_path, path in found:
        say(f"[版本] {key_path} -> {path}")
        for exe in ("WeChat.exe", "Weixin.exe"):
            exe_path = os.path.join(path, exe)
            if os.path.isfile(exe_path):
                say(f"[版本] 发现可执行文件：{exe_path}")
                ver = read_exe_version(exe_path)
                if ver:
                    major = ver.split(".")[0]
                    say(f"[版本] 文件版本 = {ver}")
                    if major.isdigit() and int(major) >= 4:
                        say(f"[版本] 判定为微信 {major}.x —— 注意：4.x 的 UI 与 3.x 差异较大，布局比例需重新标定")
                    return ver

    # 注册表没有版本号时，尝试 DisplayVersion
    for hive, key_path, _ in candidates:
        try:
            with winreg.OpenKey(hive, key_path) as k:
                ver, _ = winreg.QueryValueEx(k, "DisplayVersion")
                say(f"[版本] DisplayVersion = {ver}")
                return str(ver)
        except OSError:
            continue
    return None


def read_exe_version(exe_path):
    """用版本资源 API 读取 exe 版本号（API 属于 version.dll）。"""
    try:
        size = version_dll.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(exe_path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        # VS_FIXEDFILEINFO: 13 个 DWORD，dwFileVersionMS 在 offset 8，LS 在 12
        raw = ctypes.string_at(ptr, length.value)
        if len(raw) < 16:
            return None
        ms, ls = struct.unpack_from("<II", raw, 8)
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


# ---------------------------------------------------------------- 窗口枚举
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)


def enum_windows():
    """
    按【进程名】筛选微信窗口。
    注意：不能按窗口类名匹配。Qt5152QWindowOwnDCIcon 这类类名被大量 Qt 应用共用
    （EA Desktop、各类聊天工具都会命中），只按类名会误伤。
    """
    results = []

    def cb(hwnd, _lp):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)

        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        cname = cls.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = proc_name(pid.value)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(0, rect.right - rect.left)
        h = max(0, rect.bottom - rect.top)

        # 主判据：进程名。辅判据：窗口足够大（排除托盘/隐藏小窗）
        if pname.lower() in WECHAT_PROCS and w > 400 and h > 300:
            results.append({
                "hwnd": hwnd,
                "class": cname,
                "title": title.value,
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "pid": pid.value,
                "proc": pname,
                "size": (w, h),
            })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return results


def proc_name(pid):
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return "?"
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return os.path.basename(buf.value) if ok else "?"
    except Exception:
        return "?"


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


def capture(hwnd, flag):
    """用 PrintWindow 的指定 flag 抓一次图，返回 (bytes, w, h) 或 None。"""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mfc_dc, bmp)

    ok = user32.PrintWindow(hwnd, mfc_dc, flag)

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = h  # 正数 = bottom-up
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfc_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    if not got:
        return None
    return (buf.raw, w, h, bool(ok))


def analyze(raw, w, h):
    """统计非黑/纯色像素占比，判断是否抓到真实画面。"""
    total = w * h
    step = 16  # 抽样，避免遍历太慢
    nonblack = 0
    sampled = 0
    color_buckets = set()
    for y in range(0, h, step):
        base = y * w * 4
        for x in range(0, w, step):
            i = base + x * 4
            b = raw[i]
            g = raw[i + 1]
            r = raw[i + 2]
            sampled += 1
            if r + g + b > 30:
                nonblack += 1
            color_buckets.add((r >> 4, g >> 4, b >> 4))
    ratio = nonblack / sampled if sampled else 0.0
    return {
        "nonzero_ratio": ratio,
        "distinct_colors": len(color_buckets),
        "total_px": total,
    }


def save_bmp(path, raw, w, h):
    """把 32bpp BGRA bottom-up 写为 BMP（不依赖 Pillow）。"""
    row_size = w * 4
    pixel_size = row_size * h
    file_header = b"BM" + struct.pack("<IHHI", 14 + 40 + pixel_size, 0, 0, 14 + 40)

    bmi = BITMAPINFOHEADER()
    bmi.biSize = 40
    bmi.biWidth = w
    bmi.biHeight = h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    bmi.biSizeImage = pixel_size
    info_header = bytes(memoryview(bmi))

    with open(path, "wb") as f:
        f.write(file_header)
        f.write(info_header)
        f.write(raw)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    say("=" * 66)
    say("微信窗口抓图可行性探针")
    say("=" * 66)
    say(f"Python: {sys.version.split()[0]}")
    say(f"输出目录: {OUT_DIR}")
    say("")

    probe_version()
    say("")

    wins = enum_windows()
    say(f"[窗口] 匹配到 {len(wins)} 个候选窗口")
    for i, w_ in enumerate(wins):
        l, t, r, b = w_["rect"]
        say(f"  [{i}] hwnd={w_['hwnd']} proc={w_['proc']}")
        say(f"      class={w_['class']}")
        say(f"      title={w_['title']!r}  visible={w_['visible']}")
        say(f"      rect=({l},{t},{r},{b})  尺寸={r - l}x{b - t}")
    say("")

    if not wins:
        say("!! 没有找到微信窗口。请先启动并登录微信 PC 版，再重新运行。")
        write_report()
        return

    # 优先选真实主窗口（排除标题为纯英文 Weixin 的空壳代理窗口）
    scored = []
    for w_ in wins:
        l, t, r, b = w_["rect"]
        area = max(0, r - l) * max(0, b - t)
        penalty = 1 if (w_["title"] == "Weixin") else 0
        scored.append((penalty, -area, w_))
    scored.sort(key=lambda x: (x[0], x[1]))
    target = scored[0][2]

    say(f"[目标] 选用 hwnd={target['hwnd']}  class={target['class']}")
    say("")

    verdict = {}
    for flag in (2, 0):
        say(f"--- PrintWindow flag={flag} ---")
        res = capture(target["hwnd"], flag)
        if not res:
            say("  抓图失败（API 返回空）")
            verdict[flag] = "fail"
            continue
        raw, w, h, api_ok = res
        info = analyze(raw, w, h)
        say(f"  PrintWindow 返回值 = {api_ok}")
        say(f"  分辨率 = {w}x{h}")
        say(f"  非黑像素占比 = {info['nonzero_ratio']:.2%}")
        say(f"  不同颜色数 = {info['distinct_colors']}")

        path = os.path.join(OUT_DIR, f"win_{target['hwnd']}_flag{flag}.bmp")
        save_bmp(path, raw, w, h)
        say(f"  已保存: {path}")

        if info["nonzero_ratio"] > 0.15 and info["distinct_colors"] > 8:
            say("  判定: 抓到了真实画面")
            verdict[flag] = "good"
        elif info["nonzero_ratio"] > 0.02:
            say("  判定: 抓到内容但可能是残缺/半透明，需人工看 BMP 确认")
            verdict[flag] = "partial"
        else:
            say("  判定: 基本全黑，未抓到画面")
            verdict[flag] = "black"
        say("")

    say("=" * 66)
    say("结论")
    say("=" * 66)
    if any(v == "good" for v in verdict.values()):
        best = [f for f, v in verdict.items() if v == "good"]
        say(f"✅ 可用：PrintWindow flag={best} 能抓到真实画面。")
        say("   零注入路线在你的环境上成立，可以按改造方案继续。")
        say(f"   请打开 {OUT_DIR} 里的 BMP 亲自确认画面是否完整、清晰。")
    elif any(v == "partial" for v in verdict.values()):
        say("⚠️ 部分可用：抓到部分内容，必须人工查看 BMP。")
        say("   若画面残缺（缺聊天区/黑块），说明微信用了加速渲染，需要换抓图方式。")
    else:
        say("❌ 不可用：PrintWindow 两种 flag 都抓不到画面。")
        say("   说明当前微信版本的渲染方式无法用窗口消息抓取。")
        say("   此时「零注入」路线不成立，应转向协议层方案。")
    say("=" * 66)

    write_report()


def write_report():
    path = os.path.join(OUT_DIR, "report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print(f"\n报告已保存: {path}")


if __name__ == "__main__":
    main()
