#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化创建歌单(2026-10-23 用户需求)

"歌单封面和标题你学习一下别人是怎么做的"
→ 调研了网易云热门纯音乐歌单 TOP50:
  标题公式: 「前缀标签」+ 分隔符丨 + 诗意主题句, 平均17字, 日系物哀审美
  样本: 【日系纯音向】夏树眠 蝉在叫 人坏掉❤️(2183万播)
        『纯音乐』当雨滴敲响琴键(1215万) 〖唯美纯音〗不需要歌词,也可以打动你的心(6269万)
  封面共性: 叙事性场景插画(日系动画风), 低饱和莫兰迪色, 大量留白,
        不压大标题, 治愈/孤独/静谧氛围
→ 本模块: 周歌单(本周每日一歌7首+相似歌扩充到20+首), LLM 起名+简介,
  AI生成封面+右下角统一小红印章(账号辨识度), 一键创建。

频率: 每周日 19:30(北京时间) 一期, 也可 main.py playlist 手动触发。
"""
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import load_config, load_state, save_state, now_bjt, BJT

PLAN_SYSTEM = """你是网易云音乐博主"纯音乐--请欣赏"的资深歌单策划。
你研究过站内高播放量纯音乐歌单的起名规律:
- 公式: 「前缀标签」+ 分隔符(丨或｜) + 诗意主题句
- 前缀样式参考: 【日系纯音向】『纯音乐』〖唯美纯音〗「轻音乐」物哀纯音
- 主题句: 意象堆叠的短句, 常两组三段式, 如"夏树眠 蝉在叫 人坏掉"
- 总长 11~25 字, 物哀/治愈/唯美审美, 精致不矫情
简介规律: 60~120字, 诗意场景描写开头 + 一句情绪落点, 口语化不说教。

你将拿到本周每天分享过的歌和一批候选扩充曲目, 任务:
1. 从本周歌单里提炼本周的音乐情绪主题
2. 按上述规律起一个歌单标题
3. 写简介(结尾自然带一句"每周日更新"的系列感, 不用emoji)
4. 从候选池里挑选 18~26 首与主题气质一致的曲目(必须含本周每日一歌的全部曲目)
5. 选 3~5 个官方标签(只能从给定列表选)
6. 用英文写一段封面插画的画面描述(1~2句): 一个有故事感的静谧瞬间场景,
   与主题呼应, 日系动画插画风, 低饱和莫兰迪色, 不要出现任何文字

严格输出 JSON(不要markdown代码块, 不要解释):
{"title": "...", "desc": "...", "tags": ["..."], "track_ids": [id,...],
 "cover_scene": "english prompt for illustration"}"""

OFFICIAL_TAGS = ["轻音乐", "纯音乐", "治愈", "放松", "安静", "学习", "钢琴",
                 "日系", "古风", "BGM", "助眠", "冥想", "小清新", "夜晚",
                 "清晨", "旅行", "咖啡", "阅读", "钢琴曲", "器乐"]


def _call_llm(prompt: str, system: str, timeout: int = 300) -> str:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        cmd = ["z-ai", "chat", "--prompt", prompt,
               "--system", system, "-o", out_path]
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


def _parse_llm_json(raw: str) -> Optional[dict]:
    t = raw.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# 素材收集
# ---------------------------------------------------------------------------

def collect_week_songs(state: dict) -> List[Dict]:
    """本周的每日一歌(从 state.history)"""
    today = now_bjt().date()
    monday = today - timedelta(days=today.weekday())
    out = []
    for rec in reversed(state.get("history") or []):
        try:
            d = datetime.strptime(rec.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d >= monday:
            out.append({"id": rec.get("song_id"), "name": rec.get("song", ""),
                        "artist": (rec.get("artist") or "").strip(),
                        "from": "本周每日一歌"})
    # 兼容老 history 无 artist 字段的, 不影响选歌
    return [x for x in out if x.get("id")]


def build_track_pool(c, week_songs: List[Dict], cap: int = 60) -> List[Dict]:
    """候选池: 本周歌 + 每首的相似歌 + 今日推荐, 去重截断"""
    pool, seen = [], set()

    def _push(sid, name, artist, src):
        sid = int(sid)
        if sid and sid not in seen and name:
            seen.add(sid)
            pool.append({"id": sid, "name": name,
                         "artist": (artist or "").strip(), "from": src})

    for s in week_songs:
        _push(s["id"], s["name"], s.get("artist"), "本周每日一歌")

    # 相似歌扩充
    for s in week_songs[:7]:
        try:
            for sim in (c.simi_songs(s["id"], limit=6) or []):
                artists = "/".join(a.get("name", "")
                                   for a in (sim.get("artists") or []))
                _push(sim.get("id"), sim.get("name", ""), artists, "相似推荐")
        except Exception:
            pass
        if len(pool) >= cap:
            break

    # 今日推荐补足
    if len(pool) < cap:
        try:
            for rec in (c.daily_recommend_songs() or [])[:30]:
                artists = "/".join(a.get("name", "")
                                   for a in (rec.get("artists") or []))
                _push(rec.get("id"), rec.get("name", ""), artists, "每日推荐")
        except Exception:
            pass
    return pool[:cap]


# ---------------------------------------------------------------------------
# 封面: AI插画 + 统一小印章
# ---------------------------------------------------------------------------

def _stamp_seal(img_path: str, out_path: str) -> None:
    """右下角盖"纯音乐"小红印章(系列辨识度)"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    side = int(w * 0.115)
    pad = int(w * 0.035)
    x0, y0 = w - side - pad, h - side - pad
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # 印章底(传统印泥红, 92%不透明, 圆角)
    d.rounded_rectangle([x0, y0, x0 + side, y0 + side],
                        radius=int(side * 0.14),
                        fill=(179, 53, 44, 235))
    # 竖排三字
    chars = "纯音乐"
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/noto-serif-sc/NotoSerifSC-Bold.ttf",
        int(side * 0.26))
    for i, ch in enumerate(chars):
        bb = d.textbbox((0, 0), ch, font=font)
        cw, chh = bb[2] - bb[0], bb[3] - bb[1]
        d.text((x0 + (side - cw) / 2 - bb[0],
                y0 + side * (0.14 + 0.24 * i) - chh / 2 - bb[1] * 0.3),
               ch, font=font, fill=(255, 251, 245, 255))
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    img.save(out_path, quality=90)


def render_cover(scene_prompt: str, out_path: str) -> Optional[str]:
    """AI生成歌单封面插画 + 盖章"""
    style = (", japanese anime illustration style, narrative quiet moment, "
             "low-saturation morandi color palette, cinematic soft lighting, "
             "atmospheric, high quality, no text, no watermark, no letters")
    raw_path = out_path + ".raw.jpg"
    try:
        subprocess.run(
            ["z-ai", "image", "-p", scene_prompt + style,
             "-o", raw_path, "-s", "1024x1024"],
            capture_output=True, timeout=600, check=True)
        _stamp_seal(raw_path, out_path)
        return out_path
    except Exception as e:
        print(f"[封面] 生成失败: {e}")
        return None
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_plan(week_songs: List[Dict], pool: List[Dict]) -> Optional[dict]:
    lines = ["本周每日一歌(必须全部收录):"]
    for s in week_songs:
        lines.append(f"- {s['name']} - {s.get('artist') or '未知'} (id={s['id']})")
    lines.append("\n候选扩充曲目池(从中挑选气质一致的):")
    for p in pool:
        lines.append(f"- {p['name']} - {p['artist'] or '未知'} "
                     f"(id={p['id']}, 来源={p['from']})")
    lines.append(f"\n官方标签白名单: {', '.join(OFFICIAL_TAGS)}")
    prompt = "\n".join(lines)

    for attempt in range(2):
        raw = _call_llm(prompt, PLAN_SYSTEM)
        plan = _parse_llm_json(raw)
        if plan and plan.get("title") and plan.get("track_ids"):
            # 校正: 确保本周歌都在
            must = {s["id"] for s in week_songs}
            ids = [int(t) for t in plan["track_ids"] if str(t).isdigit()]
            plan["track_ids"] = list(dict.fromkeys(ids + list(must)))[:30]
            plan["tags"] = [t for t in (plan.get("tags") or [])
                            if t in OFFICIAL_TAGS][:5] or ["纯音乐", "轻音乐"]
            return plan
        print(f"[策划] LLM输出解析失败(第{attempt + 1}次), 重试...")
    return None


def fallback_plan(week_songs: List[Dict], pool: List[Dict]) -> dict:
    """LLM不可用时的保底方案"""
    ids = [s["id"] for s in week_songs]
    for p in pool:
        if len(ids) >= 20:
            break
        if p["id"] not in ids:
            ids.append(p["id"])
    today = now_bjt()
    return {
        "title": f"『纯音乐』本周循环的诗意 {today.month}月第{today.isocalendar()[1] - (today.replace(day=1).isocalendar()[1]) + 1}辑",
        "desc": "本周每天分享过的一首歌, 和它们气质相近的旋律。\n安静的时候点开, 让这些纯音乐替你说出说不出的话。每周日更新。",
        "tags": ["纯音乐", "轻音乐", "治愈"],
        "track_ids": ids[:24],
        "cover_scene": ("a quiet lakeside at dusk with a small wooden boat "
                        "moored at an old pier, distant mountains in mist"),
    }


def run(dry_run: bool = False, cover_only: bool = False) -> dict:
    from netease_client import NeteaseClient

    print("===== 网易云周歌单生成 =====")
    cfg = load_config()
    state = load_state()
    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    if not st.get("ok"):
        raise RuntimeError(f"cookie 失效: {st}")
    print(f"[账号] {st.get('nickname')} (uid={st.get('uid')})")

    week_songs = collect_week_songs(state)
    if not week_songs:
        print("[跳过] 本周还没有每日一歌记录, 无法提炼主题")
        return {"ok": False, "reason": "no_week_songs"}
    print(f"[素材] 本周每日一歌 {len(week_songs)} 首")

    print("[1/4] 扩充候选池(相似歌+每日推荐)...")
    pool = build_track_pool(c, week_songs)
    print(f"   候选池 {len(pool)} 首")

    print("[2/4] LLM 策划(标题/简介/选曲/封面场景)...")
    plan = generate_plan(week_songs, pool) or fallback_plan(week_songs, pool)
    print(f"   标题: {plan['title']}")
    print(f"   选曲: {len(plan['track_ids'])} 首 | 标签: {plan['tags']}")
    print(f"   封面场景: {plan['cover_scene'][:70]}...")

    if dry_run:
        print("[DRY-RUN] 到此为止, 未创建歌单")
        return {"ok": True, "dry_run": True, "plan": plan}

    print("[3/4] 生成封面(AI插画+盖章)...")
    os.makedirs("output", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cover_path = os.path.join("output", f"playlist_cover_{ts}.jpg")
    cover_ok = render_cover(plan["cover_scene"], cover_path)
    print(f"   封面: {'✓ ' + cover_path if cover_ok else '失败, 将用系统默认封面'}")

    print("[4/4] 创建歌单并填充...")
    pid = c.create_playlist(plan["title"], privacy=0)
    print(f"   [创建] pid={pid}")
    time.sleep(4)
    r = c.add_playlist_tracks(pid, plan["track_ids"])
    print(f"   [加歌] code={r.get('code')}")
    time.sleep(4)
    try:
        c.update_playlist_desc(pid, plan["desc"])
        print("   [简介] ✓")
    except Exception as e:
        print(f"   [简介] ✗ {str(e)[:80]}")
    time.sleep(4)
    try:
        c.update_playlist_tags(pid, plan["tags"])
        print("   [标签] ✓")
    except Exception as e:
        print(f"   [标签] ✗ {str(e)[:80]}")
    if cover_ok:
        time.sleep(4)
        try:
            c.update_playlist_cover(pid, cover_path)
            print("   [封面] ✓")
        except Exception as e:
            print(f"   [封面] ✗ {str(e)[:80]}")

    # 回读验证
    time.sleep(4)
    pls = c.user_playlists()
    mine = next((p for p in pls if p.get("id") == pid), None)
    n_tracks = mine.get("trackCount") if mine else "?"
    print(f"[验证] {'✓' if mine else '✗'} 曲目数={n_tracks}")

    state = load_state()
    state.setdefault("playlists", []).append({
        "date": now_bjt().strftime("%Y-%m-%d"),
        "pid": pid, "title": plan["title"],
        "tracks": plan["track_ids"], "verified": bool(mine),
    })
    state["last_playlist_week"] = now_bjt().strftime("%G-W%V")
    save_state(state)
    print(f"[完成] 歌单 id={pid} · https://music.163.com/#/playlist?id={pid}")
    return {"ok": True, "pid": pid, "title": plan["title"],
            "tracks": n_tracks}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cover-only", action="store_true",
                    help="只渲染封面看效果")
    a = ap.parse_args()
    run(dry_run=a.dry_run, cover_only=a.cover_only)
