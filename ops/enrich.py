"""补全层：对结构化接口拿到的活动，回原文补价格/营业时间/地址。

只做"填空"，不重新判断活动是什么，避免一次文章抽出多条带来脏数据。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from common import clean_text, today_sh
from extract import _chat, _parse_json

PROMPT = """你是活动信息的补全器。给定活动标题和网页正文，只输出这个活动的补充字段。
输出 JSON 对象，字段如下（原文没有就给 null，绝不编造）：
{"price_info": "门票/价格信息，原文明确写免费就填'免费'",
 "time_detail": "开放时间或场次，如'10:00-22:00'、'周一闭馆'",
 "location_name": "场馆/商场/公园名",
 "address": "路名+门牌号",
 "description": "40-80 字，只写原文事实"}
今天日期：__TODAY__。如果正文和这个活动无关，全部返回 null。"""


def enrich_event(ev: dict, text: str) -> dict:
    title = ev["title"]
    messages = [
        {"role": "system", "content": PROMPT.replace("__TODAY__", today_sh().isoformat())},
        {"role": "user", "content": f"活动标题：{title}\n正文：\n{text[:6000]}"},
    ]
    content = _chat(messages, max_tokens=600)
    obj = _parse_json(content)
    if isinstance(obj, list):
        obj = obj[0] if obj and isinstance(obj[0], dict) else {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, limit in (("price_info", 40), ("time_detail", 40), ("location_name", 40), ("address", 60), ("description", 120)):
        v = clean_text(obj.get(k) or "")
        if not v or v.lower() in ("null", "none", "无", "未提及", "未知"):
            continue
        out[k] = v[:limit]
    return out


def needs_enrichment(ev: dict) -> bool:
    if ev.get("status") not in ("ongoing", "upcoming"):
        return False
    if not ev.get("price_info"):
        return True
    if not ev.get("time_detail"):
        return True
    return False


def apply(ev: dict, patch: dict) -> list[str]:
    changed = []
    for k, v in patch.items():
        if k == "description":
            if not ev.get("description") or len(v) > len(ev["description"]):
                ev["description"] = v
                changed.append(k)
            continue
        if not ev.get(k):
            ev[k] = v
            changed.append(k)
    return changed
