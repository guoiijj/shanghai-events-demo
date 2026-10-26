"""公共工具：路径、时区、HTTP、HTML 转文本、JSON 读写。零第三方依赖。"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
REPO_DIR = OPS_DIR.parent
STATE_DIR = OPS_DIR / "state"
LOG_DIR = OPS_DIR / "logs"
REPORT_DIR = OPS_DIR / "reports"
EVENTS_FILE = STATE_DIR / "events.json"
ARCHIVE_FILE = STATE_DIR / "archive.json"
SOURCES_FILE = OPS_DIR / "sources.json"
DATA_FILE = REPO_DIR / "data.json"
META_FILE = REPO_DIR / "meta.json"

SH_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

CATEGORIES = ["exhibition", "market", "popup_store", "food_stall", "shopping", "other"]
CATEGORY_ALIASES = {
    "展览": "exhibition", "展": "exhibition", "exhibition": "exhibition",
    "市集": "market", "集市": "market", "market": "market",
    "快闪": "popup_store", "快闪店": "popup_store", "popup_store": "popup_store",
    "美食": "food_stall", "美食节": "food_stall", "food_stall": "food_stall",
    "购物": "shopping", "促销": "shopping", "shopping": "shopping",
    "其他": "other", "other": "other",
}


def now_sh() -> datetime:
    return datetime.now(SH_TZ)


def today_sh() -> date:
    return now_sh().date()


def log(msg: str) -> None:
    line = f"[{now_sh().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def ensure_dirs() -> None:
    for d in (STATE_DIR, LOG_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article", "td"}
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip > 0:
            self.skip -= 1
        if tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self.skip == 0 and data.strip():
            self.chunks.append(data.strip())


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    text = "".join(p.chunks)
    text = unescape(text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def decode_body(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def http_get(url: str, timeout: int = 20, retries: int = 2):
    """返回 (text, final_url, status)。失败抛最后一个异常。"""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return decode_body(raw), resp.geturl(), resp.status
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last if last else RuntimeError(f"请求失败: {url}")


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\s+", " ", s).strip()


def iso_date(s):
    """把各种写法的日期归一成 YYYY-MM-DD；无法解析返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"^(\d{1,2})[-/.](\d{1,2})$", s)
        if not m:
            return None
        y = today_sh().year
        mo, d = int(m.group(1)), int(m.group(2))
        if mo < today_sh().month - 2:
            y += 1
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_title(t: str) -> str:
    t = clean_text(t).lower()
    t = re.sub(r"20\d{2}\s*年?", "", t)
    t = re.sub(r"第[一二三四五六七八九十百\d]+届", "", t)
    t = t.replace("上海", "").replace("魔都", "")
    t = re.sub(r"[\s·•\-—_（）()【】\[\]「」『』“”\"'’!！?？,，.。:：/、|]", "", t)
    t = re.sub(r"活动|攻略|详情|时间|地点|门票|票价|汇总|合集", "", t)
    return t


def norm_venue(v: str) -> str:
    v = clean_text(v).lower()
    v = re.sub(r"上海市?|浦东新区|黄浦区|徐汇区|静安区|长宁区|普陀区|虹口区|杨浦区|闵行区|宝山区|嘉定区|松江区|青浦区|奉贤区|金山区|崇明区", "", v)
    v = re.sub(r"[\s·•\-—_（）()【】\[\],，.。:：/、|]", "", v)
    return v
