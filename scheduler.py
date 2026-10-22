#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度器: 每日 08:00(北京时间)自动发布每日一歌, 双fork守护进程

用法:
  python3 scheduler.py            # 前台运行(调试)
  python3 run_daemon.py           # 守护进程拉起(推荐, 沙箱内可常驻)
"""
import time
import traceback

from config import load_config, now_bjt
from netease_client import NeteaseClient
import daily_post

CHECK_INTERVAL = 300  # 5 分钟对表一次


def due_now(cfg: dict, now) -> bool:
    pt = cfg.get("post_time", {"hour": 8, "minute": 0})
    return (now.hour, now.minute) >= (pt["hour"], pt["minute"])


def should_post(state: dict, now) -> bool:
    return state.get("last_post_date") != now.strftime("%Y-%m-%d")


def tick():
    cfg = load_config()
    now = now_bjt()
    from config import load_state
    state = load_state()
    if due_now(cfg, now) and should_post(state, now):
        print(f"[调度] {now:%F %T} 到点发布")
        try:
            daily_post.run()
        except Exception:
            traceback.print_exc()
            # 失败 30 分钟后重试(通过 pending 标记)
            state = load_state()
            state["retry_at"] = time.time() + 1800
            from config import save_state
            save_state(state)
    elif state.get("retry_at") and time.time() >= state.pop("retry_at", 0):
        from config import save_state
        save_state(state)
        print("[调度] 重试上次失败任务")
        try:
            daily_post.run()
        except Exception:
            traceback.print_exc()


def main():
    print(f"[调度器] 启动, 每日 {load_config().get('post_time', {}).get('hour', 8):02d}:00 发布每日一歌")
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
