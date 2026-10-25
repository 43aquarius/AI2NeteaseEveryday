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
PLAYLIST_AT = (20, 30) # 日歌单生成时点 20:30(2026-10-25 用户需求"一天一个歌单")


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
    """日歌单(2026-10-25 改造): 每日20:30后且今日未建
    playlist_retry_at: 上次失败后的退避时间(未到不重试)"""
    if state.get("playlist_retry_at") and time.time() < state["playlist_retry_at"]:
        return False
    if (now.hour, now.minute) < PLAYLIST_AT:
        return False
    return state.get("last_playlist_date") != now.strftime("%Y-%m-%d")


def reconcile_playlist_today(cfg: dict, now) -> bool:
    """日歌单对账(state丢失防重复创建, 2026-10-25 改造):
    回读自建歌单, 今日(BJT)已创建过系列歌单(标题含丨) → 补写防重标记。
    起因: 沙箱重启清 state 导致 last_playlist_date 丢失会重复建歌单。
    仅在 due_playlist 为真时被调用, 每天最多几次 API 开销。"""
    try:
        from netease_client import NeteaseClient
        c = NeteaseClient(cfg["cookie"])
        st = c.login_status()
        uid = st.get("uid")
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for p in (c.user_playlists() or []):
            if (p.get("creator") or {}).get("userId") != uid:
                continue
            ct = p.get("createTime") or 0
            if ct / 1000 >= day0.timestamp() and "丨" in (p.get("name") or ""):
                state = load_state()
                state["last_playlist_date"] = now.strftime("%Y-%m-%d")
                state.setdefault("playlists", []).append({
                    "date": now.strftime("%Y-%m-%d"), "pid": p.get("id"),
                    "title": p.get("name"), "reconciled": True})
                save_state(state)
                print(f"[对账] {now:%F %T} 今日已有系列歌单 "
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

    # ---- 日歌单(2026-10-25 改造, 每日 20:30) ----
    if due_playlist(state, now):
        if reconcile_playlist_today(cfg, now):
            pass  # 今日已有系列歌单(对账发现), 不重复创建
        else:
            state = load_state()
            state["last_playlist_date"] = now.strftime("%Y-%m-%d")
            state.pop("playlist_retry_at", None)
            save_state(state)
            print(f"[调度] {now:%F %T} 生成日歌单")
            try:
                import playlist_maker
                playlist_maker.run(mode="daily")
            except Exception:
                traceback.print_exc()
                # 失败退避: 解除当日占坑, 30分钟后重试
                # (若实际已创建成功但对账前崩溃, reconcile 会发现并补标记)
                state = load_state()
                state.pop("last_playlist_date", None)
                state["playlist_retry_at"] = time.time() + 1800
                save_state(state)
                print("[调度] 日歌单失败, 30分钟后重试")


def main():
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL if config_exists() else 60)


if __name__ == "__main__":
    main()
