"""
入口。先跑 doctor 体检，再 once 试一轮，最后 loop 常驻。

    python src/main.py doctor       环境体检（不碰微信，不发送）
    python src/main.py once         扫一轮就退出
    python src/main.py loop         常驻运行

常用参数
    --send              真正发送（默认 dry_run，只识别不发送）
    --group "群名"       只处理指定群
    --sweep             不看红点，白名单群全部巡检一遍
    --interval 3        常驻循环间隔秒
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import groups as g_mod  # noqa: E402
import window as W     # noqa: E402


OK = "✓"
BAD = "✗"
WARN = "!"


def _line(mark, name, detail=""):
    print(f"  [{mark}] {name:26s} {detail}")


def cmd_doctor(args):
    print("=" * 66)
    print("环境体检")
    print("=" * 66)

    conf = config.load()
    fails = 0

    # 1. 窗口
    W.init_dpi()
    win = W.pick_main_window()
    if not win:
        _line(BAD, "微信窗口", "没找到，请启动并登录微信 PC 版")
        fails += 1
        print("\n后面的检查需要窗口，先解决这一条。")
        return 1
    _line(OK, "微信窗口", f"hwnd={win['hwnd']} {win['size'][0]}x{win['size'][1]} {win['proc']}")

    # 2. 抓图
    img, rect, scale = W.capture(win["hwnd"])
    if img is None:
        _line(BAD, "PrintWindow 抓图", "拿不到画面，这条路走不通")
        fails += 1
        return 1
    _line(OK, "PrintWindow 抓图", f"{img.size[0]}x{img.size[1]} DPI={scale}")

    # 3. 布局
    layout = W.compute_layout(*img.size)
    _line(OK, "布局计算",
          f"sidebar={layout['sidebar_w']} chat_x={layout['chat_start_x']} "
          f"header={layout['header_h']} input={layout['input_h']}")

    # 4. OCR
    try:
        import ocr_engine
        if ocr_engine.ready():
            lines = ocr_engine.ocr(img, max_width=conf["ocr"]["window_max_width"])
            _line(OK, "本地 OCR", f"识别到 {len(lines)} 行文字")
        else:
            _line(BAD, "本地 OCR", "引擎不可用")
            fails += 1
    except Exception as e:
        _line(BAD, "本地 OCR", str(e).splitlines()[0])
        fails += 1

    # 5. 视觉模型配置 + 真连通测试
    if config.vision_ready(conf):
        _line(OK, "视觉模型配置", f"{conf['vision']['model']} @ {conf['vision']['base_url']}")
        ok, msg = probe_vision(conf)
        if ok:
            _line(OK, "视觉接口连通性", msg)
        else:
            _line(BAD, "视觉接口连通性", msg)
            fails += 1
    else:
        _line(BAD, "视觉模型配置", "config/app.json 里的 base_url / api_key / model 没填全")
        fails += 1

    # 6. 群白名单
    gconf = g_mod.load_conf()
    real = [g for g in gconf["groups"] if g.get("enabled", True)
            and not str(g.get("name", "")).startswith("示例")]
    if real:
        _line(OK, "群白名单", f"{len(real)} 个：" + "、".join(g["name"] for g in real[:5]))
    else:
        _line(BAD, "群白名单", "还没配置真实群，去 /groups 页面加")
        fails += 1

    # 7. 固定图片
    imgp = config.path_of(conf["reply"].get("image", ""))
    if imgp and os.path.isfile(imgp):
        _line(OK, "回复固定图片", imgp)
    else:
        _line(WARN, "回复固定图片", "未配置或不存在，只会发文本")

    # 8. 数据库
    try:
        import store
        store.init_db()
        _line(OK, "订单数据库", store.DB_PATH)
    except Exception as e:
        _line(BAD, "订单数据库", str(e))
        fails += 1

    print("-" * 66)
    if conf["safety"]["dry_run"]:
        print("  当前 safety.dry_run = true，只识别不发送。")
        print("  确认无误后，运行加 --send 参数，或把 config/app.json 里改成 false。")
    else:
        print("  ⚠ safety.dry_run = false，会真的往群里发消息！")

    print(f"\n{'体检通过' if fails == 0 else f'有 {fails} 项需要处理'}")
    return 0 if fails == 0 else 1


def probe_vision(conf, timeout=90):
    """
    真发一张有明确答案的图给视觉接口，看它能不能答对。

    为什么不能只看 HTTP 200 或 token 数：
    · DeepSeek 对图片直接忽略，照样返回 200，只是回答"图片无法显示"
    · 图片太小（64×64）时 image_tokens 本来就很少，用 token 数判断会误判

    所以画一张有确定答案的图（白色背景 + 中间红色方块），问它是什么颜色，
    答对了才算能用。
    """
    import base64
    import io

    import requests
    from PIL import Image

    v = conf["vision"]
    url = v["base_url"].rstrip("/") + "/chat/completions"

    S = 320
    img = Image.new("RGB", (S, S), (255, 255, 255))
    px = img.load()
    for y in range(80, 240):
        for x in range(80, 240):
            px[x, y] = (220, 20, 20)     # 明显的红方块

    buf = io.BytesIO()
    img.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    body = {
        "model": v["model"],
        "max_tokens": 300,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "图中中间的方块是什么颜色？只回答颜色名称，两个字。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }

    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {v['api_key']}",
                                        "Content-Type": "application/json"},
                          json=body, timeout=timeout)
    except Exception as e:
        return False, f"请求失败：{type(e).__name__} {e}"

    if r.status_code != 200:
        return False, f"HTTP {r.status_code}：{r.text[:150]}"

    try:
        data = r.json()
    except Exception:
        return False, "返回不是 JSON"

    usage = data.get("usage") or {}
    img_tok = (usage.get("prompt_tokens_details") or {}).get("image_tokens", 0)
    pt = usage.get("prompt_tokens", 0)

    msg = (data.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()

    if not content and reasoning:
        return False, f"回答为空，全在 reasoning 里（推理模型需加大 max_tokens）"
    if not content:
        return False, "返回内容为空"

    if "无法" in content and ("图片" in content or "图像" in content):
        return False, f"模型说读不到图片（image_tokens={img_tok}）——不支持图片输入"
    if "红" in content or "red" in content.lower():
        return True, f"能读图，答对了（image_tokens={img_tok}，prompt_tokens={pt}）"

    return False, f"回答不对（{content[:40]!r}），图片可能没被正确处理（image_tokens={img_tok}）"


def _ensure_single_instance(cmd_name):
    """
    拿实例锁，失败就打印人话并返回 False。

    一定要在 import scheduler 之前调用：那一步会加载 OCR 模型（3 秒上下），
    先加载再被拒，用户看到的是"卡了几秒然后退出一句英文错误"。
    """
    import single_instance

    if single_instance.acquire():
        return True

    print("")
    print(f"  {single_instance.describe_owner()}。")
    print("")
    print("  同时跑两个会互相抢微信窗口 —— 可能把报价发到错的群，")
    print("  或者同一笔订单被回复两次。")
    print("")
    print("  要重启：先把之前那个窗口关掉（或从面板点「停止」），再运行这个。")
    print("")
    return False


def cmd_once(args):
    # once 也会点开图片、必要时真的发消息，所以同样要挡并发。
    # 早先只有 loop 加锁，`start_bot.bat` 还开着的时候手动跑一次 once，
    # 就会出现两个进程同时操作同一个聊天窗口。
    if not _ensure_single_instance("once"):
        return 2

    import scheduler

    bot = scheduler.Bot(
        override_dry_run=(False if args.send else None),
        only_group=args.group,
        sweep=args.sweep,
        deep=args.deep,
    )
    n = bot.run_once()
    print(f"\n本轮处理 {n} 单")
    return 0


def cmd_loop(args):
    if not _ensure_single_instance("loop"):
        return 2

    import scheduler

    bot = scheduler.Bot(
        override_dry_run=(False if args.send else None),
        only_group=args.group,
        sweep=args.sweep,
        deep=args.deep,
    )
    bot.run_forever(interval=args.interval)
    return 0


def main():
    p = argparse.ArgumentParser(description="微信多群订单自动报价")
    sub = p.add_subparsers(dest="cmd")

    for name, help_text in (("doctor", "环境体检"),
                            ("once", "扫一轮"),
                            ("loop", "常驻运行")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--send", action="store_true", help="真正发送（默认只识别）")
        sp.add_argument("--group", default=None, help="只处理指定群")
        sp.add_argument("--sweep", action="store_true", help="不看红点，全部巡检")
        sp.add_argument("--deep", action="store_true", help="即使没看到新内容也翻满所有屏（更慢，更保险）")
        sp.add_argument("--interval", type=float, default=1.0,
                        help="循环间隔秒（有像素哨兵，空转轮次只要几十毫秒，可以给很小）")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0

    return {"doctor": cmd_doctor, "once": cmd_once, "loop": cmd_loop}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
