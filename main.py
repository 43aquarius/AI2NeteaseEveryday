#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云音乐每日一歌自动发布 - CLI 入口

用法:
  python3 main.py status              # 查看账号/状态
  python3 main.py post                # 立即发布今天的一歌
  python3 main.py post --dry-run      # 干跑(不发布)
  python3 main.py verify              # 回读最近动态
  python3 main.py reply               # 立即扫一轮评论并回复
  python3 main.py reply --dry-run     # 干跑评论回复
  python3 main.py scheduler           # 前台跑调度(一般用 run_daemon.py)
"""
import argparse
import json

from config import load_config, load_state, today_bjt
from netease_client import NeteaseClient


def cmd_status():
    cfg = load_config()
    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    state = load_state()
    print(f"账号: {st.get('nickname')} (uid={st.get('uid')})")
    print(f"登录态: {'✓ 有效' if st.get('ok') else '✗ 失效'}")
    last = state.get("last_post")
    today = today_bjt()
    if last:
        print(f"上次发布: {last['date']} {last['time']} 《{last['song']}》"
              f" 验证={'✓' if last.get('verified') else '✗'}")
    print(f"今日({today}){'已发' if state.get('last_post_date') == today else '未发'}")
    print(f"历史共 {len(state.get('history', []))} 条")


def cmd_post(dry_run=False):
    import daily_post
    r = daily_post.run(dry_run=dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:600])


def cmd_verify():
    cfg = load_config()
    c = NeteaseClient(cfg["cookie"])
    c.login_status()
    evs = c.user_events(pagesize=5)
    print(f"最近 {len(evs)} 条动态:")
    for e in evs:
        try:
            j = json.loads(e.get("json") or "{}")
        except Exception:
            j = {}
        song = (j.get("song") or {})
        arts = "/".join(a.get("name", "") for a in song.get("artists", []))
        print(f"  {e.get('id')} | {e.get('showTime', '')} | "
              f"{(j.get('msg') or '')[:40]!r} | "
              f"{song.get('name', '')} - {arts}")


def cmd_reply(dry_run=False):
    import comment_reply
    r = comment_reply.check_and_reply(dry_run=dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:400])


def cmd_playlist(dry_run=False):
    import playlist_maker
    r = playlist_maker.run(dry_run=dry_run)
    print(json.dumps({k: v for k, v in (r or {}).items()
                      if k != "plan"}, ensure_ascii=False, indent=1))


def cmd_scheduler():
    import scheduler
    scheduler.main()


def main():
    parser = argparse.ArgumentParser(description="网易云音乐每日一歌")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="账号与发布状态")
    p_post = sub.add_parser("post", help="立即发布")
    p_post.add_argument("--dry-run", action="store_true")
    sub.add_parser("verify", help="回读最近动态")
    p_reply = sub.add_parser("reply", help="扫描评论并自动回复")
    p_reply.add_argument("--dry-run", action="store_true")
    p_pl = sub.add_parser("playlist", help="立即生成本周歌单")
    p_pl.add_argument("--dry-run", action="store_true")
    sub.add_parser("scheduler", help="前台运行调度器")
    args = parser.parse_args()
    {"status": cmd_status,
     "post": lambda: cmd_post(args.dry_run),
     "verify": cmd_verify,
     "reply": lambda: cmd_reply(args.dry_run),
     "playlist": lambda: cmd_playlist(args.dry_run),
     "scheduler": cmd_scheduler}.get(args.cmd, lambda: parser.print_help())()


if __name__ == "__main__":
    main()
