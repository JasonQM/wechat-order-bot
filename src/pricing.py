"""
算价：全系统唯一算钱的地方。

三条铁律
--------
1. 金额一律用整数「分」运算，不用 float。0.1+0.2 != 0.3 在钱上是事故。
2. 折扣用整数百分比（95 = 95 折），用整数除法四舍五入，避免浮点误差。
3. 算出来的结果必须能和截图合计对账，对不上就交给人。

自检：python src/pricing.py
"""

import sys

import config


def to_cents(value):
    """
    元 → 分。value 可以是 int/float/str（含 ￥、逗号、空格）。
    无法解析返回 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))

    s = str(value)
    for ch in ("￥", "¥", "元", ",", "，", " ", "\u00a0"):
        s = s.replace(ch, "")
    s = s.strip()
    if not s:
        return None
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


def discount_cents(total_cents, percent=95):
    """
    按百分比打折，四舍五入到分。
    119.80 元 × 95% = 113.81 元（11980*95=1138100, +50 → 1138150 //100 = 11381）
    """
    if total_cents is None:
        return None
    pct = int(percent)
    return (int(total_cents) * pct + 50) // 100


def format_cents(cents):
    if cents is None:
        return "-"
    neg = cents < 0
    c = abs(int(cents))
    return f"{'-' if neg else ''}{c // 100}.{c % 100:02d}"


def item_subtotal_cents(item):
    """
    一条明细的小计（分）。

    优先用已经换算好的 *_cents 字段——调用方若已做过元→分转换，
    这里再转一次就会放大 100 倍（这个坑自检抓到过一次）。
    最后才回退到「元」字段自己换算。
    """
    pre = item.get("subtotal_cents")
    if pre is not None:
        return int(pre)

    sub = to_cents(item.get("subtotal"))
    if sub is not None:
        return sub

    unit = item.get("unit_price_cents")
    if unit is None:
        unit = to_cents(item.get("unit_price"))
    qty = item.get("qty")
    if unit is None or qty is None:
        return None
    try:
        return int(round(float(qty) * unit))
    except (TypeError, ValueError):
        return None


def _auto_tolerance(total_cents):
    """
    默认容差：按总价的 0.05%，最少 3 分。

    为什么不能死守 2 分：
    加油机、地磅、水表这类计量设备，屏幕上显示的数量是四舍五入过的，
    内部精度更高。例如屏显「81.40 升 × 8.60 元」，实际可能是 81.395 升，
    所以 81.40×8.60=700.04，而屏上写 700.00 —— 差 4 分是正常的舍入，不是错。

    0.05% 对 700 元是 0.35 元，足以容纳显示舍入，又远小于任何真实录入错误。
    """
    if total_cents is None:
        return 3
    return max(3, int(round(abs(total_cents) * 0.0005)))


def check_arithmetic(items, total_cents, shipping_cents=0, doc_discount_cents=0,
                     tol_cents=None):
    """
    算术闭环校验：Σ明细 + 运费 − 单据优惠 是否等于截图合计。

    tol_cents 传 None 时按总价 0.05% 自动计算（见 _auto_tolerance）。

    返回 (ok: bool, computed_cents: int|None, reason: str)
    """
    if total_cents is None:
        return False, None, "没读到截图合计"

    if not items:
        return False, None, "没识别出任何明细行"

    subtotals = []
    for i, it in enumerate(items):
        s = item_subtotal_cents(it)
        if s is None:
            name = it.get("name") or f"第{i + 1}行"
            return False, None, f"明细「{name}」缺单价或数量，算不出小计"
        subtotals.append(s)

    computed = sum(subtotals) + int(shipping_cents or 0) - int(doc_discount_cents or 0)
    diff = computed - int(total_cents)

    tol = _auto_tolerance(total_cents) if tol_cents is None else int(tol_cents)

    if abs(diff) <= tol:
        return True, computed, "明细与合计一致"

    return False, computed, (
        f"明细算出来 {format_cents(computed)} 元，"
        f"截图合计 {format_cents(total_cents)} 元，差 {format_cents(abs(diff))} 元"
        f"（容差 {format_cents(tol)} 元）"
    )


def build_reply(sender, total_cents, final_cents, conf=None):
    """
    生成回复正文。sender 为空时自动去掉「{name}，」前缀。
    """
    c = conf or config.load()
    tpl = c["reply"]["template"]

    if sender:
        text = tpl.format(
            name=sender,
            total=format_cents(total_cents),
            final=format_cents(final_cents),
            discount_percent=c.get("discount_percent", 95),
            discount_label=c["reply"].get("discount_label", "95 折"),
        )
    else:
        text = tpl.format(
            name="",
            total=format_cents(total_cents),
            final=format_cents(final_cents),
            discount_percent=c.get("discount_percent", 95),
            discount_label=c["reply"].get("discount_label", "95 折"),
        )
        # 去掉开头的「，」或「, 」
        text = text.lstrip("，, ")

    return text.strip()


# ---------------------------------------------------------------- 自检
def _selfcheck():
    cases = []

    # 折扣
    cases.append(("119.80 × 95%", discount_cents(11980, 95), 11381))
    cases.append(("100.00 × 95%", discount_cents(10000, 95), 9500))
    # 0.01 元 = 1 分，×95% = 0.95 分，四舍五入 → 1 分（不是 0）
    cases.append(("0.01 × 95%", discount_cents(1, 95), 1))
    cases.append(("0.02 × 50%", discount_cents(2, 50), 1))
    cases.append(("0.01 × 50%", discount_cents(1, 50), 1))
    cases.append(("1.00 × 95%", discount_cents(100, 95), 95))
    # 0.05 分的情况：119_85*95 = 11385.75 → 11386
    cases.append(("119.85 × 95%", discount_cents(11985, 95), 11386))

    # 元转分
    cases.append(("'￥1,299.90'", to_cents("￥1,299.90"), 129990))
    cases.append(("'59.9'", to_cents("59.9"), 5990))
    cases.append(("59.9 float", to_cents(59.9), 5990))
    cases.append(("空值", to_cents(None), None))

    ok = 0
    fail = 0
    for name, got, want in cases:
        good = got == want
        ok += good
        fail += (not good)
        print(f"[{'PASS' if good else 'FAIL'}] {name:20s} -> {got}  (期望 {want})")

    # 算术闭环
    items = [
        {"name": "大米", "qty": 2, "unit_price": 59.9, "subtotal": 119.8},
    ]
    print()
    r = check_arithmetic(items, 11980)
    print(f"[{'PASS' if r[0] else 'FAIL'}] 单行明细一致 -> {r[0]}  {r[2]}")
    ok += r[0]
    fail += (not r[0])

    r2 = check_arithmetic(items, 12980)
    print(f"[{'PASS' if not r2[0] else 'FAIL'}] 合计对不上应拦截 -> {r2[0]}  {r2[2]}")
    ok += (not r2[0])
    fail += r2[0]

    items2 = [
        {"name": "A", "qty": 1, "unit_price": 50, "subtotal": 50},
        {"name": "B", "qty": 3, "unit_price": 10, "subtotal": 30},
    ]
    r3 = check_arithmetic(items2, 9000, shipping_cents=1000)
    print(f"[{'PASS' if r3[0] else 'FAIL'}] 含运费 80+10=90 -> {r3[0]}  {r3[2]}")
    ok += r3[0]
    fail += (not r3[0])

    # 回复文案
    conf = config.load()
    print()
    print("有昵称：", build_reply("张三", 11980, 11381, conf))
    print("无昵称：", build_reply(None, 11980, 11381, conf))

    print(f"\n{ok} 通过, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_selfcheck())
