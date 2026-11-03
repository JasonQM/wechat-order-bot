"""
生成占位素材：assets/price_card.png

这是回复里要附的那张固定图片的**占位图**，正式使用前请替换成你自己的
收款码 / 价目表 / 店铺介绍。脚本不依赖 Pillow，自己编码 PNG。
"""

import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "price_card.png")


def new_canvas(w, h, color):
    return bytearray(list(color) * (w * h))


def fill(px, w, h, x, y, rw, rh, color):
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(w, x + rw), min(h, y + rh)
    if x2 <= x1 or y2 <= y1:
        return
    row = bytes(color) * (x2 - x1)
    for yy in range(y1, y2):
        i = (yy * w + x1) * 3
        px[i:i + (x2 - x1) * 3] = row


def write_png(path, w, h, px):
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)


def build():
    W, H = 620, 820
    px = new_canvas(W, H, (255, 255, 255))

    # 顶部色带
    fill(px, W, H, 0, 0, W, 110, (37, 99, 235))
    # 标题占位条
    fill(px, W, H, 40, 44, 240, 22, (255, 255, 255))

    # 中间二维码区域外框
    box = 380
    bx, by = (W - box) // 2, 190
    fill(px, W, H, bx - 8, by - 8, box + 16, box + 16, (235, 238, 243))

    # 伪二维码：确定性的棋盘格
    cell = box // 21
    for gy in range(21):
        for gx in range(21):
            if (gx * 7 + gy * 13 + gx * gy) % 3 != 0:
                fill(px, W, H, bx + gx * cell, by + gy * cell, cell, cell, (32, 36, 44))

    # 底部说明占位条
    y = by + box + 60
    fill(px, W, H, 120, y, 380, 16, (206, 212, 222))
    fill(px, W, H, 170, y + 40, 280, 13, (224, 229, 236))
    fill(px, W, H, 200, y + 74, 220, 13, (224, 229, 236))

    write_png(OUT, W, H, px)
    print(f"已生成占位图: {OUT}")
    print("提示：正式使用前请替换成你自己的收款码/价目表图片（保持同名即可）。")


if __name__ == "__main__":
    build()
