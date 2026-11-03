"""
回复素材管理：固定图片上传、回复文案模板、折扣系数。

改完直接写回 config/app.json，机器人每轮会重新读配置，不需要重启。
"""

import hashlib
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
CONF_PATH = os.path.join(ROOT, "config", "app.json")
ASSETS_DIR = os.path.join(ROOT, "assets")

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_BYTES = 8 * 1024 * 1024  # 8MB


def load_conf():
    if not os.path.isfile(CONF_PATH):
        return {}
    with open(CONF_PATH, "r", encoding="utf-8") as f:
        return json.load(f) or {}


def save_conf(conf):
    os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
    with open(CONF_PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _abs(rel):
    if not rel:
        return ""
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def current_image():
    """返回当前固定图片的信息。"""
    conf = load_conf()
    rel = (conf.get("reply") or {}).get("image") or ""
    path = _abs(rel)

    info = {
        "rel": rel,
        "exists": bool(path and os.path.isfile(path)),
        "name": os.path.basename(path) if path else "",
        "size_kb": 0,
        "dims": "",
        "mtime": "",
        "fingerprint": "",
        "abspath": path or "",
    }
    if info["exists"]:
        try:
            info["size_kb"] = round(os.path.getsize(path) / 1024.0, 1)
            info["mtime"] = time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(os.path.getmtime(path)))
            # 指纹：用来确认「面板上这张」和「机器人实际发的那张」是同一张。
            # 日志里也会打同一个指纹，两边对得上才叫真换了。
            with open(path, "rb") as f:
                info["fingerprint"] = hashlib.md5(f.read()).hexdigest()[:8]
            from PIL import Image
            with Image.open(path) as im:
                info["dims"] = f"{im.size[0]}×{im.size[1]}"
        except Exception:
            pass
    return info


def _cleanup_stale(keep_path):
    """删掉旧的 reply_image.* 文件，避免越传越多。"""
    if not os.path.isdir(ASSETS_DIR):
        return
    for name in os.listdir(ASSETS_DIR):
        if not name.startswith("reply_image"):
            continue
        p = os.path.join(ASSETS_DIR, name)
        if os.path.abspath(p) == os.path.abspath(keep_path):
            continue
        try:
            os.remove(p)
        except Exception:
            pass


def save_upload(file_storage):
    """
    保存上传的图片，并把 config 里的 reply.image 指向它。

    返回 (ok, msg, info)
    """
    if file_storage is None or not file_storage.filename:
        return False, "没有选择文件", None

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return False, f"不支持的格式 {ext}，请用 png / jpg / webp / bmp", None

    data = file_storage.read()
    if not data:
        return False, "文件是空的", None
    if len(data) > MAX_BYTES:
        return False, f"文件太大（{len(data) / 1024 / 1024:.1f}MB），上限 8MB", None

    # 用 PIL 真正打开一次，确认是合法图片（防止改后缀的假图）
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception as e:
        return False, f"这不是一张能打开的图片：{e}", None

    if w < 50 or h < 50:
        return False, f"图片太小（{w}×{h}），微信里会看不清", None

    os.makedirs(ASSETS_DIR, exist_ok=True)
    target = os.path.join(ASSETS_DIR, f"reply_image{ext}")
    with open(target, "wb") as f:
        f.write(data)

    _cleanup_stale(target)

    conf = load_conf()
    conf.setdefault("reply", {})["image"] = os.path.relpath(target, ROOT).replace("\\", "/")
    save_conf(conf)

    return True, f"已更新固定图片（{w}×{h}，{len(data) / 1024:.0f}KB）", current_image()


def set_reply(template=None, discount_label=None, mention=None, discount_percent=None):
    """更新回复文案与折扣设置。"""
    conf = load_conf()
    r = conf.setdefault("reply", {})

    if template is not None:
        t = template.strip()
        if not t:
            return False, "回复文案不能为空"
        if "{final}" not in t:
            return False, "回复文案里必须包含 {final}（折后价），否则客户看不到实付金额"
        r["template"] = t
    if discount_label is not None:
        r["discount_label"] = discount_label.strip()
    if mention is not None:
        r["mention"] = bool(mention)
    if discount_percent is not None:
        try:
            p = int(discount_percent)
        except ValueError:
            return False, "折扣系数必须是整数百分比（95 表示 95 折）"
        if not (1 <= p <= 100):
            return False, "折扣系数要在 1~100 之间"
        conf["discount_percent"] = p

    save_conf(conf)
    return True, "设置已保存"


def preview_text(total_cents=11980, final_cents=11381, sender="张三"):
    """用配置里的模板渲染一条示例，供页面预览。"""
    import sys
    src = os.path.join(ROOT, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import config as cfg
        import pricing
        return pricing.build_reply(sender, total_cents, final_cents, cfg.load())
    except Exception as e:
        return f"（预览失败：{e}）"
