"""
后台启动面板与机器人（独立进程，关掉终端/对话也不受影响）。

为什么需要这个脚本：
    用普通方式在后台跑，进程是挂在当前 shell 下的。shell 一退出，
    进程就被回收——实测昨晚机器人就是这么在凌晨 1 点停掉的。
    DETACHED_PROCESS 让子进程不继承父进程的控制台，
    真正变成独立进程，这才是「挂机」该有的样子。

用法：
    python tools/start_background.py            # 两个都起
    python tools/start_background.py --web      # 只起面板
    python tools/start_background.py --bot      # 只起机器人
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
HEARTBEAT = os.path.join(ROOT, "data", "heartbeat.json")
PORT = 8080


def _detached_flags():
    if os.name == "nt":
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _port_open(port=PORT, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        h = k32.OpenProcess(0x1000, 0, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _bot_pid():
    if not os.path.isfile(HEARTBEAT):
        return None
    try:
        with open(HEARTBEAT, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("pid")
    except Exception:
        return None


def start_web():
    if _port_open():
        print(f"  面板已在运行（127.0.0.1:{PORT}），跳过")
        return True

    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    out = open(os.path.join(log_dir, "web.out.log"), "a", encoding="utf-8")

    p = subprocess.Popen(
        [PY, os.path.join(ROOT, "web", "app.py"), "--port", str(PORT)],
        cwd=ROOT, creationflags=_detached_flags(),
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        close_fds=True,
    )
    print(f"  面板已启动  pid={p.pid}  http://127.0.0.1:{PORT}")
    return True


def start_bot(interval="1"):
    pid = _bot_pid()
    if pid and _pid_alive(pid):
        print(f"  机器人已在运行（pid={pid}），跳过")
        return True

    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    out = open(os.path.join(log_dir, "bot.out.log"), "a", encoding="utf-8")

    p = subprocess.Popen(
        [PY, os.path.join(ROOT, "src", "main.py"), "loop",
         "--send", "--interval", interval],
        cwd=ROOT, creationflags=_detached_flags(),
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        close_fds=True,
    )
    print(f"  机器人已启动  pid={p.pid}  （真发模式，间隔 {interval}s）")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--bot", action="store_true")
    ap.add_argument("--interval", default="1")
    args = ap.parse_args()

    both = not (args.web or args.bot)

    print("后台启动：")
    if both or args.web:
        start_web()
    if both or args.bot:
        start_bot(args.interval)

    time.sleep(3)

    print()
    print("当前状态：")
    print(f"  面板端口 {PORT}: {'在监听' if _port_open() else '未监听'}")
    pid = _bot_pid()
    if pid and _pid_alive(pid):
        print(f"  机器人 pid={pid}: 存活")
    else:
        print(f"  机器人 pid={pid}: 未运行")

    if os.path.isfile(HEARTBEAT):
        age = time.time() - os.path.getmtime(HEARTBEAT)
        print(f"  最后心跳: {age:.1f} 秒前")


if __name__ == "__main__":
    main()
