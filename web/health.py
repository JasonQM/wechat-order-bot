"""
机器人运行状态（读心跳文件）。

为什么要单独一层：挂机最危险的失败不是报错，是静默停止。
进程退了、卡在某个 API 上、或者微信自己被关掉，
从面板上看「数据都正常」，实际上一条消息都不会回了。
所以状态必须由「心跳文件的新鲜度」来判定，不能靠有没有新订单。
"""
import json
import os
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT = os.path.join(ROOT, "data", "heartbeat.json")

# 心跳超过这个秒数就认为「可能出问题了」/「已经停了」
SLOW_SEC = 30
# 心跳超过这个秒数就判定为「进程不在了」。
# 循环间隔是 1 秒，正常情况每秒都该有一次心跳；45 秒没有任何更新，
# 基本只能是进程死了。不能设太长——进程被杀后心跳文件不会消失，
# 等太久的话面板一直显示「监控中」，用户以为在跑，实际一条都不回。
DEAD_SEC = 45

STATE_LABEL = {
    "never": "未启动",
    "running": "监控中",
    "paused": "已暂停",
    "quiet": "夜间低频",
    "slow": "心跳延迟",
    "stopped": "已停止",
}

PAUSE_FLAG = os.path.join(ROOT, "data", "pause.flag")
APP_CONF = os.path.join(ROOT, "config", "app.json")


def testing_amount_cents():
    """测试模式金额（分）。>0 表示所有图片都按这个金额处理，不做真实识别。"""
    try:
        with open(APP_CONF, encoding="utf-8") as f:
            d = json.load(f)
        return int(((d.get("testing") or {}).get("force_amount_cents")) or 0)
    except Exception:
        return 0


def is_paused():
    return os.path.isfile(PAUSE_FLAG)


def _pid_alive(pid):
    """
    进程还在不在。

    为什么必须查：心跳文件是「上次写下的时间」，进程被杀之后它不会消失，
    所以在 30 秒窗口内，面板会把一个已经死掉的机器人报成「监控中」——
    用户看到绿灯，以为在跑，实际一条消息都不会回。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        k32.CloseHandle(h)
        return True

    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def read():
    """返回原始心跳数据（没有就返回空字典）。"""
    if not os.path.isfile(HEARTBEAT):
        return {}
    try:
        with open(HEARTBEAT, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _instance_locked():
    """
    有没有机器人进程占着实例锁。None 表示查不出来（锁模块不可用）。

    比心跳可靠：心跳是几秒前写的文件，进程卡死或被挂起时它不会更新，
    但它其实还在占着微信窗口。以"有没有进程握着锁"为准，
    才不会出现"面板说已停止、于是又拉起一个"的双开。
    """
    try:
        import single_instance
        return single_instance.is_taken()
    except Exception:
        return None


def status():
    """把心跳文件翻译成「给人看的状态」。"""
    hb = read()
    locked = _instance_locked()

    if not hb:
        return {
            "state": "never",
            "label": STATE_LABEL["never"],
            "tone": "muted",
            "age_sec": None,
            "last_tick": "",
            "cycles": 0,
            "last_duration_sec": None,
            "dry_run": None,
            "last_error": "",
            "detail": "还没有跑过常驻循环。双击 start_bot.bat 启动。",
            "paused": is_paused(),
            "scan_mode": "",
            "sessions_seen": 0,
            "allowed_total": 0,
            "allowed_visible": 0,
            "instance_locked": locked,
        }

    age = None
    try:
        age = round(time.time() - os.path.getmtime(HEARTBEAT), 1)
    except Exception:
        pass

    raw_state = hb.get("state", "")
    if locked is False:
        # 没有任何进程握着实例锁 —— 不管心跳多新，机器人都已经不在了
        state = "stopped"
    elif raw_state == "stopped":
        state = "stopped"
    elif not _pid_alive(hb.get("pid")):
        # 心跳里那个 pid 没了，但锁还被占着：多半是新进程刚起来、还没跑完第一轮。
        # 这时判"未响应"而不是"已停止"，免得面板诱导用户再点一次启动。
        state = "slow" if locked else "stopped"
    elif raw_state == "paused" or is_paused():
        state = "paused"
    elif age is None:
        state = "stopped"
    elif age <= SLOW_SEC:
        state = "running"
    elif age <= DEAD_SEC:
        state = "slow"
    else:
        state = "stopped"

    # 夜间不是一种"运行状态"，只是把轮询间隔从 1 秒拉长到 25 秒。
    # 进程、锁、扫描逻辑都一模一样，所以状态就是"运行中"，
    # 只在说明里标一句夜间低频。
    # 早先把它单独做成 quiet 状态，面板上显示成"夜间"，
    # 用户根本看不出机器人到底开没开——这是纯粹的自找麻烦。
    quiet_low = (raw_state == "quiet") and state == "running"

    tone = {"running": "ok", "paused": "warn", "slow": "warn",
            "stopped": "bad"}.get(state, "muted")

    detail = ""
    if state == "paused":
        detail = "已暂停：进程还在，但不会扫描和回复"
    elif state == "running":
        if quiet_low:
            ival = hb.get("quiet_interval_sec") or 25
            detail = (f"运行中 · 夜间低频（扫描间隔 {ival} 秒，"
                      f"仍会处理新订单）")
        else:
            detail = f"运行中 · 最近一轮 {hb.get('last_duration_sec', 0)}s 前更新"
    elif state == "slow":
        detail = f"心跳已经 {int(age)} 秒没更新，可能卡在某个步骤上"
    else:
        detail = (f"进程已退出（pid {hb.get('pid')} 不在了），最后心跳 {int(age)} 秒前"
                  if age is not None and age < SLOW_SEC
                  else f"心跳停止于 {int(age)} 秒前，机器人可能已退出")

    return {
        "state": state,
        "label": STATE_LABEL.get(state, state),
        "tone": tone,
        "age_sec": age,
        "last_tick": hb.get("last_tick", ""),
        "started_at": hb.get("started_at", ""),
        "cycles": hb.get("cycles", 0),
        "last_duration_sec": hb.get("last_duration_sec"),
        "dry_run": hb.get("dry_run"),
        "last_error": hb.get("last_error") or "",
        "consecutive_errors": hb.get("consecutive_errors", 0),
        "pid": hb.get("pid"),
        "detail": detail,
        "paused": state == "paused",
        "testing_cents": testing_amount_cents(),
        "scan_mode": hb.get("scan_mode", ""),
        "sessions_seen": hb.get("sessions_seen", 0),
        "allowed_total": hb.get("allowed_total", 0),
        "allowed_visible": hb.get("allowed_visible", 0),
        "instance_locked": locked,
    }
