#!/usr/bin/env python3
"""测本地宝移动站的反爬阈值：跨城市均匀请求，看第几次开始触发验证。

  python rate_test.py --n 20 --gap 6
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Blocked, http_get, today_sh  # noqa: E402

CITIES = ["cd", "wh", "xa", "nj", "cs", "zz", "km", "hf", "fs", "sy", "wx", "jn",
          "xm", "fz", "wz", "heb", "sjz", "dl", "nn", "qz"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--gap", type=float, default=6.0)
    args = ap.parse_args()
    ok = blocked = 0
    for i, c in enumerate(CITIES[: args.n], 1):
        url = f"https://m.{c}.bendibao.com/xiuxian/zhoumohd/?search=&action=get_data&page=1&pagesize=20&type_id1=&type_id2=&type="
        try:
            text, final, status = http_get(url, timeout=15, retries=0)
            is_json = text.lstrip().startswith("{")
            print(f"{i:2d} {c}: {'JSON' if is_json else 'HTML'} {len(text)}B")
            ok += 1
        except Blocked:
            print(f"{i:2d} {c}: BLOCKED")
            blocked += 1
        except Exception as e:  # noqa: BLE001
            print(f"{i:2d} {c}: ERR {str(e)[:40]}")
        time.sleep(args.gap)
    print(f"summary ok={ok} blocked={blocked} gap={args.gap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
