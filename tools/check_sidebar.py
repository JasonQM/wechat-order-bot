"""
会话列表诊断：把整个列表（含滚动）扫一遍，标出漏单风险点。

回答两个问题：
  1. 一共有多少个会话？当前窗口一屏能显示几个？超出的是不是根本扫不到？
  2. 哪些群是免打扰（只有小圆点没有数字）？这些是最容易静默漏掉的。

用法：
    python tools/check_sidebar.py

不改任何设置，只观察。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import desktop      # noqa: E402
import groups as g  # noqa: E402
import ocr_engine   # noqa: E402
import window as W  # noqa: E402

# collect_sidebar 里用的红点阈值，这里保持一致便于解释
RED_STRONG, RED_WEAK = desktop.RED_STRONG, desktop.RED_WEAK


def main():
    W.init_dpi()

    win = W.pick_main_window()
    if not win:
        print("没找到微信窗口，请先启动并登录。")
        return 1

    hwnd = win["hwnd"]
    img, rect, _ = W.capture(hwnd)
    if img is None:
        print("抓图失败。")
        return 1

    layout = W.compute_layout(*img.size)
    gconf = g.load_conf()

    # 当前可见的（一屏能看到的）
    lines = ocr_engine.ocr(img, max_width=1400)
    visible = desktop.sidebar_entries(img, lines, layout)
    visible_names = {g.normalize(e["text"], gconf) for e in visible}

    print("=" * 74)
    print(f"窗口 {img.size[0]}x{img.size[1]}   当前一屏可见 {len(visible)} 个会话")
    print("=" * 74)

    # 整个列表（滚动扫描）
    all_entries = desktop.collect_sidebar(hwnd, layout, rect, conf_groups=gconf)

    rows = []
    for key, e in all_entries.items():
        v = g.match(e["text"], gconf)
        rows.append({
            "name": e["text"],
            "unread": e["unread"],
            "red": e["red"],
            "in_whitelist": v["allowed"],
            "visible": key in visible_names,
        })

    # 白名单群优先显示
    rows.sort(key=lambda r: (not r["in_whitelist"], not r["visible"], r["name"]))

    print(f"\n{'':2s} {'群/会话':30s} {'未读':8s} {'红像素':>6s}  {'白名单':6s} 可见")
    print("-" * 74)
    for r in rows:
        mark = "★" if r["in_whitelist"] else " "
        unread = {"count": "有(数字)", "dot": "免打扰点", "none": "无"}[r["unread"]]
        print(f"{mark:2s} {r['name'][:30]:30s} {unread:8s} {r['red']:6d}  "
              f"{'是' if r['in_whitelist'] else '否':6s} {'是' if r['visible'] else '否'}")

    # ---------------- 风险判断 ----------------
    print("\n" + "=" * 74)
    print("漏单风险评估")
    print("=" * 74)

    risks = 0

    beyond = [r for r in rows if not r["visible"]]
    if beyond:
        print(f"\n[!] 有 {len(beyond)} 个会话不在当前可见区内：")
        for r in beyond[:10]:
            print(f"      {r['name']}")
        print("    影响：不开滚动扫描就会永久扫不到。本版已支持滚动，但要注意")
        print("          每轮都要滚完整列表，会话越多越慢。")
        risks += 1
    else:
        print("\n[✓] 所有会话都在一屏内，暂时没有『超出一屏』的问题。")

    muted = [r for r in rows if r["unread"] == "dot"]
    if muted:
        print(f"\n[!] 有 {len(muted)} 个免打扰会话（只有小圆点、没有数字）：")
        for r in muted:
            print(f"      {r['name']}（红像素 {r['red']}）")
        print(f"    阈值：>= {RED_STRONG} 判为有数字，>= {RED_WEAK} 判为免打扰点。")
        print("    风险：小圆点像素数如果低于阈值，会被判成『无未读』直接跳过。")
        risks += 1
    else:
        print("\n[✓] 当前没有检测到免打扰会话的小圆点。")
        print(f"    （注意：这不能证明免打扰群没问题，只说明这一屏没有）")

    wl = [r for r in rows if r["in_whitelist"]]
    print(f"\n[·] 白名单群 {len(wl)} 个：")
    for r in wl:
        print(f"      {r['name']}  未读={r['unread']}")

    if len(wl) > 8:
        print(f"\n[!] 白名单群超过 8 个。微信一屏大约只能显示 8-10 个会话，")
        print("    且免打扰群有新消息时【不会】自动浮到顶部——这是最大漏点。")
        print("    建议：把这几个群设为『消息免打扰 + 置顶』，置顶会话永远在最上面。")
        risks += 1

    print("\n" + "=" * 74)
    print(f"共 {len(rows)} 个会话，{len(wl)} 个白名单群，{risks} 类风险提示")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
