"""xHamster search + direct-source extraction. Scrapes public pages."""
import json
import re
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

LAST_DUMP = ""
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


def _walk(node, path, out):
    """Collect every video-looking URL in a nested dict/list, with its path."""
    if isinstance(node, dict):
        if isinstance(node.get("url"), str):
            out.append((path, node, node["url"]))
        for k, v in node.items():
            if k != "url":
                _walk(v, path + [str(k)], out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, path, out)
    elif isinstance(node, str) and (node.startswith("http") or
                                    node.startswith("//")):
        out.append((path, {}, node))


def _quality(path, node, url):
    cands = [str(node.get("quality") or ""), str(node.get("label") or "")]
    cands += list(reversed(path)) + [url]
    for c in cands:
        m = re.search(r"(\d{3,4})p", c)
        if m:
            return m.group(1) + "p"
    return "480p"


def _get(url):
    """Fetch a page with a real-browser TLS fingerprint when possible."""
    try:
        from curl_cffi import requests as cr
        return cr.get(url, impersonate="chrome", timeout=25,
                      headers={"Accept-Language": "en-US,en;q=0.9",
                               "Referer": BASE + "/"})
    except Exception as e:
        print("[debug] curl_cffi not used:", repr(e)[:150])
        return requests.get(url, headers=HEADERS, timeout=25)


def sources(page_url):
    """Return (mp4_variants, hls_url, seconds)."""
    resp = _get(page_url)
    html = resp.text
    data = _initials(html)
    print("[debug] status={0} len={1} has_initials={2} has_player={3}".format(
        resp.status_code, len(html), "window.initials" in html,
        bool(data.get("xplayerSettings"))))
    src = (data.get("xplayerSettings", {}) or {}).get("sources", {}) or {}
    print("[debug] source keys:", list(src.keys()))
    global LAST_DUMP
    LAST_DUMP = json.dumps(src)[:1500]
    print("[debug] sources dump:", LAST_DUMP)

    found = []
    _walk(src, [], found)

    variants, hls_urls = [], []
    for path, node, url in found:
        if url.startswith("//"):
            url = "https:" + url
        url = url.replace("\\/", "/")
        low = url.lower()
        in_hls = "hls" in [p.lower() for p in path]
        if ".m3u8" in low or in_hls:
            hls_urls.append((path, url))
        elif ".mp4" in low or "standard" in [p.lower() for p in path]:
            variants.append({"quality": _quality(path, node, url),
                             "url": url,
                             "size": node.get("size") or 0,
                             "av1": any("av1" in p.lower() for p in path)})

    # Telegram plays H.264 best; only keep AV1 if nothing else exists
    non_av1 = [v for v in variants if not v["av1"]]
    variants = non_av1 or variants

    non_av1_hls = [u for p, u in hls_urls
                   if not any("av1" in s.lower() for s in p)]
    hls = (non_av1_hls or [u for p, u in hls_urls] or [None])[0]

    # Fallback: scan the raw page for any .mp4 / .m3u8 links
    if not variants and not hls:
        raw = html.replace("\\/", "/").replace("\\u002F", "/")
        links = re.findall(
            r"https?://[^\"'\s<>\\]+?\.(?:m3u8|mp4)[^\"'\s<>\\]*", raw)
        seen = set()
        for u in links:
            if u in seen:
                continue
            seen.add(u)
            if ".m3u8" in u.lower():
                if not hls:
                    hls = u
            else:
                variants.append({"quality": _quality([], {}, u), "url": u,
                                 "size": 0, "av1": False})
        print("[debug] html fallback found {0} links".format(len(seen)))

    LAST_DUMP = "xps={0} src={1} html_m3u8={2} html_mp4={3}".format(
        json.dumps(data.get("xplayerSettings"))[:250], json.dumps(src)[:300],
        html.count(".m3u8"), html.count(".mp4"))
    print("[debug]", LAST_DUMP)

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
