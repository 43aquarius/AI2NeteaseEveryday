#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度器: 每日 08:00(北京时间)自动发布每日一歌 + 评论自动回复巡检

自愈设计(2026-10-22):
  - 凭据缺失时优雅等待(每60s重探), 不崩溃 — cookie 一旦恢复自动接管
  - 发帖前对账: 回读"我的动态"API, 当日已有 #每日一歌# 帖则跳过
    (state.json 被环境重启清除的场景下防止重复发帖)
  - 失败 30 分钟后重试(state.retry_at)
  - 周歌单对账(2026-10-24): state 丢失时回读自建歌单,
    本周已有「..丨..」系列歌单则补防重标记, 防重复创建

评论巡检(2026-10-22 新增, 用户需求"有人评论最好能够回复"):
  - 08:00~23:00 之间每 2 小时扫描一次评论区, 他人评论自动回复
  - state.replied_comments 防重复回复

每日歌单(2026-10-27 改造, 用户指令"一天一歌单, 以后不能再漏"):
  - 每天 20:00 生成一期(原为每周日19:30, 已升级)
  - 三重防漏: ① state.last_playlist_date 当日标记(含 .secrets 镜像)
    ② API 对账(user_playlists 当日已有创建即补标记)
    ③ playlist_retry_at 失败30分钟冷却重试, 当天内持续补

用法:
  python3 scheduler.py            # 前台运行(调试)
  python3 run_daemon.py           # 守护进程拉起(推荐, 沙箱内可常驻)
"""
import json
import time
import traceback
from datetime import datetime, timedelta

from config import (BJT, config_exists, load_config, load_state, now_bjt,
                    save_state, today_bjt)
import daily_post

CHECK_INTERVAL = 300   # 5 分钟对表一次
REPLY_INTERVAL = 7200  # 评论巡检间隔 2 小时
REPLY_HOURS = (8, 23)  # 巡检时段 08:00~23:00
PLAYLIST_AT = (20, 0)  # 每日歌单生成时点 20:00(2026-10-27 改每日)
PLAYLIST_RETRY = 1800  # 歌单失败后重试冷却(秒)


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
    """每日歌单: 20:00后且今日未建(日期防重, 2026-10-27 改每日)"""
    if (now.hour, now.minute) < PLAYLIST_AT:
        return False
    return state.get("last_playlist_date") != now.strftime("%Y-%m-%d")


def reconcile_playlist_today(cfg: dict, now) -> bool:
    """每日歌单对账(state丢失防重复创建, 2026-10-27 每日版):
    回读自建歌单, 今天(createTime>=今日00:00)已有创建 → 补写防重标记。
    起因: 沙箱周期性清 state 导致漏建/重复建, API 对账才是真相源。"""
    try:
        from netease_client import NeteaseClient
        c = NeteaseClient(cfg["cookie"])
        st = c.login_status()
        uid = st.get("uid")
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for p in (c.user_playlists() or []):
            if (p.get("creator") or {}).get("userId") != uid:
                continue
            ct = p.get("createTime") or 0
            if ct / 1000 >= midnight.timestamp():
                state = load_state()
                state["last_playlist_date"] = now.strftime("%Y-%m-%d")
                state.pop("playlist_retry_at", None)
                save_state(state)
                print(f"[对账] {now:%F %T} 今天已建过歌单 "
                      f"{p.get('name')!r}, 补防重标记, 跳过创建")
                return True
    except Exception:
        traceback.print_exc()
    return False


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

    # ---- 每日歌单(2026-10-27 改造: 每天20:00, 三重防漏) ----
    if due_playlist(state, now):
        if reconcile_playlist_today(cfg, now):
            pass  # 今天已建(对账发现), 不重复创建
        elif state.get("playlist_retry_at") and \
                time.time() < state["playlist_retry_at"]:
            pass  # 上次失败冷却中, 到点自动重试
        else:
            print(f"[调度] {now:%F %T} 生成每日歌单")
            try:
                import playlist_maker
                r = playlist_maker.run()
                state = load_state()
                state.pop("playlist_retry_at", None)
                if not r.get("ok") and r.get("reason") == "no_today_song":
                    # 今日歌还没发(发帖失败/延迟), 5分钟后再来看
                    pass
                save_state(state)
            except Exception:
                traceback.print_exc()
                state = load_state()
                state["playlist_retry_at"] = time.time() + PLAYLIST_RETRY
                save_state(state)


def main():
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL if config_exists() else 60)


if __name__ == "__main__":
    main()
