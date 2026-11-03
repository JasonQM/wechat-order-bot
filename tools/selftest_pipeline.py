"""
离线全链路自检：把 OCR 和视觉模型打桩，验证「识别 → 交叉校验 → 算术闭环 → 算价 → 落库」。

这一步不需要微信、不需要 API Key，专门用来证明管道逻辑是对的。
真机联调时遇到的问题通常不是逻辑错，而是坐标/阈值错，所以两者要分开验。

    python tools/selftest_pipeline.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "web"))

import config          # noqa: E402
import ocr_engine      # noqa: E402
import pricing         # noqa: E402
import recognize       # noqa: E402
import store           # noqa: E402

OK = 0
FAIL = 0


def chk(name, cond, detail=""):
    global OK, FAIL
    OK += bool(cond)
    FAIL += (not cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name:40s} {detail}")


def fake_ocr(text):
    """把 OCR 打桩：返回一行大文本，坐标随便给。"""
    def _ocr(img, max_width=None, min_score=None):
        return [{"text": text, "score": 0.99,
                 "x1": 0, "y1": 0, "x2": 100, "y2": 20,
                 "cx": 50, "cy": 10, "w": 100, "h": 20}]
    return _ocr


def fake_vision(payload):
    def _call(image_path, conf):
        return payload, "{}", (100, 100)
    return _call


ORDER_TEXT = """五常稻花香大米 5kg
单价 59.90  数量 2  小计 119.80
运费 0.00
合计 119.80"""

GOOD = {
    "items": [{"name": "五常稻花香大米", "spec": "5kg", "qty": 2,
               "unit_price": 59.90, "subtotal": 119.80}],
    "shipping": 0, "doc_discount": 0, "total": 119.80,
    "confidence": {"items": 0.96, "total": 0.98},
    "uncertain_fields": [],
}

MADE_UP = dict(GOOD)
MADE_UP = {
    "items": [{"name": "五常稻花香大米", "spec": "5kg", "qty": 2,
               "unit_price": 59.90, "subtotal": 119.80}],
    "shipping": 0, "doc_discount": 0, "total": 168.00,   # 图上根本没有 168
    "confidence": {"items": 0.95, "total": 0.95},
    "uncertain_fields": [],
}

NO_TOTAL = dict(GOOD)
NO_TOTAL = {
    "items": GOOD["items"], "shipping": 0, "doc_discount": 0,
    "total": None, "confidence": {"items": 0.9, "total": 0.1},
    "uncertain_fields": ["total"],
}

BAD_ARITH = {
    "items": [{"name": "大米", "spec": "5kg", "qty": 2,
               "unit_price": 59.90, "subtotal": 119.80}],
    "shipping": 0, "doc_discount": 0, "total": 129.80,   # 图上确有 129.80，但明细对不上
    "confidence": {"items": 0.95, "total": 0.95},
    "uncertain_fields": [],
}

BAD_ARITH_TEXT = ORDER_TEXT.replace("合计 119.80", "合计 129.80")


def run_case(name, text, payload, expect_ok):
    ocr_engine.ocr = fake_ocr(text)
    recognize.call_vision = fake_vision(payload)

    # 直接传 ocr_lines，跳过 Image.open（桩不依赖真实图片内容）
    lines = ocr_engine.ocr(None)

    # 这里必须显式指定 cloud 模式。
    # 主流程默认是 local_then_cloud（先本地读图），而这套测试验证的是
    # 「OCR 原文 + 模型返回 → 校验 → 算价」这条管道，全程用桩。
    # 不指定 mode 的话本地会去读 dummy.png（不存在），直接失败，
    # 6 个用例会一起挂掉——而且挂的原因跟被测逻辑毫无关系。
    conf = config.load()
    conf["recognition"] = dict(conf.get("recognition") or {}, mode="cloud")

    r = recognize.recognize_order("dummy.png", conf, ocr_lines=lines)

    chk(name, r["ok"] == expect_ok,
        f"-> ok={r['ok']} total={r['total_cents']} 原因={r['reasons']}")
    return r


def main():
    print("=" * 70)
    print("离线全链路自检（OCR 与视觉模型已打桩）")
    print("=" * 70)

    # 1. 正常订单应通过
    r = run_case("正常订单：应通过", ORDER_TEXT, GOOD, True)
    chk("  合计解析正确", r["total_cents"] == 11980, f"-> {r['total_cents']}")

    # 2. 折扣
    final = pricing.discount_cents(r["total_cents"], 95)
    chk("  95折计算", final == 11381, f"-> {final} 分 = {pricing.format_cents(final)} 元")

    # 3. 模型编造数字应被拦
    r2 = run_case("模型编造金额：应拦截", ORDER_TEXT, MADE_UP, False)
    chk("  拦截原因含「找不到出处」",
        any("找不到出处" in x for x in r2["reasons"]), f"-> {r2['reasons']}")

    # 4. 缺合计应被拦
    r3 = run_case("缺合计：应拦截", ORDER_TEXT, NO_TOTAL, False)
    # 文案会随实现演进（早期是"没识别到合计"，多采样后是"多次识别都没得到合计金额"），
    # 所以断言只看"确实因为拿不到合计而被拦"，不绑死具体措辞。
    chk("  拦截原因指向「拿不到合计金额」",
        any(("合计" in x) or ("不确定" in x) for x in r3["reasons"]), f"-> {r3['reasons']}")

    # 5. 明细与合计不符应被拦
    r4 = run_case("明细与合计不符：应拦截", BAD_ARITH_TEXT, BAD_ARITH, False)
    chk("  拦截原因含差值说明",
        any("差" in x for x in r4["reasons"]), f"-> {r4['reasons']}")

    # 6. 落库 + 幂等
    print("-" * 70)
    store.init_db()
    import hashlib

    import db as _db

    h = hashlib.sha256(b"selftest-image").hexdigest()
    gname = "__自检群__"

    with _db.connect() as c:
        c.execute("DELETE FROM orders WHERE group_name=?", (gname,))

    oid = store.insert_order(
        created_at="2026-01-01T00:00:00", group_name=gname, sender="自检",
        image_path="selftest.png", image_sha256=h,
        ocr_total_cents=11980, discount_rate=0.95, status="auto_sent",
        detail_check="pass", min_confidence=0.96,
    )
    chk("落库成功", bool(oid), f"-> 订单 #{oid}")

    found = store.find_by_sha(gname, h)
    chk("按图片哈希能查到（幂等基础）", found is not None and found["id"] == oid)

    row = _db.fetch_order(oid)
    chk("折后价已算好并入库", row["final_cents"] == 11381,
        f"-> {row['final_cents']} 分")

    # 重复插入应被唯一索引挡住
    dup_blocked = False
    try:
        store.insert_order(
            created_at="2026-01-01T00:00:00", group_name=gname, sender="自检",
            image_path="selftest.png", image_sha256=h,
            ocr_total_cents=11980, discount_rate=0.95, status="auto_sent",
        )
    except Exception:
        dup_blocked = True
    chk("同一张图重复入库被数据库挡住", dup_blocked)

    with _db.connect() as c:
        c.execute("DELETE FROM orders WHERE group_name=?", (gname,))

    print("-" * 70)
    print(f"{OK} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
