"""发布层：把主库编译成站点消费的 data.json / meta.json，并通过 GitHub API 推送。"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from datetime import date

from common import DATA_FILE, META_FILE, REPO_DIR, now_sh, read_json, today_sh, write_json

REPO_SLUG = "guoiijj/shanghai-events-demo"
BRANCH = "main"


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


def build_feed(store: dict, meta_extra: dict | None = None) -> list[dict]:
    events = [e for e in store["events"] if not e.get("hidden") and e.get("status") != "ended"]
    # 时间待确认的：只保留地点和置信度都像样的，且不能是三个月前的老信息
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


def publish(store: dict, meta_extra: dict | None = None) -> dict:
    feed = build_feed(store)
    write_json(DATA_FILE, feed)
    counts = {
        "published": len(feed),
        "ongoing": sum(1 for e in feed if e["status"] == "ongoing"),
        "upcoming": sum(1 for e in feed if e["status"] == "upcoming"),
        "unknown": sum(1 for e in feed if e["status"] == "unknown"),
        "total_db": len(store["events"]),
    }
    meta = {
        "generated_at": now_sh().isoformat(timespec="seconds"),
        "counts": counts,
    }
    if meta_extra:
        meta.update(meta_extra)
    write_json(META_FILE, meta)
    return meta


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


def push_site(store: dict, message: str) -> str | None:
    files = {}
    for name in ("data.json", "meta.json", "index.html"):
        p = REPO_DIR / name
        if p.exists():
            files[name] = p.read_bytes()
    if "data.json" not in files:
        return None
    return push_files(files, message)
