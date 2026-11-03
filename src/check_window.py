"""
窗口布局体检：抓一张微信画面，把各个区域框画出来，供人工确认是否贴合。

这一步是"改造"的地基。切会话、点输入框、找气泡，全都依赖这里的坐标对不对。
微信 4.x 的布局比例和 3.x 不同，必须先用肉眼标定一次。

用法：
    # 微信需已启动并登录
    python src/check_window.py

输出：
    probe_out/layout_check.png   带标注框的画面
    控制台打印各区域像素值
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw  # noqa: E402

import window as W  # noqa: E402


OUT_DIR = os.path.join(W.ROOT, "probe_out")

COLORS = {
    "sidebar": (200, 90, 60),
    "header": (60, 110, 200),
    "chat": (30, 150, 110),
    "input": (150, 90, 200),
}


def draw_boxes(img, layout):
    im = img.copy()
    d = ImageDraw.Draw(im)

    boxes = [
        ("sidebar", layout["sidebar_box"], "会话列表"),
        ("header", (layout["chat_start_x"], 0,
                    layout["total_w_px"], layout["header_h"]), "顶栏"),
        ("chat", layout["chat_box"], "聊天区"),
        ("input", layout["input_box"], "输入区"),
    ]

    for key, (x1, y1, x2, y2), label in boxes:
        color = COLORS[key]
        d.rectangle([x1, y1, x2 - 1, y2 - 1], outline=color, width=2)
        tx, ty = x1 + 6, max(0, y1 + 4)
        head = f"{label} ({x1},{y1})-({x2},{y2})"
        tw = d.textlength(head)
        d.rectangle([tx - 3, ty - 2, tx + tw + 3, ty + 14], fill=(255, 255, 255))
        d.text((tx, ty), head, fill=color)

    return im


def main():
    W.init_dpi()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("=" * 62)
    print("微信窗口布局体检")
    print("=" * 62)

    wins = W.find_wechat_windows()
    if not wins:
        print("\n没找到微信窗口。")
        print("请确认：① 微信 PC 版已启动 ② 已登录 ③ 主窗口没有最小化")
        return 1

    print(f"\n找到 {len(wins)} 个微信窗口：")
    for w in wins:
        l, t, r, b = w["rect"]
        shell = "  [空壳窗口]" if w["is_shell"] else ""
        print(f"  hwnd={w['hwnd']}  {w['size'][0]}x{w['size'][1]}  "
              f"class={w['class']}{shell}")
        print(f"      标题={w['title']!r}  位置=({l},{t})")

    main_win = W.pick_main_window(wins)
    print(f"\n选中主窗口 hwnd={main_win['hwnd']}  {main_win['size'][0]}x{main_win['size'][1]}")

    img, rect, scale = W.capture(main_win["hwnd"])
    if img is None:
        print("\n抓图失败：PrintWindow 两种 flag 都没拿到画面。")
        print("这条路线在你的微信版本上不成立。")
        return 2

    W_, H_ = img.size
    print(f"抓图成功：{W_}x{H_}  DPI 缩放={scale}")

    layout = W.compute_layout(W_, H_)
    print("\n布局计算结果（像素）：")
    for k in ("sidebar_w", "chat_start_x", "header_h", "input_h"):
        print(f"  {k:16s} = {layout[k]}")
    print(f"  {'chat_box':16s} = {layout['chat_box']}")

    raw_path = os.path.join(OUT_DIR, "layout_raw.png")
    img.save(raw_path)

    marked = draw_boxes(img, layout)
    marked_path = os.path.join(OUT_DIR, "layout_check.png")
    marked.save(marked_path)

    print("\n已保存：")
    print(f"  原始画面  {raw_path}")
    print(f"  标注画面  {marked_path}")
    print("\n请打开标注画面，检查四个框是否贴合：")
    print("  · 会话列表（红）  应正好覆盖左侧联系人列")
    print("  · 顶栏（蓝）      应正好覆盖群名那一条")
    print("  · 聊天区（绿）    应正好覆盖消息区，不含输入框")
    print("  · 输入区（紫）    应正好覆盖底部输入框")
    print("\n若有偏差，改 config/layout.json 里的四个 ratio，再跑一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
