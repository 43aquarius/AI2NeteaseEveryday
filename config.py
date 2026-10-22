#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载 + state 防重管理 + 凭据三层持久化(2026-10-22 重启生存设计)

沙箱重启机制(实测结论):
  - 平台周期性把 my-project 快照到本地 git 仓库; 重启时回滚到最近快照点
  - 快照点之后新建/修改的文件(含被 .gitignore 排除的 config.json)会被清除
  - 被 .gitignore 排除的文件永远不会进快照 → 纯 config.json 必丢

因此凭据做三层存储, load_config 自动逐层回退并自愈:
  1. {项目}/config.json                     运行位(重启可能丢)
  2. /home/z/.secrets/netease_config.json   项目外副本(不属快照管辖, 可能存活)
  3. {项目}/secrets_backup.json             base64(JSON) 快照追踪位(最可靠)
     ⚠ secrets_backup.json 严禁推送 GitHub(推送脚本一律用显式文件清单)
"""
import base64
import json
import os
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
SECRETS_DIR = "/home/z/.secrets"
SECRETS_PATH = os.path.join(SECRETS_DIR, "netease_config.json")
BACKUP_PATH = os.path.join(BASE, "secrets_backup.json")

BJT = timezone(timedelta(hours=8))


def _wrap(obj: dict) -> dict:
    return {"_warning": "本地凭据备份(base64), 严禁推送GitHub或外传",
            "payload_b64": base64.b64encode(
                json.dumps(obj, ensure_ascii=False).encode()).decode()}


def _unwrap(data: dict) -> dict:
    raw = base64.b64decode(data.get("payload_b64", ""))
    return json.loads(raw.decode())


def write_config(cfg: dict):
    """凭据写入三层(运行位 + 项目外 + 快照追踪位)"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.makedirs(SECRETS_DIR, exist_ok=True)
        with open(SECRETS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    with open(BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(_wrap(cfg), f, ensure_ascii=False, indent=1)


def load_config() -> dict:
    """三层回退加载; 上游缺失时自动回写自愈(含运行位重建)"""
    # 1) 运行位
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    # 2) 项目外副本
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            write_config(cfg)
            return cfg
        except (OSError, ValueError):
            pass
    # 3) 快照追踪位(base64)
    if os.path.exists(BACKUP_PATH):
        try:
            with open(BACKUP_PATH, encoding="utf-8") as f:
                cfg = _unwrap(json.load(f))
            if cfg.get("cookie"):
                write_config(cfg)
                return cfg
        except Exception:
            pass
    raise FileNotFoundError(
        f"三层凭据全部缺失: {CONFIG_PATH} / {SECRETS_PATH} / {BACKUP_PATH} — "
        f"需要用户提供 MUSIC_U cookie, 运行 scripts/rebuild_netease_config.py")


def config_exists() -> bool:
    return any(os.path.exists(p) for p in
               (CONFIG_PATH, SECRETS_PATH, BACKUP_PATH))


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"history": []}


def save_state(st: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def today_bjt() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


def now_bjt() -> datetime:
    return datetime.now(BJT)
