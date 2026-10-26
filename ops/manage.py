#!/usr/bin/env python3
"""上海活动库 - 日常运营入口。

  python manage.py run --max-new 8 --publish --push   # 抓取 -> 抽取 -> 合并 -> 发布 -> 推 GitHub
  python manage.py publish --push                     # 只重新发布/推送
  python manage.py stats                              # 看库的状态
  python manage.py review                             # 看需要人工确认的条目
  python manage.py prune --days 45                    # 归档已结束的活动
  python manage.py import-legacy <data.json>          # 导入旧数据
  python manage.py hide <id>                          # 下架一条（人工纠错）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish as publisher
import store as db
import enrich
from common import (
    REPORT_DIR, SOURCES_FILE, ensure_dirs, log, now_sh, read_json, today_sh, write_json,
)
from extract import extract_events
from fetchers import fetch_api_items, fetch_article, fetch_candidates

SEEN_FILE = Path(__file__).resolve().parent / "state" / "seen_urls.json"


def load_seen() -> dict:
    return read_json(SEEN_FILE, default={}) or {}


def save_seen(seen: dict) -> None:
    # 只保留最近 120 天，避免无限膨胀
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=120)).isoformat()
    trimmed = {u: v for u, v in seen.items() if (v.get("first_seen") or "")[:10] >= cutoff}
    write_json(SEEN_FILE, trimmed or seen)


def cmd_run(args) -> int:
    ensure_dirs()
    store = db.load_store()
    seen = load_seen()
    sources = [s for s in (read_json(SOURCES_FILE, default=[]) or []) if s.get("enabled")]
    if args.sources:
        wanted = {s.strip() for s in args.sources.split(",")}
        sources = [s for s in sources if s["id"] in wanted]

    stats = {"new": 0, "merged": 0, "articles": 0, "fail": 0, "per_source": {}}
    new_titles: list[str] = []

    for src in sources:
        log(f"来源 {src['id']} ({src['name']})")
        per = {"items": 0, "articles": 0, "new": 0, "merged": 0, "fail": 0}
        if src.get("mode") == "bendibao_api":
            try:
                items = fetch_api_items(src)
            except Exception as e:  # noqa: BLE001
                log(f"  接口失败: {e}")
                per["fail"] += 1
                stats["fail"] += 1
                stats["per_source"][src["id"]] = per
                continue
            per["items"] = len(items)
            for it in items:
                res = db.upsert(store, it)
                per[res] += 1
                stats[res] += 1
                if res == "new":
                    new_titles.append(it["title"])
            log(f"  接口入库：{len(items)} 条，新增 {per['new']}，合并 {per['merged']}")

        try:
            candidates = fetch_candidates(src)
        except Exception as e:  # noqa: BLE001
            log(f"  列表页失败: {e}")
            per["fail"] += 1
            stats["fail"] += 1
            stats["per_source"][src["id"]] = per
            continue

        max_new = args.max_new or src.get("max_new", 10)
        todo = [c for c in candidates if c["url"] not in seen][:max_new]
        log(f"  待抓取 {len(todo)} 篇（已有 {len(candidates) - len(todo)} 篇处理过）")
        for cand in todo:
            per["items"] += 1
            try:
                text, page_title = fetch_article(cand["url"])
                events = extract_events(
                    text, src, cand["url"], page_title=page_title,
                    date_hint=cand.get("date_hint", ""),
                )
            except Exception as e:  # noqa: BLE001
                log(f"  [失败] {cand['title'][:30]} -> {e}")
                per["fail"] += 1
                stats["fail"] += 1
                continue
            per["articles"] += 1
            stats["articles"] += 1
            for ev in events:
                res = db.upsert(store, ev)
                per[res] += 1
                stats[res] += 1
                if res == "new":
                    new_titles.append(ev["title"])
                    log(f"    + {ev['title']} ({ev.get('start_time')}~{ev.get('end_time')})")
            seen[cand["url"]] = {"first_seen": now_sh().isoformat(timespec="seconds"),
                                 "source_id": src["id"], "events": len(events)}
        stats["per_source"][src["id"]] = per

    enr = {"done": 0, "fields": 0, "fail": 0}
    if args.enrich:
        db.refresh_status(store)
        cands = [e for e in store["events"] if enrich.needs_enrichment(e) and (e.get("enrich_tries") or 0) < 3]
        cands.sort(key=lambda e: (-(e.get("confidence") or 0), e.get("start_time") or "9999"))
        log(f"补全队列：{len(cands)} 条，本次处理 {min(args.enrich, len(cands))} 条")
        for e in cands[: args.enrich]:
            urls = [s.get("url") for s in e.get("sources", []) if s.get("url")]
            if not urls:
                continue
            try:
                text, _ = fetch_article(urls[0], timeout=12, retries=1)
                patch = enrich.enrich_event(e, text)
            except Exception as ex:  # noqa: BLE001
                enr["fail"] += 1
                e["enrich_tries"] = (e.get("enrich_tries") or 0) + 1
                log(f"  [补全失败] {e['title'][:24]} -> {ex}")
                continue
            got = enrich.apply(e, patch)
            enr["done"] += 1
            enr["fields"] += len(got)
            if got:
                log(f"  ~ {e['title'][:24]} 补上 {','.join(got)}")
    stats["enrich"] = enr

    db.dedupe(store)
    db.refresh_status(store)
    moved = db.prune(store, keep_ended_days=args.keep_ended_days)
    db.save_store(store)
    save_seen(seen)
    log(f"入库完成：新增 {stats['new']}，合并 {stats['merged']}，文章 {stats['articles']}，失败 {stats['fail']}，归档 {moved}")

    meta = None
    if args.publish or args.push:
        meta = publisher.publish(store, meta_extra={"run": {
            "new": stats["new"], "merged": stats["merged"],
            "articles": stats["articles"], "failed": stats["fail"],
        }})
        log(f"已生成 data.json：{meta['counts']}")
    if args.push:
        counts = (meta or {}).get("counts", {})
        commit = publisher.push_site(store, f"每日更新 {today_sh().isoformat()}：{counts.get('published', 0)} 场在架")
        log(f"已推送 GitHub：{commit[:8]}")

    write_report(store, stats, new_titles, meta)
    return 0


def write_report(store: dict, stats: dict, new_titles: list[str], meta: dict | None) -> None:
    s = db.stats(store)
    lines = [
        f"# 运营日报 {today_sh().isoformat()}",
        "",
        f"- 抓取文章：{stats['articles']} 篇，失败 {stats['fail']} 个源/篇",
        f"- 新增活动：{stats['new']} 条，合并重复：{stats['merged']} 条",
        f"- 主库：{s['total']} 条（进行中 {s['ongoing']} / 即将开始 {s['upcoming']} / 时间待确认 {s['unknown']} / 已结束 {s['ended']}）",
        f"- 时间信息不完整：{s['fuzzy']} 条（占 {round(s['fuzzy'] * 100 / max(s['total'], 1))}%）",
    ]
    enr = stats.get("enrich") or {}
    if enr.get("done"):
        lines.append(f"- 补全：{enr['done']} 条回原文补齐 {enr['fields']} 个字段（失败 {enr['fail']}）")
    if meta:
        lines.append(f"- 已发布：{meta['counts']['published']} 场")
    lines += ["", "## 各来源", "", "| 来源 | 抓取 | 新增 | 合并 | 失败 |", "|---|---|---|---|---|"]
    for sid, p in stats["per_source"].items():
        lines.append(f"| {sid} | {p['items']} | {p['new']} | {p['merged']} | {p['fail']} |")
    if new_titles:
        lines += ["", "## 新增清单", ""] + [f"- {t}" for t in new_titles[:60]]
    review = db.review_queue(store)
    if review:
        lines += ["", "## 待人工确认", ""]
        for item in review[:20]:
            e = item["event"]
            lines.append(f"- [{','.join(item['reasons'])}] {e['title']} · {e.get('location_name') or '无地点'} · "
                         f"{e.get('start_time') or '?'}~{e.get('end_time') or '?'} · "
                         f"{(e.get('sources') or [{}])[0].get('url', '')}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{today_sh().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"日报：{path}")


def cmd_publish(args) -> int:
    store = db.load_store()
    merged = db.dedupe(store)
    if merged:
        log(f"发布前去重合并 {merged} 条")
    db.refresh_status(store)
    db.save_store(store)
    meta = publisher.publish(store)
    log(f"已发布 {meta['counts']}")
    if args.push:
        commit = publisher.push_site(store, f"发布更新 {today_sh().isoformat()}")
        log(f"已推送：{commit[:8]}")
    return 0


def cmd_stats(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    s = db.stats(store)
    print(f"主库 {s['total']} 条：进行中 {s['ongoing']} / 即将开始 {s['upcoming']} / "
          f"待确认 {s['unknown']} / 已结束 {s['ended']}")
    print("分类：", s["by_category"])
    print("来源：", s["by_source"])
    print(f"时间不完整 {s['fuzzy']} 条")
    for e in sorted(store["events"], key=lambda x: x.get("start_time") or "9999")[:20]:
        print(f"  {e['status']:8s} {e.get('start_time')}~{e.get('end_time')} {e['title']}")
    return 0


def cmd_review(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    for item in db.review_queue(store, limit=args.limit):
        e = item["event"]
        print(f"[{','.join(item['reasons'])}] {e['title']}")
        print(f"    {e.get('start_time')} ~ {e.get('end_time')} | {e.get('location_name')} | "
              f"conf={e.get('confidence')} | id={e['id']}")
        for s in e.get("sources", []):
            print(f"    {s.get('url')}")
    return 0


def cmd_prune(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    moved = db.prune(store, keep_ended_days=args.days)
    db.save_store(store)
    log(f"归档 {moved} 条")
    return 0


def cmd_hide(args) -> int:
    store = db.load_store()
    hit = [e for e in store["events"] if e["id"] == args.id or e["id"].startswith(args.id)]
    if not hit:
        print("没找到这个 id")
        return 1
    for e in hit:
        e["hidden"] = True
        e["notes"] = args.reason or "人工下架"
    db.save_store(store)
    log(f"已下架 {len(hit)} 条")
    return 0


def cmd_import_legacy(args) -> int:
    store = db.load_store()
    data = read_json(Path(args.path), default=[]) or []
    n = 0
    for raw in data:
        if not isinstance(raw, dict):
            continue
        ev = {
            "title": raw.get("title", ""),
            "category": raw.get("category", "other"),
            "sub_tags": raw.get("sub_tags", []),
            "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"),
            "time_detail": raw.get("time_detail"),
            "is_time_fuzzy": raw.get("is_time_fuzzy", True),
            "location_name": raw.get("location_name"),
            "address": raw.get("address"),
            "price_info": raw.get("price_info"),
            "description": raw.get("description"),
            "confidence": raw.get("confidence", 0.5),
            "source_url": raw.get("source_url"),
            "source_id": "legacy",
            "source_name": "历史数据",
        }
        if not ev["title"]:
            continue
        res = db.upsert(store, ev)
        n += 1
    db.refresh_status(store)
    db.save_store(store)
    log(f"导入 {n} 条，主库现在 {len(store['events'])} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="上海活动库运营工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="抓取+抽取+合并+发布")
    r.add_argument("--max-new", type=int, default=0, help="每个源本次最多抓几篇新文章")
    r.add_argument("--sources", default="", help="只跑指定源，逗号分隔")
    r.add_argument("--publish", action="store_true")
    r.add_argument("--push", action="store_true")
    r.add_argument("--keep-ended-days", type=int, default=45)
    r.add_argument("--enrich", type=int, default=25, help="本次最多补全多少条活动的价格/时间")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("publish")
    p.add_argument("--push", action="store_true")
    p.set_defaults(func=cmd_publish)

    st = sub.add_parser("stats")
    st.set_defaults(func=cmd_stats)

    rv = sub.add_parser("review")
    rv.add_argument("--limit", type=int, default=40)
    rv.set_defaults(func=cmd_review)

    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=45)
    pr.set_defaults(func=cmd_prune)

    h = sub.add_parser("hide")
    h.add_argument("id")
    h.add_argument("--reason", default="")
    h.set_defaults(func=cmd_hide)

    il = sub.add_parser("import-legacy")
    il.add_argument("path")
    il.set_defaults(func=cmd_import_legacy)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
