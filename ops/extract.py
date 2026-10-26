"""AI 抽取层：DeepSeek 把文章正文转成结构化活动记录，并做本地校验。

用 urllib 直连，不依赖 openai SDK；密钥读 DEEPSEEK_API_KEY / OPENAI_API_KEY。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date, timedelta

from common import CATEGORIES, CATEGORY_ALIASES, clean_text, iso_date, log, today_sh

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是上海临时活动聚合产品的结构化抽取引擎。输入是一篇网页正文，可能包含 0 到多个活动。
只抽取"值得专门跑一趟"的线下活动：展览、市集、快闪店、美食节/摆摊、商场主题活动。
不要抽：讲座、招聘、志愿服务、社区日常课程、体育赛事、演出/话剧/音乐会（本产品不覆盖票务演出）、纯新闻。

今天是 {today}。请输出 JSON 对象：{{"events": [ ... ]}}，不要输出任何其他文字。

每个活动字段：
- title: 活动名，去掉 emoji、序号、营销后缀（如"活动详情+打卡攻略"），保留专有名词
- category: exhibition | market | popup_store | food_stall | shopping | other
- sub_tags: 2-4 个细分标签（如"二次元""宠物友好""免费""亲子"），无依据就空数组
- start_time: YYYY-MM-DD；原文只写月日时，用能使其最接近今天的年份；完全找不到就 null
- end_time: 同上；只写"起"或长期开放就填 null 并把 is_time_fuzzy 设为 true
- time_detail: 原文的时间段（如"10:00-22:00"），没有就 null
- location_name: 场所名（商场/美术馆/公园名），不要带区名和地址
- address: 详细地址（路名+门牌），原文没有就 null，绝不编造
- price_info: 原文写的价格（"免费""早鸟38元起"），没提就 null
- description: 一句话，40-80 字，只写原文有的事实（规模、看点、特色）
- confidence: 0-1。起止时间+地点+标题都明确 >=0.85；缺一项 0.55-0.8；时间模糊 <0.5

硬规则：
1. 不编造。日期宁可 null。
2. 同一活动在文中反复出现只输出一次。
3. 已明确结束（结束日期早于今天）的活动不要输出。
4. 与上海无关的活动不要输出。
5. 输出必须是合法 JSON。"""


def _api_key() -> str:
    for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v
    raise RuntimeError("未找到 DEEPSEEK_API_KEY / OPENAI_API_KEY")


def _chat(messages: list[dict], max_tokens: int = 3500) -> str:
    base = (os.environ.get("EXTRACT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE).rstrip("/")
    model = os.environ.get("EXTRACT_MODEL") or DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _parse_json(content: str) -> list[dict]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("events", "data", "items", "result"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return []


def _norm_category(v) -> str:
    s = str(v or "").strip().lower()
    return CATEGORY_ALIASES.get(s, s if s in CATEGORIES else "other")


def normalize_event(raw: dict, source: dict, url: str) -> dict | None:
    """本地二次归一 + 校验，挡掉明显脏数据。"""
    title = clean_text(raw.get("title", ""))
    title = re.sub(r"^\s*\d+[、.．)]\s*", "", title)
    if len(title) < 4 or len(title) > 80:
        return None

    start = iso_date(raw.get("start_time"))
    end = iso_date(raw.get("end_time"))
    today = today_sh()
    if start and end and end < start:
        start, end = end, start
    # 明显超过 1.5 年的区间视为解析错误
    if start and end:
        try:
            if (date.fromisoformat(end) - date.fromisoformat(start)).days > 550:
                end = None
        except ValueError:
            start = end = None
    # 已结束超过 3 天的活动直接丢弃
    if end and date.fromisoformat(end) < today - timedelta(days=3):
        return None
    if start and not end and date.fromisoformat(start) < today - timedelta(days=45):
        return None

    location = clean_text(raw.get("location_name", ""))
    address = clean_text(raw.get("address", ""))
    if address and location and (address == location or location in address and len(location) > 6):
        address = "" if address == location else address
    if not location and address:
        location = address
        address = ""
    if location and len(location) > 40:
        location = location[:40]

    desc = clean_text(raw.get("description", ""))
    if len(desc) > 120:
        desc = desc[:118] + "…"

    try:
        conf = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    if start and end and location:
        conf = max(conf, 0.8)
    if not start and not end:
        conf = min(conf, 0.35)

    return {
        "title": title,
        "category": _norm_category(raw.get("category")),
        "sub_tags": [clean_text(t)[:12] for t in (raw.get("sub_tags") or []) if clean_text(t)][:5],
        "start_time": start,
        "end_time": end,
        "time_detail": clean_text(raw.get("time_detail", ""))[:40] or None,
        "is_time_fuzzy": not (start and end),
        "location_name": location or None,
        "address": address or None,
        "price_info": clean_text(raw.get("price_info", ""))[:40] or None,
        "description": desc or None,
        "confidence": round(min(max(conf, 0.1), 0.98), 2),
        "source_url": url,
        "source_id": source["id"],
        "source_name": source["name"],
    }


def extract_events(text: str, source: dict, url: str, page_title: str = "", date_hint: str = "") -> list[dict]:
    if len(text) < 80:
        return []
    user = (
        f"来源：{source['name']}\nURL：{url}\n页面标题：{page_title}\n"
        f"列表页日期提示：{date_hint or '无'}\n\n正文：\n{text}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=today_sh().isoformat())},
        {"role": "user", "content": user},
    ]
    content = ""
    for attempt in range(3):
        try:
            content = _chat(messages)
            break
        except Exception as e:  # noqa: BLE001
            log(f"    LLM 调用失败({attempt + 1}/3): {e}")
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raw_events = _parse_json(content)
    out = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        ev = normalize_event(raw, source, url)
        if ev:
            out.append(ev)
    return out
