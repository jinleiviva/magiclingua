#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
MagicLingua Native Messaging Host
================================

Chrome 扩展本身不能启动本地进程（沙箱限制），这个脚本是官方允许的通路：
Chrome 以 stdio 管道拉起本脚本，扩展通过它控制翻译服务的启停。

协议（Chrome Native Messaging）：
    stdin  : 4 字节小端长度前缀 + UTF-8 JSON
    stdout : 同样格式回一条 JSON

支持动作：
    {"action": "start"}   启动服务（launchd 优先，失败则直接派生进程）
    {"action": "stop"}    停止服务（HTTP 优雅退出，超时后强制结束）
    {"action": "status"}  查询服务状态
    {"action": "ping"}    探活

只用标准库，系统 python3 即可运行，不依赖项目 venv。
"""

import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

LABEL = "com.magiclingua.server"
HOST_NAME = "com.magiclingua.host"
PORT = int(os.getenv("HYMT_PORT", "18770"))
BASE = "http://127.0.0.1:%d" % PORT

# native_host/ 的上一级就是项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 安装到 Library 等独立位置时，脚本旁边的 hymt_root.txt 指明项目根目录
_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hymt_root.txt")
if os.path.exists(_CONF):
    try:
        _r = open(_CONF).read().strip()
        if _r and os.path.isdir(_r):
            ROOT = _r
    except Exception:
        pass
VENV_PY = os.path.join(ROOT, "venv", "bin", "python3")
SERVER = os.path.join(ROOT, "server_gguf.py")

# Chrome 派生的宿主进程对 Downloads 目录没有写权限（实测），
# 所以宿主自己的运行时文件（日志/pid/服务日志）全部放在脚本所在目录（Library）。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "log_server.txt")
PID_FILE = os.path.join(SCRIPT_DIR, ".server.pid")
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)

GUI = "gui/%d" % os.getuid()

# 调试日志（写到文件，绝不写 stdout，避免污染 native messaging 协议）
DEBUG_LOG = os.path.join(SCRIPT_DIR, "hymt_debug.log")


def dlog(msg):
    try:
        with open(DEBUG_LOG, "ab") as f:
            line = ("%s %s\n" % (time.strftime("%H:%M:%S"), msg)).encode("utf-8")
            f.write(line)
            f.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Native Messaging 帧读写
# ---------------------------------------------------------------------------

def read_message():
    """读一帧：4 字节小端长度 + payload。读不到完整帧返回 None。"""
    try:
        header = sys.stdin.buffer.read(4)
    except Exception as e:
        dlog("read header error: %s" % e)
        return None
    if len(header) < 4:
        dlog("read header EOF/short: got %d bytes" % len(header))
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0 or length > 16 * 1024 * 1024:
        dlog("bad length: %d" % length)
        return None
    payload = b""
    try:
        while len(payload) < length:
            chunk = sys.stdin.buffer.read(length - len(payload))
            if not chunk:
                dlog("payload EOF: got %d/%d bytes" % (len(payload), length))
                return None
            payload += chunk
    except Exception as e:
        dlog("read payload error: %s" % e)
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as e:
        dlog("json decode error: %s (payload=%r)" % (e, payload[:200]))
        return None


def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        sys.stdout.buffer.write(struct.pack("<I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception as e:
        dlog("send_message error: %s" % e)
        raise
    dlog("sent %d bytes: %s" % (len(data), data[:160].decode("utf-8", "replace")))


# ---------------------------------------------------------------------------
# 服务状态探测
# ---------------------------------------------------------------------------

def probe(timeout=1.5):
    """返回 'ok' / 'loading' / None（服务未起）。"""
    try:
        req = urllib.request.Request(BASE + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("status")
    except Exception as e:
        dlog("probe error: %s" % e)
        return None


def wait_up(seconds):
    """等待服务端口起来（模型加载前 /health 会返回 loading，也算起来了）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = probe()
        if state:
            return state
        time.sleep(0.5)
    return None


def wait_down(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if probe() is None:
            return True
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def launchctl(*args):
    return subprocess.run(
        ["launchctl"] + list(args),
        capture_output=True, text=True, timeout=20,
    )


def start_via_launchd():
    """launchd 已注册时用它拉起，进程生命周期交给系统管理。"""
    if not os.path.exists(PLIST):
        return False

    r = launchctl("kickstart", "-k", "%s/%s" % (GUI, LABEL))
    if r.returncode == 0:
        return True

    # 还没 bootstrap 过（比如刚装完或重启后）
    b = launchctl("bootstrap", GUI, PLIST)
    if b.returncode == 0 or "already" in (b.stderr or "").lower():
        r = launchctl("kickstart", "-k", "%s/%s" % (GUI, LABEL))
        return r.returncode == 0
    return False


def start_direct():
    """兜底：直接派生进程，脱离当前会话。"""
    python = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    dlog("start_direct using python=%s" % python)
    env = os.environ.copy()
    env["HYMT_PORT"] = str(PORT)
    env.setdefault("HYMT_IDLE_EXIT", "20")

    log = open(LOG_FILE, "ab")
    proc = subprocess.Popen(
        [python, SERVER],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
    except Exception:
        pass
    return True


def do_start():
    state = probe()
    if state:
        return {"ok": True, "state": state, "detail": "already-running"}

    used = []
    if start_via_launchd():
        used.append("launchd")
        if wait_up(15):
            return {"ok": True, "state": probe(), "detail": "launchd"}

    start_direct()
    used.append("direct")
    state = wait_up(20)
    if state:
        return {"ok": True, "state": state, "detail": "direct"}
    return {"ok": False, "state": "failed", "detail": "timeout", "tried": used}


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------

def do_stop():
    # 1) 最优雅：让服务自己结束（退出码 0，launchd 不会重启）
    try:
        req = urllib.request.Request(BASE + "/v1/shutdown", data=b"{}", method="POST")
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass

    if wait_down(8):
        if os.path.exists(PLIST):
            launchctl("stop", "%s/%s" % (GUI, LABEL))
        # 停掉后清掉 pid 文件，避免下次误判
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return {"ok": True, "state": "stopped"}

    # 2) launchd 兜底
    if os.path.exists(PLIST):
        launchctl("kill", "SIGTERM", "%s/%s" % (GUI, LABEL))
        if wait_down(6):
            return {"ok": True, "state": "stopped"}

    # 3) 最后手段：按 pid / 进程名结束
    pid = None
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
        except Exception:
            pid = None
    try:
        if pid:
            os.kill(pid, 15)
            time.sleep(1.0)
            os.kill(pid, 9)
        else:
            subprocess.run(["pkill", "-f", "server_gguf.py"], timeout=10)
    except Exception:
        pass

    stopped = wait_down(6)
    return {"ok": stopped, "state": "stopped" if stopped else "still-running"}


def do_status():
    state = probe() or "offline"
    return {
        "ok": True,
        "running": state != "offline",
        "state": state,
        "launchd_registered": os.path.exists(PLIST),
        "port": PORT,
    }


# ---------------------------------------------------------------------------

def handle(msg):
    action = (msg or {}).get("action", "")
    if action == "start":
        return do_start()
    if action == "stop":
        return do_stop()
    if action == "status":
        return do_status()
    if action == "ping":
        return {"ok": True, "pong": True}
    return {"ok": False, "error": "unknown action: %s" % action}


def main():
    env_keys = ",".join(sorted(os.environ.keys()))
    dlog("START pid=%d argv=%s cwd=%s py=%s"
         % (os.getpid(), sys.argv, os.getcwd(), sys.version.split()[0]))
    dlog("ENV KEYS=%s" % env_keys)
    dlog("ENV PATH=%s" % os.environ.get("PATH", "<none>"))
    dlog("ENV PYTHONHOME=%s PYTHONPATH=%s"
         % (os.environ.get("PYTHONHOME", "<none>"), os.environ.get("PYTHONPATH", "<none>")))
    while True:
        msg = read_message()
        if msg is None:
            dlog("main: no message (EOF/parse fail) -> exit")
            break
        dlog("main: action=%s" % ((msg or {}).get("action")))
        try:
            resp = handle(msg)
        except Exception as e:
            resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            dlog("main: handle exception: %s" % resp["error"])
        try:
            send_message(resp)
        except Exception:
            dlog("main: send failed, exit")
            break
    dlog("END pid=%d" % os.getpid())


if __name__ == "__main__":
    main()
