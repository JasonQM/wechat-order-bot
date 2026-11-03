"""
回归测试：同一张图不能被反复点开。

背景
----
真实事故：机器人对着同一张订单截图反复点开、不往下走。
根因是「已处理」标记打在气泡对象上，而每轮循环都会重新 OCR 解析、
生成全新的气泡对象，标记等于没打；再加上去重（图片 sha256）发生在
【取图之后】，所以每次都会先点开一次，永远出不来。

修法：用「缩略图像素哈希」在【取图之前】判重，并限制总尝试次数。

本测试把微信交互全部打桩，只验证这个循环逻辑，不碰真实微信。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image  # noqa: E402

import perceive          # noqa: E402
import scheduler         # noqa: E402
import window as W       # noqa: E402

OK = FAIL = 0


def chk(name, cond, detail=""):
    global OK, FAIL
    OK += bool(cond)
    FAIL += (not cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name:42s} {detail}")


def make_bot(attempt_result):
    bot = scheduler.Bot(override_dry_run=True)

    # 固定一张图，保证 thumb_key 稳定
    fake_img = Image.new("RGB", (896, 648), (250, 250, 250))
    for y in range(300, 460):
        for x in range(380, 600):
            fake_img.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, 120))

    W.capture = lambda hwnd, flags=(2, 0): (fake_img, (0, 0, 896, 648), 1.0)
    W.compute_layout = lambda w, h: {
        "sidebar_w": 55, "chat_start_x": 320, "header_h": 85, "input_h": 140,
        "total_w_px": 896, "total_h_px": 648,
        "chat_box": (320, 85, 896, 508),
        "sidebar_box": (55, 85, 320, 628),
        "input_box": (320, 508, 896, 648),
    }

    import ocr_engine
    ocr_engine.ocr = lambda img, max_width=None, min_score=None: []

    bubble = {"type": "image", "side": "in", "sender": ".M", "text": "",
              "y_top": 300, "y_bottom": 460, "x1": 380, "x2": 600,
              "box": (380, 300, 600, 460)}

    # 每次解析都返回【全新的】气泡对象，模拟真实情况
    perceive.parse_chat = lambda img, lines, lay, th=None: ([dict(bubble)], None)
    perceive.pending_from = lambda bubbles, divider_y=None, last_cursor_y=None: list(bubbles)

    import desktop
    desktop.switch_to_group_by_name = lambda hwnd, lay, rect, name, tries=3, conf_groups=None: (True, name)
    desktop.scroll_to_bottom = lambda *a, **k: True
    desktop.scroll_chat = lambda *a, **k: (False, "stub")

    import groups as g
    g.match = lambda title, conf=None: {"allowed": True, "canonical": title, "reason": "stub"}

    bot.calls = 0

    def fake_process(hwnd, lay, rect, name, target, tkey=None):
        bot.calls += 1
        return attempt_result

    bot._process_one_image = fake_process
    return bot


def main():
    print("=" * 72)
    print("回归测试：同一张图只应被尝试一次（不接触真实微信）")
    print("=" * 72)

    # 场景 1：取图失败 —— 这是当时导致无限点击的情况
    bot = make_bot((False, "grab_failed"))
    bot.process_group(None, {}, (0, 0, 896, 648), {"text": "测试群"})
    chk("取图失败时不重复点开", bot.calls == 1, f"-> 实际尝试 {bot.calls} 次")

    # 场景 2：识别失败转人工
    bot2 = make_bot((True, "manual_pending"))
    bot2.process_group(None, {}, (0, 0, 896, 648), {"text": "测试群"})
    chk("识别失败转人工时只点一次", bot2.calls == 1, f"-> 实际尝试 {bot2.calls} 次")

    # 场景 3：正常发送
    bot3 = make_bot((True, "auto_sent"))
    bot3.process_group(None, {}, (0, 0, 896, 648), {"text": "测试群"})
    chk("正常发送时只点一次", bot3.calls == 1, f"-> 实际尝试 {bot3.calls} 次")

    # 场景 4：跨轮绝对不能无限点。
    #
    # 判定标准从「正好 3 次」改成「不超过 3 次」：
    # 后来加了位置记忆（图片框 IoU > 0.55 直接跳过），同一位置的图
    # 一轮之内就会被记住，后面几轮根本不会再试——实测只有 1 次。
    # 只尝试 1 次比尝试 3 次更好，所以这里验的是「有上限」，不是「必须用满上限」。
    bot4 = make_bot((False, "grab_failed"))
    for _ in range(6):
        bot4.process_group(None, {}, (0, 0, 896, 648), {"text": "测试群"})
    chk("连续 6 轮累计不超过 3 次（不会无限点）", 1 <= bot4.calls <= 3,
        f"-> 累计 {bot4.calls} 次")

    # 场景 5：thumb_key 稳定性（用有图案的图，纯色图任何区域都一样）
    img = Image.new("RGB", (100, 100), (10, 20, 30))
    for yy in range(100):
        for xx in range(100):
            img.putpixel((xx, yy), ((xx * 3) % 256, (yy * 3) % 256, (xx * yy) % 256))

    k1 = scheduler.thumb_key(img, (10, 10, 60, 60))
    k2 = scheduler.thumb_key(img, (10, 10, 60, 60))
    k3 = scheduler.thumb_key(img, (70, 70, 90, 90))
    chk("同区域哈希稳定", k1 == k2, f"-> {k1} / {k2}")
    chk("不同区域哈希不同", k1 != k3, f"-> {k1} / {k3}")

    # 场景 6：细微渲染差异不能改变哈希（这是防重复点击的关键）
    import random
    rnd = random.Random(7)
    noisy = img.copy()
    for _ in range(400):
        x, y = rnd.randint(0, 99), rnd.randint(0, 99)
        r, g, b = noisy.getpixel((x, y))
        delta = rnd.choice([-2, -1, 1, 2])
        noisy.putpixel((x, y), (
            max(0, min(255, r + delta)),
            max(0, min(255, g + delta)),
            max(0, min(255, b + delta)),
        ))
    k4 = scheduler.thumb_key(noisy, (10, 10, 60, 60))
    chk("轻微像素扰动后哈希不变", k1 == k4, f"-> {k1} / {k4}")

    print("-" * 72)
    print(f"{OK} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
