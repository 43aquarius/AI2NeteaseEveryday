#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云QQ音乐 API 客户端 (纯 Python 实现 weapi 加密, 无 Node 依赖)

参考开源协议实现: NeteaseCloudMusicApi (Binaryify, MIT) 及其社区维护分支
  - weapi 加密: AES-128-CBC 双层 + RSA(无padding) 大整数模幂
  - 发帖端点: /weapi/e/event(文字动态), /weapi/e/event/share/resource(分享歌曲)
"""
import base64
import hashlib
import json
import random
import time
from typing import Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

PRESET_KEY = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
EAPI_KEY = b"e82ckenh8dichen8"
PUBKEY_E = 0x10001
PUBKEY_N = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b7251"
    "52b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecb"
    "da92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813c"
    "fe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _aes_cbc_b64(text: str, key: bytes) -> str:
    cipher = AES.new(key, AES.MODE_CBC, IV)
    data = pad(text.encode("utf-8"), 16, style="pkcs7")
    return base64.b64encode(cipher.encrypt(data)).decode()


def weapi(data: dict) -> dict:
    """网易云 weapi 加密, 返回表单字段 params / encSecKey"""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    secret_key = "".join(random.choice(BASE62) for _ in range(16)).encode()
    params = _aes_cbc_b64(_aes_cbc_b64(text, PRESET_KEY), secret_key)
    m = int.from_bytes(secret_key[::-1], "big")
    enc = pow(m, PUBKEY_E, PUBKEY_N)
    return {"params": params, "encSecKey": format(enc, "x").zfill(256)}


def _reqid() -> str:
    import uuid
    return str(uuid.uuid4()).replace("-", "")


def eapi(uri: str, data: dict) -> dict:
    """网易云 eapi 加密: /api/xxx 路径 + AES-ECB(e82ckenh8dichen8) + MD5 摘要"""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    message = f"nobody{uri}use{text}md5forencrypt"
    digest = hashlib.md5(message.encode("utf-8")).hexdigest()
    payload = f"{uri}-36cd479b6b5-{text}-36cd479b6b5-{digest}"
    cipher = AES.new(EAPI_KEY, AES.MODE_ECB)
    enc = cipher.encrypt(pad(payload.encode("utf-8"), 16, style="pkcs7"))
    return {"params": enc.hex().upper()}


class NeteaseClient:
    def __init__(self, cookie: str, timeout: int = 20):
        # 去重 cookie(保留后值), 统一分号分隔
        jar = {}
        for part in cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            jar[k.strip()] = v.strip()
        jar.setdefault("os", "pc")
        self.cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        self.uid: Optional[int] = None
        self.nickname: Optional[str] = None
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://music.163.com",
            "Accept": "*/*",
        })

    # ---------- 基础请求 ----------
    def _post_weapi(self, url: str, data: dict) -> dict:
        form = weapi(data)
        r = self.s.post(url, data=form, headers={
            "Cookie": self.cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        }, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _get_api(self, url: str, params: dict = None) -> dict:
        r = self.s.get(url, params=params or {}, headers={
            "Cookie": self.cookie,
        }, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---------- 账号 ----------
    def login_status(self) -> dict:
        """登录状态: 返回 {ok, uid, nickname}"""
        body = self._get_api("https://music.163.com/api/nuser/account/get")
        profile = (body or {}).get("profile") or {}
        account = (body or {}).get("account") or {}
        uid = profile.get("userId") or account.get("id")
        if not uid:
            return {"ok": False, "raw": body}
        self.uid = uid
        self.nickname = profile.get("nickname")
        return {"ok": True, "uid": uid, "nickname": self.nickname}

    # ---------- 数据端点 ----------
    def daily_recommend_songs(self) -> list:
        """每日推荐歌曲(需登录), 返回 dailySongs 列表"""
        body = self._post_weapi(
            "https://music.163.com/weapi/v1/discovery/recommend/songs", {})
        if body.get("code") != 200:
            raise RuntimeError(f"每日推荐失败: {body.get('code')} {body.get('message') or body.get('msg')}")
        return body.get("data", {}).get("dailySongs", [])

    def toplist_tracks(self, idx: int = 3779629) -> list:
        """热歌榜(idx=3779629)曲目, 每日推荐不可用时降级"""
        body = self._post_weapi(
            "https://music.163.com/weapi/v6/playlist/detail",
            {"id": idx, "n": 30, "s": 0})
        if body.get("code") != 200:
            raise RuntimeError(f"榜单获取失败: {body.get('code')}")
        return (body.get("playlist") or {}).get("trackIds", [])

    def song_detail(self, ids: list) -> list:
        """歌曲详情: 名称/歌手/专辑封面"""
        c = json.dumps([{"id": int(i)} for i in ids], separators=(",", ":"))
        body = self._post_weapi("https://music.163.com/weapi/v3/song/detail",
                                {"c": c, "ids": json.dumps([int(i) for i in ids])})
        if body.get("code") != 200:
            raise RuntimeError(f"歌曲详情失败: {body.get('code')}")
        return body.get("songs", [])

    # ---------- 发帖端点 ----------
    def _csrf(self) -> str:
        for part in self.cookie.split(";"):
            part = part.strip()
            if part.startswith("__csrf="):
                return part.split("=", 1)[1]
        return ""

    # 分享接口要求 android 客户端协议: os=android + appver=9.1.65 + android UA
    # (2026-10-22 实测: eapi+android 伪装直接 200, 无需 xeapi/x25519)
    ANDROID_UA = ("NeteaseMusic/9.1.65.240916161425(9001165);Dalvik/2.1.0 "
                  "(Linux; U; Android 16;)")
    DEVICE_ID = "be2b8e2c8c4a4d8f9e1a2b3c4d5e6f70"

    def _eapi_android(self, uri: str, data: dict) -> dict:
        """eapi + android 伪装 POST (发帖类接口的可靠路径)"""
        data = dict(data)
        data["header"] = {
            "os": "android", "appver": "9.1.65", "channel": "10005124",
            "requestId": _reqid(),
        }
        form = eapi(uri, data)
        cookie = self.cookie.replace("os=pc", "os=android")
        cookie += ("; appver=9.1.65; osver=16; buildver="
                   f"{int(time.time())}; deviceId={self.DEVICE_ID}")
        r = self.s.post(f"https://music.163.com/eapi/{uri[5:]}",
                        data=form, headers={
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self.ANDROID_UA,
        }, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def share_resource(self, rid, msg: str, rtype: str = "song") -> dict:
        """分享资源到动态(歌曲卡片): rtype=song, rid=歌曲id, msg=文案"""
        body = self._eapi_android("/api/share/friends/resource",
                                  {"type": rtype, "msg": msg, "id": int(rid)})
        if body.get("code") != 200:
            raise RuntimeError(f"分享失败: {body.get('code')} {body.get('message')}")
        return body

    def send_text(self, msg: str) -> dict:
        """发布纯文字动态"""
        body = self._eapi_android("/api/e/event",
                                  {"msgs": json.dumps([{"msg": msg}],
                                                      ensure_ascii=False),
                                   "type": "text"})
        if body.get("code") != 200:
            raise RuntimeError(f"发文字动态失败: {body}")
        return body

    def del_event(self, ev_id) -> dict:
        """删除动态(测试帖清理用): /api/event/delete (weapi, 参数名是 id)"""
        body = self._post_weapi("https://music.163.com/weapi/event/delete",
                                {"id": int(ev_id),
                                 "csrf_token": self._csrf()})
        if body.get("code") != 200:
            raise RuntimeError(f"删动态失败: {body}")
        return body

    # ---------- 回读验证 ----------
    def user_events(self, pagesize: int = 5) -> list:
        """本人最近动态(原声 api, 用于发布后回读验证)"""
        if not self.uid:
            self.login_status()
        body = self._get_api(
            f"https://music.163.com/api/event/get/{self.uid}",
            {"pagesize": pagesize, "lasttime": -1})
        if body.get("code") != 200:
            raise RuntimeError(f"动态回读失败: {body.get('code')}")
        return (body.get("events") or [])


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from config import load_config  # noqa
    cfg = load_config()
    c = NeteaseClient(cfg["cookie"])
    st = c.login_status()
    print("[登录态]", st)
