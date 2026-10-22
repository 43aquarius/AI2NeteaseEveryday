#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一歌内容管线: 日推选歌 → LLM 文案 → 分享歌曲动态 → 回读验证

内容形态: 歌曲卡片动态(自动带封面可点击播放) + 走心文案 + #每日一歌# 话题
选歌策略: 每日推荐列表(33首)中以日期为种子随机选 1 首(同日可复现);
          日推不可用时降级热歌榜。
"""
import json
import random
import re
import subprocess
import tempfile
import os
from datetime import datetime

from config import load_config, load_state, save_state, today_bjt
from netease_client import NeteaseClient

HOT_TOPLIST_ID = 3779629  # 热歌榜


# ---------- LLM ----------
SYSTEM_PROMPT = (
    "你是网易云音乐社区的资深乐评人, 擅长用简短走心的文字分享歌曲。"
    "文案要求: 30~90字, 有画面感或情绪共鸣, 口语化自然, 像朋友间随口的推荐, "
    "走心但不油腻, 不说教不喊口号。绝不使用 emoji 和颜文字。"
    "直接输出文案正文, 不要任何解释、引号或前后缀。"
)


def _call_llm(prompt: str, timeout: int = 300) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        cmd = ["z-ai", "chat", "--prompt", prompt,
               "--system", SYSTEM_PROMPT, "-o", out_path]
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


def _clean_copy(raw: str) -> str:
    """清洗 LLM 输出: 去引号包裹/前后缀行/emoji"""
    t = raw.strip()
    t = re.sub(r"^```.*?\n|\n```$", "", t, flags=re.S)
    # 去首尾成对引号
    for q in ('"', "'", "“", "”", "「", "」"):
        if t.startswith(q) and t.endswith(q) and len(t) > 2:
            t = t[1:-1].strip()
    # 只要第一段有效文字(防 LLM 加说明行)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    t = lines[0] if lines else t
    # emoji 与符号类字符过滤(云村文案保持纯净)
    emoji_pat = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
        "\U0001F300-\U0001F5FF\u2b50\u2728\u2764\ufe0f]+", flags=re.UNICODE)
    t = emoji_pat.sub("", t).strip()
    # 折叠多余空白
    t = re.sub(r"\s+", " ", t)
    return t


def gen_copy(song: dict) -> str:
    """为歌曲生成走心文案(纯文本, 无 JSON 解析风险)"""
    artists = "/".join(a["name"] for a in song.get("ar") or []) or "未知歌手"
    album = (song.get("al") or {}).get("name") or ""
    prompt = (
        f"今天要分享的歌曲: 《{song['name']}》 - {artists}"
        + (f" (专辑: {album})" if album else "")
        + "。\n请写一段 30~90 字的分享文案, 可以描述这首歌适合的场景、"
          "情绪或一个小故事, 让看到的人有点开的欲望。"
          "直接输出文案本身。"
    )
    for attempt in range(3):
        try:
            text = _clean_copy(_call_llm(prompt))
            cfg = load_config()
            min_len = cfg.get("llm", {}).get("min_len", 30)
            max_len = cfg.get("llm", {}).get("max_len", 90)
            if len(text) >= min_len:
                if len(text) > max_len:
                    text = text[:max_len - 1].rstrip("，。,.;；、") + "。"
                return text
            print(f"  [LLM] 第{attempt + 1}次文案太短({len(text)}字), 重试")
        except subprocess.TimeoutExpired:
            print(f"  [LLM] 第{attempt + 1}次超时, 重试")
    # 降级: 固定模板
    return f"今日份的循环曲目, 分享给同样需要它的你。来自《{song['name']}》的温柔。"


# ---------- 选歌 ----------
def pick_song(c: NeteaseClient, seed_date: str) -> dict:
    try:
        songs = c.daily_recommend_songs()
        src = "每日推荐"
    except Exception as e:
        print(f"  [选歌] 日推不可用({e}), 降级热歌榜")
        tids = c.toplist_tracks(HOT_TOPLIST_ID)
        ids = [t["id"] for t in tids[:30]]
        songs = c.song_detail(ids)
        src = "热歌榜"
    if not songs:
        raise RuntimeError("无候选歌曲")
    random.seed(seed_date)  # 同日可复现, 每日轮换
    pick = random.choice(songs[:min(len(songs), 30)])
    # 日推 v1 的 ar/al 可能为 null, 补全详情
    if not pick.get("ar"):
        det = c.song_detail([pick["id"]])
        if det:
            pick = det[0]
    print(f"  [选歌] 来源={src} → 《{pick['name']}》- "
          f"{'/'.join(a['name'] for a in pick.get('ar') or [])} (id={pick['id']})")
    return pick


# ---------- 主流程 ----------
def run(dry_run: bool = False) -> dict:
    cfg = load_config()
    state = load_state()
    today = today_bjt()
    last = state.get("last_post_date")
    if last == today:
        print(f"[防重] 今天({today})已发过: {state.get('last_post')}")
        return {"skipped": True, "reason": "already_posted"}

    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    if not st.get("ok"):
        raise RuntimeError(f"cookie 失效: {st}")
    print(f"[账号] {st['nickname']} (uid={st['uid']})")

    print("[1/3] 选歌")
    song = pick_song(c, today.replace("-", ""))

    print("[2/3] LLM 生成文案")
    copy_ = gen_copy(song)
    tag = cfg.get("topic_tag", "每日一歌")
    full_msg = f"{copy_}\n\n#{tag}#"
    print("  [文案]", full_msg.replace("\n", " / "))

    if dry_run:
        print("[dry-run] 未发布")
        return {"dry_run": True, "msg": full_msg, "song_id": song["id"]}

    print("[3/3] 发布分享动态")
    body = c.share_resource(song["id"], full_msg, rtype="song")
    time_str = datetime.now().strftime("%H:%M:%S")
    print(f"  [发布] code={body.get('code')} ({time_str})")

    # 回读验证
    ok, evidence = verify(c, song, copy_)
    rec = {"date": today, "song_id": song["id"], "song": song["name"],
           "msg": full_msg, "verified": ok, "time": time_str}
    state["last_post_date"] = today
    state["last_post"] = rec
    state.setdefault("history", []).append(rec)
    save_state(state)
    print(f"[验证] {'通过' if ok else '失败: ' + str(evidence)}")
    return {"ok": ok, "post": rec}


def verify(c: NeteaseClient, song: dict, copy_: str) -> tuple:
    """发布后回读本人动态, 确认歌曲卡片与文案落地"""
    try:
        events = c.user_events(pagesize=5)
        if not events:
            return False, "无动态返回"
        ev = events[0]
        ev_json = json.dumps(ev, ensure_ascii=False)
        key = copy_[:10]
        song_hit = str(song["id"]) in ev_json
        text_hit = key in ev_json
        if song_hit or text_hit:
            return True, f"动态已落地(歌曲命中={song_hit}, 文案命中={text_hit})"
        return False, f"最新动态不匹配: {ev.get('type')} {ev_json[:200]}"
    except Exception as e:
        return False, f"回读异常: {e}"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
