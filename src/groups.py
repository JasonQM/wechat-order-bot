"""
群白名单：决定"哪些群需要回复"。

设计要点
--------
1. 微信群名后面总带人数，如「五常大米下单群(128)」。人来人走数字会变，
   所以匹配前必须先把人数后缀剥掉，否则加个人就失配、程序就静默不干活了。
2. 匹配失败要【告警】，不能静默跳过。参考仓库最危险的设计就是静默跳过——
   你以为在跑，其实早就漏了。
3. 默认只回白名单里的群。做成"全部群都回"等于把机器人放进所有客户群，
   这是事故级默认值。

用法
----
    python src/groups.py --list                  # 看当前配置
    python src/groups.py --test "五常大米下单群(128)"   # 试一个群名
    python src/groups.py --selfcheck             # 跑内置用例
"""

import argparse
import json
import os
import re
import sys
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
CONF_PATH = os.path.join(ROOT, "config", "groups.json")

MEMBER_SUFFIX = re.compile(r"\(\d+\)\s*$")
# 侧边栏 OCR 常把群名和右侧时间粘成一行：「测试加油11:32」
TRAIL_TIME = re.compile(
    r"(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|昨天|前天|星期[一二三四五六日天])\s*$")
ELLIPSIS = ("...", "…", "..")


def load_conf():
    if not os.path.isfile(CONF_PATH):
        return {"mode": "whitelist", "groups": [], "exclude_keywords": [],
                "match": {"ignore_member_count": True, "ignore_space": True,
                          "ignore_case": True, "allow_contains": False}}
    with open(CONF_PATH, "r", encoding="utf-8") as f:
        conf = json.load(f) or {}

    conf.setdefault("mode", "whitelist")
    conf.setdefault("groups", [])
    conf.setdefault("exclude_keywords", [])
    m = conf.setdefault("match", {})
    m.setdefault("ignore_member_count", True)
    m.setdefault("ignore_space", True)
    m.setdefault("ignore_case", True)
    m.setdefault("allow_contains", False)
    return conf


def normalize(name, conf=None):
    """
    归一化群名，用于比较。

    NFKC 会把全角括号/数字/空格统一成半角，这样「（１２８）」和「(128)」等价，
    避免 OCR 认错全半角导致失配。
    """
    if not name:
        return ""

    c = (conf or load_conf()).get("match", {})
    s = unicodedata.normalize("NFKC", str(name)).strip()

    if c.get("ignore_member_count", True):
        s = MEMBER_SUFFIX.sub("", s)

    if c.get("ignore_space", True):
        s = re.sub(r"\s+", "", s)

    if c.get("ignore_case", True):
        s = s.lower()

    return s


def enabled_groups(conf=None):
    conf = conf or load_conf()
    return [g for g in conf.get("groups", []) if g.get("enabled", True) and g.get("name")]


def match(title, conf=None):
    """
    判断一个群标题是否需要回复。

    返回 dict:
        allowed    True/False
        canonical  命中的配置名（白名单里那一条的 name），未命中为 None
        reason     人话说明，用于日志/告警
    """
    conf = conf or load_conf()

    if not title or not str(title).strip():
        return {"allowed": False, "canonical": None, "reason": "标题为空（OCR 没读到）"}

    raw = str(title).strip()
    norm = normalize(raw, conf)
    if not norm:
        return {"allowed": False, "canonical": None, "reason": "归一化后为空"}

    mode = conf.get("mode", "whitelist")

    def _hits_exclude():
        for kw in conf.get("exclude_keywords", []):
            k = normalize(kw, conf)
            if k and k in norm:
                return kw
        return None

    # 白名单模式：以名单为准，排除词【不参与】。
    # 理由：白名单本身就是"我明确要回这些群"的声明；
    # 再拿子串去拦会把「测试加油」这种正常群名误杀掉（踩过）。
    if mode == "whitelist":
        groups = enabled_groups(conf)
        for g in groups:
            for c in (g.get("name"), g.get("alias")):
                if not c:
                    continue
                nc = normalize(c, conf)
                if not nc:
                    continue
                if nc == norm:
                    return {"allowed": True, "canonical": g.get("name"),
                            "reason": f"命中白名单「{g.get('name')}」"}
                if conf["match"].get("allow_contains") and (nc in norm or norm in nc):
                    return {"allowed": True, "canonical": g.get("name"),
                            "reason": f"包含匹配白名单「{g.get('name')}」"}
        return {"allowed": False, "canonical": None,
                "reason": f"不在白名单（归一化后为「{norm}」）"}

    # 以下模式排除词生效
    kw = _hits_exclude()
    if kw:
        return {"allowed": False, "canonical": None, "reason": f"命中排除词「{kw}」"}

    if mode == "all":
        return {"allowed": True, "canonical": raw, "reason": "mode=all 全部放行"}

    # blacklist
    groups = enabled_groups(conf)
    for g in groups:
        for c in (g.get("name"), g.get("alias")):
            if not c:
                continue
            nc = normalize(c, conf)
            if not nc:
                continue
            if nc == norm or (conf["match"].get("allow_contains")
                              and (nc in norm or norm in nc)):
                return {"allowed": False, "canonical": None,
                        "reason": f"命中黑名单「{g.get('name')}」"}

    return {"allowed": True, "canonical": raw, "reason": "不在黑名单"}


def is_allowed(title, conf=None):
    return match(title, conf)["allowed"]


def match_sidebar(text, truncated=False, conf=None):
    """
    侧边栏候选筛选：比严格匹配宽松一档。

    为什么需要：微信会话列表里的长群名会被截断成「小鲸出海2群（广..」，
    严格全等永远匹配不上。所以候选阶段允许「被截断的前缀」。

    安全边界：这里只是选出"值得切进去看看"的群，
    真正的把关在切进去之后的【顶栏校验】——顶栏是完整群名，不会截断。
    宽进严出，宁可多切一次，不可漏一个群。
    """
    conf = conf or load_conf()

    # 防御性清洗：即使调用方没先洗，这里也把尾部时间和省略号剥掉
    raw = str(text or "").strip()
    truncated = bool(truncated) or raw.endswith(ELLIPSIS)
    for _ in range(3):
        before = raw
        raw = TRAIL_TIME.sub("", raw).strip()
        for e in ELLIPSIS:
            if raw.endswith(e):
                raw = raw[:-len(e)].strip()
        if raw == before:
            break

    clean = normalize(raw, conf)
    if not clean:
        return {"allowed": False, "canonical": None, "reason": "侧边栏文本为空"}

    mode = conf.get("mode", "whitelist")

    if mode != "whitelist":
        base = match(text, conf)
        if base["allowed"]:
            return base
        # 非白名单模式下再试一次截断前缀
        for g in enabled_groups(conf):
            nc = normalize(g.get("name"), conf)
            if nc and truncated and nc.startswith(clean):
                return {"allowed": False, "canonical": None,
                        "reason": f"被截断的黑名单候选「{g.get('name')}」"}
        return base

    for g in enabled_groups(conf):
        for c in (g.get("name"), g.get("alias")):
            if not c:
                continue
            nc = normalize(c, conf)
            if not nc:
                continue
            if nc == clean:
                return {"allowed": True, "canonical": g.get("name"),
                        "reason": f"命中白名单「{g.get('name')}」"}
            if truncated and nc.startswith(clean) and len(clean) >= 4:
                return {"allowed": True, "canonical": g.get("name"),
                        "reason": f"侧边栏被截断，按前缀命中「{g.get('name')}」"}

    return {"allowed": False, "canonical": None,
            "reason": f"不在白名单（侧边栏读作「{clean}」）"}


# ---------------------------------------------------------------- CLI
def _list():
    conf = load_conf()
    print(f"配置文件: {CONF_PATH}")
    print(f"模式: {conf['mode']}")
    print(f"排除词: {conf.get('exclude_keywords') or '（无）'}")
    print(f"匹配选项: {conf['match']}")
    print()
    gs = conf.get("groups", [])
    if not gs:
        print("群列表为空。请编辑 config/groups.json 填入你要回复的群名。")
        return
    print(f"共 {len(gs)} 个群：")
    for g in gs:
        flag = "启用" if g.get("enabled", True) else "停用"
        alias = f"  (别名: {g['alias']})" if g.get("alias") else ""
        print(f"  [{flag}] {g.get('name')}{alias}")
        if g.get("note"):
            print(f"         备注: {g['note']}")


def _test(title):
    r = match(title)
    print(f"群名: {title}")
    print(f"需要回复: {'是' if r['allowed'] else '否'}")
    print(f"命中配置: {r['canonical'] or '-'}")
    print(f"原因: {r['reason']}")
    print(f"归一化: {normalize(title)}")


def _selfcheck():
    cases = [
        ("五常大米下单群(128)", True, "标准情况：带人数"),
        ("五常大米下单群", True, "不带人数"),
        ("五常大米下单群（１２８）", True, "全角括号+全角数字"),
        ("五 常大米下单群 (128)", True, "群名中间有空格"),
        ("闲聊灌水群(30)", False, "不在白名单"),
        ("五常大米下单群2(12)", False, "名字相近但不是同一个，不能误命中"),
        ("文件传输助手", False, "非群聊"),
        ("内部测试群(5)", False, "不在白名单"),
        ("", False, "空标题"),
    ]

    # 白名单优先：群名里含排除词，但只要点了名就应该回
    cases_wl = [
        ("测试加油(3)", True, "白名单优先于排除词"),
        ("测试群(8)", False, "没进白名单的仍然不回"),
    ]

    # 用一份确定的配置做测试，不依赖用户当前填了什么
    conf = {
        "mode": "whitelist",
        "groups": [
            {"name": "五常大米下单群", "alias": "", "enabled": True},
            {"name": "粮油批发1群", "alias": "粮油", "enabled": True},
            {"name": "测试加油", "alias": "", "enabled": True},
            {"name": "已停用的群", "alias": "", "enabled": False},
        ],
        "exclude_keywords": ["测试"],
        "match": {"ignore_member_count": True, "ignore_space": True,
                  "ignore_case": True, "allow_contains": False},
    }

    ok = 0
    fail = 0
    for title, expect, desc in cases + cases_wl:
        r = match(title, conf)
        good = r["allowed"] == expect
        ok += good
        fail += (not good)
        mark = "PASS" if good else "FAIL"
        print(f"[{mark}] {desc:28s} {title!r:24s} -> {r['allowed']}  ({r['reason']})")

    print(f"\n{ok} 通过, {fail} 失败")

    # 侧边栏候选匹配（含时间粘连、截断）
    print("\n--- 侧边栏候选匹配 ---")
    side_cases = [
        ("测试加油11:32", False, True, "群名与时间粘连"),
        ("测试加油", False, True, "干净群名"),
        ("小鲸出海2群（广", True, False, "截断但不在白名单"),
        ("测试", True, False, "过短前缀不认，防误命中"),
        ("闲聊群11:30", False, False, "不在白名单"),
    ]
    ok2 = fail2 = 0
    for text, trunc, expect, desc in side_cases:
        r = match_sidebar(text, trunc, conf)
        good = r["allowed"] == expect
        ok2 += good
        fail2 += (not good)
        print(f"[{'PASS' if good else 'FAIL'}] {desc:24s} {text!r:20s} -> {r['allowed']}  ({r['reason']})")

    print(f"\n{ok + ok2} 通过, {fail + fail2} 失败")
    return 0 if (fail + fail2) == 0 else 1


def main():
    p = argparse.ArgumentParser(description="群白名单工具")
    p.add_argument("--list", action="store_true", help="显示当前配置")
    p.add_argument("--test", metavar="群名", help="测试一个群名是否需要回复")
    p.add_argument("--selfcheck", action="store_true", help="跑内置用例")
    a = p.parse_args()

    if a.selfcheck:
        return _selfcheck()
    if a.test:
        _test(a.test)
        return 0
    _list()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
