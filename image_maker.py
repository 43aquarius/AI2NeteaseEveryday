#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一歌卡片图生成器 (HTML + Playwright, 1080x1440 3:4)

设计: 网易云红 (#EC4141) 暗色氛围 + 黑胶唱片 + 专辑封面 + 文案
字体: 标题 Noto Sans SC / 文案 LXGW WenKai(文艺感)
"""
import base64
import html
import os
import re
import tempfile
from datetime import datetime


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def _fetch_pic_b64(url: str, timeout: int = 20) -> str:
    """下载专辑封面转 base64(p1/p2.music.126.net 公开CDN)"""
    import requests
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return base64.b64encode(r.content).decode()


def build_card_html(song: dict, copy_text: str, tags: list,
                    date_str: str = None) -> str:
    """生成卡片 HTML"""
    name = _esc(song.get("name", "未知歌曲"))
    artists = "/".join(a.get("name", "") for a in song.get("ar") or [])
    artists = _esc(artists or "未知歌手")
    album = _esc((song.get("al") or {}).get("name") or "")
    pic_url = (song.get("al") or {}).get("picUrl") or ""
    date_str = date_str or datetime.now().strftime("%m.%d")
    tag_html = "".join(
        f'<span class="tag">#{_esc(t)}#</span>' for t in tags if t)
    try:
        pic_b64 = _fetch_pic_b64(pic_url) if pic_url else ""
    except Exception:
        pic_b64 = ""
    cover = (f'<img class="cover" src="data:image/jpeg;base64,{pic_b64}" '
             f'alt="">' if pic_b64 else
             '<div class="cover cover-blank">♪</div>')
    # 文案按长度分档字号(保排版)
    L = len(copy_text or "")
    if L <= 60:
        copy_cls = "copy copy-xl"
    elif L <= 110:
        copy_cls = "copy copy-lg"
    else:
        copy_cls = "copy copy-md"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:1080px; height:1440px; overflow:hidden;
         font-family:'Noto Sans SC',sans-serif; }}
  .card {{ width:1080px; height:1440px; position:relative;
    background:
      radial-gradient(ellipse 900px 700px at 78% 12%, rgba(236,65,65,.32), transparent 62%),
      radial-gradient(ellipse 800px 600px at 12% 92%, rgba(120,20,25,.5), transparent 65%),
      linear-gradient(160deg, #241113 0%, #170c0e 46%, #1d1013 100%);
    color:#f5ece7; display:flex; flex-direction:column;
    align-items:center; padding:74px 84px 64px; }}
  .grain {{ position:absolute; inset:0; opacity:.05; pointer-events:none;
    background-image:repeating-linear-gradient(0deg, #fff 0 1px, transparent 1px 3px); }}
  .topbar {{ width:100%; display:flex; justify-content:space-between;
    align-items:center; font-size:26px; letter-spacing:6px;
    color:rgba(245,236,231,.75); }}
  .topbar .brand {{ color:#ff6b6b; font-weight:700; letter-spacing:4px; }}
  .hero {{ margin-top:58px; position:relative; width:560px; height:560px;
    display:flex; align-items:center; justify-content:center; }}
  .vinyl {{ position:absolute; right:-118px; width:470px; height:470px;
    border-radius:50%;
    background: radial-gradient(circle at 50% 50%,
      #3a2326 0%, #2a181b 28%, #120a0c 30%, #241316 31%,
      #150c0e 55%, #0d0709 100%);
    box-shadow: 0 22px 70px rgba(0,0,0,.75); }}
  .vinyl::after {{ content:""; position:absolute; left:50%; top:50%;
    width:92px; height:92px; transform:translate(-50%,-50%);
    border-radius:50%; background:#ec4141;
    box-shadow: inset 0 0 0 14px #170c0e; }}
  .vinyl::before {{ content:""; position:absolute; inset:16%; border-radius:50%;
    background:repeating-radial-gradient(circle at 50% 50%,
      transparent 0 5px, rgba(255,255,255,.045) 5px 6px); }}
  .cover {{ width:470px; height:470px; border-radius:14px; object-fit:cover;
    position:relative; z-index:2;
    box-shadow: 0 26px 80px rgba(0,0,0,.7), 0 6px 22px rgba(236,65,65,.28); }}
  .cover-blank {{ display:flex; align-items:center; justify-content:center;
    font-size:150px; color:#8a4a4e;
    background:linear-gradient(140deg,#3a2326,#241417); }}
  .meta {{ margin-top:64px; text-align:center; z-index:2; width:100%; }}
  .title {{ font-size:56px; font-weight:800; letter-spacing:2px;
    text-shadow:0 4px 26px rgba(236,65,65,.35); }}
  .artist {{ margin-top:18px; font-size:30px; color:rgba(245,236,231,.82);
    letter-spacing:3px; }}
  .album {{ margin-top:8px; font-size:23px; color:rgba(245,236,231,.5);
    letter-spacing:2px; }}
  .divider {{ margin:44px auto 0; width:110px; height:5px; border-radius:3px;
    background:linear-gradient(90deg,#ec4141,#ff8f70); }}
  .quote {{ margin-top:40px; }}
  .{copy_cls.split()[1]} {{ }}
  .copy {{ font-family:'LXGW WenKai','Noto Sans SC',serif; text-align:center;
    line-height:1.85; color:#f2e4dc; max-width:840px; white-space:pre-wrap; }}
  .copy-xl {{ font-size:42px; }}
  .copy-lg {{ font-size:37px; }}
  .copy-md {{ font-size:32px; }}
  .tags {{ margin-top:auto; padding-top:40px; display:flex; gap:16px;
    flex-wrap:wrap; justify-content:center; }}
  .tag {{ font-size:24px; color:#ff8f7d; background:rgba(236,65,65,.14);
    border:1px solid rgba(236,65,65,.4); padding:10px 22px;
    border-radius:999px; letter-spacing:2px; }}
  .foot {{ margin-top:26px; font-size:20px; letter-spacing:4px;
    color:rgba(245,236,231,.35); }}
</style></head>
<body><div class="card">
  <div class="grain"></div>
  <div class="topbar"><span class="brand">♪ 每日一歌</span><span>{date_str}</span></div>
  <div class="hero"><div class="vinyl"></div>{cover}</div>
  <div class="meta">
    <div class="title">{name}</div>
    <div class="artist">{artists}</div>
    {f'<div class="album">《{album}》</div>' if album else ''}
  </div>
  <div class="divider"></div>
  <div class="quote"><div class="{copy_cls}">{_esc(copy_text)}</div></div>
  <div class="tags">{tag_html}</div>
  <div class="foot">网易云音乐 · 每日一歌</div>
</div></body></html>"""


def render_card(song: dict, copy_text: str, tags: list,
                out_path: str = None, date_str: str = None) -> str:
    """渲染卡片图(1080x1440 JPEG), 返回路径"""
    from playwright.sync_api import sync_playwright
    html_str = build_card_html(song, copy_text, tags, date_str)
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(),
                                f"netease_card_{int(datetime.now().timestamp())}.jpg")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1440},
                                device_scale_factor=1)
        page.set_content(html_str, wait_until="networkidle")
        page.wait_for_timeout(400)
        page.screenshot(path=out_path, type="jpeg", quality=92,
                        clip={"x": 0, "y": 0, "width": 1080, "height": 1440})
        browser.close()
    return out_path


if __name__ == "__main__":
    # 本地测试: 用一首知名歌测试渲染
    song = {"name": "言って。",
            "ar": [{"name": "ヨルシカ"}],
            "al": {"name": "だから僕は音楽を辞めた",
                   "picUrl": "https://p1.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg"}}
    copy_ = ("夏夜的蝉鸣里，这首歌像一阵凉风拂过心间。"
             "那些说不出口的话，在钢琴声里找到了归宿。")
    tags = ["每日一歌", "ヨルシカ", "今日份听歌打卡"]
    out = render_card(song, copy_, tags,
                      out_path="/home/z/my-project/output_preview/netease_card_test.jpg")
    print("渲染完成:", out, os.path.getsize(out), "bytes")
