#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度器: 每日 08:00(北京时间)自动发布每日一歌 + 评论自动回复巡检

自愈设计(2026-10-22):
  - 凭据缺失时优雅等待(每60s重探), 不崩溃 — cookie 一旦恢复自动接管
  - 发帖前对账: 回读"我的动态"API, 当日已有 #每日一歌# 帖则跳过
    (state.json 被环境重启清除的场景下防止重复发帖)
  - 失败 30 分钟后重试(state.retry_at)

评论巡检(2026-10-22 新增, 用户需求"有人评论最好能够回复"):
  - 08:00~23:00 之间每 2 小时扫描一次评论区, 他人评论自动回复
  - state.replied_comments 防重复回复

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

CHECK_INTERVAL = 300   # 5 分钟对表一次
REPLY_INTERVAL = 7200  # 评论巡检间隔 2 小时
REPLY_HOURS = (8, 23)  # 巡检时段 08:00~23:00
PLAYLIST_WDAY = 6      # 周日(0=周一)
PLAYLIST_AT = (19, 30) # 周歌单生成时点 19:30


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


def due_reply(now) -> bool:
    if not (REPLY_HOURS[0] <= now.hour < REPLY_HOURS[1]):
        return False
    state = load_state()
    last = state.get("last_reply_scan_at") or 0
    return time.time() - last >= REPLY_INTERVAL


def due_playlist(state: dict, now) -> bool:
    """周歌单: 周日19:30后且本周未建(周号防重)"""
    if now.weekday() != PLAYLIST_WDAY:
        return False
    if (now.hour, now.minute) < PLAYLIST_AT:
        return False
    return state.get("last_playlist_week") != now.strftime("%G-W%V")


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
        else:
            print(f"[调度] {now:%F %T} 到点发布")
            try:
                daily_post.run()
            except Exception:
                traceback.print_exc()
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

    # ---- 评论巡检 ----
    if due_reply(now):
        state = load_state()
        state["last_reply_scan_at"] = time.time()
        save_state(state)
        print(f"[调度] {now:%F %T} 评论巡检")
        try:
            import comment_reply
            comment_reply.check_and_reply()
        except Exception:
            traceback.print_exc()

    # ---- 周歌单(2026-10-23 新增, 每周日 19:30) ----
    if due_playlist(state, now):
        state = load_state()
        state["last_playlist_week"] = now.strftime("%G-W%V")
        save_state(state)
        print(f"[调度] {now:%F %T} 生成周歌单")
        try:
            import playlist_maker
            playlist_maker.run()
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
