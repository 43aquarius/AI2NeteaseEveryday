#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评论自动回复模块 (2026-10-22 用户需求: "评论区里面如果有人评论,最好能够回复")

机制:
- 扫描本人最近 N 天动态的评论区
- 他人发表且未回复过的顶层评论 → LLM 生成走心短回复 → xeapi 回复
- xeapi 失败自动降级: add_comment + "@昵称 " 提及式回复
- state.replied_comments 记录已回复 commentId, 绝不重复回复
- 每轮最多回复 max_per_run 条, 防刷屏

2026-10-22 实测备注: eapi/weapi 的 /api/v1/resource/comments/reply 均已
404(端点升级), 必须走 xeapi(纯Python复刻, 见 netease_client._xeapi)。
"""
import json
import subprocess
import tempfile
import os
import time

from config import load_config, load_state, save_state, today_bjt
from netease_client import NeteaseClient

REPLY_SYSTEM_PROMPT = (
    "你是网易云音乐博主'纯音乐--请欣赏', 每天分享一首好歌。"
    "现在要回复乐迷在你动态下的评论。要求: 10~45字, 口语化, 像老朋友聊天, "
    "真诚具体不说套话, 可以适当可爱但不过分。感谢夸奖时带上点个人听歌感受, "
    "回答问题时不确定的信息不要编造。绝不使用 emoji。"
    "直接输出回复内容本身, 不要引号不要解释。"
)


def _call_llm(prompt: str, timeout: int = 240) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        cmd = ["z-ai", "chat", "--prompt", prompt,
               "--system", REPLY_SYSTEM_PROMPT, "-o", out_path]
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
        with open(out_path) as f:
            data = json.load(f)
        return ((data.get("choices") or [{}])[0]
                .get("message", {}).get("content", "")).strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _clean(text: str) -> str:
    t = text.strip()
    for q in ('"', "'", "“", "”", "「", "」"):
        if t.startswith(q) and t.endswith(q) and len(t) > 2:
            t = t[1:-1].strip()
    import re
    emoji_pat = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\u2b50\u2728\ufe0f]+",
        flags=re.UNICODE)
    t = emoji_pat.sub("", t).strip()
    t = re.sub(r"\s+", " ", t)
    # 硬上限(评论别太长)
    return t[:80]


def gen_reply(comment: str, song_name: str) -> str:
    prompt = (f"乐迷在你的动态(分享了歌曲《{song_name}》)下评论: "
              f"\"{comment[:120]}\"\n请写一条回复。")
    for attempt in range(2):
        try:
            t = _clean(_call_llm(prompt))
            if len(t) >= 4:
                return t
        except subprocess.TimeoutExpired:
            pass
    # 降级模板
    import random
    return random.choice([
        "谢谢喜欢, 这首歌最近也在单曲循环。",
        "被你这么一说, 又想再听一遍了。",
        "谢谢你, 明天还有好歌, 记得来看看。",
        "同好! 戴上耳机世界都是这首歌的。",
    ])


def recent_threads(c: NeteaseClient, days: int = 7) -> list:
    """本人最近动态的 (threadId, 歌名) 列表"""
    if not c.uid:
        c.login_status()
    events = c.user_events(pagesize=20)
    now_ms = time.time() * 1000
    out = []
    for ev in events:
        if now_ms - (ev.get("showTime") or 0) > days * 86400 * 1000:
            continue
        tid = (ev.get("info") or {}).get("threadId") or ev.get("threadId")
        if not tid:
            continue
        j = json.loads(ev.get("json") or "{}")
        song_name = (j.get("song") or {}).get("name") or "每日一歌"
        out.append({"thread_id": tid, "song": song_name,
                    "event_id": ev.get("id")})
    return out


def check_and_reply(dry_run: bool = False) -> dict:
    """主入口: 扫评论 → 回复 → 记账"""
    cfg = load_config()
    state = load_state()
    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    if not st.get("ok"):
        print("[评论] cookie 失效, 跳过本轮")
        return {"ok": False, "reason": "cookie_expired"}
    my_uid = st["uid"]

    replied = set(state.get("replied_comments") or [])
    max_per_run = cfg.get("comment_reply", {}).get("max_per_run", 5)
    days = cfg.get("comment_reply", {}).get("scan_days", 7)

    threads = recent_threads(c, days)
    print(f"[评论] 扫描 {len(threads)} 条动态(近{days}天), "
          f"已回复过 {len(replied)} 条评论")

    done, skipped, failed = 0, 0, 0
    for th in threads:
        if done >= max_per_run:
            break
        try:
            body = c.read_comments(th["thread_id"], limit=30)
        except Exception as e:
            print(f"  [评论] 读取失败 {th['event_id']}: {e}")
            continue
        for cm in body.get("comments") or []:
            if done >= max_per_run:
                break
            user = cm.get("user") or {}
            cid = cm.get("commentId")
            content = (cm.get("content") or "").strip()
            if not cid or user.get("userId") == my_uid:
                continue           # 自己的评论
            if cid in replied:
                continue           # 已回复
            be = cm.get("beReplied") or []
            if be and isinstance(be, list) and be[0]:
                # 楼中楼: 只回应"回复我的"那条, 他人间对话不插嘴
                target = (be[0].get("user") or {}).get("userId")
                if target != my_uid:
                    continue
            if not content:
                continue
            nickname = user.get("nickname") or "乐迷"
            print(f"  [新评论] {nickname}: {content[:40]}")
            if dry_run:
                reply = gen_reply(content, th["song"])
                print(f"  [dry-run] 将回复: {reply}")
                replied.add(cid)
                done += 1
                continue
            reply = gen_reply(content, th["song"])
            try:
                r = c.reply_comment(th["thread_id"], cid, reply)
                print(f"  [回复✓] {reply} (xeapi, id="
                      f"{(r.get('comment') or {}).get('commentId')})")
            except Exception as e:
                # 降级: @提及式新评论
                print(f"  [回复✗] xeapi失败({str(e)[:60]}), 尝试@提及降级")
                try:
                    c.add_comment(th["thread_id"], f"@{nickname} {reply}")
                    print(f"  [回复✓] {reply} (@提及降级)")
                except Exception as e2:
                    print(f"  [回复✗] 降级也失败: {str(e2)[:60]}")
                    failed += 1
                    continue
            replied.add(cid)
            done += 1
            state["replied_comments"] = sorted(replied)[-2000:]  # 防膨胀
            state["last_reply_date"] = today_bjt()
            save_state(state)
            time.sleep(8)  # 礼貌间隔

    print(f"[评论] 本轮完成: 回复{done} 失败{failed}")
    return {"ok": True, "replied": done, "failed": failed,
            "scanned_threads": len(threads)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    check_and_reply(dry_run=args.dry_run)
