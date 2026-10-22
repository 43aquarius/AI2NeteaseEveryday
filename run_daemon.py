#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护进程启动器: 双fork脱离会话拉起 scheduler.py (沙箱内实测可常驻)

用法: python3 run_daemon.py
"""
import os
import sys
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "daemon.log")
PID_FILE = os.path.join(BASE, "daemon.pid")


def daemonize() -> bool:
    pid = os.fork()
    if pid > 0:
        return False
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    log = open(LOG, "a")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    if os.path.exists(PID_FILE):
        try:
            old = int(open(PID_FILE).read().strip())
            if alive(old):
                print(f"调度器已在运行 (pid={old})")
                return
        except Exception:
            pass
    if not daemonize():
        print(f"daemon 已 fork 到后台, 日志: {LOG}")
        return
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    subprocess.run([sys.executable, os.path.join(BASE, "scheduler.py")])


if __name__ == "__main__":
    main()
