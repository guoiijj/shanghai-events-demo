#!/usr/bin/env python3
"""城市源体检：检查本地宝某城市的 4 个频道是否可用、能出多少条。

  python city_probe.py bj gz cd --delay 7
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import http_get, log

CHANNELS = [
    ("xiuxian", "http://{code}.bendibao.com/xiuxian/", "html_cards"),
    ("zhanlan", "http://{code}.bendibao.com/xiuxian/zhanlan/?search=&action=get_all_activity&page=1&pagesize=50", "json"),
    ("shiji", "http://{code}.bendibao.com/xiuxian/shiji/?search=&action=get_all_activity&page=1&pagesize=50", "json"),
    ("huodong", "http://{code}.bendibao.com/xiuxian/huodong/", "html_news"),
]


def probe(code: str, delay: float = 7.0) -> dict:
    import time
    out = {"code": code, "channels": {}, "ok": False}
    for name, tpl, kind in CHANNELS:
        url = tpl.format(code=code)
        try:
            text, final, status = http_get(url, timeout=20, retries=1)
        except Exception as e:  # noqa: BLE001
            out["channels"][name] = {"ok": False, "error": str(e)[:60]}
            time.sleep(delay)
            continue
        info = {"ok": status == 200, "status": status, "bytes": len(text)}
        if kind == "json":
            try:
                data = json.loads(text[text.find("{"):])
                res = data.get("res") or []
                info["items"] = len(res)
                info["futures"] = sum(
                    1 for it in res
                    if re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", str(it.get("dates_format", "")))
                )
                info["ok"] = True
            except Exception:
                info["ok"] = False
                info["error"] = "not_json"
        elif kind == "html_cards":
            info["items"] = text.count("main-header-left-title")
            info["ok"] = info["ok"] and info["items"] > 0
        else:
            info["items"] = len(re.findall(r'listNews-item-s1', text))
            info["ok"] = info["ok"] and info["items"] > 0
        out["channels"][name] = info
        time.sleep(delay)
    out["ok"] = sum(1 for c in out["channels"].values() if c.get("ok")) >= 2
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+")
    ap.add_argument("--delay", type=float, default=7.0)
    args = ap.parse_args()
    results = []
    for c in args.codes:
        r = probe(c, args.delay)
        results.append(r)
        log(f"{c}: ok={r['ok']} " + " ".join(
            f"{k}={v.get('items', 'ERR')}" for k, v in r["channels"].items()))
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
