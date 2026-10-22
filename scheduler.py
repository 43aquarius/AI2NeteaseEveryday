#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度器: 每日 08:00(北京时间)自动发布每日一歌

自愈设计(2026-10-22):
  - 凭据缺失时优雅等待(每60s重探), 不崩溃 — cookie 一旦恢复自动接管
  - 发帖前对账: 回读"我的动态"API, 当日已有 #每日一歌# 帖则跳过
    (state.json 被环境重启清除的场景下防止重复发帖)
  - 失败 30 分钟后重试(state.retry_at)

用法:
  python3 scheduler.py            # 前台运行(调试)
  python3 run_daemon.py           # 守护进程拉起(推荐, 沙箱内可常驻)
"""
import json
import time
import traceback
from datetime import datetime

from config import (BJT, config_exists, load_config, load_state, now_bjt,
                    save_state, today_bjt)
import daily_post

CHECK_INTERVAL = 300  # 5 分钟对表一次


def posted_today_via_api(cfg: dict) -> bool:
    """回读我的动态: 当日已有 #每日一歌# 帖则视为已发(state丢失防双发)"""
    try:
        from netease_client import NeteaseClient
        client = NeteaseClient(cfg["cookie"])
        events = client.user_events(pagesize=10) or []
        today_ms = today_bjt()
        for ev in events:
            ts = ev.get("showTime") or ev.get("eventTime") or 0
            if not ts:
                continue
            ev_date = datetime.fromtimestamp(
                ts / 1000, BJT).strftime("%Y-%m-%d")
            if ev_date == today_ms and "每日一歌" in json.dumps(ev, ensure_ascii=False):
                return True
    except Exception:
        pass
    return False


def due_now(cfg: dict, now) -> bool:
    pt = cfg.get("post_time", {"hour": 8, "minute": 0})
    return (now.hour, now.minute) >= (pt["hour"], pt["minute"])


def should_post(state: dict, now) -> bool:
    return state.get("last_post_date") != now.strftime("%Y-%m-%d")


def tick():
    if not config_exists():
        print(f"[等待] {now_bjt():%F %T} 凭据未配置(三层均缺), "
              f"等待用户提供 MUSIC_U ...")
        return
    cfg = load_config()
    now = now_bjt()
    state = load_state()
    if due_now(cfg, now) and should_post(state, now):
        # state 丢失场景: 先回读对账防双发
        if posted_today_via_api(cfg):
            print(f"[对账] {now:%F %T} 动态回读发现今日已发, 补写标记")
            state["last_post_date"] = today_bjt()
            save_state(state)
            return
        print(f"[调度] {now:%F %T} 到点发布")
        try:
            daily_post.run()
        except Exception:
            traceback.print_exc()
            # 失败 30 分钟后重试(通过 pending 标记)
            state = load_state()
            state["retry_at"] = time.time() + 1800
            save_state(state)
    elif state.get("retry_at") and time.time() >= state.pop("retry_at", 0):
        save_state(state)
        print("[调度] 重试上次失败任务")
        try:
            daily_post.run()
        except Exception:
            traceback.print_exc()


def main():
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL if config_exists() else 60)


if __name__ == "__main__":
    main()
