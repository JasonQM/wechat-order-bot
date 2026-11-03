"""
全局配置读取。每次调用都重新读文件，改配置不需要重启程序。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
APP_CONF = os.path.join(ROOT, "config", "app.json")

DEFAULTS = {
    "discount_percent": 95,
    "recognition": {"mode": "local_then_ocr_space",
                    "skip_cloud_without_panel": True},
    "vision": {"base_url": "", "api_key": "", "model": "", "timeout": 45,
               "max_image_side": 1600, "temperature": 0},
    "text_model": {"base_url": "", "api_key": "", "model": "", "timeout": 60,
                   "max_tokens": 1500},
    "ocr": {"window_max_width": 1400, "min_score": 0.5},
    "safety": {"dry_run": True, "send_interval_sec": 8, "send_interval_jitter": 3,
               "per_group_hourly_limit": 30, "max_scroll_screens": 5,
               "send_verify_timeout_sec": 3, "full_scan_interval_sec": 300,
               "max_grab_attempts": 3, "max_grabs_per_group": 8,
               "force_full_interval_sec": 60, "quiet_hours": "23:30-07:00", "quiet_interval_sec": 25},
    "reply": {"template": "{name}，订单合计 {total} 元，{discount_percent} 折后 {final} 元",
              "discount_label": "95 折", "image": "", "mention": True},
    "paths": {"images": "data/images", "logs": "logs"},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    conf = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if os.path.isfile(APP_CONF):
        try:
            with open(APP_CONF, "r", encoding="utf-8") as f:
                conf = _merge(conf, json.load(f) or {})
        except Exception:
            pass
    return conf


def path_of(rel):
    """把配置里的相对路径转成绝对路径。"""
    if not rel:
        return ""
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def vision_ready(conf=None):
    c = (conf or load())["vision"]
    return bool(c.get("base_url") and c.get("api_key") and c.get("model"))


def text_model_ready(conf=None):
    c = (conf or load()).get("text_model") or {}
    return bool(c.get("base_url") and c.get("api_key") and c.get("model"))
