#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云QQ音乐 API 客户端 (纯 Python 实现 weapi 加密, 无 Node 依赖)

参考开源协议实现: NeteaseCloudMusicApi (Binaryify, MIT) 及其社区维护分支
  - weapi 加密: AES-128-CBC 双层 + RSA(无padding) 大整数模幂
  - 发帖端点: /weapi/e/event(文字动态), /weapi/e/event/share/resource(分享歌曲)
"""
import base64
import gzip
import hashlib
import hmac
import json
import random
import time
import os
import urllib.parse
from typing import Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

PRESET_KEY = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
EAPI_KEY = b"e82ckenh8dichen8"

# ---------- xeapi (2026-10 评论回复端点已升级 xeapi 加密) ----------
# 参考 NeteaseCloudMusicApiEnhanced/util/crypto.js + module/register_xeapikey.js
XEAPI_STATIC_KEY = bytes.fromhex(
    "ab1d5a430f6bb04a3f01e81ddd72bd916d5ce591248ac128714806d7f8fb1b84")
# 注意: Node createHmac(key为字符串) 用其 UTF-8 字节, 不做 base64 解码
XEAPI_SIGN_KEY = (b"mUHCwVNWJbunMqAHf5MImuirT6plvs6VSFW62MGHstFQxhBGdEoIh"
                  b"LItH3djc4+FB/OKty3+lL2rGeoFBpVe5g==")
XEAPI_UA = ("NeteaseMusic/9.5.61.260802021928(9005061);Dalvik/2.1.0 "
            "(Linux; U; Android 12; HBN-AL00 Build/cd737a2.0)")
X25519_SPKI_PREFIX = bytes.fromhex("302a300506032b656e032100")


def _aes_ecb(key: bytes, data: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(pad(data, 16, style="pkcs7"))


def _aes_ecb_dec(key: bytes, data: bytes) -> bytes:
    return unpad(AES.new(key, AES.MODE_ECB).decrypt(data), 16)


def _xeapi_sign(timestamp: str, nonce: str) -> str:
    mac = hmac.new(XEAPI_SIGN_KEY, f"{timestamp}{nonce}".encode(),
                   hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _xeapi_mid_transform(ciphertext: bytes) -> bytes:
    rnd = random.randbytes(16)
    xored = bytes(b ^ rnd[i & 0x0F] for i, b in enumerate(ciphertext))
    rot = (rnd[0] & 0x0F) % len(xored) if xored else 0
    return rnd + xored[rot:] + xored[:rot]


def _x25519_aes_key(shared: bytes, eph_pub_raw: bytes) -> bytes:
    prk = hmac.new(b"\x00" * 32, shared, hashlib.sha256).digest()
    return hmac.new(prk, eph_pub_raw + b"\x01", hashlib.sha256).digest()[:16]
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

    # ---------- 图片上传(动态配图) 三步链路 ----------
    # 2026-10-22 调研(GitHub: XiaoMengXinX/Music163Api-Go + chaunsin/netease-cloud-music 交叉验证):
    #   1) eapi /api/nos/token/alloc 拿 NOS 上传凭证(bucket/objectKey/token)
    #   2) PUT {nos_node}/{bucket}/{objectKey} 原始字节(非 multipart, x-nos-token 头)
    #      nos_node 来自 http://wanproxy.127.net/lbs?version=1.0&bucketname=cloudmusic
    #   3) eapi /api/upload/event/img/v1 用 docId 换多尺寸图片ID(originId/squareId/...)
    # 发帖时 pics 参数 = JSON字符串 [{"originId":"..","squareId":"..",...}](服务端按ID生成URL)

    def _lbs_nodes(self) -> list:
        r = self.s.get("http://wanproxy.127.net/lbs",
                       params={"version": "1.0", "bucketname": "cloudmenu"},
                       timeout=self.timeout)
        # 实测 bucketname 参数值不影响返回 cloudmusic 节点列表
        r.raise_for_status()
        body = r.json()
        return body.get("upload") or []

    def nos_token_alloc(self, filename: str, size: int, md5: str, ext: str = "jpg") -> dict:
        body = self._eapi_android("/api/nos/token/alloc", {
            "filename": filename, "local": "false", "nos_product": 0,
            "fileSize": size, "md5": md5, "ext": ext, "type": "image",
        })
        if body.get("code") != 200:
            raise RuntimeError(f"NOS凭证失败: {body}")
        return body.get("result") or {}

    def nos_upload(self, bucket: str, object_key: str, token: str, data: bytes,
                   content_type: str = "image/jpeg") -> None:
        nodes = self._lbs_nodes() or ["http://nosup-hz1.127.net"]
        last_err = None
        for node in nodes[:3]:
            url = (f"{node}/{bucket}/{object_key}"
                   f"?version=1.0&offset=0&complete=true")
            r = self.s.put(url, data=data, headers={
                "x-nos-token": token,
                "Content-Type": content_type,
            }, timeout=60)
            if r.status_code == 200:
                return
            last_err = f"{r.status_code} {r.text[:120]}"
        raise RuntimeError(f"NOS上传失败: {last_err}")

    def upload_event_img(self, doc_id: str, fmt: str = "jpeg") -> dict:
        """用 docId 换动态图片信息(多尺寸ID)"""
        body = self._eapi_android("/api/upload/event/img/v1",
                                  {"imgid": doc_id, "format": fmt})
        if body.get("code") != 200:
            raise RuntimeError(f"图片信息换取失败: {body}")
        return body

    def upload_image(self, data: bytes, filename: str = "daily_song.jpg") -> dict:
        """完整上传链路: 返回 pics 数组元素(dict)"""
        md5 = hashlib.md5(data).hexdigest()
        tk = self.nos_token_alloc(filename, len(data), md5)
        self.nos_upload(tk["bucket"], tk["objectKey"], tk["token"], data)
        info = self.upload_event_img(tk["docId"])
        pic = info.get("picInfo") or {}
        if not pic.get("originIdStr"):
            raise RuntimeError(f"上传链路异常: {info}")
        entry = {
            "originId": str(pic["originIdStr"]),
            "squareId": str(pic.get("squareIdStr") or pic["originIdStr"]),
            "rectangleId": str(pic.get("rectangleIdStr") or pic["originIdStr"]),
            "format": info.get("picSubtype") or "jpeg",
            "width": pic.get("width"),
            "height": pic.get("height"),
            "index": 0,
        }
        # 完整版字段(安卓客户端对齐, chaunsin 实现)
        for k in ("pcSquareId", "pcRectangleId", "originJpgId"):
            if pic.get(f"{k}Str"):
                entry[k] = str(pic[f"{k}Str"])
        print(f"  [上传] {filename} {len(data)}B → originId={entry['originId'][:18]}… "
              f"{pic.get('width')}x{pic.get('height')}")
        return entry

    # ---------- 分享(支持配图) ----------
    def share_resource(self, rid, msg: str, rtype: str = "song",
                       pics: list = None) -> dict:
        """分享资源到动态(歌曲卡片): rtype=song, rid=歌曲id, msg=文案
        pics: upload_image() 返回的元素列表(配图)"""
        data = {"type": rtype, "msg": msg, "id": int(rid)}
        if pics:
            data["pics"] = json.dumps(pics, ensure_ascii=False,
                                      separators=(",", ":"))
        body = self._eapi_android("/api/share/friends/resource", data)
        if body.get("code") != 200:
            raise RuntimeError(f"分享失败: {body.get('code')} {body.get('message')}")
        return body

    # ---------- xeapi (评论回复专用) ----------
    def fetch_xeapi_pubkey(self, force: bool = False) -> dict:
        """明文 /api/bsr/sk/get 获取 xeapi 公钥(HMAC签名验证), 结果缓存"""
        cached = getattr(self, "_xeapi_pubkey", None)
        if cached and not force:
            return cached
        nonce = "".join(str(random.randint(0, 9)) for _ in range(16))
        timestamp = str(int(time.time() * 1000))
        data = {
            "appVersion": "9.5.61",
            "currentKeyVersion": "",
            "deviceId": self.DEVICE_ID,
            "nonce": nonce, "os": "android", "requestType": "active",
            "signature": _xeapi_sign(timestamp, nonce),
            "t1": "", "t2": "", "timestamp": timestamp, "uid": "",
        }
        r = self.s.post("https://interface.music.163.com/api/bsr/sk/get",
                        data=data, headers={"User-Agent": XEAPI_UA},
                        timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 200:
            raise RuntimeError(f"xeapi公钥获取失败: {body}")
        d = body.get("data") or {}
        if not d.get("encryptedData"):
            raise RuntimeError(f"xeapi公钥响应异常: {body}")
        if d.get("signature") != _xeapi_sign(str(d.get("timestamp")), nonce):
            raise RuntimeError("xeapi公钥签名校验失败")
        pub = json.loads(_aes_ecb_dec(XEAPI_STATIC_KEY,
                                      base64.b64decode(d["encryptedData"])))
        if not pub.get("sk"):
            raise RuntimeError(f"xeapi公钥缺sk: {pub}")
        pub["deviceId"] = self.DEVICE_ID
        self._xeapi_pubkey = pub
        print(f"  [xeapi] 公钥获取成功 version={pub.get('version')} "
              f"sk={str(pub.get('sk'))[:8]}…")
        return pub

    def _xeapi(self, uri: str, data: dict) -> dict:
        """xeapi 加密请求(评论回复等新端点)
        uri: /api/xxx 形式; 返回解密后的 JSON"""
        pub = self.fetch_xeapi_pubkey()
        # 1) 明文: JSON {content: urlencode(body)}
        body_str = urllib.parse.urlencode(
            {k: ("" if v is None else str(v)) for k, v in data.items()})
        plaintext = json.dumps({"content": body_str},
                               separators=(",", ":")).encode()
        # 2) 双层AES + mid transform
        inner = _aes_ecb(XEAPI_STATIC_KEY, plaintext)
        mid = _xeapi_mid_transform(inner)
        dynamic_key = random.randbytes(16)
        c = _aes_ecb(dynamic_key, mid)
        # 3) S: X25519 ECDH + AES-GCM 包装动态密钥
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey)
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat)
        eph = X25519PrivateKey.generate()
        eph_raw = eph.public_key().public_bytes(Encoding.Raw,
                                                PublicFormat.Raw)
        peer_raw = base64.b64decode(pub["publicKey"])
        shared = eph.exchange(X25519PublicKey.from_public_bytes(peer_raw))
        aes_key = _x25519_aes_key(shared, eph_raw)
        iv = random.randbytes(12)
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv, mac_len=16)
        s_pt = (f"{base64.b64encode(dynamic_key).decode()}|android|"
                f"{pub.get('sk') or ''}").encode()
        ct, tag = cipher.encrypt_and_digest(s_pt)
        s = eph_raw + iv + ct + tag
        # 4) R: 版本声明
        r_field = _aes_ecb(XEAPI_STATIC_KEY,
                           f"{pub.get('version') or ''}|".encode())
        music_u = ""
        for part in self.cookie.split(";"):
            if part.strip().startswith("MUSIC_U="):
                music_u = part.strip().split("=", 1)[1]
        headers = {
            "User-Agent": XEAPI_UA,
            "X-Client-Enc-State": "ENCRYPTED",
            "x-aeapi": "true",
            "content-type": "application/x-www-form-urlencoded;charset=utf-8",
            "x-deviceid": self.DEVICE_ID,
            "x-os": "android", "x-osver": "16", "x-appver": "9.5.61",
            "x-sdeviceid": self.DEVICE_ID,
            "x-buildver": str(int(time.time())),
            "Cookie": (f"deviceId={self.DEVICE_ID}; os=android; osver=16; "
                       f"appver=9.5.61; MUSIC_U={music_u}"),
        }
        if music_u:
            headers["x-music-u"] = music_u
        form = {"C": base64.urlsafe_b64encode(c).decode(),
                "S": base64.b64encode(s).decode(),
                "R": base64.b64encode(r_field).decode()}
        r = self.s.post(f"https://interface3.music.163.com/xeapi/{uri[5:]}",
                        data=form, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        raw = r.content
        try:
            dec = _aes_ecb_dec(EAPI_KEY, raw)
        except Exception:
            return json.loads(raw)
        if dec[:2] == b"\x1f\x8b":
            dec = gzip.decompress(dec)
        return json.loads(dec)

    def reply_comment(self, thread_id: str, comment_id, content: str) -> dict:
        """回复评论(2026-10 实测: eapi/weapi 均404, 必须走 xeapi)"""
        body = self._xeapi("/api/v1/resource/comments/reply", {
            "threadId": thread_id,
            "commentId": str(comment_id),
            "content": content,
            "forwardEvent": "0",
        })
        if body.get("code") != 200:
            raise RuntimeError(f"回复失败: {body}")
        return body

    # ---------- 评论(读取/回复) ----------
    # threadId 格式: A_EV_2_{event_id}_{uid} (动态), 歌曲评论 R_SO_4_{songId}
    def read_comments(self, thread_id: str, limit: int = 20,
                      before_time: int = -1) -> dict:
        """读取资源评论(明文api即可, 2026-10-22 实测)"""
        body = self._get_api(
            f"https://music.163.com/api/v1/resource/comments/{thread_id}",
            {"rid": thread_id, "limit": limit, "offset": 0,
             "beforeTime": before_time})
        if body.get("code") != 200:
            raise RuntimeError(f"评论读取失败: {body.get('code')}")
        return body

    def add_comment(self, thread_id: str, content: str) -> dict:
        """直接评论资源(测试用): eapi + android"""
        body = self._eapi_android("/api/v1/resource/comments/add", {
            "threadId": thread_id,
            "content": content,
            "commentId": "0",
            "forwardEvent": "0",
            "resourceType": "0",
            "expressionPicId": "-1",
            "bubbleId": "-1",
        })
        if body.get("code") != 200:
            raise RuntimeError(f"评论失败: {body}")
        return body

    def del_event(self, ev_id) -> dict:
        """删除动态(测试帖清理用): /api/event/delete (weapi, 参数名是 id)"""
        body = self._post_weapi("https://music.163.com/weapi/event/delete",
                                {"id": int(ev_id),
                                 "csrf_token": self._csrf()})
        if body.get("code") != 200:
            raise RuntimeError(f"删动态失败: {body}")
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

    # ---------- 歌单(2026-10-23 新增: 自动化创建歌单) ----------
    def create_playlist(self, name: str, privacy: int = 0) -> int:
        """创建歌单, 返回歌单id
        privacy: 0=公开, 10=隐私(仅自己可见)"""
        body = self._post_weapi(
            "https://music.163.com/weapi/playlist/create",
            {"name": name, "privacy": privacy,
             "csrf_token": self._csrf()})
        if body.get("code") != 200 or not (body.get("id") or body.get("playlist_id")):
            raise RuntimeError(f"创建歌单失败: {body}")
        return int(body.get("id") or body.get("playlist_id"))

    def update_playlist_cover(self, pid: int, img_path: str) -> dict:
        """更新歌单封面: NOS上传 → cover/update
        参数名是 coverImgId(非imgId, imgSize/imgX/imgY是裁剪接口的,
        本端点不传) — 2026-10-23 探针验证"""
        import hashlib as _h
        with open(img_path, "rb") as f:
            data = f.read()
        fname = os.path.basename(img_path)
        md5 = _h.md5(data).hexdigest()
        tk = self.nos_token_alloc(fname, len(data), md5,
                                  ext="jpg" if fname.endswith(("jpg", "jpeg")) else "png")
        self.nos_upload(tk["bucket"], tk["objectKey"], tk["token"], data)
        body = self._post_weapi(
            "https://music.163.com/weapi/playlist/cover/update",
            {"id": int(pid), "coverImgId": tk["docId"],
             "csrf_token": self._csrf()})
        if body.get("code") != 200:
            raise RuntimeError(f"封面更新失败: {body}")
        return body

    def update_playlist_desc(self, pid: int, desc: str) -> dict:
        """更新歌单简介(参数名是desc非description, 2026-10-23 探针验证)"""
        body = self._post_weapi(
            "https://music.163.com/weapi/playlist/desc/update",
            {"id": int(pid), "desc": desc,
             "csrf_token": self._csrf()})
        if body.get("code") != 200:
            raise RuntimeError(f"简介更新失败: {body}")
        return body

    def update_playlist_tags(self, pid: int, tags: list) -> dict:
        """更新歌单标签(影响推荐曝光, 最多10个; eapi通道实测可用)"""
        body = self._eapi_android(
            "/api/playlist/tags/update",
            {"id": int(pid), "tags": ",".join(tags[:10])})
        if body.get("code") != 200:
            raise RuntimeError(f"标签更新失败: {body}")
        return body

    def add_playlist_tracks(self, pid: int, track_ids: list) -> dict:
        """歌单添加歌曲(eapi通道, weapi有-462风控, 2026-10-23 实测)"""
        if not track_ids:
            return {"code": 200, "empty": True}
        body = self._eapi_android(
            "/api/playlist/manipulate/tracks",
            {"op": "add", "pid": int(pid),
             "trackIds": json.dumps([int(t) for t in track_ids]),
             "imme": "true"})
        if body.get("code") != 200:
            # 502=歌曲已存在, 视为可接受
            if body.get("code") == 502:
                return {"code": 200, "dup": True}
            raise RuntimeError(f"添加歌曲失败: {body}")
        return body

    def delete_playlist(self, pid: int) -> dict:
        """删除歌单(eapi, 参数名pid, 2026-10-23 实测)"""
        body = self._eapi_android("/api/playlist/delete", {"pid": int(pid)})
        if body.get("code") != 200:
            raise RuntimeError(f"删除歌单失败: {body}")
        return body

    def user_playlists(self) -> list:
        """本人歌单列表(回读验证用)"""
        if not self.uid:
            self.login_status()
        body = self._get_api(
            f"https://music.163.com/api/user/playlist/",
            {"uid": self.uid, "limit": 100, "offset": 0,
             "includeVideo": "true"})
        return body.get("playlist") or []

    def search_songs(self, keyword: str, limit: int = 30) -> list:
        """搜索歌曲(明文api), 返回 songs 列表"""
        body = self._get_api(
            "https://music.163.com/api/search/get/",
            {"s": keyword, "type": 1, "limit": limit, "offset": 0})
        if body.get("code") != 200:
            raise RuntimeError(f"搜索失败: {body.get('code')}")
        return body.get("result", {}).get("songs") or []

    def simi_songs(self, song_id: int, limit: int = 20) -> list:
        """相似歌曲(基于单曲推荐, 明文api)"""
        body = self._get_api(
            "https://music.163.com/api/v1/discovery/simiSong",
            {"songid": int(song_id), "limit": limit, "offset": 0})
        if body.get("code") != 200:
            raise RuntimeError(f"相似歌曲失败: {body.get('code')}")
        return (body.get("songs") or [])[:limit]

    def artist_hot_songs(self, artist_id: int, limit: int = 50) -> list:
        """歌手热门歌曲(2026-10-25 新增: 乐迷团歌手选歌池)
        返回标准 song 结构(name/ar/al/id), 供 pick_song 消费"""
        body = self._post_weapi(
            f"https://music.163.com/weapi/v1/artist/top/song",
            {"id": int(artist_id), "limit": limit, "offset": 0,
             "total": True})
        if body.get("code") != 200:
            raise RuntimeError(f"歌手热门失败: {body.get('code')}")
        return body.get("songs") or []

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
