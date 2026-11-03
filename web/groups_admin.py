"""
群白名单的后台管理：读写 config/groups.json。

与 src/groups.py 共用同一个配置文件，保证 Web 端改完机器人立刻能用。
配置文件里那些 "_说明" 开头的注释键会原样保留，不会被覆盖掉。
"""

import json
import os
import sys
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
CONF_PATH = os.path.join(ROOT, "config", "groups.json")

SRC_DIR = os.path.join(ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

MODES = [("whitelist", "只回列出的群（推荐）"),
         ("blacklist", "列出的群不回，其他都回"),
         ("all", "全部群都回（不推荐）")]


def load():
    if not os.path.isfile(CONF_PATH):
        return {"mode": "whitelist", "groups": [], "exclude_keywords": [],
                "match": {"ignore_member_count": True, "ignore_space": True,
                          "ignore_case": True, "allow_contains": False}}
    with open(CONF_PATH, "r", encoding="utf-8") as f:
        conf = json.load(f) or {}
    conf.setdefault("mode", "whitelist")
    conf.setdefault("groups", [])
    conf.setdefault("exclude_keywords", [])
    conf.setdefault("match", {}).setdefault("allow_contains", False)
    ensure_ids(conf)
    return conf


def save(conf):
    os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
    with open(CONF_PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ensure_ids(conf):
    changed = False
    for g in conf.get("groups", []):
        if not g.get("id"):
            g["id"] = uuid.uuid4().hex[:8]
            changed = True
    if changed:
        try:
            save(conf)
        except Exception:
            pass


def _find(conf, gid):
    for i, g in enumerate(conf.get("groups", [])):
        if g.get("id") == gid:
            return i, g
    return -1, None


def add_group(name, alias="", note=""):
    name = (name or "").strip()
    if not name:
        return False, "群名不能为空"

    conf = load()
    for g in conf.get("groups", []):
        if (g.get("name") or "").strip() == name:
            return False, f"「{name}」已经存在了"

    conf.setdefault("groups", []).append({
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "alias": (alias or "").strip(),
        "enabled": True,
        "note": (note or "").strip(),
    })
    save(conf)
    return True, f"已添加「{name}」"


def add_many(text):
    """批量导入：每行一个群名，支持「群名,别名」或「群名 别名」的写法。"""
    lines = [l.strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]

    conf = load()
    exist = {(g.get("name") or "").strip() for g in conf.get("groups", [])}

    added, skipped = [], []
    for line in lines:
        if "," in line or "\t" in line:
            parts = [p.strip() for p in line.replace("\t", ",").split(",", 1)]
            name, alias = parts[0], (parts[1] if len(parts) > 1 else "")
        else:
            name, alias = line, ""

        if name in exist:
            skipped.append(name)
            continue
        conf.setdefault("groups", []).append({
            "id": uuid.uuid4().hex[:8],
            "name": name, "alias": alias, "enabled": True, "note": "",
        })
        exist.add(name)
        added.append(name)

    save(conf)
    msg = []
    if added:
        msg.append(f"新增 {len(added)} 个")
    if skipped:
        msg.append(f"跳过 {len(skipped)} 个已存在")
    return (bool(added), "，".join(msg) or "没有可导入的内容")


def update_group(gid, name=None, alias=None, note=None, enabled=None):
    conf = load()
    i, g = _find(conf, gid)
    if g is None:
        return False, "找不到这个群"
    if name is not None:
        n = name.strip()
        if not n:
            return False, "群名不能为空"
        for j, other in enumerate(conf["groups"]):
            if j != i and (other.get("name") or "").strip() == n:
                return False, f"「{n}」与已有群重复"
        g["name"] = n
    if alias is not None:
        g["alias"] = alias.strip()
    if note is not None:
        g["note"] = note.strip()
    if enabled is not None:
        g["enabled"] = bool(enabled)
    save(conf)
    return True, "已保存"


def toggle_group(gid):
    conf = load()
    _, g = _find(conf, gid)
    if g is None:
        return False, "找不到这个群"
    g["enabled"] = not g.get("enabled", True)
    save(conf)
    return True, f"「{g['name']}」已{'启用' if g['enabled'] else '停用'}"


def delete_group(gid):
    conf = load()
    i, g = _find(conf, gid)
    if g is None:
        return False, "找不到这个群"
    name = g.get("name")
    conf["groups"].pop(i)
    save(conf)
    return True, f"已删除「{name}」"


def set_settings(mode=None, exclude_keywords=None, allow_contains=None):
    conf = load()
    if mode in ("whitelist", "blacklist", "all"):
        conf["mode"] = mode
    if exclude_keywords is not None:
        kws = [k.strip() for k in exclude_keywords.replace("，", ",").split(",")]
        conf["exclude_keywords"] = [k for k in kws if k]
    if allow_contains is not None:
        conf.setdefault("match", {})["allow_contains"] = bool(allow_contains)
    save(conf)
    return True, "设置已保存"


def test_match(title):
    """用当前配置测一个群名，返回 src/groups.py 的判定结果。"""
    try:
        import groups as g_mod
        return g_mod.match(title)
    except Exception as e:
        return {"allowed": False, "canonical": None, "reason": f"匹配模块异常: {e}"}
