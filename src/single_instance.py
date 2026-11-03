"""
单实例保护：保证同一时刻只有一个机器人在操作微信。

为什么必须有
------------
两个机器人同时跑不是"慢一点"，而是会发错消息：

    进程 A 正在输入框里打金额 → 进程 B 把窗口切去另一个群
    → A 的 Ctrl+V 落在 B 的群上 → 报价发给了不相干的客户。

而且同一张订单图两个进程都会看到，各回一次，客户收到两条报价。
这类故障现场很难复现，事后也说不清是谁发的。

为什么用命名互斥体，不用文件锁
------------------------------
早先的实现是锁 data/bot.lock。它有三个洞：

1. **只对"同一个文件"生效**。用户把项目复制一份、或者从另一个目录再起一个，
   锁文件根本不共享，保护直接失效——而复制目录恰恰是最常见的试新版本方式。
2. **锁的是"文件里从指针开始的 N 字节"**，一不小心就被 open("w") 截断打乱。
3. 只能挡同目录，挡不住"面板启动的"和"bat 启动的"这种不同来源。

命名互斥体是内核对象，全局唯一、跟工作目录无关、进程被强杀或断电时
由系统自动回收（不会留下"假占用"让用户以为还在跑）。

用法
----
    import single_instance
    if not single_instance.acquire():
        print(single_instance.describe_owner())
        sys.exit(2)

拿到锁之后**不要主动释放**——句柄留在模块变量里，进程活着锁就在。
进程退出（正常或异常）由系统释放。

只查询不占用的场景（Web 面板判断能不能启动）用 is_taken()。
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(ROOT, "data")
OWNER_FILE = os.path.join(DATA_DIR, "instance.json")

# 互斥体名字。改名字等于换了把锁，会让新旧版本互相看不见，不要随意改。
MUTEX_NAMES = ("Global\\WeChatOrderBot", "Local\\WeChatOrderBot")

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

_handle = None
_used_name = None


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def _create(name):
    """创建（或打开已存在的）互斥体。返回 (句柄, last_error)。"""
    k = _k32()
    h = k.CreateMutexW(None, True, name)
    return h, ctypes.get_last_error()


def acquire():
    """
    尝试取得实例锁。成功返回 True。

    先试 Global\\（跨登录会话也有效），没有权限再退到 Local\\。
    如果名字已存在（说明有实例在跑），立刻返回 False，不降级——
    降级会让"已经有人占着"变成"没占到"，保护就形同虚设。
    """
    global _handle, _used_name
    if _handle is not None:
        return True

    if os.name != "nt":
        return _acquire_posix()

    k = _k32()
    last_err = 0
    for name in MUTEX_NAMES:
        h, err = _create(name)
        if not h:
            last_err = err
            continue                 # 这个名字建不出来（多半是权限），换下一个
        if err == ERROR_ALREADY_EXISTS:
            k.CloseHandle(h)
            return False             # 有实例在跑
        _handle = h
        _used_name = name
        _write_owner()
        return True

    # 两个名字都建不出来：大概率是权限问题。宁可放行也不能把用户挡在门外，
    # 但要在日志里喊一声，方便排查。
    print(f"[warn] 无法创建实例互斥体（错误码 {last_err}），单实例保护未生效")
    return True


def _acquire_posix():
    """非 Windows 兜底：文件锁。开发/调试用，生产是 Windows。"""
    global _handle
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fh = open(os.path.join(DATA_DIR, "bot.lock"), "w")
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _handle = fh
        _write_owner()
        return True
    except Exception:
        return False


def is_taken():
    """
    只查询"有没有别人占着"，自己不去占。

    给 Web 面板用：点「启动」之前先问一句，别每次都要等子进程自己退出才发现重复。
    注意本进程如果自己持有锁，这里也会返回 True。
    """
    if _handle is not None:
        return True
    if os.name != "nt":
        return False

    k = _k32()
    for name in MUTEX_NAMES:
        h = k.OpenMutexW(SYNCHRONIZE, False, name)
        if h:
            k.CloseHandle(h)
            return True
    return False


# ---------------------------------------------------------------- 持有者信息
def _write_owner():
    """把 pid / 启动时间写下来，纯粹为了出错时能告诉用户"是哪一个在跑"。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        info = {
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "argv": sys.argv[1:],
        }
        with open(OWNER_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def read_owner():
    """读持有者信息。文件可能是上次崩溃留下的，只作提示，不作判据。"""
    try:
        with open(OWNER_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def describe_owner():
    """
    给用户看的一句话说明，尽量具体到"哪个 pid、什么时候起的"。

    先看锁还在不在：instance.json 是普通文件，进程被强杀时来不及删，
    留着上次的 pid。只看文件会对着一个早就退出的进程说"它在运行"，
    用户去关窗口时会一头雾水。锁（内核对象）在不在才是真的。
    """
    if not is_taken():
        return "当前没有机器人在运行"

    info = read_owner()
    pid = info.get("pid")
    started = info.get("started_at", "")[:19].replace("T", " ")
    if pid and started:
        return f"已经有一个机器人在运行（pid {pid}，启动于 {started}）"
    if pid:
        return f"已经有一个机器人在运行（pid {pid}）"
    return "已经有一个机器人在运行"


def clear_owner():
    try:
        os.remove(OWNER_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 自检
def _selfcheck():
    print(f"互斥体名字: {MUTEX_NAMES}")
    print(f"当前是否已被占用: {is_taken()}")
    ok = acquire()
    print(f"本进程获取锁: {ok}")
    print(f"获取后再查: {is_taken()}")
    print(f"持有者: {read_owner()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
