#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一歌内容管线 v2: 日推选歌 → LLM 文案 → 卡片配图 → 发布(乐迷团+话题挂载) → 回读验证

发布模式(config.post_mode, 2026-10-25 用户需求"评论也要带上歌曲+乐迷团"):
  song_card: 歌曲卡片+自定义配图(可点击播放, 2026-10-25 起主模式)
        /eapi/share/friends/resource {type:song, id, pics, activityInfoList}
  note: 图文笔记(纯卡片图, 无歌曲) — 仅作 song_card 失败的降级
        /eapi/note/share/friends/resource + pics + activityInfoList
  song: 纯歌曲卡片(无自定义图, 自动带专辑封面) — 最终降级

话题挂载(activityInfoList, 2026-10-22 实测验证):
  - 歌手乐迷团: 已加入的乐迷团中匹配本曲歌手 → {id: boardId, type:3, subType:11}
  - 文本话题:   {id: topicId, type:2, subType:0} (显式ID挂载, 免文本解析)
关键格式: 正文与话题行之间单个\\n, 话题行末尾带空格("#tag# ")
"""
import json
import random
import re
import subprocess
import tempfile
import os
import time
import uuid as _uuid
from datetime import datetime

from config import load_config, load_state, save_state, today_bjt
from netease_client import NeteaseClient

HOT_TOPLIST_ID = 3779629  # 热歌榜

# 已验证存在的话题(act/detail 核实), 挂载用
KNOWN_TOPICS = [
    {"id": "151944318", "type": 2, "subType": 0, "name": "今日份听歌打卡"},
]

# 主乐迷团(ヨルシカ): 歌手不匹配已加入团时的兜底挂载
# 2026-10-25 用户要求"有的时候连乐迷团都没有带上"→ 每帖必带
PRIMARY_FAN_BOARD = {"id": "303859", "type": 3, "subType": 11,
                     "name": "ヨルシカ的乐迷团"}


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
    for q in ('"', "'", "“", "”", "「", "」"):
        if t.startswith(q) and t.endswith(q) and len(t) > 2:
            t = t[1:-1].strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    t = lines[0] if lines else t
    emoji_pat = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
        "\U0001F300-\U0001F5FF\u2b50\u2728\u2764\ufe0f]+", flags=re.UNICODE)
    t = emoji_pat.sub("", t).strip()
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
    return f"今日份的循环曲目, 分享给同样需要它的你。来自《{song['name']}》的温柔。"


# ---------- 选歌 ----------
def pick_song(c: NeteaseClient, seed_date: str, state: dict = None) -> dict:
    """选歌(2026-10-25 重构: 乐迷团歌手池优先)

    背景: 服务端校验歌曲歌手必须匹配乐迷团才挂载(不匹配静默丢弃,
    实测 10-25 伊藤サチコ挂ヨルシカ团被丢)。用户要求"每帖必带乐迷团"
    → 从 13 个已加入团的歌手热门歌里按日期轮换选歌。

    链路: fan_artists 池(state.fan_boards 的 headId) → 每个歌手热门50
    → 日期种子确定性轮换 → 失败降级每日推荐/热歌榜(无团但保发帖)
    """
    random.seed(seed_date)  # 同日可复现, 每日轮换
    state = state or {}
    board_map = state.get("fan_boards") or {}

    def _fit(aid: str) -> bool:
        """过滤不适合账号定位的团(ASMR 类音轨不是纯音乐)"""
        name = (board_map.get(aid) or {}).get("name") or ""
        return "ASMR" not in name.upper()

    def _track_fit(s: dict) -> bool:
        n = s.get("name") or ""
        return ("ASMR" not in n.upper() and len(n) <= 40)

    artist_ids = [a for a in board_map.keys() if _fit(a)]
    src = None
    songs = []
    if artist_ids:
        # 日期种子轮换歌手: 稳定打散, 同周不同日尽量不同歌手
        idx = int(seed_date.replace("-", "")) % len(artist_ids)
        tries = 0
        while tries < len(artist_ids) and not songs:
            aid = artist_ids[(idx + tries) % len(artist_ids)]
            try:
                hot = c.artist_hot_songs(int(aid), limit=50)
                songs = [s for s in hot
                         if s.get("ar") and _track_fit(s)]
                if songs:
                    src = f"乐迷团歌手池-{board_map[aid].get('name', aid)}"
            except Exception as e:
                print(f"  [选歌] 歌手{aid}热门失败({str(e)[:50]})")
            tries += 1
    if not songs:
        try:
            songs = c.daily_recommend_songs()
            src = "每日推荐(降级, 无乐迷团)"
        except Exception as e:
            print(f"  [选歌] 日推不可用({e}), 降级热歌榜")
            tids = c.toplist_tracks(HOT_TOPLIST_ID)
            ids = [t["id"] for t in tids[:30]]
            songs = c.song_detail(ids)
            src = "热歌榜(降级, 无乐迷团)"
    if not songs:
        raise RuntimeError("无候选歌曲")
    pick = random.choice(songs[:min(len(songs), 30)])
    if not pick.get("ar"):
        det = c.song_detail([pick["id"]])
        if det:
            pick = det[0]
    print(f"  [选歌] 来源={src} → 《{pick['name']}》- "
          f"{'/'.join(a['name'] for a in pick.get('ar') or [])} (id={pick['id']})")
    return pick


# ---------- 话题/乐迷团挂载 ----------
def my_fan_groups(c: NeteaseClient) -> list:
    """已加入的乐迷团列表, 失败重试1次"""
    for attempt in range(2):
        try:
            body = c._eapi_android("/api/social/fansgroup/bff/user/groups/get", {})
            return ((body.get("data") or {}).get("groups")) or []
        except Exception as e:
            if attempt:
                print(f"  [乐迷团] 获取失败: {e}")
    return []


def fan_board_map(c: NeteaseClient, state: dict) -> dict:
    """headId → {boardId, name} 映射, 三层容错:
    1) state.fan_boards 缓存(防 detail 接口抖动)
    2) groups+detail 实时拉取(首次/缓存缺失), 结果回写 state
    3) 实时拉取全败时返回旧缓存"""
    cached = state.get("fan_boards") or {}
    mapping = dict(cached)
    try:
        groups = my_fan_groups(c)
        changed = False
        for g in groups:
            head = g.get("headId")
            if not head or str(head) in mapping:
                continue
            try:
                d = c._eapi_android("/api/social/fansgroup/bff/detail/get",
                                    {"groupId": str(g.get("fansGroupId"))})
                board = ((d.get("data") or {}).get("fansGroupInfo") or {}) \
                    .get("boardId")
                if board:
                    mapping[str(head)] = {"boardId": str(board),
                                          "name": g.get("fansGroupName")}
                    changed = True
            except Exception:
                pass
        if changed or (groups and not cached):
            state["fan_boards"] = mapping
            save_state(state)
            print(f"  [乐迷团] 缓存 {len(mapping)} 个团的 boardId → state")
    except Exception:
        pass
    return mapping


def build_activity_info(c: NeteaseClient, song: dict, state: dict) -> list:
    """构造 activityInfoList: 歌手乐迷团(不匹配挂主团) + 已验证话题
    2026-10-25 修复: 之前歌手不匹配时乐迷团静默丢失 → 每帖必带"""
    acts = []
    artist_ids = {str(a.get("id")) for a in song.get("ar") or []}
    board_map = fan_board_map(c, state)
    matched = None
    for head_id, info in board_map.items():
        if head_id in artist_ids:
            matched = info
            break
    if matched:
        acts.append({"id": matched["boardId"], "type": 3, "subType": 11,
                     "name": matched["name"]})
        print(f"  [挂载] 乐迷团(歌手匹配): {matched['name']} "
              f"(boardId={matched['boardId']})")
    else:
        acts.append(dict(PRIMARY_FAN_BOARD))
        print(f"  [挂载] 乐迷团(主团兜底): {PRIMARY_FAN_BOARD['name']} "
              f"(boardId={PRIMARY_FAN_BOARD['id']})")
    for t in KNOWN_TOPICS:
        acts.append(dict(t))
    return acts


def build_tags(song: dict) -> list:
    """文本话题标签(渲染进正文, 存在的话题服务端也会挂载)"""
    tags = ["每日一歌"]
    artists = "/".join(a["name"] for a in song.get("ar") or [])
    if artists:
        tags.append(artists)
    tags.append("今日份听歌打卡")
    return tags


def build_msg(copy_: str, tags: list) -> str:
    """正文: 文案 + 单个\\n + 话题行(末尾空格) — 2026-10-22 实测验证的格式"""
    return copy_ + "\n" + " ".join(f"#{t}#" for t in tags) + " "


# ---------- 发布 ----------
def _post_note(c: NeteaseClient, msg: str, pics: list, acts: list) -> dict:
    """图文笔记模式(自定义配图)"""
    data = {
        "msg": msg, "type": "noresource", "uuid": _uuid.uuid4().hex,
        "addComment": "false", "privacySetting": "0",
        "socialSpaceVisible": 1,
    }
    if pics:
        data["pics"] = json.dumps(pics, ensure_ascii=False,
                                  separators=(",", ":"))
    if acts:
        data["activityInfoList"] = json.dumps(acts, ensure_ascii=False,
                                              separators=(",", ":"))
    body = c._eapi_android("/api/note/share/friends/resource", data)
    if body.get("code") != 200:
        raise RuntimeError(f"笔记发布失败: {body.get('code')} "
                           f"{body.get('message')}")
    return body


def _post_song_card(c: NeteaseClient, song_id, msg: str, acts: list,
                    pics: list) -> dict:
    """歌曲卡片+配图(2026-10-25 主模式): 可播放歌曲卡片 + 自定义卡片图
    一个接口同时带 type=song/id + pics + activityInfoList"""
    data = {"type": "song", "id": int(song_id), "msg": msg,
            "uuid": _uuid.uuid4().hex, "addComment": "false",
            "privacySetting": "0", "socialSpaceVisible": 1}
    if pics:
        data["pics"] = json.dumps(pics, ensure_ascii=False,
                                  separators=(",", ":"))
    if acts:
        data["activityInfoList"] = json.dumps(acts, ensure_ascii=False,
                                              separators=(",", ":"))
    body = c._eapi_android("/api/share/friends/resource", data)
    if body.get("code") != 200:
        raise RuntimeError(f"歌曲卡片发布失败: {body.get('code')} "
                           f"{body.get('message')}")
    return body


def _post_song(c: NeteaseClient, song_id, msg: str, acts: list) -> dict:
    """纯歌曲卡片模式(无自定义图, 自动专辑封面; 最终降级)"""
    data = {"type": "song", "id": int(song_id), "msg": msg,
            "uuid": _uuid.uuid4().hex, "addComment": "false"}
    if acts:
        data["activityInfoList"] = json.dumps(acts, ensure_ascii=False,
                                              separators=(",", ":"))
    body = c._eapi_android("/api/share/friends/resource", data)
    if body.get("code") != 200:
        raise RuntimeError(f"歌曲分享失败: {body.get('code')} "
                           f"{body.get('message')}")
    return body


# ---------- 主流程 ----------
def run(dry_run: bool = False, force: bool = False) -> dict:
    cfg = load_config()
    state = load_state()
    today = today_bjt()
    last = state.get("last_post_date")
    if last == today and not force:
        print(f"[防重] 今天({today})已发过: {state.get('last_post')}")
        return {"skipped": True, "reason": "already_posted"}

    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    if not st.get("ok"):
        raise RuntimeError(f"cookie 失效: {st}")
    print(f"[账号] {st['nickname']} (uid={st['uid']})")

    print("[1/6] 选歌")
    fan_board_map(c, state)   # 先确保乐迷团缓存就绪(选歌依赖歌手池)
    song = pick_song(c, today.replace("-", ""), state)

    print("[2/6] LLM 生成文案")
    copy_ = gen_copy(song)
    tags = build_tags(song)

    # ---- 合并发布(2026-10-28 用户指令"歌和歌单一起发"): 发帖前以当日选歌为种子建歌单 ----
    print("[3/6] 生成同款歌单(合并发布)")
    pl_info = None
    try:
        import playlist_maker
        seed = {"id": song["id"], "name": song["name"],
                "artist": "/".join(a.get("name", "") for a in song.get("ar") or []),
                "from": "今日每日一歌"}
        pl_info = playlist_maker.run(dry_run=dry_run, seed_song=seed)
    except Exception as e:
        print(f"  [歌单] 生成失败(不阻断发帖, 调度器gap-fill会补): {str(e)[:80]}")
    if pl_info and pl_info.get("ok"):
        pl_title = pl_info.get("title") or (pl_info.get("plan") or {}).get("title")
        n_tr = pl_info.get("tracks")
        if not (isinstance(n_tr, int) and n_tr > 0):
            n_tr = len((pl_info.get("plan") or {}).get("track_ids") or [])
        if pl_title:
            mention = f"同款歌单《{pl_title}》也整理好了"
            if n_tr:
                mention += f"，{n_tr}首连播"
            mention += "，主页可听。"
            BUDGET = 140  # 正文(文案+mention)字数预算, 超长截文案保歌单信息
            if len(copy_) + len(mention) > BUDGET:
                copy_ = copy_[:BUDGET - len(mention) - 1].rstrip("，。,.;；、") + "。"
            copy_ = copy_ + mention
            print(f"  [歌单] ✓ 《{pl_title}》({n_tr or '?'}首) 已并入正文")
    msg = build_msg(copy_, tags)
    print("  [文案]", msg.replace("\n", " / "))

    print("[4/6] 挂载信息(乐迷团+话题)")
    acts = build_activity_info(c, song, state)

    mode = cfg.get("post_mode", "song_card")
    if mode not in ("song_card", "note", "song"):
        mode = "song_card"
    pics = []
    if dry_run:
        print("[dry-run] 未发布")
        return {"dry_run": True, "msg": msg, "song_id": song["id"],
                "mode": mode, "acts": [a.get("name") for a in acts],
                "playlist": pl_info}

    # 配图(note 与 song_card 模式都要)
    if mode in ("song_card", "note"):
        print("[5/6] 渲染卡片配图并上传")
        try:
            from image_maker import render_card
            img_path = render_card(song, copy_, tags)
            with open(img_path, "rb") as f:
                img = f.read()
            pics = [c.upload_image(img, f"daily_song_{today}.jpg")]
        except Exception as e:
            print(f"  [配图] 失败({e})")
            if mode == "note":
                mode = "song"   # note 没图没意义
            else:
                mode = "song"   # song_card 没图退化为纯歌卡
    else:
        print("[5/6] 跳过配图(纯歌曲卡片模式)")

    print(f"[6/6] 发布(mode={mode})")
    ev_id = None
    if mode == "song_card":
        try:
            body = _post_song_card(c, song["id"], msg, acts, pics)
            ev_id = body.get("id") or (body.get("event") or {}).get("id")
            print(f"  [发布] 歌曲卡片+配图 code={body.get('code')} id={ev_id}")
        except Exception as e:
            print(f"  [发布] 歌曲卡片失败({e}), 降级图文笔记")
            mode = "note"
    if mode == "note" and not ev_id:
        try:
            body = _post_note(c, msg, pics, acts)
            ev_id = body.get("id") or (body.get("event") or {}).get("id")
            print(f"  [发布] 笔记模式 code={body.get('code')} id={ev_id}")
        except Exception as e:
            print(f"  [发布] 笔记模式失败({e}), 降级歌曲卡片")
            mode = "song"
    if mode == "song" or not ev_id:
        body = _post_song(c, song["id"], msg, acts)
        ev_id = body.get("id") or (body.get("event") or {}).get("id")
        print(f"  [发布] 歌曲模式 code={body.get('code')} id={ev_id}")
    if not ev_id:
        raise RuntimeError(f"发布响应缺id: {body}")
    time_str = datetime.now().strftime("%H:%M:%S")

    # 回读验证(带重试; 用发布返回的id核对, 避免误删教训)
    time.sleep(2)
    ok, evidence = verify(c, song, copy_, ev_id)
    rec = {"date": today, "song_id": song["id"], "song": song["name"],
           "artist": "/".join(a.get("name", "") for a in song.get("ar") or []),
           "msg": msg, "mode": mode, "event_id": ev_id,
           "verified": ok, "time": time_str,
           "acts": [a.get("name") for a in acts]}
    if pl_info and pl_info.get("ok") and pl_info.get("pid"):
        rec["playlist"] = {"pid": pl_info.get("pid"),
                           "title": pl_info.get("title")}
    state["last_post_date"] = today
    state["last_post"] = rec
    state.setdefault("history", []).append(rec)
    save_state(state)
    print(f"[验证] {'通过' if ok else '失败: ' + str(evidence)}")
    return {"ok": ok, "post": rec}


def verify(c: NeteaseClient, song: dict, copy_: str, ev_id) -> tuple:
    """发布后回读本人动态, 确认落地(以发布返回id为准核对)"""
    for attempt in range(3):
        try:
            events = c.user_events(pagesize=5)
            hit = next((e for e in events if e.get("id") == ev_id), None)
            if hit is None:
                return False, f"动态列表未见 id={ev_id}"
            ev_json = json.dumps(hit, ensure_ascii=False)
            song_hit = str(song["id"]) in ev_json
            text_hit = copy_[:10] in ev_json
            ext = hit.get("extJsonInfo") or {}
            if song_hit or text_hit:
                return True, (f"已落地(歌曲={song_hit}, 文案={text_hit}, "
                              f"actIds={ext.get('actIds')})")
            return False, f"内容不匹配: {ev_json[:150]}"
        except Exception as e:
            if attempt == 2:
                return False, f"回读异常: {e}"
            time.sleep(3)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="忽略防重强制重发(修复今日帖用)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)
