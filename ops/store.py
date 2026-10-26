"""存储层：活动主库（events.json）+ 去重合并 + 时效状态 + 归档。"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta

from common import (
    ARCHIVE_FILE, CATEGORIES, EVENTS_FILE, now_sh, norm_title, norm_venue,
    read_json, sha1, today_sh, write_json,
)

SCHEMA = 2


def load_store() -> dict:
    store = read_json(EVENTS_FILE)
    if not store:
        return {"schema": SCHEMA, "updated_at": None, "events": []}
    if isinstance(store, list):  # 从旧格式迁移
        return {"schema": SCHEMA, "updated_at": None, "events": store}
    store.setdefault("events", [])
    return store


def save_store(store: dict) -> None:
    store["schema"] = SCHEMA
    store["updated_at"] = now_sh().isoformat(timespec="seconds")
    write_json(EVENTS_FILE, store)


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _dates_close(a: dict, b: dict) -> bool:
    sa, sb = a.get("start_time"), b.get("start_time")
    if not sa or not sb:
        return True
    try:
        gap = abs((date.fromisoformat(sa) - date.fromisoformat(sb)).days)
    except ValueError:
        return True
    return gap <= 120


def _overlaps(a: dict, b: dict) -> bool:
    def rng(e):
        s = e.get("start_time") or e.get("end_time")
        t = e.get("end_time") or e.get("start_time")
        if not s or not t:
            return None
        try:
            return date.fromisoformat(s), date.fromisoformat(t)
        except ValueError:
            return None
    ra, rb = rng(a), rng(b)
    if not ra or not rb:
        return True
    return ra[0] <= rb[1] + timedelta(days=3) and rb[0] <= ra[1] + timedelta(days=3)


def _source_urls(e: dict) -> set:
    urls = {s.get("url") for s in e.get("sources", []) if s.get("url")}
    if e.get("source_url"):
        urls.add(e["source_url"])
    return urls


def is_same_event(a: dict, b: dict) -> bool:
    # 同一条来源链接 = 同一个活动，最可靠的判据
    if _source_urls(a) & _source_urls(b):
        return True
    if not _dates_close(a, b):
        return False
    ta, tb = norm_title(a.get("title", "")), norm_title(b.get("title", ""))
    if not ta or not tb:
        return False
    t_sim = _sim(ta, tb)
    va, vb = norm_venue(a.get("location_name") or ""), norm_venue(b.get("location_name") or "")
    if va and vb and (va in vb or vb in va) and min(len(va), len(vb)) >= 4:
        v_sim = max(_sim(va, vb), 0.9)
    else:
        v_sim = _sim(va, vb) if va and vb else 1.0
    if t_sim >= 0.92:
        return v_sim >= 0.4 or not va or not vb
    if (ta in tb or tb in ta) and min(len(ta), len(tb)) >= 6 and v_sim >= 0.5 and _overlaps(a, b):
        return True
    if t_sim >= 0.66 and v_sim >= 0.72 and _overlaps(a, b):
        return True
    if va and vb and v_sim >= 0.85 and (ta in tb or tb in ta) and _overlaps(a, b):
        return True
    return False


def make_event(ev: dict) -> dict:
    today = today_sh().isoformat()
    src = {
        "url": ev.get("source_url"),
        "source_id": ev.get("source_id"),
        "source_name": ev.get("source_name"),
        "first_seen": today,
        "last_verified": today,
    }
    return {
        "id": sha1(f"{norm_title(ev['title'])}|{norm_venue(ev.get('location_name') or '')}|{ev.get('start_time') or 'na'}")[:12],
        "title": ev["title"],
        "category": ev.get("category", "other"),
        "sub_tags": ev.get("sub_tags", []),
        "start_time": ev.get("start_time"),
        "end_time": ev.get("end_time"),
        "time_detail": ev.get("time_detail"),
        "is_time_fuzzy": ev.get("is_time_fuzzy", True),
        "location_name": ev.get("location_name"),
        "address": ev.get("address"),
        "price_info": ev.get("price_info"),
        "description": ev.get("description"),
        "confidence": ev.get("confidence", 0.5),
        "status": "unknown",
        "sources": [src] if src["url"] else [],
        "first_seen": today,
        "last_verified": today,
        "manual": False,
        "hidden": False,
        "notes": None,
    }


FIELD_PRIORITY = ("title", "category", "sub_tags", "start_time", "end_time", "time_detail",
                  "location_name", "address", "price_info", "description")


def _union_dates(old: dict, new: dict) -> tuple:
    """同一活动的多次抓取：区间取并集，避免把长展期缩成某个子活动的一天。"""
    os_, oe = old.get("start_time"), old.get("end_time")
    ns, ne = new.get("start_time"), new.get("end_time")
    try:
        cands_s = [date.fromisoformat(x) for x in (os_, ns) if x]
        cands_e = [date.fromisoformat(x) for x in (oe, ne) if x]
    except ValueError:
        return os_, oe
    if not cands_s and not cands_e:
        return os_, oe
    s = min(cands_s).isoformat() if cands_s else None
    e = max(cands_e).isoformat() if cands_e else None
    return s, e


def merge_into(old: dict, new: dict) -> None:
    """把新抽到的记录合进旧记录：补空字段；置信度更高时覆盖时间/地点。"""
    conf_up = new.get("confidence", 0) >= old.get("confidence", 0) - 0.05
    for f in FIELD_PRIORITY:
        nv = new.get(f)
        ov = old.get(f)
        if not nv:
            continue
        if f in ("start_time", "end_time"):
            continue  # 时间字段统一在下面做并集
        if not ov:
            old[f] = nv
        elif f in ("time_detail", "location_name", "address", "price_info") and conf_up:
            old[f] = nv
        elif f == "sub_tags":
            old[f] = list(dict.fromkeys([*(ov or []), *(nv or [])]))[:6]
        elif f == "description" and len(nv) > len(ov or ""):
            old[f] = nv
    s, e = _union_dates(old, new)
    old["start_time"], old["end_time"] = s, e
    old["confidence"] = round(max(old.get("confidence", 0), new.get("confidence", 0)), 2)
    old["is_time_fuzzy"] = not (old.get("start_time") and old.get("end_time"))
    if new.get("source_url"):
        refs = {s["url"]: s for s in old.get("sources", []) if s.get("url")}
        s = refs.get(new["source_url"])
        if s:
            s["last_verified"] = today_sh().isoformat()
        else:
            old.setdefault("sources", []).append({
                "url": new["source_url"],
                "source_id": new.get("source_id"),
                "source_name": new.get("source_name"),
                "first_seen": today_sh().isoformat(),
                "last_verified": today_sh().isoformat(),
            })
    old["last_verified"] = today_sh().isoformat()


def upsert(store: dict, ev: dict) -> str:
    for old in store["events"]:
        if is_same_event(old, ev):
            merge_into(old, ev)
            return "merged"
    store["events"].append(make_event(ev))
    return "new"


def status_of(e: dict, today: date | None = None) -> str:
    today = today or today_sh()
    s = e.get("start_time")
    t = e.get("end_time")
    try:
        sd = date.fromisoformat(s) if s else None
        ed = date.fromisoformat(t) if t else None
    except ValueError:
        sd = ed = None
    if sd and ed:
        if ed < today:
            return "ended"
        if sd <= today:
            return "ongoing"
        return "upcoming"
    if ed:
        return "ongoing" if ed >= today else "ended"
    if sd:
        if sd > today:
            return "upcoming"
        # 只写了开始、没有结束：60 天内算进行中，超过 180 天视为早已结束
        if (today - sd).days > 180:
            return "ended"
        return "unknown" if (today - sd).days > 60 else "ongoing"
    return "unknown"


def refresh_status(store: dict) -> dict:
    counts = {"ongoing": 0, "upcoming": 0, "ended": 0, "unknown": 0}
    for e in store["events"]:
        e["status"] = status_of(e)
        counts[e["status"]] += 1
    return counts


def days_left(e: dict) -> int | None:
    t = e.get("end_time")
    if not t:
        return None
    try:
        return (date.fromisoformat(t) - today_sh()).days
    except ValueError:
        return None


def dedupe(store: dict) -> int:
    """对全库做一次去重合并，保留来源最多、信息最全的那条。"""
    events = sorted(store["events"], key=lambda e: (-len(e.get("sources", [])), -(e.get("confidence") or 0)))
    kept: list[dict] = []
    merged = 0
    for e in events:
        target = next((k for k in kept if is_same_event(k, e)), None)
        if target is None:
            kept.append(e)
            continue
        for s in e.get("sources", []):
            if s.get("url") and s["url"] not in {x.get("url") for x in target.get("sources", [])}:
                target.setdefault("sources", []).append(s)
        merge_into(target, {
            "title": e.get("title"), "category": e.get("category"), "sub_tags": e.get("sub_tags"),
            "start_time": e.get("start_time"), "end_time": e.get("end_time"),
            "time_detail": e.get("time_detail"), "location_name": e.get("location_name"),
            "address": e.get("address"), "price_info": e.get("price_info"),
            "description": e.get("description"), "confidence": e.get("confidence"),
        })
        merged += 1
    store["events"] = kept
    return merged


def prune(store: dict, keep_ended_days: int = 45) -> int:
    """已结束超过 N 天的移入归档，主库保持精简。"""
    archive = read_json(ARCHIVE_FILE, default={"events": []}) or {"events": []}
    keep, moved = [], []
    today = today_sh()
    for e in store["events"]:
        if e.get("status") == "ended":
            t = e.get("end_time") or e.get("start_time")
            try:
                if t and (today - date.fromisoformat(t)).days > keep_ended_days:
                    moved.append(e)
                    continue
            except ValueError:
                pass
        keep.append(e)
    if moved:
        archive.setdefault("events", []).extend(moved)
        write_json(ARCHIVE_FILE, archive)
    store["events"] = keep
    return len(moved)


def stats(store: dict) -> dict:
    events = store["events"]
    srcs = {}
    for e in events:
        for s in e.get("sources", []):
            srcs[s.get("source_id")] = srcs.get(s.get("source_id"), 0) + 1
    fuzzy = [e for e in events if e.get("is_time_fuzzy")]
    return {
        "total": len(events),
        "ongoing": sum(1 for e in events if e.get("status") == "ongoing"),
        "upcoming": sum(1 for e in events if e.get("status") == "upcoming"),
        "unknown": sum(1 for e in events if e.get("status") == "unknown"),
        "ended": sum(1 for e in events if e.get("status") == "ended"),
        "fuzzy": len(fuzzy),
        "by_source": srcs,
        "by_category": {c: sum(1 for e in events if e.get("category") == c) for c in CATEGORIES},
    }


def review_queue(store: dict, limit: int = 40) -> list[dict]:
    """需要人工看一眼的：低置信 / 时间模糊 / 无地点 / 多源冲突。"""
    q = []
    for e in store["events"]:
        if e.get("status") == "ended":
            continue
        reasons = []
        if e.get("confidence", 1) < 0.6:
            reasons.append("低置信")
        if not e.get("start_time") or not e.get("end_time"):
            reasons.append("时间不全")
        if not e.get("location_name"):
            reasons.append("无地点")
        if len(e.get("sources", [])) > 1:
            reasons.append("多源")
        if reasons:
            q.append({"event": e, "reasons": reasons})
    q.sort(key=lambda x: (x["event"].get("confidence", 0), -len(x["event"].get("sources", []))))
    return q[:limit]
