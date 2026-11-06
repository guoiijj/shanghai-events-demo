#!/usr/bin/env python3
"""多城市活动库 - 日常运营入口。

  python manage.py run --city bj --publish --push
  python manage.py run --due --limit 25 --publish --push   # 按 cadence 轮转到期的城市
  python manage.py publish --push
  python manage.py stats [--city bj]
  python manage.py review [--city bj]
  python manage.py cities
  python manage.py hide <id>
  python manage.py import-legacy <C:/abs/data.json>
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import enrich
import publish as publisher
import store as db
from common import (
    OPS_DIR, REPORT_DIR, Blocked, ensure_dirs, log, now_sh, read_json, today_sh, write_json,
)
from extract import extract_events
from fetchers import fetch_api_items, fetch_article, fetch_candidates

CITIES_FILE = OPS_DIR / "cities.json"
SEEN_FILE = OPS_DIR / "state" / "seen_urls.json"
RUNS_FILE = OPS_DIR / "state" / "city_runs.json"
COOLDOWN_FILE = OPS_DIR / "state" / "cooldown.json"


def cooldown_until():
    data = read_json(COOLDOWN_FILE, default={}) or {}
    s = data.get("until")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def set_cooldown(minutes: int = 20) -> None:
    write_json(COOLDOWN_FILE, {"until": (now_sh() + timedelta(minutes=minutes)).isoformat(timespec="seconds")})


# ---------- 城市与源 ----------

def load_cities() -> list[dict]:
    return read_json(CITIES_FILE, default=[]) or []


def city_by_code(cities: list[dict], code: str) -> dict | None:
    return next((c for c in cities if c["code"] == code), None)


def sources_for(city: dict) -> list[dict]:
    """一个城市 5 个结构化源，全部走移动站（桌面站有反爬，移动站目前宽松且接口同构）。"""
    d = city["domain"]
    name = city["name"]
    c = city["code"]
    base = f"https://m.{d}.bendibao.com/xiuxian"
    return [
        {"id": f"{c}_zhoumohd", "name": f"{name}·周末活动",
         "list_url": f"{base}/zhoumohd/", "mode": "bendibao_api",
         "action": "get_data", "extra": "&type_id1=&type_id2=&type=", "pages": 2, "max_new": 0, "enabled": True},
        {"id": f"{c}_mianfeihd", "name": f"{name}·免费活动",
         "list_url": f"{base}/mianfeihd/", "mode": "bendibao_api",
         "action": "get_data", "extra": "&type_id1=&type_id2=&type=", "pages": 2, "max_new": 0, "enabled": True},
        {"id": f"{c}_zhanlan", "name": f"{name}·展览",
         "list_url": f"{base}/zhanlan/", "mode": "bendibao_api",
         "action": "get_all_activity", "pages": 2, "max_new": 0, "enabled": True},
        {"id": f"{c}_shiji", "name": f"{name}·市集",
         "list_url": f"{base}/shiji/", "mode": "bendibao_api",
         "action": "get_all_activity", "pages": 2, "max_new": 0, "enabled": True},
        {"id": f"{c}_shiling", "name": f"{name}·时令活动",
         "list_url": f"{base}/shilinghuodong/", "mode": "bendibao_api",
         "action": "get_all_activity", "pages": 1, "max_new": 0, "enabled": True},
    ]


def due_cities(cities: list[dict], runs: dict, limit: int) -> list[dict]:
    today = today_sh()
    out = []
    for c in cities:
        if not c.get("enabled", True):
            continue
        last = runs.get(c["code"])
        cadence = c.get("cadence", 3)
        if last:
            try:
                gap = (today - date.fromisoformat(last)).days
            except ValueError:
                gap = 99
            if gap < cadence:
                continue
        out.append(c)
    out.sort(key=lambda c: (c.get("tier", 9), runs.get(c["code"]) or "", c["code"]))
    return out[:limit] if limit else out


# ---------- 抓取 ----------

def load_seen() -> dict:
    return read_json(SEEN_FILE, default={}) or {}


def save_seen(seen: dict) -> None:
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=120)).isoformat()
    trimmed = {u: v for u, v in seen.items() if (v.get("first_seen") or "")[:10] >= cutoff}
    write_json(SEEN_FILE, trimmed or seen)


def run_city(city: dict, store: dict, seen: dict, args) -> dict:
    """抓一个城市，返回统计。"""
    per = {"items": 0, "articles": 0, "new": 0, "merged": 0, "fail": 0}
    for src in sources_for(city):
        log(f"  {src['id']}")
        if src.get("mode") == "bendibao_api":
            try:
                items = fetch_api_items(src)
            except Exception as e:  # noqa: BLE001
                log(f"    接口失败: {str(e)[:60]}")
                per["fail"] += 1
                continue
            per["items"] += len(items)
            for it in items:
                it["city"] = city["code"]
                res = db.upsert(store, it)
                per[res] += 1
            log(f"    接口 {len(items)} 条，新增 {per['new']}，累计合并 {per['merged']}")
        limit = args.max_new if getattr(args, "max_new", None) is not None else src.get("max_new", 0)
        if limit <= 0:
            time.sleep(1.0)
            continue
        try:
            candidates = fetch_candidates(src)
        except Exception as e:  # noqa: BLE001
            log(f"    列表失败: {str(e)[:60]}")
            per["fail"] += 1
            continue
        todo = [c for c in candidates if c["url"] not in seen][:limit]
        log(f"    文章候选 {len(candidates)}，本次抓 {len(todo)}")
        for cand in todo:
            try:
                text, page_title = fetch_article(cand["url"])
                events = extract_events(text, src, cand["url"], page_title=page_title,
                                        date_hint=cand.get("date_hint", ""))
            except Exception as e:  # noqa: BLE001
                log(f"    [失败] {cand['title'][:24]} {str(e)[:40]}")
                per["fail"] += 1
                continue
            per["articles"] += 1
            for ev in events:
                ev["city"] = city["code"]
                res = db.upsert(store, ev)
                per[res] += 1
                if res == "new":
                    log(f"    + {ev['title'][:36]}")
            seen[cand["url"]] = {"first_seen": now_sh().isoformat(timespec="seconds"),
                                 "city": city["code"], "events": len(events)}
            time.sleep(2.0)  # 本地宝详情页抓快了会限流
        time.sleep(1.0)
    return per


def enrich_city(city: dict, store: dict, limit: int) -> dict:
    enr = {"done": 0, "fields": 0, "fail": 0}
    if limit <= 0:
        return enr
    db.refresh_status(store)
    cands = [e for e in store["events"]
             if e.get("city") == city["code"] and enrich.needs_enrichment(e)
             and (e.get("enrich_tries") or 0) < 3]
    cands.sort(key=lambda e: (-(e.get("confidence") or 0), e.get("start_time") or "9999"))
    for e in cands[:limit]:
        urls = [s.get("url") for s in e.get("sources", []) if s.get("url")]
        if not urls:
            continue
        try:
            text, _ = fetch_article(urls[0], timeout=12, retries=1)
            patch = enrich.enrich_event(e, text)
        except Exception as ex:  # noqa: BLE001
            enr["fail"] += 1
            e["enrich_tries"] = (e.get("enrich_tries") or 0) + 1
            log(f"    [补全失败] {e['title'][:20]} {str(ex)[:30]}")
            continue
        got = enrich.apply(e, patch)
        enr["done"] += 1
        enr["fields"] += len(got)
        time.sleep(1.2)
    return enr


# ---------- 命令 ----------

def select_cities(args) -> list[dict]:
    cities = load_cities()
    if args.city:
        c = city_by_code(cities, args.city)
        if not c:
            raise SystemExit(f"未知城市 {args.city}")
        return [c]
    if args.cities:
        wanted = [x.strip() for x in args.cities.split(",") if x.strip()]
        return [c for c in cities if c["code"] in wanted]
    if args.tier:
        tiers = {int(x) for x in args.tier.split(",")}
        return [c for c in cities if c.get("tier") in tiers and c.get("enabled", True)]
    if args.due:
        runs = read_json(RUNS_FILE, default={}) or {}
        return due_cities(cities, runs, args.limit)
    return [c for c in cities if c.get("enabled", True)]


def cmd_run(args) -> int:
    ensure_dirs()
    store = db.load_store()
    seen = load_seen()
    runs = read_json(RUNS_FILE, default={}) or {}
    cities = select_cities(args)
    if not cities:
        log("今天没有到期的城市")
        return 0
    log(f"本次处理 {len(cities)} 个城市：" + "、".join(c["name"] for c in cities))
    stats = {"cities": {}, "new": 0, "merged": 0, "articles": 0, "fail": 0, "enrich": {"done": 0, "fields": 0, "fail": 0}}
    started = time.time()

    for i, city in enumerate(cities, 1):
        log(f"[{i}/{len(cities)}] {city['name']} ({city['code']}, tier{city['tier']})")
        per = run_city(city, store, seen, args)
        enrich_limit = args.enrich if args.enrich is not None else (3 if city["tier"] <= 2 else 1)
        enr = enrich_city(city, store, enrich_limit)
        for k in ("done", "fields", "fail"):
            stats["enrich"][k] += enr[k]
        stats["cities"][city["code"]] = {"name": city["name"], **per, "enrich": enr["done"]}
        for k in ("new", "merged", "articles", "fail"):
            stats[k] += per[k]
        # 至少有一半源成功才记入轮转表，全失败的下次重试
        if per["fail"] < 4:
            runs[city["code"]] = today_sh().isoformat()
        db.refresh_status(store)
        db.save_store(store)
        save_seen(seen)
        write_json(RUNS_FILE, runs)
        time.sleep(args.city_gap)

    db.dedupe(store)
    db.refresh_status(store)
    moved = db.prune(store, keep_ended_days=args.keep_ended_days)
    db.save_store(store)
    elapsed = int(time.time() - started)
    log(f"完成 {len(cities)} 城：新增 {stats['new']}，合并 {stats['merged']}，文章 {stats['articles']}，"
        f"失败 {stats['fail']}，归档 {moved}，耗时 {elapsed}s")

    meta = None
    if args.publish or args.push:
        meta = publisher.publish(store, load_cities(), meta_extra={"run": {
            "cities": len(cities), "new": stats["new"], "articles": stats["articles"],
            "failed": stats["fail"], "seconds": elapsed}})
        log(f"已发布：{meta['cities']} 城，共 {meta['totals']['published']} 场")
    if args.push:
        commit = publisher.push_site(f"多城市更新 {today_sh().isoformat()}：{len(cities)} 城")
        log(f"已推送 GitHub：{(commit or 'no-change')[:8]}")

    write_report(stats, cities, meta, elapsed)
    return 0


def write_report(stats: dict, cities: list[dict], meta: dict | None, elapsed: int) -> None:
    lines = [f"# 运营日报 {today_sh().isoformat()}", "",
             f"- 城市：{len(cities)} 个，耗时 {elapsed}s",
             f"- 新增活动 {stats['new']}，合并 {stats['merged']}，文章 {stats['articles']}，失败 {stats['fail']}"]
    if meta:
        lines.append(f"- 发布：{meta['cities']} 城 / {meta['totals']['published']} 场")
    lines += ["", "| 城市 | 新增 | 合并 | 文章 | 失败 | 补全 |", "|---|---|---|---|---|---|"]
    for code, p in stats["cities"].items():
        lines.append(f"| {p['name']}({code}) | {p['new']} | {p['merged']} | {p['articles']} | {p['fail']} | {p.get('enrich', 0)} |")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{today_sh().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"日报：{path}")


CHANNELS = {
    "zhoumohd": ("周末活动", "get_data", "&type_id1=&type_id2=&type=", 2),
    "mianfeihd": ("免费活动", "get_data", "&type_id1=&type_id2=&type=", 2),
    "zhanlan": ("展览", "get_all_activity", "", 2),
    "shiji": ("市集", "get_all_activity", "", 2),
    "shiling": ("时令活动", "get_all_activity", "", 1),
}


def source_for(city: dict, channel: str) -> dict:
    label, action, extra, pages = CHANNELS[channel]
    base = f"https://m.{city['domain']}.bendibao.com/xiuxian"
    return {
        "id": f"{city['code']}_{channel}", "name": f"{city['name']}·{label}",
        "list_url": f"{base}/{channel}/", "mode": "bendibao_api",
        "action": action, "extra": extra, "pages": pages, "max_new": 0,
    }


def cmd_sweep(args) -> int:
    """轮转扫描：按频道外层、城市内层，把请求摊到 75 个城市域名上，避免单个站被限流。"""
    ensure_dirs()
    store = db.load_store()
    runs = read_json(RUNS_FILE, default={}) or {}
    cities = select_cities(args)
    if not cities:
        log("今天没有到期的城市")
        return 0
    channels = args.channels.split(",") if args.channels and args.channels != "auto" else None
    if channels is None:
        # 每天只扫一个频道，把请求量压到反爬阈值以下
        order = ["zhoumohd", "zhanlan", "mianfeihd", "shiji", "shiling"]
        channels = [order[today_sh().weekday() % len(order)]]
    until = cooldown_until()
    if until and now_sh() < until:
        log(f"冷却中，跳过本轮（到 {until.strftime('%H:%M')}）")
        return 0
    log(f"扫描 {len(channels)} 个频道 × {len(cities)} 城，间隔 {args.gap}s")
    stats = {"new": 0, "merged": 0, "blocked": 0, "fail": 0, "per_channel": {}, "per_city": {}}
    blocked_hosts: set[str] = set()
    aborted = False
    started = time.time()

    for ch in channels:
        ch_new = ch_merged = 0
        for i, city in enumerate(cities, 1):
            host = city["domain"]
            if host in blocked_hosts:
                continue
            src = source_for(city, ch)
            try:
                items = fetch_api_items(src)
            except Blocked:
                blocked_hosts.add(host)
                stats["blocked"] += 1
                set_cooldown(20)
                log(f"  {city['name']}·{ch} 触发验证 → 停止本轮，冷却 20 分钟")
                aborted = True
                break
            except Exception as e:  # noqa: BLE001
                stats["fail"] += 1
                log(f"  {city['name']}·{ch} 失败 {str(e)[:40]}")
                continue
            for it in items:
                it["city"] = city["code"]
                res = db.upsert(store, it)
                stats[res] += 1
                ch_new += res == "new"
                ch_merged += res == "merged"
            pc = stats["per_city"].setdefault(city["code"], {"name": city["name"], "new": 0, "items": 0})
            pc["new"] += sum(1 for _ in items)
            pc["items"] += len(items)
            if items:
                runs[city["code"]] = today_sh().isoformat()
            time.sleep(args.gap)
        stats["per_channel"][ch] = {"new": ch_new, "merged": ch_merged}
        db.refresh_status(store)
        db.save_store(store)
        write_json(RUNS_FILE, runs)
        log(f"频道 {ch} 完成：新增 {ch_new}，合并 {ch_merged}")
        if aborted:
            break

    db.dedupe(store)
    db.refresh_status(store)
    moved = db.prune(store, keep_ended_days=args.keep_ended_days)
    db.save_store(store)
    elapsed = int(time.time() - started)
    counts = db.stats(store)
    log(f"扫描完成：新增 {stats['new']}，合并 {stats['merged']}，被拦 {stats['blocked']}，失败 {stats['fail']}，"
        f"归档 {moved}，耗时 {elapsed}s；库内 {counts['total']} 条，在架 {counts['ongoing'] + counts['upcoming']}")

    meta = None
    if args.publish or args.push:
        meta = publisher.publish(store, load_cities(), meta_extra={"run": {
            "mode": "sweep", "cities": len(cities), "channels": len(channels),
            "new": stats["new"], "blocked": stats["blocked"], "seconds": elapsed}})
        log(f"已发布：{meta['cities']} 城，共 {meta['totals']['published']} 场")
    if args.push:
        commit = publisher.push_site(f"全国更新 {today_sh().isoformat()}：{len(cities)} 城扫描")
        log(f"已推送 GitHub：{(commit or 'no-change')[:8]}")

    write_sweep_report(stats, cities, meta, elapsed)
    return 0


def write_sweep_report(stats: dict, cities: list[dict], meta: dict | None, elapsed: int) -> None:
    lines = [f"# 全国扫描日报 {today_sh().isoformat()}", "",
             f"- 城市 {len(cities)} 个，耗时 {elapsed}s，被拦截 {stats['blocked']} 次",
             f"- 新增 {stats['new']}，合并 {stats['merged']}，失败 {stats['fail']}"]
    if meta:
        lines.append(f"- 发布：{meta['cities']} 城 / {meta['totals']['published']} 场")
    lines += ["", "## 频道", "", "| 频道 | 新增 | 合并 |", "|---|---|---|"]
    for ch, p in stats["per_channel"].items():
        lines.append(f"| {ch} | {p['new']} | {p['merged']} |")
    lines += ["", "## 城市", "", "| 城市 | 抓到 | 新增 |", "|---|---|---|"]
    for code, p in sorted(stats["per_city"].items(), key=lambda kv: -kv[1]["items"])[:60]:
        lines.append(f"| {p['name']} | {p['items']} | {p['new']} |")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"sweep-{today_sh().isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"日报：{path}")


def cmd_publish(args) -> int:
    store = db.load_store()
    merged = db.dedupe(store)
    if merged:
        log(f"发布前去重合并 {merged} 条")
    db.refresh_status(store)
    db.save_store(store)
    meta = publisher.publish(store, load_cities())
    log(f"已发布：{meta['cities']} 城，共 {meta['totals']['published']} 场")
    if args.push:
        commit = publisher.push_site(f"发布更新 {today_sh().isoformat()}")
        log(f"已推送：{(commit or 'no-change')[:8]}")
    return 0


def cmd_stats(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    s = db.stats(store, args.city)
    print(f"{'城市 ' + args.city if args.city else '全库'}：{s['total']} 条，"
          f"进行中 {s['ongoing']} / 即将开始 {s['upcoming']} / 待确认 {s['unknown']} / 已结束 {s['ended']}")
    print("分类：", s["by_category"])
    for c in load_cities():
        cs = db.stats(store, c["code"])
        if cs["total"]:
            print(f"  {c['name']:6s}({c['code']:8s}) {cs['total']:4d} 条  "
                  f"在架 {cs['ongoing'] + cs['upcoming']:3d}")
    return 0


def cmd_review(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    for item in db.review_queue(store, limit=args.limit, city=args.city):
        e = item["event"]
        print(f"[{','.join(item['reasons'])}] {e.get('city', 'sh')} | {e['title']}")
        print(f"    {e.get('start_time')} ~ {e.get('end_time')} | {e.get('location_name')} | id={e['id']}")
        for s in e.get("sources", []):
            print(f"    {s.get('url')}")
    return 0


def cmd_cities(args) -> int:
    store = db.load_store()
    db.refresh_status(store)
    runs = read_json(RUNS_FILE, default={}) or {}
    for c in load_cities():
        s = db.stats(store, c["code"])
        print(f"t{c['tier']} {c['name']:6s} {c['code']:10s} 库 {s['total']:4d}  "
              f"在架 {s['ongoing'] + s['upcoming']:3d}  上次 {runs.get(c['code'], '-')}")
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
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        ev = {
            "title": raw.get("title"), "category": raw.get("category", "other"),
            "sub_tags": raw.get("sub_tags", []), "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"), "time_detail": raw.get("time_detail"),
            "is_time_fuzzy": raw.get("is_time_fuzzy", True), "location_name": raw.get("location_name"),
            "address": raw.get("address"), "price_info": raw.get("price_info"),
            "description": raw.get("description"), "confidence": raw.get("confidence", 0.5),
            "source_url": raw.get("source_url"), "source_id": "legacy", "source_name": "历史数据",
            "city": args.city,
        }
        db.upsert(store, ev)
        n += 1
    db.refresh_status(store)
    db.save_store(store)
    log(f"导入 {n} 条到 {args.city}，主库现在 {len(store['events'])} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="多城市活动库运营工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="抓取+抽取+合并+发布")
    r.add_argument("--city", default="", help="只跑一个城市，如 bj")
    r.add_argument("--cities", default="", help="跑多个城市，逗号分隔")
    r.add_argument("--tier", default="", help="按分级跑，如 1,2")
    r.add_argument("--due", action="store_true", help="只跑按 cadence 到期的城市")
    r.add_argument("--limit", type=int, default=0, help="配合 --due，本次最多几个城市")
    r.add_argument("--max-new", type=int, default=None, help="覆盖每源文章上限")
    r.add_argument("--enrich", type=int, default=None, help="每城补全条数")
    r.add_argument("--city-gap", type=float, default=3.0, help="城市之间停顿秒数")
    r.add_argument("--publish", action="store_true")
    r.add_argument("--push", action="store_true")
    r.add_argument("--keep-ended-days", type=int, default=45)
    r.set_defaults(func=cmd_run)

    sw = sub.add_parser("sweep", help="按频道轮转扫描多城（推荐日常用）")
    sw.add_argument("--city", default="")
    sw.add_argument("--cities", default="")
    sw.add_argument("--tier", default="")
    sw.add_argument("--due", action="store_true")
    sw.add_argument("--limit", type=int, default=0)
    sw.add_argument("--channels", default="", help="只扫指定频道，逗号分隔")
    sw.add_argument("--gap", type=float, default=2.5, help="每次请求之间停顿秒数")
    sw.add_argument("--publish", action="store_true")
    sw.add_argument("--push", action="store_true")
    sw.add_argument("--keep-ended-days", type=int, default=45)
    sw.set_defaults(func=cmd_sweep)

    p = sub.add_parser("publish")
    p.add_argument("--push", action="store_true")
    p.set_defaults(func=cmd_publish)

    st = sub.add_parser("stats")
    st.add_argument("--city", default="")
    st.set_defaults(func=cmd_stats)

    rv = sub.add_parser("review")
    rv.add_argument("--city", default="")
    rv.add_argument("--limit", type=int, default=40)
    rv.set_defaults(func=cmd_review)

    ci = sub.add_parser("cities")
    ci.set_defaults(func=cmd_cities)

    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=45)
    pr.set_defaults(func=cmd_prune)

    h = sub.add_parser("hide")
    h.add_argument("id")
    h.add_argument("--reason", default="")
    h.set_defaults(func=cmd_hide)

    il = sub.add_parser("import-legacy")
    il.add_argument("path")
    il.add_argument("--city", default="sh")
    il.set_defaults(func=cmd_import_legacy)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
