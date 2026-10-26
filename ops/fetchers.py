"""抓取层：列表页 -> 候选文章 -> 正文文本。只搬运，不解释。"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from urllib.parse import urljoin

from common import clean_text, html_to_text, http_get, iso_date, log, today_sh


def parse_bendibao_news_list(html: str, base_url: str) -> list[dict]:
    """'周末去哪玩' 这类资讯列表页：25 条/页，标题 + 摘要 + 发布时间。"""
    items = []
    for m in re.finditer(r'<li class="J-has-share listNews-item-s1[^"]*">(.*?)</li>', html, re.S):
        block = m.group(1)
        link = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        title = clean_text(link.group(2))
        if len(title) < 6:
            continue
        desc = re.search(r'class="desc"[^>]*>(.*?)</p>', block, re.S)
        items.append({
            "title": title,
            "summary": clean_text(desc.group(1)) if desc else "",
            "date_hint": "",
            "url": urljoin(base_url, link.group(1)),
        })
    return items


def parse_bendibao_list(html: str, base_url: str) -> list[dict]:
    """解析本地宝频道页的活动卡片：标题 + 摘要 + 日期区间 + 详情链接。"""
    items = []
    pattern = re.compile(
        r'main-header-left-title"[^>]*>(?P<title>.*?)</div>(?P<body>.*?)(?=main-header-left-title"|\Z)',
        re.S,
    )
    for m in pattern.finditer(html):
        title = clean_text(m.group("title"))
        body = m.group("body")[:4000]
        link = re.search(r'href="(https?://sh\.bendibao\.com/xiuxian/\d+/\d+\.shtm)"', body)
        if not title or not link:
            continue
        url = urljoin(base_url, link.group(1))
        texts = [clean_text(t) for t in re.findall(r'class="normal-text"[^>]*>(.*?)</div>', body, re.S)]
        summary = texts[0] if texts else ""
        date_hint = ""
        for t in texts:
            if re.search(r"\d{1,2}\s*[.\-/]\s*\d{1,2}", t) and ("-" in t or "—" in t or "至" in t or "~" in t):
                date_hint = t
                break
        items.append({"title": title, "summary": summary, "date_hint": date_hint, "url": url})
    # 同一链接只保留一次（频道页会重复推荐同一篇）
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def parse_date_hint(hint: str) -> tuple[str | None, str | None]:
    """'09.13 - 11.22' -> ('2026-09-13','2026-11-22')，跨年自动 +1。"""
    if not hint:
        return None, None
    m = re.search(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*(?:-|—|–|~|～|至)\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})", hint)
    if m:
        m1, d1, m2, d2 = (int(x) for x in m.groups())
        y = today_sh().year
        try:
            start = date(y, m1, d1)
            end = date(y, m2, d2)
        except ValueError:
            return None, None
        if end < start:  # 跨年（12.20 - 1.05）
            end = date(y + 1, m2, d2)
        elif start < today_sh() - timedelta(days=310):  # 明显是去年的区间
            start, end = date(y + 1, m1, d1), date(y + 1, m2, d2)
        return start.isoformat(), end.isoformat()
    single = re.search(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})", hint)
    if single:
        return iso_date(f"{single.group(1)}-{single.group(2)}"), None
    return None, None


def clean_article_text(text: str, title: str = "") -> str:
    """去掉导航/推荐位，只留正文，避免抽到别处的活动。"""
    t = text
    # 头部：从标题的第二次出现或【导语】开始
    for marker in ("【导语】", "【活动简介】"):
        i = t.find(marker)
        if i > 0:
            t = t[i + len(marker):]
            break
    else:
        if title:
            first = t.find(title)
            second = t.find(title, first + 1) if first >= 0 else -1
            if second > 0:
                t = t[second + len(title):]
            elif first >= 0:
                t = t[first + len(title):]
    # 尾部：推荐阅读/相关推荐/手机访问
    for marker in ("相关推荐", "推荐阅读", "相关文章", "手机访问", "上一篇文章", "编辑推荐"):
        i = t.find(marker)
        if i > 200:
            t = t[:i]
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()[:9000]


def fetch_candidates(source: dict) -> list[dict]:
    """抓列表页，返回候选文章（含日期提示）。"""
    mode = source.get("mode")
    html, final_url, status = http_get(source["list_url"])
    log(f"  列表页 {source['id']} HTTP {status} {len(html)} chars")
    if mode == "bendibao_list" or mode == "bendibao_api":
        items = parse_bendibao_list(html, final_url)
    elif mode == "bendibao_news":
        items = parse_bendibao_news_list(html, final_url)
    elif mode == "generic_list":
        items = parse_generic_list(html, final_url)
    else:
        raise ValueError(f"未知 mode: {mode}")
    today = today_sh()
    fresh = []
    for it in items:
        s, e = parse_date_hint(it.get("date_hint", ""))
        it["start_hint"], it["end_hint"] = s, e
        if e and date.fromisoformat(e) < today - timedelta(days=2):
            continue  # 卡片上就写明结束了，不必再抓详情
        fresh.append(it)
    log(f"  候选文章 {len(fresh)} 篇（列表共 {len(items)} 篇）")
    return fresh


def parse_generic_list(html: str, base_url: str) -> list[dict]:
    """通用兜底：抓页面上的链接 + 周围文字，交给 LLM 判断。"""
    items = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, label = m.group(1), clean_text(m.group(2))
        if len(label) < 6 or href.startswith("#") or "javascript" in href:
            continue
        items.append({"title": label[:80], "summary": "", "date_hint": "", "url": urljoin(base_url, href)})
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def fetch_article(url: str, timeout: int = 15, retries: int = 1) -> tuple[str, str]:
    """返回 (去噪后的正文, 页面标题)。"""
    html, final_url, status = http_get(url, timeout=timeout, retries=retries)
    text = html_to_text(html)
    page_title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        page_title = clean_text(m.group(1))
    return clean_article_text(text, page_title), page_title


# ---------- 本地宝结构化活动接口（优先于文章抽取：字段全、无需 LLM） ----------

_DATE_RE = re.compile(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")


def _parse_date_range(dates_format: str):
    found = _DATE_RE.findall(dates_format or "")
    dates = []
    for y, m, d in found:
        try:
            dates.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if not dates:
        return None, None
    if len(dates) == 1:
        return dates[0].isoformat(), dates[0].isoformat()
    return min(dates).isoformat(), max(dates).isoformat()


def _guess_category(type_name: str, channel: str) -> str:
    t = type_name or ""
    if any(k in t for k in ("市集", "集市", "夜市", "摆摊")):
        return "market"
    if any(k in t for k in ("快闪", "IP", "联名")):
        return "popup_store"
    if any(k in t for k in ("美食", "汉堡", "小吃", "咖啡", "餐饮")):
        return "food_stall"
    if "购物" in t or "促销" in t:
        return "shopping"
    if any(k in t for k in ("展", "艺术", "美术", "博物馆")):
        return "exhibition"
    if channel == "shiji":
        return "market"
    if channel == "zhanlan":
        return "exhibition"
    return "other"


def _s(v) -> str:
    """接口字段偶尔是 list/dict，统一转字符串。"""
    if isinstance(v, (list, tuple)):
        return " ".join(_s(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_s(x) for x in v.values())
    return "" if v is None else str(v)


def fetch_api_items(source: dict) -> list[dict]:
    """本地宝活动列表接口：一次 50 条，带日期区间、场馆名和地址。"""
    base = source["list_url"].rstrip("/") + "/"
    today = today_sh()
    out, seen_urls = [], set()
    for page in (1, 2, 3):
        url = f"{base}?search=&action=get_all_activity&page={page}&pagesize=50"
        text, _, status = http_get(url)
        if not text.lstrip().startswith("{"):
            log(f"    接口第 {page} 页不是 JSON（HTTP {status}），停止翻页")
            break
        data = json.loads(text)
        res = data.get("res") or []
        venues = data.get("venue_info") or {}
        if not res:
            break
        added = 0
        for it in res:
            pcurl = _s(it.get("pcurl"))
            if not pcurl or pcurl in seen_urls:
                continue
            seen_urls.add(pcurl)
            added += 1
            start, end = _parse_date_range(_s(it.get("dates_format")))
            if end and date.fromisoformat(end) < today:
                continue
            v = venues.get(_s(it.get("venue_id"))) or {}
            venue_name = clean_text(_s(v.get("name")))
            address = clean_text(_s(v.get("address")))
            type_name = clean_text(_s(it.get("type_name")))
            desc = clean_text(_s(it.get("heighlight")))[:110]
            out.append({
                "title": clean_text(_s(it.get("name"))),
                "category": _guess_category(type_name, source["id"].split("_")[-1]),
                "sub_tags": [x for x in [type_name] if x and x != "其他"][:2],
                "start_time": start,
                "end_time": end,
                "time_detail": None,
                "is_time_fuzzy": not (start and end),
                "location_name": venue_name or None,
                "address": address or None,
                "price_info": None,
                "description": desc or None,
                "confidence": 0.9 if (start and end and venue_name) else 0.75,
                "source_url": pcurl,
                "source_id": source["id"],
                "source_name": source["name"],
            })
        log(f"    接口第 {page} 页：{len(res)} 条，新增 {added}，保留 {len(out)}")
        if added == 0:
            break
    return out

