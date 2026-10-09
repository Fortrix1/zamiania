"""xHamster search + direct-source extraction. Scrapes public pages."""
import json
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://xhamster.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://xhamster.com/",
}

BITRATE = {"240p": 40_000, "360p": 80_000, "480p": 140_000,
           "720p": 280_000, "1080p": 500_000}
ORDER = ["1080p", "720p", "480p", "360p", "240p"]
SORTS = {"trending": "trending", "newest": "newest",
         "top": "top-rated", "all": "top-rated"}


def search(query, sort="trending", limit=60):
    sort = SORTS.get(sort, sort)
    url = f"{BASE}/search/{quote(query)}"
    params = {}
    if sort in ("newest", "top-rated", "trending"):
        params["sort"] = sort
    html = requests.get(url, params=params, headers=HEADERS, timeout=25).text
    soup = BeautifulSoup(html, "html.parser")

    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        m = None
        if "/videos/" in a["href"]:
            m = re_search_vid(a["href"])
        if not m:
            continue
        vid = m
        if vid in seen:
            continue
        img = a.find("img")
        if not img:
            continue
        thumb = img.get("src") or img.get("data-src")
        if not thumb or thumb.startswith("data:"):
            continue
        seen.add(vid)
        href = a["href"]
        out.append({
            "id": vid,
            "url": href if href.startswith("http") else BASE + href,
            "thumb": thumb,
            "title": (img.get("alt") or vid).strip()[:120],
        })
        if len(out) >= limit:
            break
    return out


def re_search_vid(href):
    """Extract the video slug from a href like /videos/some-title-12345."""
    part = href.split("/videos/", 1)[1]
    slug = part.split("?")[0].split("#")[0].strip("/")
    return slug or None


def _initials(html):
    """Extract window.initials JSON by counting braces (survives nesting)."""
    start = html.find("window.initials")
    if start == -1:
        return {}
    i = html.find("{", start)
    if i == -1:
        return {}
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:j + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def sources(page_url):
    """Return (mp4_variants, hls_url, seconds)."""
    resp = requests.get(page_url, headers=HEADERS, timeout=25)
    html = resp.text
    data = _initials(html)
    print("[debug] status={0} len={1} has_initials={2} has_player={3}".format(
        resp.status_code, len(html), "window.initials" in html,
        bool(data.get("xplayerSettings"))))
    src = (data.get("xplayerSettings", {}) or {}).get("sources", {}) or {}
    print("[debug] source keys:", list(src.keys()))

    variants = []
    std = ((src.get("standard", {}) or {}).get("mp4", {})) or {}
    for quality, info in std.items():
        if isinstance(info, dict) and info.get("url"):
            variants.append({"quality": quality, "url": info["url"],
                             "size": info.get("size") or 0})

    hls = None
    h = src.get("hls", {}) or {}
    if isinstance(h, dict):
        for k in ("hls", "standard"):
            node = h.get(k)
            if isinstance(node, dict) and node.get("url"):
                hls = node["url"]
                break

    seconds = (data.get("videoModel", {}) or {}).get("duration") or None
    return variants, hls, seconds


def pick_under(variants, max_bytes, seconds=None):
    def est(v):
        if v["size"]:
            return v["size"]
        if seconds:
            return BITRATE.get(v["quality"], 200_000) * seconds
        return 0

    fits = [v for v in variants if est(v) <= max_bytes]
    pool = fits or variants
    for q in ORDER:
        for v in pool:
            if v["quality"] == q:
                return v
    return pool[0] if pool else None
