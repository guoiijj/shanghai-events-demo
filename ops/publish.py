"""发布层：把主库按城市编译成 data/{code}.json + cities.json，并通过 GitHub API 推送。"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import time
from datetime import date

from common import DATA_FILE, META_FILE, REPO_DIR, STATE_DIR, now_sh, read_json, today_sh, write_json

REPO_SLUG = "guoiijj/shanghai-events-demo"
BRANCH = "main"
DATA_DIR = REPO_DIR / "data"
CITY_INDEX = REPO_DIR / "cities.json"
PUSH_STATE = STATE_DIR / "pushed_hashes.json"
TIER_LABELS = {1: "一线", 2: "新一线", 3: "二线", 4: "三线热门"}


def _days_to_start(e: dict) -> int | None:
    s = e.get("start_time")
    if not s:
        return None
    try:
        return (date.fromisoformat(s) - today_sh()).days
    except ValueError:
        return None


def _days_left(e: dict) -> int | None:
    t = e.get("end_time")
    if not t:
        return None
    try:
        return (date.fromisoformat(t) - today_sh()).days
    except ValueError:
        return None


def build_feed(store: dict, city: str = "sh") -> list[dict]:
    events = [e for e in store["events"]
              if e.get("city", "sh") == city and not e.get("hidden") and e.get("status") != "ended"]
    today = today_sh()

    def _unknown_ok(e):
        if e.get("status") != "unknown":
            return True
        if not ((e.get("location_name") or e.get("address")) and (e.get("confidence") or 0) >= 0.4):
            return False
        s = e.get("start_time")
        if s:
            try:
                return (today - date.fromisoformat(s)).days <= 90
            except ValueError:
                return False
        return True

    events = [e for e in events if _unknown_ok(e)]
    order = {"ongoing": 0, "upcoming": 1, "unknown": 2}
    events.sort(key=lambda e: (
        order.get(e.get("status"), 3),
        (_days_left(e) if e.get("status") == "ongoing" and _days_left(e) is not None else 999),
        (_days_to_start(e) if e.get("status") == "upcoming" and _days_to_start(e) is not None else 999),
        -(e.get("confidence") or 0),
    ))
    feed = []
    for e in events:
        sources = [s for s in e.get("sources", []) if s.get("url")]
        feed.append({
            "id": e["id"],
            "title": e["title"],
            "category": e.get("category", "other"),
            "sub_tags": e.get("sub_tags", []),
            "start_time": e.get("start_time"),
            "end_time": e.get("end_time"),
            "time_detail": e.get("time_detail"),
            "is_time_fuzzy": e.get("is_time_fuzzy", True),
            "location_name": e.get("location_name"),
            "address": e.get("address"),
            "price_info": e.get("price_info"),
            "description": e.get("description"),
            "confidence": e.get("confidence", 0.5),
            "status": e.get("status", "unknown"),
            "days_left": _days_left(e),
            "days_to_start": _days_to_start(e),
            "source_url": sources[0]["url"] if sources else None,
            "source_count": len(sources),
            "source_names": list(dict.fromkeys(s.get("source_name") or "" for s in sources if s.get("source_name"))),
            "last_verified": e.get("last_verified"),
        })
    return feed


def _counts(feed: list[dict]) -> dict:
    return {
        "published": len(feed),
        "ongoing": sum(1 for e in feed if e["status"] == "ongoing"),
        "upcoming": sum(1 for e in feed if e["status"] == "upcoming"),
        "unknown": sum(1 for e in feed if e["status"] == "unknown"),
    }


def publish(store: dict, cities: list[dict], meta_extra: dict | None = None) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    index_cities = []
    totals = {"published": 0, "ongoing": 0, "upcoming": 0, "unknown": 0}
    sh_feed: list[dict] = []
    for c in cities:
        if not c.get("enabled", True):
            continue
        feed = build_feed(store, c["code"])
        write_json(DATA_DIR / f"{c['code']}.json", feed)
        cnt = _counts(feed)
        for k in totals:
            totals[k] += cnt[k]
        index_cities.append({
            "code": c["code"], "name": c["name"], "tier": c["tier"],
            "tier_label": TIER_LABELS.get(c["tier"], ""),
            **cnt,
        })
        if c["code"] == "sh":
            sh_feed = feed

    index_cities.sort(key=lambda x: (x["tier"], -x["published"], x["name"]))
    write_json(CITY_INDEX, {
        "generated_at": now_sh().isoformat(timespec="seconds"),
        "city_count": len(index_cities),
        "total_published": totals["published"],
        "cities": index_cities,
    })
    # 兼容旧入口：根目录 data.json / meta.json 仍然是上海
    write_json(DATA_FILE, sh_feed)
    counts = {**_counts(sh_feed), "total_db": len(store["events"]), "all_cities": totals}
    meta = {"generated_at": now_sh().isoformat(timespec="seconds"), "counts": counts, "city": "sh"}
    if meta_extra:
        meta.update(meta_extra)
    write_json(META_FILE, meta)
    return {"counts": counts, "cities": len(index_cities), "totals": totals}


def _gh_path() -> str:
    p = shutil.which("gh")
    if p:
        return p
    return r"C:\Users\15647\AppData\Local\Programs\GitHubCLI\bin\gh.exe"


def _gh(args: list[str], payload: dict | None = None, retries: int = 3) -> str:
    cmd = [_gh_path(), "api", *args]
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(
            cmd,
            input=(json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None),
            capture_output=True,
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", "ignore").strip()
        last = proc.stderr.decode("utf-8", "ignore")[:800]
        if attempt < retries - 1:
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"gh api 失败: {' '.join(args)}\n{last}")


def push_files(files: dict[str, bytes], message: str) -> str:
    """用 GitHub Git Data API 提交多个文件（本机 git 协议到 github.com 不通，API 可用）。"""
    head = _gh([f"repos/{REPO_SLUG}/git/ref/heads/{BRANCH}", "--jq", ".object.sha"])
    tree = _gh([f"repos/{REPO_SLUG}/git/commits/{head}", "--jq", ".tree.sha"])
    entries = []
    for path, content in files.items():
        blob = _gh([f"repos/{REPO_SLUG}/git/blobs", "-X", "POST", "--input", "-"],
                   {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        sha = json.loads(blob)["sha"]
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})
    new_tree = json.loads(_gh([f"repos/{REPO_SLUG}/git/trees", "-X", "POST", "--input", "-"],
                              {"base_tree": tree, "tree": entries}))["sha"]
    commit = json.loads(_gh([f"repos/{REPO_SLUG}/git/commits", "-X", "POST", "--input", "-"],
                            {"message": message, "tree": new_tree, "parents": [head]}))["sha"]
    _gh([f"repos/{REPO_SLUG}/git/refs/heads/{BRANCH}", "-X", "PATCH", "--input", "-"],
        {"sha": commit, "force": False})
    return commit


def collect_site_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in ("index.html", "data.json", "meta.json", "cities.json"):
        p = REPO_DIR / name
        if p.exists():
            files[name] = p.read_bytes()
    for p in sorted(DATA_DIR.glob("*.json")):
        files[f"data/{p.name}"] = p.read_bytes()
    return files


def push_site(message: str) -> str | None:
    """只推送内容有变化的文件，避免每天 75 个 blob 全传一遍。"""
    files = collect_site_files()
    if not files:
        return None
    prev = read_json(PUSH_STATE, default={}) or {}
    changed, hashes = {}, dict(prev)
    for path, content in files.items():
        h = hashlib.sha1(content).hexdigest()
        if prev.get(path) != h:
            changed[path] = content
            hashes[path] = h
    if not changed:
        return None
    commit = push_files(changed, message)
    write_json(PUSH_STATE, hashes)
    return commit
