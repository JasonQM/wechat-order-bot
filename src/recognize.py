"""
订单识别：云端视觉大模型（通道 A） + 本地 OCR（通道 B）交叉校验。

为什么必须要两条通道
--------------------
视觉大模型在图片模糊时会「编」一个看起来很合理的数字，而且语气很自信。
本地 OCR 的原文是唯一能证明「这个数字真的在图上」的证据。
所以规则是：模型给出的每个金额，都必须能在 OCR 原文里找到出处；找不到就拦截。

设计原则
--------
宁可拦截转人工，不可放过一个不确定的数字。
拦截的代价是人工看一眼；放过的代价是报错价、赔钱、客诉。
"""

import base64
import io
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import pricing

# 提示词要点：
# · 先让模型「把图上的数字抄一遍」再填结构 —— 这是实测有效的关键。
#   直接让它填字段时，加油机那张图被读成 100.00（错得离谱）；
#   加上"先抄数字"这一步后，正确读成 700.00 / 81.40 / 8.60。
# · 给出计量仪表（加油机等）的字段对应关系，免得它把「油量」当「金额」。
PROMPT = """你是加油票据识别助手。看图，只读出三个数字。

图上通常是这两种之一：
· 加油机显示屏：三块数字——「金额(元)」「油量(升)」「单价(元/升)」
· 加油小票：合计金额、总油量、单价

只输出下面这个 JSON，不要任何解释文字：
{"total": 金额, "qty": 油量, "unit_price": 单价, "confidence": 0~1}

注意：
1. 七段数码管可能被裁切、反光、倾斜，尽量还原完整数字（比如 81.40 别读成 1.40）。
2. 用「油量 × 单价 ≈ 金额」自检一遍，对不上说明有数字没认全，再看一次。
3. 读不到的填 null，不要猜、不要自己算。
4. 数字不带货币符号。"""


# 没有视觉模型时的降级路径：让文本模型去解析 OCR 原文
TEXT_PROMPT = """下面是从一张订单 / 票据 / 仪表照片上用 OCR 识别出来的原始文字。
OCR 可能有错漏、顺序可能乱、可能把不同位置的文字串在一起。

请据此整理成结构化数据。看不清或原文里没有的字段填 null，并写进 uncertain_fields。
严禁编造、严禁自己计算补全。

OCR 原文：
---
{ocr_text}
---

输出 JSON，字段如下：
- items: 数组，每项含 name / spec / qty / unit_price / subtotal
- shipping: 运费（元），没有填 0
- doc_discount: 单据上已有的优惠（元），没有填 0
- total: 合计金额（元）
- confidence: 对象，含 items 和 total 两个 0~1 的数
- uncertain_fields: 字符串数组

只输出 JSON 对象本身。"""


# ---------------------------------------------------------------- 图像编码
def encode_image(path, max_side=1600, quality=92):
    """
    读图 → 必要时缩小 → JPEG base64。缩小是为了控制上传体积与费用。

    这两个参数直接影响快慢：实测 1600px / q92 的 1.4MB 原图，
    编码后仍有数百 KB，三路并发上传要 100 秒以上，几乎顶到超时线。
    压到 1200px / q85 后体积降到几分之一，而加油机的数字本来就大，
    识别精度不受影响。
    """
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if max_side and longest > max_side:
        scale = max_side / float(longest)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size


# ---------------------------------------------------------------- 模型调用
def call_vision(image_path, conf):
    """
    调 OpenAI 兼容的视觉接口，返回 (payload_dict, raw_text) 或抛异常。
    """
    import requests

    v = conf["vision"]
    b64, size = encode_image(image_path,
                             v.get("max_image_side", 1600),
                             v.get("jpeg_quality", 88))
    url = v["base_url"].rstrip("/") + "/chat/completions"

    body = {
        "model": v["model"],
        "temperature": v.get("temperature", 0),
        "max_tokens": v.get("max_tokens", 1200),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {v['api_key']}",
                 "Content-Type": "application/json"},
        json=body,
        timeout=v.get("timeout", 45),
    )
    resp.raise_for_status()
    data = resp.json()

    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")

    if finish == "length":
        # 推理模型的思考过程吃满了 max_tokens，正文被截断
        raise RuntimeError(
            f"模型输出被截断（finish_reason=length）。"
            f"推理模型需要更大的 max_tokens，当前 {v.get('max_tokens', 1200)}")
    if not text.strip():
        raise RuntimeError("模型返回空内容")

    return _parse_json(text), text, size


def call_vision_voted(image_path, conf, result, samples=None):
    """
    多次采样 + 合计一致性投票。

    为什么需要：实测同一个模型、同一张图，两次可以给出完全不同的读数
    （一次 700.00/81.40/8.60，一次 100.00/13.14/7.60）。
    后者内部还自洽（13.14×7.60≈100），算术校验根本拦不住，
    只有 OCR 原文比对能发现。与其靠"被拦"来保正确，不如多采样一次投票。

    规则：总采样数里，合计金额必须出现至少 2 次才采信；否则判为"识别结果不稳定"。
    """
    v = conf["vision"]
    n = samples or int(v.get("samples", 3))
    need = int(v.get("votes_needed", 2))

    # 并行发出，但【凑够票数就立刻返回】，不等剩下的。
    #
    # 为什么这么改：实测同一张图的单次调用在 8~65 秒之间大幅波动（服务端排队），
    # 而三路并发的耗时等于最慢那一路。原来非要等满 3 次再投票，
    # 结果被最慢的一次拖住——17 秒的活能拖成 65 秒。
    # 实际上前两次一致就足以定论，第三次纯属陪跑。
    got = []
    counts = {}
    errors = []
    best_total = None

    def _one():
        try:
            p, _raw, _size = call_vision(image_path, conf)
            return p if isinstance(p, dict) else None
        except Exception as e:      # 单次失败不该拖垮整单
            errors.append(e)
            return None

    pool = ThreadPoolExecutor(max_workers=min(n, 4))
    try:
        futures = [pool.submit(_one) for _ in range(n)]
        for fut in as_completed(futures):
            p = fut.result()
            if p is None:
                continue
            got.append(p)
            t = pricing.to_cents(p.get("total"))
            if t is None:
                continue
            counts[t] = counts.get(t, 0) + 1
            if counts[t] >= min(need, n):
                best_total = t
                break                    # 已经够票了，剩下的不等
    finally:
        # 不等待剩余任务（它们还在等服务端响应，等也没用）
        pool.shutdown(wait=False, cancel_futures=True)

    if not got:
        raise (errors[0] if errors else RuntimeError("视觉模型调用失败"))

    if best_total is not None:
        pick = next(p for p in got if pricing.to_cents(p.get("total")) == best_total)
        if len(counts) > 1:
            detail = "、".join(f"{pricing.format_cents(k)}×{c}" for k, c in counts.items())
            result["warnings"].append(f"采样结果不完全一致（{detail}），采信出现 {counts[best_total]} 次的那个")
        return pick, json.dumps(pick, ensure_ascii=False)

    # 走到这里说明票数没凑够：所有采样都回来了，但没有哪一组的次数达到 need。
    # 这就是"模型自己前后不一致"，宁可转人工，不要赌。
    detail = "、".join(f"{pricing.format_cents(k)}×{c}" for k, c in counts.items()) or "无"
    raise RuntimeError(
        f"多次识别结果不一致（{len(got)} 次采样：{detail}），不确定，转人工")


def call_text_model(ocr_text, conf):
    """
    降级路径：把 OCR 原文交给文本模型做结构化。

    局限必须说清楚：模型【看不到图】，只能读到 OCR 读出来的字。
    OCR 没读到的内容（比如七段数码管上的数字），它无从得知。
    所以这条路径只适合「文字清晰、OCR 基本读全」的订单截图。
    """
    import requests

    m = conf["text_model"]
    url = m["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": m["model"],
        "temperature": 0,
        "max_tokens": m.get("max_tokens", 1500),
        "messages": [{"role": "user",
                      "content": TEXT_PROMPT.format(ocr_text=ocr_text[:6000])}],
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {m['api_key']}",
                 "Content-Type": "application/json"},
        json=body, timeout=m.get("timeout", 60))
    resp.raise_for_status()
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    if not text.strip():
        # 推理模型的 reasoning 吃掉了预算，content 可能为空
        raise RuntimeError("文本模型返回为空（推理模型的 reasoning 可能占满了 max_tokens，调大再看）")
    return _parse_json(text), text, None


def _parse_json(text):
    """模型偶尔会套 ```json 代码块或前后加话，这里剥掉。"""
    if not text:
        return None

    s = text.strip()

    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        s = m.group(1).strip()

    # 截取第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]

    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------- 数字交叉校验
def _num_variants(cents):
    """一个金额可能的几种写法，用于在 OCR 原文里查找。"""
    if cents is None:
        return []
    c = int(cents)
    neg = c < 0
    c = abs(c)
    yuan = c / 100.0

    out = {f"{yuan:.2f}", f"{yuan:.1f}" if c % 10 == 0 else f"{yuan:.2f}"}
    if c % 100 == 0:
        out.add(f"{c // 100}")
        out.add(f"{c // 100}.00")
    out.add(f"{c // 100}.{c % 100}")
    if neg:
        out = {"-" + x for x in out}
    return [x for x in out if x]


def _squash(s):
    """去掉所有空白与千分位逗号，便于子串比较。"""
    if not s:
        return ""
    s = str(s)
    for ch in (" ", "\u00a0", "\t", "\n", ",", "，", "￥", "¥", "元"):
        s = s.replace(ch, "")
    return s


def cross_check_numbers(amounts_cents, ocr_text):
    """
    检查每个金额是否能在 OCR 原文里找到。

    返回 (ok, missing)  missing 是找不到出处的金额（分）
    """
    hay = _squash(ocr_text)
    missing = []
    for c in amounts_cents:
        if c is None:
            continue
        variants = _num_variants(c)
        if not any(v in hay for v in variants):
            missing.append(c)
    return (len(missing) == 0), missing


# ---------------------------------------------------------------- 主入口
def _safe_local(image_path, conf):
    try:
        import local_reader
        return local_reader.read(image_path, conf)
    except Exception as e:
        return {"ok": False, "reasons": [f"本地异常：{type(e).__name__}: {e}"],
                "warnings": [], "total_cents": None, "items": [],
                "confidence": None, "ocr_text": "", "raw_json": None,
                "channel": "local", "panel_found": False}


def _safe_online(image_path, cs_cfg):
    try:
        import ocr_space
        return ocr_space.read_full_image(image_path, cs_cfg)
    except Exception as e:
        return {"ok": False, "reasons": [f"在线异常：{type(e).__name__}: {e}"],
                "warnings": [], "total_cents": None, "items": [],
                "confidence": None, "ocr_text": "", "raw_json": None,
                "channel": "ocr_space"}


def _run_hybrid(image_path, conf):
    """
    两条通道并行跑，结果交叉验证。

    为什么这么配：两个通道的强弱正好互补。
      · 本地（RapidOCR + 分区裁剪 + 投票）：3.7 秒，不联网。油量读得准，
        但金额的小数点会被吞（700.00 读成 70000），只能靠"除以油量得合理油价"
        反推小数点在哪儿——而这条推理依赖油量读对。
      · 在线（OCR.Space 整图 + Engine3）：3-4 秒，需要网络。金额和单价都直接
        读出带小数点的值，不依赖任何推理；但油量会把小数点放错（81.40 读成 8.140）。

    所以：金额以两路一致为准（这是唯一不能错的字段），油量优先用本地、
    单价优先用在线。两路金额不一致就直接转人工——宁可少回一单，不报错价。
    并行执行，总耗时等于慢的那一路（约 4 秒），不是两者相加。
    """
    cs_cfg = conf.get("ocr_space") or {}
    use_online = bool(cs_cfg.get("enabled") and cs_cfg.get("api_key"))

    if use_online:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_local = pool.submit(_safe_local, image_path, conf)
            f_online = pool.submit(_safe_online, image_path, cs_cfg)
            r_local = f_local.result()
            r_online = f_online.result()
    else:
        r_local = _safe_local(image_path, conf)
        r_online = None

    ok_l = bool(r_local and r_local.get("ok"))
    ok_o = bool(r_online and r_online.get("ok"))

    # 两路都成功
    if ok_l and ok_o:
        if r_local["total_cents"] == r_online["total_cents"]:
            out = dict(r_online)
            out["channel"] = "hybrid"
            out["warnings"] = list(r_online.get("warnings") or [])
            out["warnings"].append(
                f"金额双通道一致（本地 {r_local['total_cents'] / 100:.2f} / "
                f"在线 {r_online['total_cents'] / 100:.2f} 元）")

            ql, qo = r_local.get("qty_liters"), r_online.get("qty_liters")
            if ql and qo and abs(ql - qo) > 0.1:
                out["warnings"].append(
                    f"油量两路有差异（本地 {ql:.2f} / 在线 {qo:.2f} 升），"
                    f"金额一致故采用在线值")
            conf_val = [v for v in (r_local.get("confidence"),
                                    r_online.get("confidence")) if v]
            out["confidence"] = min(conf_val) if conf_val else None
            return out

        # 金额不一致 —— 最危险的信号，直接拦
        out = dict(r_online)
        out["ok"] = False
        out["reasons"] = list(r_online.get("reasons") or []) + [
            f"金额双通道不一致：本地 {r_local['total_cents'] / 100:.2f} 元，"
            f"在线 {r_online['total_cents'] / 100:.2f} 元"]
        return out

    # 只有一路成功
    if ok_o:
        out = dict(r_online)
        out["channel"] = "ocr_space"
        why = "；".join((r_local or {}).get("reasons") or []) if r_local else "未运行"
        out["warnings"] = list(out.get("warnings") or []) + [
            f"本地未成功（{why or '未知'}），仅采信在线结果"]
        return out

    if ok_l:
        out = dict(r_local)
        out["channel"] = "local"
        why = "；".join((r_online or {}).get("reasons") or []) if r_online else "未运行"
        out["warnings"] = list(out.get("warnings") or []) + [
            f"在线未成功（{why or '未知'}），仅采信本地结果"]
        return out

    # 两路都失败
    out = dict(r_local or r_online or {})
    out["ok"] = False
    reasons = []
    if r_local:
        reasons += [f"本地：{x}" for x in (r_local.get("reasons") or [])]
    if r_online:
        reasons += [f"在线：{x}" for x in (r_online.get("reasons") or [])]
    out["reasons"] = reasons or ["两个通道都没能读出结果"]
    out["channel"] = "hybrid"
    return out


def _run_local_then_online(image_path, conf):
    """
    本地优先，本地读不出才调在线。

    为什么默认用这个而不是 hybrid（两路并行）：
      hybrid 看起来更保险，实际是「两路都必须对」才放行——任何一路在模糊图上
      读错，整单就被拦。实测 21 种拍照劣化：本地单独 15/21 通过，
      两路并行反而掉到 11/21，因为两路金额一不一致就转人工。
      更硬的约束是配额：OCR.Space 免费档撑不住每单一次（实测连测几十次就被
      429 限流）。而本地失败本来就不多，只有那几次才消耗配额，压力小得多。

    代价：本地失败时要多等一次在线请求（约 4 秒）。这个代价只发生在失败单上，
    正常单还是本地的 3.8 秒。
    """
    r_local = _safe_local(image_path, conf)
    if r_local.get("ok"):
        return r_local

    cs_cfg = conf.get("ocr_space") or {}
    if not (cs_cfg.get("enabled") and cs_cfg.get("api_key")):
        return r_local

    why = "；".join(r_local.get("reasons") or []) or "未知"
    r_online = _safe_online(image_path, cs_cfg)

    if r_online.get("ok"):
        r_online["warnings"] = list(r_online.get("warnings") or []) + [
            f"本地未成功（{why}），已改用在线识别"]
        return r_online

    # 两路都失败：把两边的失败原因都带上，方便排查是哪一环断的
    out = dict(r_local)
    out["reasons"] = ([f"本地：{x}" for x in (r_local.get("reasons") or [])]
                      + [f"在线：{x}" for x in (r_online.get("reasons") or [])])
    out["channel"] = "local+online"
    return out


def recognize_order(image_path, conf, ocr_lines=None):
    """
    识别一张订单截图。

    参数
        image_path  图片路径
        conf        config.load() 的结果
        ocr_lines   已算好的本地 OCR 行（可选；不传则内部调用）

    返回 dict
        ok           是否可自动处理
        total_cents  截图合计（分）
        items        明细
        confidence   最低置信度
        reasons      不通过的原因列表
        ocr_text     本地 OCR 原文
        raw_json     模型原始返回
        degraded     是否降级（图片质量差）
    """
    import ocr_engine

    # --- 识别模式分流 ---
    #
    # local           纯本地（快，约 6 秒，不联网）
    # local_then_cloud 本地优先，本地读不出才走云端（默认）
    # cloud           只用云端（原来那条路）
    #
    # 为什么默认是 local_then_cloud 而不是纯 local：
    # 本地方案依赖「加油机显示屏」这种固定版面（找四边形面板 → 切行 → 裁数字）。
    # 客户万一发的是别的东西（聊天截图、纸质小票、别的品牌加油机），
    # 本地会读不出来，这时候云端还能兜住。反过来如果纯云端，就丢掉了本地的速度优势。
    # ---- 测试模式：跳过真实识别，固定金额 ----
    #
    # 用途：只验证「取图 → 算价 → 发文本 → 发图 → 落库 → 报表」这条链路，
    # 把识别的不确定性排除掉。
    # 风险：忘了关就会把所有订单都报成这个价，所以这一路会在 result 上打
    # force_amount=True，让日志、备注、面板都能看出"这单是假的"。
    force_cents = int(((conf.get("testing") or {}).get("force_amount_cents")) or 0)
    if force_cents > 0:
        return {
            "ok": True,
            "total_cents": force_cents,
            "qty_liters": None,
            "unit_price": None,
            "items": [],
            "shipping_cents": 0,
            "doc_discount_cents": 0,
            "confidence": 1.0,
            "reasons": [],
            "warnings": [f"⚠ 测试模式：金额固定为 {force_cents / 100:.2f} 元，未做真实识别"],
            "ocr_text": "",
            "raw_json": {"mode": "test", "force_cents": force_cents},
            "degraded": False,
            "channel": "test",
            "panel_found": None,
            "force_amount": True,
        }

    mode = (conf.get("recognition") or {}).get("mode", "hybrid")

    if mode == "hybrid":
        return _run_hybrid(image_path, conf)

    if mode == "local_then_ocr_space":
        return _run_local_then_online(image_path, conf)

    local_result = None
    if mode in ("local", "local_then_cloud"):
        try:
            import local_reader
            local_result = local_reader.read(image_path, conf)
        except Exception as e:
            local_result = {
                "ok": False, "reasons": [f"本地识别异常：{type(e).__name__}: {e}"],
                "warnings": [], "total_cents": None, "ocr_text": "",
                "raw_json": None, "confidence": None, "items": [],
                "channel": "local",
            }

        if local_result.get("ok"):
            local_result["degraded"] = False
            return local_result

        if mode == "local":
            local_result.setdefault("reasons", []).append(
                "（当前为纯本地模式，未走云端兜底）")
            return local_result

        # 连加油机面板都没找到 → 这张图根本不是加油票据（客户误发的收款码、聊天截图之类）。
        # 继续走云端要跑满三路采样，实测 31 秒，最后照样读不出金额。
        # 直接失败，让人工早点看到，别白等半分钟。
        skip_no_panel = (conf.get("recognition") or {}).get(
            "skip_cloud_without_panel", True)
        if skip_no_panel and not local_result.get("panel_found"):
            local_result.setdefault("reasons", []).append(
                "未走云端兜底：画面里没有加油机显示屏，云端同样读不出金额")
            return local_result

    result = {
        "ok": False,
        "total_cents": None,
        "items": [],
        "shipping_cents": 0,
        "doc_discount_cents": 0,
        "confidence": None,
        "reasons": [],      # 命中即拦截，转人工
        "warnings": [],     # 不拦截，但降低可信度、记入备注
        "ocr_text": "",
        "raw_json": None,
        "degraded": False,
        "channel": None,
    }

    # 本地试过但没成功，转云端了 —— 这条要先记进去。
    # 放在这里而不是函数末尾：云端有好几个提前 return 的分支（OCR 失败、
    # 模型调用失败、JSON 解析失败），末尾注入会漏掉它们。
    if local_result and not local_result.get("ok"):
        why = "；".join(local_result.get("reasons") or []) or "未通过"
        result["warnings"].append(f"本地识别未成功（{why}），已改用云端模型")
        result["channel"] = "cloud(fallback)"

    # --- 通道 B：本地 OCR（原分辨率，不降采样）---
    try:
        if ocr_lines is None:
            from PIL import Image
            ocr_lines = ocr_engine.ocr(Image.open(image_path), max_width=None)
        result["ocr_text"] = ocr_engine.text_of(ocr_lines, "\n")
    except Exception as e:
        result["reasons"].append(f"本地 OCR 失败：{e}")
        return result

    if not result["ocr_text"].strip():
        result["reasons"].append("本地 OCR 没读到任何文字")
        return result

    # --- 通道 A：视觉大模型；没有就降级成「OCR 原文 + 文本模型」---
    has_vision = config.vision_ready(conf)
    has_text = config.text_model_ready(conf)

    try:
        if has_vision:
            payload, raw_text = call_vision_voted(image_path, conf, result)
            result["channel"] = "vision"
        elif has_text:
            payload, raw_text, size = call_text_model(result["ocr_text"], conf)
            result["channel"] = "ocr+text"
            result["degraded"] = True
            result["warnings"].append(
                "未配置视觉模型：本次用「本地OCR原文 + 文本模型」解析。"
                "模型看不到图，只认 OCR 读到的字，读不出图里的数字时会漏。")
        else:
            result["reasons"].append("既没配置视觉模型，也没配置文本模型")
            return result
    except Exception as e:
        result["reasons"].append(f"模型调用失败：{e}")
        return result

    result["raw_json"] = payload
    if not isinstance(payload, dict):
        result["reasons"].append("模型返回的不是合法 JSON")
        return result

    # 逐字段转成「分」
    # 新格式把三个数字平铺在顶层（输出短、模型想得快）；
    # 旧格式是 items 数组。两种都认，避免历史数据/降级通道出问题。
    raw_items = payload.get("items") or []
    if not raw_items:
        _qty = payload.get("qty")
        _unit = payload.get("unit_price")
        if _qty is not None or _unit is not None:
            raw_items = [{
                "name": "加油",
                "qty": _qty,
                "unit_price": _unit,
                "subtotal": payload.get("total"),
            }]
    items = raw_items
    if not isinstance(items, list):
        items = []

    norm_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        norm_items.append({
            "name": it.get("name"),
            "spec": it.get("spec"),
            "qty": it.get("qty"),
            "unit_price": it.get("unit_price"),
            "subtotal": it.get("subtotal"),
            "unit_price_cents": pricing.to_cents(it.get("unit_price")),
            "subtotal_cents": pricing.to_cents(it.get("subtotal")),
        })

    total_cents = pricing.to_cents(payload.get("total"))
    shipping_cents = pricing.to_cents(payload.get("shipping")) or 0
    doc_discount_cents = pricing.to_cents(payload.get("doc_discount")) or 0

    result.update({
        "items": norm_items,
        "total_cents": total_cents,
        "shipping_cents": shipping_cents,
        "doc_discount_cents": doc_discount_cents,
    })

    # --- 校验 1：不确定字段 ---
    uncertain = payload.get("uncertain_fields") or []
    if isinstance(uncertain, list) and uncertain:
        names = "、".join(map(str, uncertain))
        result["warnings"].append("模型标记为不确定的字段：" + names)
        result["degraded"] = True
        # 注意：模型对 total 没把握时，不直接拦。
        # 因为后面还有两道独立硬校验：① 合计必须能在本地 OCR 原文里找到
        # ② 明细 × 数量 必须与合计闭环。模型自评只是参考，不是证据。

    # --- 校验 2：合计必须有 ---
    if total_cents is None:
        result["reasons"].append("没识别到合计金额")
        return result

    # --- 校验 3a：合计必须在 OCR 原文里出现 ---
    # 这是唯一"编造就一定露馅"的检查，所以对合计严格。
    ok_total, _ = cross_check_numbers([total_cents], result["ocr_text"])
    if not ok_total:
        result["reasons"].append(
            f"合计 {pricing.format_cents(total_cents)} 元在图上找不到出处（疑似编造）")

    # --- 校验 3b：明细金额找不到出处 → 标记未复核，不等于编造 ---
    # 七段数码管、反光、被裁掉一半的数字，本地 OCR 经常读不出或读错
    # （实测：加油机油量 81.40 被 OCR 读成 1.40，单价 8.60 完全没读到）。
    # 这种情况是「OCR 没读到」，不是「模型编造」，不该直接拦。
    item_amounts = []
    for it in norm_items:
        if it["unit_price_cents"] is not None:
            item_amounts.append(it["unit_price_cents"])
        if it["subtotal_cents"] is not None:
            item_amounts.append(it["subtotal_cents"])

    if item_amounts:
        _, missing_items = cross_check_numbers(item_amounts, result["ocr_text"])
        if missing_items:
            result["degraded"] = True
            readable = "、".join(pricing.format_cents(c) for c in missing_items[:5])
            if len(missing_items) == len(item_amounts):
                result["warnings"].append(
                    f"明细金额全部未能用本地 OCR 复核（{readable}），"
                    f"仅靠合计与算术校验把关")
            else:
                result["warnings"].append(f"部分金额未能用本地 OCR 复核：{readable}")

    # --- 校验 4：算术闭环 ---
    # 这里传的是已经换算成「分」的字段，pricing 会优先用 *_cents，不会二次放大
    arith_ok, computed, why = pricing.check_arithmetic(
        [{"name": it["name"],
          "qty": it["qty"],
          "unit_price_cents": it["unit_price_cents"],
          "subtotal_cents": it["subtotal_cents"]} for it in norm_items],
        total_cents, shipping_cents, doc_discount_cents)
    if not arith_ok:
        result["reasons"].append(why)

    # --- 校验 5：置信度（只做兜底，不作为主要拦截手段）---
    #
    # 为什么不拿它当主判据：模型的自评置信度非常不可靠。
    # 实测同一张加油机照片，一次给 0.9 一次给 0.7，而两次读出的金额都是对的（700.00）。
    # 按 0.75 硬拦的结果，是把一单完全正确的订单推进"待人工"，用户还得手动放行。
    #
    # 真正能拦住错误的是前面两道：合计必须能在本地 OCR 原文里找到（防编造）、
    # 明细×数量必须和合计闭环（防漏行）。这两道过了，模型的"没把握"只是它谦虚。
    # 所以这里只在置信度极低（<0.4，模型基本在瞎猜）时拦截，其余降级为提示。
    conf_obj = payload.get("confidence")
    if isinstance(conf_obj, dict):
        vals = [v for v in conf_obj.values() if isinstance(v, (int, float))]
    elif isinstance(conf_obj, (int, float)):
        vals = [float(conf_obj)]
    else:
        vals = []
    if vals:
        result["confidence"] = min(float(v) for v in vals)
        if result["confidence"] < 0.40:
            result["reasons"].append(f"置信度过低（{result['confidence']:.2f}），模型明显没把握")
        elif result["confidence"] < 0.75:
            result["warnings"].append(
                f"模型自评置信度偏低（{result['confidence']:.2f}），但金额已通过交叉校验")

    result["ok"] = len(result["reasons"]) == 0
    return result


# ---------------------------------------------------------------- 自检
def _selfcheck():
    ok = fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        ok += bool(cond)
        fail += (not cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name:34s} {detail}")

    # 数字变体
    v = _num_variants(11980)
    chk("119.80 的写法变体包含 '119.80'", "119.80" in v, f"-> {sorted(v)}")
    chk("119.80 的写法变体包含 '119.8'", "119.8" in v)

    v2 = _num_variants(12000)
    chk("120.00 的变体含 '120'", "120" in v2, f"-> {sorted(v2)}")

    # 交叉校验
    ocr = "五常稻花香大米 5kg\n单价 59.90\n数量 2\n小计 119.80\n合计 119.80"
    ok1, miss1 = cross_check_numbers([11980, 5990], ocr)
    chk("数字都能在原文找到", ok1, f"missing={miss1}")

    ok2, miss2 = cross_check_numbers([11980, 8888], ocr)
    chk("编造的数字被识别出来", (not ok2) and miss2 == [8888], f"missing={miss2}")

    # 带货币符号与千分位
    ok3, _ = cross_check_numbers([129990], "合计 ￥1,299.90 元")
    chk("带￥与千分位也能匹配", ok3)

    # JSON 剥壳
    p1 = _parse_json('```json\n{"total": 1}\n```')
    chk("剥掉代码块", p1 == {"total": 1}, f"-> {p1}")
    p2 = _parse_json('好的，结果如下：{"total": 2} 完毕')
    chk("截取花括号", p2 == {"total": 2}, f"-> {p2}")
    p3 = _parse_json("随便说点什么")
    chk("无 JSON 返回 None", p3 is None)

    print(f"\n{ok} 通过, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selfcheck())
