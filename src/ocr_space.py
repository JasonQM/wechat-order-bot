"""
OCR.Space 金额交叉验证（在线，只核对金额，不替代本地识别）。

关键：必须发【整张原图】，不能发裁剪后的小图。
  实测对比（同一天然气加油机照片）：
    · 整图 + Engine3 → "金额 700.00 元 / 油量 8.140 升 / 单价(元/升) 8.60"  ✅ 三字段齐全
    · 只发金额行小图 + Engine3 → "700.00"（金额对，但没有上下文）
    · 只发油量行小图 + Engine3 → "8:40"（读错）
    · 整图 + Engine1/2 → 只读到中文标签，数字全丢
  为什么会差这么多：整图里带着「金额」「油量」「单价(元/升)」这些中文标签，
  Engine3 会按标签与数字的相对位置组织结果，相当于自带版面理解。
  裁成小图就只剩一串孤立的数字，它反而读不准了。

Engine3 + chs 是唯一能出完整结构化的组合，耗时约 2 秒。

设计原则：这一段是"附加保险"，不是主流程。
  超时、配额用尽、网络断、返回空——任何异常都只返回 None，
  绝不让它拖垮或阻断本地识别。
"""

import io
import re
import threading
import time

import cv2
import requests

# 免费档限制约「每秒 1 次」，请求太密会直接返回 HTTP 500。
# 实测连续跑几次混合识别后就开始报 500，表现是"在线通道突然不可用"，
# 很容易被误判成接口坏了。这里统一压一下节奏。
_MIN_INTERVAL = 1.1
_last_call = 0.0
_throttle_lock = threading.Lock()


def _throttle():
    global _last_call
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()

API_URL = "https://api.ocr.space/parse/image"

# 默认参数取自 OCR.Space 网页端的一组实测配置：
# ChineseSimplified + 自动放大 + Engine3。
# 语言用 chs 而不是 eng 是有原因的：英文引擎会把小数点当标点吞掉，
# 中文引擎反而保留了它——同一张金额行，eng 读 "70000"，chs 读 "700.00"。
DEFAULTS = {
    "language": "chs",
    "engine": 3,
    "scale": "true",
    "timeout": 30,
}

# 合理区间，与 local_reader 保持一致
PRICE_MIN, PRICE_MAX = 4.5, 11.0
QTY_MIN, QTY_MAX = 1.0, 600.0
AMOUNT_MIN_CENTS, AMOUNT_MAX_CENTS = 500, 800000


def _encode_png(crop_bgr):
    ok, buf = cv2.imencode(".png", crop_bgr)
    if not ok:
        return None
    return buf.tobytes()


def parse_image_bytes(png_bytes, cfg):
    """把 PNG 字节发给 OCR.Space，返回 (文本, 错误说明)。"""
    key = (cfg or {}).get("api_key")
    if not key:
        return "", "未配置 api_key"

    data = {
        "language": cfg.get("language", DEFAULTS["language"]),
        "isOverlayRequired": "false",
        "OCREngine": str(cfg.get("engine", DEFAULTS["engine"])),
        "scale": str(cfg.get("scale", DEFAULTS["scale"])).lower(),
        "detectOrientation": "false",
        "isTable": "false",
    }

    try:
        r = requests.post(
            API_URL,
            headers={"apikey": key},
            files={"file": ("crop.png", png_bytes, "image/png")},
            data=data,
            timeout=cfg.get("timeout", DEFAULTS["timeout"]),
        )
    except Exception as e:
        return "", f"请求失败：{type(e).__name__}"

    if r.status_code != 200:
        return "", f"HTTP {r.status_code}"

    try:
        d = r.json()
    except Exception:
        return "", "返回不是 JSON"

    if d.get("IsErroredOnProcessing"):
        msg = d.get("ErrorMessage") or ""
        if isinstance(msg, list):
            msg = "；".join(map(str, msg))
        return "", f"服务端报错：{str(msg)[:80]}"

    results = d.get("ParsedResults") or [{}]
    text = (results[0].get("ParsedText") or "").strip()
    return text, ""


def _to_cents(text):
    """
    从返回文本里抠出金额（分）。

    只接受"带小数点的数字"——这正是它相对本地的价值所在。
    如果它也只给出纯整数（比如 70000），那和小数点被吞没区别，
    拿来做交叉验证反而会误导，所以宁可判为无效。
    """
    if not text:
        return None
    s = text.replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+)\.(\d{1,2})", s)
    if not m:
        return None
    try:
        return int(round(float(f"{m.group(1)}.{m.group(2)}") * 100))
    except ValueError:
        return None


def read_amount(crop_bgr, cfg, retry=1):
    """
    读一张金额行的裁剪图，返回 (金额分|None, 原文, 错误说明)。

    失败会重试：实测 5 次里有 1 次返回空文本（不是报错，就是空），
    重试一次基本能拿到。
    """
    if not (cfg or {}).get("enabled"):
        return None, "", "未启用"

    png = _encode_png(crop_bgr)
    if png is None:
        return None, "", "图片编码失败"

    last_err = ""
    for _ in range(retry + 1):
        text, err = parse_image_bytes(png, cfg)
        if err:
            last_err = err
            continue
        cents = _to_cents(text)
        if cents is not None:
            return cents, text, ""
        last_err = "返回里没有带小数点的金额" if not text else f"文本里没有金额：{text[:40]}"
    return None, "", last_err


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

    # 密钥从 config/app.json 读，不要写死在这里：
    # 这个文件是进版本库的，写死的 key 会跟着一起公开出去。
    import config
    cfg = config.load().get("ocr_space", {})
    if not cfg.get("api_key"):
        print("config/app.json 里没配 ocr_space.api_key")
        raise SystemExit(1)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "probe_out", "os_p0_r0.png")
    img = cv2.imread(path)
    if img is None:
        print(f"读不到 {path}")
    else:
        cents, text, err = read_amount(img, cfg)
        print("金额:", f"{cents / 100:.2f} 元" if cents else "-")
        print("原文:", repr(text))
        print("错误:", err or "无")


# ---------------------------------------------------------------- 整图识别
# 标签 → 字段。中英文写法都列上，不同加油机厂商的屏上写法不一样。
#
# 标签和数字之间不能只认「空白 + 可选冒号」。
# OCR.Space Engine3 经常把版面还原成 **Markdown 表格**：
#     | 金额 | 700.00 |
#     | 油量 | 8.140 |
# 这里「金额」和「700.00」之间隔着 " | "，竖线不是空白也不是冒号，
# 按老写法整条匹配不上，日志就变成"文本里找不到「金额」"。
#
# 但放宽也不能跨行。Engine3 另一种还原方式是「标签一列、数字一列」：
#     金额
#     油量
#     单价(元/升)
#     700.00
#     81.40
#     8.50
# 一旦允许 `[^\d]{0,12}` 吃掉换行，每个标签都能越过别家标签抓到同一个
# 700.00 —— 实测三个字段全变成 700.0，这比读不出来危险得多：
# 万一后续校验没拦住，就按 700 报价了。
# 所以这里限定「不跨行」，跨行的版面交给下面的 _block_pair 按顺序配对。
_GAP = r"[^\d\n]{0,12}?"

_AMOUNT_RE = re.compile(r"(?:金额|总金额|应收|实收)" + _GAP + r"(\d+(?:\.\d+)?)")
_QTY_RE = re.compile(r"(?:油量|加油量|数量|升数)" + _GAP + r"(\d+(?:\.\d+)?)")
# 单价和数字之间常隔着单位，比如 "单价(元/升) 8.60"。
# 早先用了 [^\d\n] 排除换行，于是 "单价(元/升)\n8.60" 完全匹配不上，
# 单价取不到，后面就拿未修正的油量去反算，700 ÷ 8.14 = 86 元/升，
# 整单被误判为不合理。这里同样交给 _block_pair 兜底。
_PRICE_RE = re.compile(r"(?:单价|油价)" + _GAP + r"(\d+(?:\.\d+)?)")

_LABEL_RULES = (
    ("amount", re.compile(r"金额|总金额|应收|实收")),
    ("qty", re.compile(r"油量|加油量|数量|升数")),
    # 注意这里必须是 unit_price，和结果字典的键一致。
    # 写成 price 的话配对结果会被静默丢弃，单价永远是空的。
    ("unit_price", re.compile(r"单价|油价")),
)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _block_pair(text):
    """
    处理「标签一列、数字一列」的版面：按出现顺序一一配对。

    只在标签数和数字数完全相等时才采信 —— 多一个少一个都可能是
    OCR 读串了行，这时候猜错就是报错价，宁可返回 None 让它转人工。
    """
    hits = []
    for key, rx in _LABEL_RULES:
        m = rx.search(text)
        if m:
            hits.append((m.start(), key))
    if len(hits) < 2:
        return None

    hits.sort()
    seq = [k for _, k in hits]
    nums = [m.group(0) for m in _NUM_RE.finditer(text)]
    if len(nums) != len(seq):
        return None

    try:
        return {k: float(v) for k, v in zip(seq, nums)}
    except ValueError:
        return None


def _first_num(rx, text):
    m = rx.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_fuel_text(text):
    """
    从 OCR 返回的整段文本里，按中文标签抠出三个字段。

    顺序：先按「标签同行跟数字」匹配（表格、冒号、"金额 700.00"都走这条）；
    有字段没拿到，再看是不是「标签块 + 数字块」的版面，按顺序配对。
    已拿到的值不会被后面覆盖，避免兜底逻辑把对的改成错的。
    """
    out = {"amount": None, "qty": None, "unit_price": None, "raw": text}
    if not text:
        return out

    out["amount"] = _first_num(_AMOUNT_RE, text)
    out["qty"] = _first_num(_QTY_RE, text)
    out["unit_price"] = _first_num(_PRICE_RE, text)

    if any(out[k] is None for k in ("amount", "qty", "unit_price")):
        paired = _block_pair(text)
        if paired:
            for k, v in paired.items():
                if out.get(k) is None:
                    out[k] = v
    return out


def fix_qty_by_algebra(amount, qty, unit_price, rel_tol=0.01):
    """
    用「油量 = 金额 ÷ 单价」修正油量的小数点错位。

    为什么需要：实测 OCR 把 81.40 升读成 "8.140"，小数点位置错了。
    但金额和单价它都读对了，两个一除就是 81.395——正好能定位错在哪。
    只处理整整 10 倍的偏移：真实油量不会因为排版就查 10 倍，
    所以只接受 ×10 / ÷10 这种明确的量级错误，其余一律不动。
    """
    if not amount or not qty or not unit_price:
        return qty, False

    expect = amount / unit_price
    if expect <= 0:
        return qty, False
    if abs(qty - expect) <= expect * rel_tol:
        return qty, False

    for factor, label in ((10.0, "左移一位"), (0.1, "右移一位")):
        cand = qty * factor
        if abs(cand - expect) <= expect * rel_tol:
            return cand, True
    return qty, False


def read_full_image(image_path, cfg):
    """
    直接对整张图做识别，返回与 local_reader.read 兼容的结构。

    这是在线通道的主入口。失败时 ok=False，调用方据此决定降级。
    """
    result = {
        "ok": False,
        "total_cents": None,
        "qty_liters": None,
        "unit_price": None,
        "items": [],
        "shipping_cents": 0,
        "doc_discount_cents": 0,
        "confidence": None,
        "reasons": [],
        "warnings": [],
        "ocr_text": "",
        "raw_json": None,
        "degraded": False,
        "channel": "ocr_space",
        "panel_found": None,
    }

    if not (cfg or {}).get("enabled"):
        result["reasons"].append("OCR.Space 未启用")
        return result

    try:
        with open(image_path, "rb") as f:
            raw = f.read()
    except Exception as e:
        result["reasons"].append(f"读不到图片：{e}")
        return result

    # 不压缩、不缩放、不裁剪 —— 原样上传。
    # 这条是实测结论：裁成小图后 Engine3 反而读不准，整图才有版面上下文。
    name = "upload.jpg" if image_path.lower().endswith((".jpg", ".jpeg")) else "upload.png"
    mime = "image/jpeg" if name.endswith(".jpg") else "image/png"

    key = cfg.get("api_key")
    data = {
        "language": cfg.get("language", DEFAULTS["language"]),
        "isOverlayRequired": "false",
        "OCREngine": str(cfg.get("engine", DEFAULTS["engine"])),
        "scale": str(cfg.get("scale", DEFAULTS["scale"])).lower(),
        "detectOrientation": "false",
        "isTable": "false",
    }

    text, err = "", ""
    for attempt in range(3):               # 偶发返回空 / 500，重试
        _throttle()
        try:
            r = requests.post(API_URL, headers={"apikey": key},
                              files={"file": (name, raw, mime)}, data=data,
                              timeout=cfg.get("timeout", DEFAULTS["timeout"]))
            if r.status_code != 200:
                err = f"HTTP {r.status_code}"
                if r.status_code >= 500:
                    time.sleep(1.5)        # 服务端抖动，等一会儿再试
                continue
            d = r.json()
            if d.get("IsErroredOnProcessing"):
                msg = d.get("ErrorMessage") or ""
                if isinstance(msg, list):
                    msg = "；".join(map(str, msg))
                err = f"服务端报错：{str(msg)[:80]}"
                continue
            text = ((d.get("ParsedResults") or [{}])[0].get("ParsedText") or "").strip()
            if text:
                break
            err = "返回空文本"
        except Exception as e:
            err = f"{type(e).__name__}"

    result["ocr_text"] = text
    if not text:
        result["reasons"].append(f"OCR.Space 没返回内容（{err}）")
        return result

    parsed = parse_fuel_text(text)
    amount, qty, unit = parsed["amount"], parsed["qty"], parsed["unit_price"]

    if amount is None:
        result["reasons"].append(f"OCR.Space 文本里找不到「金额」（{text[:60]!r}）")
        return result

    total_cents = int(round(amount * 100))
    if not (AMOUNT_MIN_CENTS <= total_cents <= AMOUNT_MAX_CENTS):
        result["reasons"].append(f"金额 {amount:.2f} 元不在合理范围")
        return result

    if unit is not None and not (PRICE_MIN <= unit <= PRICE_MAX):
        result["warnings"].append(f"单价 {unit:.2f} 元/升 超出常见范围，已改用金额÷油量推算")
        unit = None

    qty_fixed, moved = fix_qty_by_algebra(amount, qty, unit)
    if moved:
        result["warnings"].append(
            f"油量被读成 {qty:.3f}，按「金额÷单价」修正为 {qty_fixed:.2f} 升（小数点错位）")
        qty = qty_fixed

    if qty is None or unit is None:
        if qty and not unit:
            # 单价没读到时，不要盲目用 amount/qty 反算——
            # 如果油量本身小数点就错了，反算出来的单价会离谱，
            # 然后整单被"单价不合理"拦掉，真正的问题（油量错位）反而没被修正。
            # 正确做法是搜索量级：哪个倍数能让单价落回合理区间，就用哪个。
            for factor in (1.0, 10.0, 0.1, 100.0, 0.01):
                cand = amount / (qty * factor)
                if PRICE_MIN <= cand <= PRICE_MAX:
                    if factor != 1.0:
                        result["warnings"].append(
                            f"油量读成 {qty:.3f} 时油价是 {amount / qty:.2f}，不合理；"
                            f"按 {factor:g} 倍修正为 {qty * factor:.2f} 升")
                        qty = qty * factor
                    unit = cand
                    break
            else:
                result["reasons"].append(
                    f"油量 {qty:.2f} 升配不出合理油价（金额 {amount:.2f} 元）")
                return result
        elif unit and not qty:
            qty = amount / unit
        else:
            result["reasons"].append("油量和单价都没读到，无法交叉验证")
            return result

    if not (QTY_MIN <= qty <= QTY_MAX):
        result["reasons"].append(f"油量 {qty:.2f} 升不在合理范围（{QTY_MIN:.0f}~{QTY_MAX:.0f}）")
        return result
    if not (PRICE_MIN <= unit <= PRICE_MAX):
        result["reasons"].append(f"单价 {unit:.2f} 元/升不在合理范围（{PRICE_MIN}~{PRICE_MAX}）")
        return result

    calc = int(round(qty * unit * 100))
    tol = max(50, int(total_cents * 0.002))
    if abs(calc - total_cents) > tol:
        result["reasons"].append(
            f"算术不闭环：{qty:.2f} 升 × {unit:.2f} 元/升 = {calc / 100:.2f} 元，"
            f"与金额 {amount:.2f} 元差 {abs(calc - total_cents) / 100:.2f} 元")
        return result

    result.update({
        "ok": True,
        "total_cents": total_cents,
        "qty_liters": qty,
        "unit_price": unit,
        "items": [{"name": "加油", "spec": "升", "qty": qty,
                   "unit_price": unit, "subtotal": total_cents / 100.0}],
        "raw_json": parsed,
        "confidence": 0.9,
    })
    return result
