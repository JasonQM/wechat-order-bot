"""
发一条消息到指定群（带顶栏校验）。

用途：联调时手动发一条，验证「切群 → 校验 → 发送 → 验收」这条链路。
也方便在不跑完整识别流程的情况下测试发送能力。

用法：
    python tools/send_message.py --group "测试加油" --text "订单合计 700.00 元，95 折后 665.00 元"
    python tools/send_message.py --group "测试加油" --text "..." --image assets/price_card.png

安全：
    · 必须指定 --group，发送前会校验顶栏是否真的是这个群
    · 顶栏不对就中止，不会blind发
    · 不加 --yes 时只预览不发送
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import desktop      # noqa: E402
import sender       # noqa: E402
import window as W  # noqa: E402


def main():
    p = argparse.ArgumentParser(description="发一条消息到指定群")
    p.add_argument("--group", required=True, help="目标群名（会校验顶栏）")
    p.add_argument("--text", required=True, help="要发送的文本")
    p.add_argument("--image", default=None, help="附带发送的图片路径")
    p.add_argument("--yes", action="store_true", help="确认真发（不加则只预览）")
    a = p.parse_args()

    W.init_dpi()
    win = W.pick_main_window()
    if not win:
        print("✗ 没找到微信窗口")
        return 1

    hwnd = win["hwnd"]
    img, rect, _ = W.capture(hwnd)
    if img is None:
        print("✗ 抓图失败")
        return 1
    layout = W.compute_layout(*img.size)

    print("=" * 66)
    print(f"目标群：{a.group}")
    print(f"正文  ：{a.text}")
    print(f"图片  ：{a.image or '（无）'}")
    print("=" * 66)

    ok, title = desktop.switch_to_group_by_name(hwnd, layout, rect, a.group)
    if not ok:
        print(f"✗ 切群失败，顶栏读作「{title}」，中止")
        return 2
    print(f"✓ 已切到群，顶栏确认：{title}")

    if not a.yes:
        print("\n（预览模式，未发送。加 --yes 才会真发）")
        return 0

    r1 = sender.send_text(hwnd, layout, a.text, group_name=title, force=True)
    print(f"文本：{'✓' if r1.get('ok') else '✗'} {r1.get('note')}")
    if not r1.get("ok"):
        return 3

    if a.image:
        path = a.image if os.path.isabs(a.image) else os.path.join(ROOT, a.image)
        if not os.path.isfile(path):
            print(f"⚠ 图片不存在：{path}")
            return 4
        import time
        time.sleep(0.8)
        # 发图前重新抓一张，布局取最新的
        img2, _, _ = W.capture(hwnd)
        lay2 = W.compute_layout(*img2.size) if img2 else layout
        r2 = sender.send_image(hwnd, lay2, path, group_name=title, force=True)
        print(f"图片：{'✓' if r2.get('ok') else '✗'} {r2.get('note')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
