#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云凭据一键重建(2026-10-22 重启丢凭据后设计)

背景: 沙箱重启会清除快照点之后新建的文件, config.json(.gitignore 排除,
从不进快照)首当其冲。2026-10-22 15:09 的重启清掉了刚建好的网易云
config.json, 而 MUSIC_U 完整串只存在于已耗尽的上一个会话上下文中。

用法:
  python3 rebuild_config.py "MUSIC_U=xxx; __csrf=yyy"   # 参数直传
  python3 rebuild_config.py                             # 交互输入
  MUSIC_U=xxx python3 rebuild_config.py --env           # 环境变量

写入三层(见 config.py):
  1. {项目}/config.json                  运行位(重启可能丢)
  2. /home/z/.secrets/netease_config.json  项目外持久层(重启免疫)
  3. {项目}/secrets_backup.json           base64 快照追踪位(最可靠)
     ⚠ secrets_backup.json 严禁推送 GitHub

随后自动验证登录态; 等待中的调度器会在 60 秒内自动接管。
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def normalize_cookie(raw: str) -> str:
    """容错: 用户可能粘贴整段 Cookie 头或只有 MUSIC_U"""
    raw = raw.strip().strip('"').strip("'").strip()
    jar = {}
    for p in raw.split(";"):
        p = p.strip()
        if "=" in p:
            k, _, v = p.partition("=")
            jar[k.strip()] = v.strip()
    if "MUSIC_U" not in jar:
        raise ValueError("cookie 中未找到 MUSIC_U 字段")
    jar.setdefault("__csrf", "")
    jar.setdefault("os", "pc")
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cookie", nargs="?", help="cookie 串(含 MUSIC_U)")
    ap.add_argument("--env", action="store_true", help="从 $MUSIC_U_COOKIE 读")
    args = ap.parse_args()

    if args.cookie:
        raw = args.cookie
    elif args.env and os.environ.get("MUSIC_U_COOKIE"):
        raw = os.environ["MUSIC_U_COOKIE"]
    else:
        print("请粘贴网易云音乐 cookie(至少含 MUSIC_U=...):")
        raw = input("> ").strip()

    cfg = {
        "cookie": normalize_cookie(raw),
        "post_time": {"hour": 8, "minute": 0},
        "topic_tag": "每日一歌",
        "llm": {"style": "warm", "min_len": 30, "max_len": 90},
    }

    from config import write_config
    write_config(cfg)
    print("[写入] 三层凭据完成: config.json + .secrets + secrets_backup.json")

    from netease_client import NeteaseClient
    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    if st.get("ok"):
        print(f"[验证] ✅ 登录正常: {st.get('nickname')} (uid={st.get('uid')})")
        print("[后续] 等待中的调度器自动接管; 明日 08:00 自动发帖")
        return 0
    print(f"[验证] ❌ 登录失败: {st}")
    print("       cookie 可能不完整或已过期, 请重新从浏览器复制")
    return 1


if __name__ == "__main__":
    sys.exit(main())
