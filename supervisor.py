#!/usr/bin/env python3
"""后厨管家 AI 问数服务守护进程：保持 app.py 持续运行并监听 8011。"""
import os
import signal
import subprocess
import sys
import time
from urllib import request

PORT = 8011
CHECK_INTERVAL = 15          # 健康检查间隔（秒）
STARTUP_GRACE = 5            # 启动后宽限时间（秒）
RESTART_BACKOFF = 5          # 启动失败重试间隔（秒）
PYTHON = os.environ.get(
    "PYTHON",
    "/Users/phil/.workbuddy/binaries/python/versions/3.13.12/bin/python3",
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(APP_DIR, "app.py")
URL = f"http://127.0.0.1:{PORT}/"
LOG = "/tmp/aiqa.log"

running = True
proc = None


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [supervisor] {msg}", flush=True)


def kill_proc(p):
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass


def is_healthy():
    try:
        req = request.Request(URL, method="GET")
        with request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def signal_handler(signum, _frame):
    global running
    log(f"收到信号 {signum}，准备退出...")
    running = False
    kill_proc(proc)


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def main():
    global proc
    log("Supervisor 启动")
    while running:
        need_start = False
        if proc is None or proc.poll() is not None:
            log("app.py 未运行，准备启动")
            need_start = True
        elif not is_healthy():
            log("健康检查失败，重启 app.py")
            kill_proc(proc)
            need_start = True

        if not need_start:
            time.sleep(CHECK_INTERVAL)
            continue

        kill_proc(proc)
        log(f"启动: {PYTHON} -u {APP}")
        try:
            proc = subprocess.Popen(
                [PYTHON, "-u", APP],
                cwd=APP_DIR,
                stdout=open(LOG, "a"),
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log(f"启动失败: {e}")
            time.sleep(RESTART_BACKOFF)
            continue

        time.sleep(STARTUP_GRACE)
        if is_healthy():
            log("app.py 启动成功，健康检查通过")
        else:
            log("app.py 启动后健康检查未通过，将继续监控")

    kill_proc(proc)
    log("Supervisor 退出")


if __name__ == "__main__":
    main()
