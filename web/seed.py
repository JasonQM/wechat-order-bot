"""
演示数据生成器：往库里灌一批订单，并生成占位订单截图。

不依赖 Pillow：自己用 zlib + struct 编码 PNG，画一张"小票样子"的占位图。
真实运行时截图由机器人写入 data/images/，这里只是让面板能立刻看效果。

用法：
    python web/seed.py            # 追加 40 条演示数据
    python web/seed.py --reset    # 清空后重建
"""

import argparse
import os
import random
import struct
import sys
import zlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

GROUPS = [
    "五常大米下单群(128)",
    "粮油批发1群(246)",
    "团购接龙·城南(89)",
    "周末优选群(63)",
]

SENDERS = [
    "王海燕", "李国强", "张敏", "刘志伟", "陈晓芳", "赵磊",
    "孙丽娟", "周建华", "吴丹", "郑凯", "冯雪", "何俊",
]

NOTES = {
    "manual_pending": ["置信度偏低", "明细与合计不符", "群内重名，未 @", "图片较模糊"],
    "failed": ["发送验收未通过", "预览窗未关闭"],
}


# ---------------------------------------------------------------- 极简 PNG
def new_canvas(w, h, color=(255, 255, 255)):
    return bytearray(list(color) * (w * h))


def fill_rect(px, w, h, x, y, rw, rh, color):
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
        raw.append(0)  # filter type 0
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


def make_receipt(path, seed=0, accent=(96, 140, 220)):
    """画一张像订单小票的占位图：抬头条 + 若干文字行 + 合计条。"""
    rnd = random.Random(seed)
    W, H = 420, 620
    px = new_canvas(W, H, (255, 255, 255))

    # 外边框
    fill_rect(px, W, H, 0, 0, W, 4, (232, 235, 240))
    fill_rect(px, W, H, 0, H - 4, W, 4, (232, 235, 240))

    # 抬头
    fill_rect(px, W, H, 24, 26, 150, 22, accent)
    fill_rect(px, W, H, 330, 30, 66, 14, (226, 230, 236))

    # 分割线
    fill_rect(px, W, H, 24, 74, W - 48, 2, (238, 240, 244))

    # 明细行
    y = 100
    for _ in range(5):
        w_name = rnd.randint(140, 230)
        fill_rect(px, W, H, 24, y, w_name, 13, (206, 212, 222))
        fill_rect(px, W, H, 330, y, 66, 13, (222, 227, 234))
        y += 46

    # 分割线
    fill_rect(px, W, H, 24, y + 10, W - 48, 2, (238, 240, 244))

    # 合计
    fill_rect(px, W, H, 24, y + 34, 90, 15, (150, 158, 170))
    fill_rect(px, W, H, 250, y + 30, 146, 26, accent)

    # 底部二维码占位
    base = y + 90
    size = 96
    bx, by = (W - size) // 2, base
    for gx in range(0, size, 8):
        for gy in range(0, size, 8):
            if rnd.random() < 0.45:
                fill_rect(px, W, H, bx + gx, by + gy, 8, 8, (60, 64, 72))

    fill_rect(px, W, H, 130, by + size + 18, 160, 10, (225, 229, 235))

    write_png(path, W, H, px)


# ---------------------------------------------------------------- 造数据
def seed(count=40, reset=False):
    db.init_db()

    if reset:
        with db.connect() as conn:
            conn.execute("DELETE FROM orders")
        print("已清空 orders 表")
    else:
        with db.connect() as conn:
            existing = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        if existing:
            print(f"库里已有 {existing} 条记录，将追加 {count} 条（如需重来请加 --reset）")

    accents = [(96, 140, 220), (110, 170, 150), (210, 150, 90), (170, 130, 200)]
    now = datetime.now()

    rows = []
    for i in range(count):
        # 越靠后的越接近现在，保证"今日"有数据
        minutes_ago = int((count - i) * random.uniform(20, 90))
        created = now - timedelta(minutes=minutes_ago)

        group = random.choice(GROUPS)
        sender = random.choice(SENDERS)
        total_cents = random.randint(3000, 88000)

        r = random.random()
        if r < 0.76:
            status = "auto_sent"
        elif r < 0.90:
            status = "manual_pending"
        elif r < 0.96:
            status = "failed"
        else:
            status = "manual_sent"

        note = None
        if status in NOTES:
            note = random.choice(NOTES[status])

        accent = accents[i % len(accents)]
        rel = f"{created.strftime('%Y-%m-%d')}/demo_{i + 1:03d}.png"
        abs_path = os.path.join(db.IMAGE_DIR, rel)
        if not os.path.exists(abs_path):
            make_receipt(abs_path, seed=i, accent=accent)

        at_result = "ok" if status in ("auto_sent", "manual_sent") else (
            "degraded" if status == "manual_pending" else None)
        conf = round(random.uniform(0.78, 0.99), 3)
        if status in ("manual_pending", "failed"):
            conf = round(random.uniform(0.42, 0.74), 3)

        rows.append({
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%S"),
            "group_name": group,
            "sender": sender,
            "image_path": rel,
            "ocr_total_cents": total_cents,
            "status": status,
            "at_result": at_result,
            "elapsed_ms": random.randint(3200, 9800),
            "detail_check": "pass" if status != "manual_pending" else random.choice(["pass", "fail"]),
            "min_confidence": conf,
            "note": note,
        })

    for r in rows:
        db.insert_order(**r)

    print(f"已写入 {len(rows)} 条演示订单")
    print(f"图片目录: {db.IMAGE_DIR}")
    print(f"数据库:   {db.DB_PATH}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=40)
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()
    seed(a.count, a.reset)
