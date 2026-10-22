#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置加载 + state 防重管理"""
import json
import os
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")

BJT = timezone(timedelta(hours=8))


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


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
