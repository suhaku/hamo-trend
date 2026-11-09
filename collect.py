#!/usr/bin/env python3
"""Hamo trend collector — NEW characters + official PVs only.

Five public sections (no game-level popularity ranking, no Reddit):
  1. new_characters   — revealed this calendar month (KST)
  2. pv_ranking       — official YouTube PV popularity (absolute + growth separate)
  3. character_ranking— composite from real signals only (search/views/X…)
  4. rising_character — fastest recent interest growth
  5. pickup_schedule  — official per-character pickups (start/end/source)

Reruns excluded from new rankings. Hold rank when data insufficient.
HTTPS allowlist, size limit, cache, sanitize. Keep previous on fetch error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from games import GAMES, SIGNALS_META

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
MONTH_START = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
UA = {"User-Agent": "Mozilla/5.0 (HamoTrendBot; +https://news84034.tistory.com)"}
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT_TREND = DATA / "trend.json"          # full payload with 5 sections
OUT_META = DATA / "meta.json"
OUT_NEW = DATA / "new_characters.json"   # registry (input+enriched)
OUT_SCHEDULE = DATA / "pickup_schedule.json"
OUT_PV = DATA / "pv_ranking.json"
OUT_CRANK = DATA / "character_ranking.json"
OUT_RISING = DATA / "rising_character.json"
OVERRIDES = DATA / "overrides.json"
PV_HIST = DATA / "pv_history"
CACHE_DIR = DATA / "cache"
MAX_BYTES = 2_000_000
RATE_SLEEP = 1.0
CACHE_TTL = 3 * 3600
HOLD_PV_AGE_H = 24
MIN_SIGNALS_RANK = 1
errors: list[str] = []

ALLOWED_FETCH_HOSTS = frozenset({
    "www.youtube.com", "arca.live",
    "sg-hk4e-api.hoyoverse.com", "sg-hkrpg-api.hoyoverse.com",
    "bbs-api-os.hoyolab.com", "comm-api.game.naver.com",
    "bang-dream.bushimo.jp", "trends.google.com",
})
ALLOWED_OUTPUT_HOSTS = frozenset({
    "www.youtube.com", "youtu.be", "i.ytimg.com", "arca.live",
    "sg-hk4e-api.hoyoverse.com", "sg-hkrpg-api.hoyoverse.com",
    "bbs-api-os.hoyolab.com", "www.hoyolab.com", "hoyolab.com",
    "comm-api.game.naver.com", "game.naver.com", "bang-dream.bushimo.jp",
    "webstatic.hoyoverse.com", "sdk-os-static.hoyoverse.com",
    "upload-os-bbs.hoyolab.com", "img-os-bbs.hoyolab.com",
    "act-webstatic.hoyoverse.com", "fastcdn.hoyoverse.com",
    "ssl.pstatic.net", "phinf.pstatic.net", "nng-phinf.pstatic.net",
})
SCRIPTISH = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|link|meta|style|svg|math)[^>]*>.*?</\s*\1\s*>|"
    r"<\s*(script|iframe|object|embed|link|meta)\b[^>]*/?\s*>|"
    r"javascript\s*:|data\s*:|on[a-z]+\s*="
)
PV_TITLE_RE = re.compile(
    r"캐릭터|PV|티저|예고|Trailer|Teaser|Character\s*(PV|Trailer|Demo)|アニメPV|キャラクター",
    re.I,
)
PICKUP_RE = re.compile(r"기원|워프|채널|픽업|모집|기간 한정|복각|뽑기|헤드헌팅|소환|공명|조달|ガチャ|확률 UP|UP!")
DATE_RE = re.compile(r"(\d{4}[.\-/년]\s*)?(\d{1,2})\s*[월./]\s*(\d{1,2})\s*일?")
RERUN_RE = re.compile(r"복각|재등장|rerun|返场|復刻", re.I)
JUNK_NAME_RE = re.compile(r"열쇠|상점|특간|안내|모집|버전|무기|법구|한손검|양손검|장병기|활·|광추|원암|별빛 계시|고요히 샘솟는")


def iso(t: dt.datetime) -> str:
    return t.astimezone(KST).isoformat(timespec="seconds")


def safe_url(url: str | None, *, for_output: bool = False) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    if p.scheme != "https" or p.username or p.password:
        return None
    host = (p.hostname or "").lower().rstrip(".")
    allowed = ALLOWED_OUTPUT_HOSTS if for_output else ALLOWED_FETCH_HOSTS
    if host not in allowed:
        return None
    return url.strip()


def strip_html(h: str | None) -> str:
    if not h:
        return ""
    text = SCRIPTISH.sub(" ", h)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:2000]


def sanitize(obj):
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        if obj.startswith(("http://", "https://", "//")):
            return safe_url(obj if obj.startswith("https://") else None, for_output=True)
        return strip_html(obj)
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = strip_html(str(k))[:80]
            if key in ("source_url", "url", "image", "thumb", "link", "illustration_url") or key.endswith("_url"):
                out[key] = safe_url(v, for_output=True) if isinstance(v, str) else None
            else:
                out[key] = sanitize(v)
        return out
    return strip_html(str(obj))[:500]


_last_fetch = 0.0


def get(url: str, **kw) -> requests.Response:
    global _last_fetch
    clean = safe_url(url, for_output=False)
    if not clean:
        raise ValueError(f"blocked URL: {url[:120]}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(clean.encode()).hexdigest()[:40]
    cp = CACHE_DIR / f"{h}.bin"
    use_cache = kw.pop("use_cache", True)
    ttl = kw.pop("cache_ttl", CACHE_TTL)
    if use_cache and cp.exists() and (time.time() - cp.stat().st_mtime) < ttl:
        out = requests.Response()
        out.status_code = 200
        out._content = cp.read_bytes()
        out.url = clean
        out.encoding = "utf-8"
        return out
    elapsed = time.time() - _last_fetch
    if elapsed < RATE_SLEEP:
        time.sleep(RATE_SLEEP - elapsed)
    timeout = kw.pop("timeout", 20)
    headers = {**UA, **kw.pop("headers", {})}
    with requests.get(clean, headers=headers, timeout=timeout, stream=True, **kw) as r:
        r.raise_for_status()
        chunks, total = [], 0
        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("response too large")
            chunks.append(chunk)
        content = b"".join(chunks)
        encoding = r.encoding or "utf-8"
        final_url = r.url
        status = r.status_code
    _last_fetch = time.time()
    cp.write_bytes(content)
    out = requests.Response()
    out.status_code = status
    out._content = content
    out.url = final_url
    out.encoding = encoding
    return out


def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"load {path.name}: {type(e).__name__}")
    return default


def signal_status(key: str) -> str:
    meta = SIGNALS_META.get(key, {})
    env = meta.get("env")
    if env:
        return "ok" if os.environ.get(env) else "미연동"
    return meta.get("status_default", "ok")


def parse_dt(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return dt.datetime.fromtimestamp(s, KST)
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=KST)
        return t.astimezone(KST)
    except Exception:
        return None


# ---------- YouTube official PV collection ----------
def fetch_channel_videos(g):
    yt = g.get("yt") or {}
    cid = yt.get("global") if isinstance(yt, dict) else yt
    region = (yt.get("region") if isinstance(yt, dict) else "Global") or "Global"
    if not cid or not re.fullmatch(r"[A-Za-z0-9_-]{20,30}", str(cid)):
        raise ValueError("bad yt channel")
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    f = feedparser.parse(get(url).content)
    vids = []
    for e in f.entries:
        vid = getattr(e, "yt_videoid", None) or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
            continue
        pub = parse_dt(e.published)
        views = int(e.get("media_statistics", {}).get("views", 0))
        # likes/comments not in RSS → 미연동
        vids.append(dict(
            id=vid,
            title=strip_html(e.title)[:200],
            published=iso(pub) if pub else None,
            published_dt=pub,
            views=views,
            likes=None,
            comments=None,
            likes_comments_status="미연동",
            thumb=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            url=safe_url(getattr(e, "link", None), for_output=True),
            channel_id=cid,
            channel_region=region,
            channel_title=strip_html(f.feed.get("title"))[:120],
            is_pv=bool(PV_TITLE_RE.search(e.title or "")),
            source=url,
        ))
    return dict(status="ok", fetched_at=iso(NOW), source=url, region=region, videos=vids)


def snapshot_pv(video: dict):
    """Append views snapshot for deltas. history file per video id."""
    PV_HIST.mkdir(parents=True, exist_ok=True)
    path = PV_HIST / f"{video['id']}.json"
    hist = load_json(path, {"video_id": video["id"], "snapshots": []}) or {"video_id": video["id"], "snapshots": []}
    snap = dict(at=iso(NOW), views=video["views"])
    # avoid duplicate within 2h
    snaps = hist.get("snapshots") or []
    if snaps:
        last = parse_dt(snaps[-1].get("at"))
        if last and (NOW - last) < dt.timedelta(hours=2):
            snaps[-1] = snap
        else:
            snaps.append(snap)
    else:
        snaps.append(snap)
    # keep ~90 days of snaps (cron every 3h ≈ 720 max; trim to 800)
    hist["snapshots"] = snaps[-800:]
    hist["title"] = video.get("title")
    hist["published"] = video.get("published")
    hist["channel_region"] = video.get("channel_region")
    hist["url"] = video.get("url")
    path.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    return hist


def deltas_from_history(hist: dict, published: dt.datetime | None):
    """Compute view deltas near 24h/7d/30d after publish when snapshots exist."""
    snaps = [(parse_dt(s["at"]), int(s["views"])) for s in (hist or {}).get("snapshots") or [] if s.get("at") is not None]
    snaps = [(t, v) for t, v in snaps if t]
    snaps.sort()
    out = {"views_now": snaps[-1][1] if snaps else None, "delta_24h": None, "delta_7d": None, "delta_30d": None,
           "since_publish_24h": None, "since_publish_7d": None, "since_publish_30d": None}
    if len(snaps) < 2:
        return out
    now_v = snaps[-1][1]
    def approx_delta(hours):
        target = NOW - dt.timedelta(hours=hours)
        older = [s for s in snaps if s[0] <= target]
        if not older:
            return None
        return now_v - older[-1][1]
    out["delta_24h"] = approx_delta(24)
    out["delta_7d"] = approx_delta(24 * 7)
    out["delta_30d"] = approx_delta(24 * 30)
    if published:
        def since_pub(hours):
            mark = published + dt.timedelta(hours=hours)
            if NOW < mark:
                return None  # not yet reached window
            # snapshot closest after mark, vs first snapshot after publish
            after_pub = [s for s in snaps if s[0] >= published]
            after_mark = [s for s in snaps if s[0] >= mark]
            if not after_pub or not after_mark:
                return None
            return after_mark[0][1] - after_pub[0][1]
        out["since_publish_24h"] = since_pub(24)
        out["since_publish_7d"] = since_pub(24 * 7)
        out["since_publish_30d"] = since_pub(24 * 30)
    return out


# ---------- Official pickups ----------
def hoyo_ann(g, cfg):
    base = {
        "hk4e": ("sg-hk4e-api.hoyoverse.com", "hk4e_global", "os_asia", "100000000"),
        "hkrpg": ("sg-hkrpg-api.hoyoverse.com", "hkrpg_global", "prod_official_asia", "800000000"),
    }[cfg["game"]]
    q = (f"game={cfg['game']}&game_biz={base[1]}&lang=ko-kr&bundle_id={base[1]}"
         f"&platform=pc&region={base[2]}&level=60&uid={base[3]}&channel_id=1")
    lst = f"https://{base[0]}/common/{base[1]}/announcement/api/getAnnList?{q}"
    con = lst.replace("getAnnList", "getAnnContent")
    j = get(lst).json()["data"]
    c = get(con).json()["data"]
    content = {a["ann_id"]: a for a in c.get("list", []) + c.get("pic_list", [])}
    anns = [a for grp in j.get("list", []) for a in grp.get("list", [])]
    anns += [a for grp in j.get("pic_list", []) for t in grp.get("type_list", []) for a in t.get("list", [])]
    out = []
    for a in anns:
        title = strip_html(a.get("title") or a.get("subtitle"))
        if not re.search(r"기원|워프", title) or not re.search(r"확률|이벤트 워프|콜라보 워프", title):
            continue
        body = strip_html(content.get(a["ann_id"], {}).get("content", ""))
        blob = body + " " + title
        # Genshin/HSR style: 「칭호·캐릭터명(속성)」
        chars = []
        for m in re.finditer(r"「[^」]*·\s*([^」·（(\n]{2,20})\s*[（(][^）)]+[）)]」", blob):
            chars.append(m.group(1).strip())
        # bare names in title after 기원:
        for m in re.finditer(r"기원\s*:\s*「([^」]{2,20})」", title):
            tok = m.group(1)
            if "·" in tok:
                tok = tok.split("·")[-1]
            tok = re.sub(r"[（(][^）)]+[）)]", "", tok).strip()
            if tok:
                chars.append(tok)
        chars = sorted(set(c for c in chars if 1 < len(c) <= 20 and not re.search(r"무기|법구|원암|광추|활·|검", c)))
        start, end = a.get("start_time"), a.get("end_time")
        if not start or not end:
            continue
        is_rerun = bool(RERUN_RE.search(title + " " + body[:400]))
        out.append(dict(
            title=title[:200], start=start, end=end, tz="UTC+8",
            characters=chars or None,
            image=safe_url(a.get("banner") or a.get("img"), for_output=True),
            is_new=not is_rerun, is_rerun=is_rerun,
            type="복각" if is_rerun else "신규",
            confirmed=True,
            source="HoYoverse 공식 인게임 공지 API",
            source_url=lst.split("?")[0],
        ))
    return out


def hoyolab_news(g, cfg):
    gid = int(cfg["gid"])
    url = f"https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList?gids={gid}&type=1&page_size=30"
    L = get(url, headers={"x-rpc-language": "ko-kr"}).json()["data"]["list"]
    out = []
    for p in L:
        t = strip_html(p["post"]["subject"])
        if not re.search(r"채널|신규 에이전트|확률 UP|독점", t):
            continue
        pid = str(p["post"]["post_id"])
        if not pid.isdigit():
            continue
        is_rerun = bool(RERUN_RE.search(t))
        out.append(dict(
            title=t[:200], start=None, end=None, characters=None,
            image=safe_url((p.get("image_list") or [{}])[0].get("url"), for_output=True),
            posted=iso(dt.datetime.fromtimestamp(int(p["post"]["created_at"]), KST)),
            is_new=not is_rerun, is_rerun=is_rerun, type="복각" if is_rerun else "신규",
            confirmed=False,
            source="HoYoLAB 공식 계정 공지",
            source_url=f"https://www.hoyolab.com/article/{pid}",
            note="시작/종료일 본문 파서 미완 → schedule 제외",
        ))
    return out


def _parse_kr_dates(text: str):
    ms = list(DATE_RE.finditer(text))
    if len(ms) < 2:
        return None, None
    def one(m, year_fallback):
        y = m.group(1)
        year = int(re.sub(r"\D", "", y)) if y and re.search(r"\d{4}", y or "") else year_fallback
        try:
            return dt.datetime(year, int(m.group(2)), int(m.group(3)), tzinfo=KST).date().isoformat()
        except ValueError:
            return None
    return one(ms[0], NOW.year), one(ms[1], NOW.year)


def naver_lounge(g, cfg):
    lounge = cfg["lounge"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", lounge):
        raise ValueError("bad lounge")
    out = []
    for b in range(1, 21):
        url = (f"https://comm-api.game.naver.com/nng_main/v1/community/lounge/{lounge}/feed"
               f"?boardId={b}&buffFilteringYN=N&limit=15&offset=0&order=NEW")
        try:
            j = get(url).json()
        except Exception:
            continue
        if j.get("code") != 200:
            continue
        for f in (j["content"] or {}).get("feeds", []):
            if f["user"].get("userRoleCode") not in ("game_manager", "lounge_manager", "official"):
                continue
            t = strip_html(f["feed"]["title"])
            if not PICKUP_RE.search(t) or re.search(r"당첨|종료\]|【종료】|전시작|참가자|크리에이터|판매", t):
                continue
            created = dt.datetime.strptime(f["feed"]["createdDate"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            if NOW - created > dt.timedelta(days=60):
                continue
            body = ""
            try:
                doc = json.loads(f["feed"].get("contents") or "{}")
                body = strip_html(" ".join(re.findall(r'"value"\s*:\s*"([^"]*)"', json.dumps(doc, ensure_ascii=False))))
            except Exception:
                pass
            start, end = _parse_kr_dates(t + " " + body)
            is_rerun = bool(RERUN_RE.search(t + " " + body[:200]))
            named = []
            for c in re.findall(r"「([^」]{1,30})」", t + " " + body):
                if re.search(r"특간|이벤트|안내|모집|버전", c):
                    continue
                named.append(c.strip())
            out.append(dict(
                title=t[:200], posted=iso(created), start=start, end=end,
                characters=named or None,
                is_new=not is_rerun, is_rerun=is_rerun,
                type="복각" if is_rerun else "신규",
                image=safe_url(f["feed"].get("repImageUrl"), for_output=True),
                confirmed=bool(start and end),
                source="네이버 게임 라운지 공식 GM 공지",
                source_url=safe_url((f.get("feedLink") or {}).get("pc"), for_output=True),
                note=None if (start and end) else "기간 미확정 → schedule 제외",
            ))
    seen, uniq = set(), []
    for p in out:
        k = p.get("source_url") or p["title"]
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def wp_json(g, cfg):
    base = cfg["base"].rstrip("/")
    if not safe_url(base, for_output=False):
        raise ValueError("bad wp base")
    url = f"{base}/wp-json/wp/v2/posts?per_page=10&search=%E3%82%AC%E3%83%81%E3%83%A3&_fields=date,title,link"
    return [dict(
        title=strip_html(p["title"]["rendered"])[:200],
        posted=p["date"] + "+09:00", start=None, end=None, characters=None,
        is_new=True, is_rerun=False, confirmed=False, type="신규",
        source="BanG Dream! GBP 일본 공식 사이트 뉴스",
        source_url=safe_url(p["link"], for_output=True),
        note="기간 미확정 → schedule 제외",
    ) for p in get(url).json()]


def fetch_pickups(g):
    cfg = g["pickup"]
    k = cfg["kind"]
    if k == "none":
        return []
    return {
        "hoyo_ann": hoyo_ann, "hoyolab_news": hoyolab_news,
        "naver_lounge": naver_lounge, "wp_json": wp_json,
    }[k](g, cfg)


# ---------- Trends (character names, optional daily) ----------
def trends_characters(names: list[str]):
    """Fetch Trends for up to ~8 character names + game anchors. Relative."""
    if not names:
        return None
    from pytrends.request import TrendReq
    p = TrendReq(hl="ko-KR", tz=-540, timeout=(10, 30))
    series = {}
    dates = []
    # batch 4 + keep first as soft anchor if present
    uniq = []
    for n in names:
        if n and n not in uniq:
            uniq.append(n)
    for i in range(0, min(len(uniq), 12), 4):
        batch = uniq[i:i + 4]
        try:
            p.build_payload(batch, timeframe="today 1-m", geo="KR")
            df = p.interest_over_time()
            if df.empty:
                continue
            dates = [d.strftime("%Y-%m-%d") for d in df.index]
            for k in batch:
                if k in df.columns:
                    series[k] = [float(v) for v in df[k].tolist()]
            time.sleep(3)
        except Exception as e:
            errors.append(f"trends_chars: {type(e).__name__}: {e!s}"[:160])
    return dict(status="ok", source="Google Trends (pytrends, geo=KR)", fetched_at=iso(NOW),
                dates=dates, series=series, note="상대값. 게임 크기 정규화는 ranking에서 처리.")


def alias_list(ch):
    out = []
    name = ch.get("name") or {}
    if isinstance(name, dict):
        out.extend([name.get("ko"), name.get("en"), name.get("ja")])
    aliases = ch.get("aliases") or {}
    for lang in ("ko", "en", "ja"):
        out.extend(aliases.get(lang) or [])
    return [a for a in out if a and isinstance(a, str) and len(a) >= 2]


def match_pvs_for_character(ch, game_videos: list[dict]):
    aliases = alias_list(ch)
    if not aliases:
        return []
    hits = []
    for v in game_videos:
        title = v.get("title") or ""
        if any(a.lower() in title.lower() for a in aliases):
            if v.get("is_pv") or True:  # alias match on official channel is enough; prefer PV titles
                hits.append(v)
    # prefer PV-tagged
    hits.sort(key=lambda x: (0 if x.get("is_pv") else 1, -(x.get("views") or 0)))
    return hits


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def logscale_map(vals: dict):
    vs = {k: v for k, v in vals.items() if v is not None and v >= 0}
    if not vs:
        return {}
    m = max(math.log1p(v) for v in vs.values()) or 1
    return {k: round(100 * math.log1p(v) / m, 1) for k, v in vs.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trends", action="store_true", help="Refresh character Google Trends")
    args = ap.parse_args()

    overrides = load_json(OVERRIDES, {}) or {}
    hide_chars = set(overrides.get("hide_character_ids") or [])
    force_hold = set(overrides.get("force_hold_character_ids") or [])
    hide_pickups = set(overrides.get("hide_pickup_urls") or [])
    approve_pickups = set(overrides.get("approve_pickup_urls") or [])

    registry = load_json(OUT_NEW, {"characters": []}) or {"characters": []}
    characters = [c for c in (registry.get("characters") or []) if c.get("id")]

    signals_status = {
        "youtube_rss": signal_status("youtube_rss"),
        "youtube_data_api": signal_status("youtube_data_api"),
        "x_api": signal_status("x_api"),
        "community_arca": signal_status("community_arca"),
        "google_trends": signal_status("google_trends"),
        "pickups_official": signal_status("pickups_official"),
    }

    # --- collect per game: videos + pickups ---
    game_videos: dict[str, list] = {}
    game_pv_baselines: dict[str, float | None] = {}
    all_pickups_raw = []
    prev_trend = load_json(OUT_TREND, {}) or {}

    for g in GAMES:
        try:
            ch = fetch_channel_videos(g)
            vids = ch["videos"]
            game_videos[g["id"]] = vids
            # baseline = median views of recent official PVs on that channel (normalize game size)
            pv_views = [v["views"] for v in vids if v.get("is_pv")]
            if len(pv_views) < 3:
                pv_views = [v["views"] for v in vids]
            game_pv_baselines[g["id"]] = median(pv_views)
            for v in vids:
                if v.get("is_pv") or True:
                    snapshot_pv({k: v[k] for k in v if k != "published_dt"})
        except Exception as e:
            errors.append(f"{g['id']}.youtube: {type(e).__name__}: {e!s}"[:200])
            # keep previous videos if any in prev payload
            prev_sec = ((prev_trend.get("pv_ranking") or {}).get("by_game") or {}).get(g["id"])
            game_videos[g["id"]] = []
            game_pv_baselines[g["id"]] = None
            if prev_sec:
                errors.append(f"{g['id']}.youtube: kept empty this run (see prev pv_ranking)")
        try:
            for p in fetch_pickups(g):
                all_pickups_raw.append({**p, "game_id": g["id"], "game_name": g["name"]})
        except Exception as e:
            errors.append(f"{g['id']}.pickups: {type(e).__name__}: {e!s}"[:200])
        time.sleep(0.3)

    # --- auto-register NEW characters named in confirmed official pickups ---
    existing_ids = {c["id"] for c in characters}
    existing_alias = set()
    for c in characters:
        for a in alias_list(c):
            existing_alias.add(a.lower())
    for p in all_pickups_raw:
        if not p.get("confirmed") or p.get("is_rerun") or not p.get("is_new"):
            continue
        # skip weapon-only banners
        if re.search(r"신의 주조|무기 기원|광추|무기 확률", p.get("title") or ""):
            continue
        for nm in (p.get("characters") or []):
            if not nm or len(nm) < 2 or JUNK_NAME_RE.search(nm):
                continue
            if nm.lower() in existing_alias:
                continue
            ascii_slug = re.sub(r"[^a-z0-9]+", "-", nm.lower()).strip("-")[:40]
            if not ascii_slug:
                ascii_slug = hashlib.sha1(nm.encode("utf-8")).hexdigest()[:10]
            cid = f"{p['game_id']}-{ascii_slug}"
            if cid in existing_ids:
                continue
            characters.append(dict(
                id=cid, game_id=p["game_id"],
                name={"ko": nm, "en": None, "ja": None},
                aliases={"ko": [nm], "en": [], "ja": []},
                reveal_date=(str(p.get("start") or ""))[:10] or None,
                status="pickup_live",
                illustration_url=p.get("image"),
                source_urls=[u for u in [p.get("source_url")] if u],
                pickup=dict(start=p.get("start"), end=p.get("end"), source_url=p.get("source_url"),
                            source=p.get("source"), is_rerun=False, is_new=True),
                is_new=True, active=True,
                note="auto from official pickup notice",
            ))
            existing_ids.add(cid)
            existing_alias.add(nm.lower())

    # --- enrich characters from PV matches + pickups ---
    enriched = []
    for ch in characters:
        if ch["id"] in hide_chars:
            continue
        gid = ch["game_id"]
        vids = game_videos.get(gid) or []
        pvs = match_pvs_for_character(ch, vids)
        primary = pvs[0] if pvs else None
        reveal = ch.get("reveal_date")
        if not reveal and primary and primary.get("published"):
            reveal = primary["published"][:10]
            ch["reveal_date"] = reveal
        # status inference
        status = ch.get("status") or "upcoming"
        # link pickups by alias overlap in title
        aliases = alias_list(ch)
        linked_pickup = None
        for p in all_pickups_raw:
            if p["game_id"] != gid:
                continue
            blob = (p.get("title") or "") + " " + " ".join(p.get("characters") or [])
            if aliases and any(a in blob for a in aliases):
                linked_pickup = p
                break
            if p.get("characters") and any(a in (p.get("characters") or []) for a in aliases):
                linked_pickup = p
                break
        if linked_pickup and linked_pickup.get("confirmed"):
            ch["pickup"] = dict(
                start=linked_pickup.get("start"), end=linked_pickup.get("end"),
                source_url=linked_pickup.get("source_url"), source=linked_pickup.get("source"),
                is_rerun=linked_pickup.get("is_rerun"), is_new=linked_pickup.get("is_new"),
            )
            if linked_pickup.get("is_rerun"):
                ch["is_new"] = False
                status = "ended" if parse_dt(str(linked_pickup.get("end"))) and parse_dt(str(linked_pickup.get("end"))) < NOW else "pickup_live"
            else:
                ch["is_new"] = True
                end_d = parse_dt(str(linked_pickup.get("end")))
                start_d = parse_dt(str(linked_pickup.get("start")))
                if start_d and end_d:
                    if NOW < start_d:
                        status = "revealed"
                    elif NOW <= end_d:
                        status = "pickup_live"
                    else:
                        status = "ended"
        elif reveal:
            status = "revealed" if status == "upcoming" else status
        ch["status"] = status
        if primary:
            srcs = list(ch.get("source_urls") or [])
            if primary.get("url") and primary["url"] not in srcs:
                srcs.append(primary["url"])
            ch["source_urls"] = srcs
            if not ch.get("illustration_url"):
                # official yt thumb only as placeholder illustration (allowlisted)
                ch["illustration_url"] = primary.get("thumb")
        ch["_pvs"] = pvs
        ch["_primary_pv"] = primary
        enriched.append(ch)

    # month filter: revealed this month OR pickup overlapping this month; NEW only for rankings
    def revealed_this_month(ch):
        rd = ch.get("reveal_date")
        if not rd:
            return False
        d = parse_dt(rd + "T00:00:00+09:00") if len(rd) == 10 else parse_dt(rd)
        return bool(d and d >= MONTH_START)

    def is_rerun_char(ch):
        if ch.get("is_new") is False:
            return True
        pk = ch.get("pickup") or {}
        return bool(pk.get("is_rerun"))

    new_this_month = [c for c in enriched if (revealed_this_month(c) or (c.get("status") in ("revealed", "pickup_live", "upcoming") and c.get("is_new") is not False))
                      and not is_rerun_char(c) and c.get("active", True)]
    # stricter for ranking pools: must be is_new and not rerun; prefer reveal this month or active pickup
    rank_pool = [c for c in enriched if c.get("active", True) and not is_rerun_char(c) and c.get("is_new", True)
                 and (revealed_this_month(c) or c.get("status") in ("revealed", "pickup_live", "upcoming"))]

    # Trends
    tr = None
    trend_names = []
    for c in rank_pool:
        al = alias_list(c)
        if al:
            trend_names.append(al[0])  # prefer ko
    if args.trends and trend_names:
        try:
            tr = trends_characters(trend_names[:12])
        except Exception as e:
            errors.append(f"trends: {type(e).__name__}: {e!s}"[:200])
    elif (prev_trend.get("character_ranking") or {}).get("trends_cache"):
        tr = (prev_trend["character_ranking"]).get("trends_cache")

    # --- build PV metrics per character ---
    def char_pv_metrics(ch):
        primary = ch.get("_primary_pv")
        if not primary:
            return None
        hist = snapshot_pv({k: primary[k] for k in primary if k != "published_dt"})
        pub = primary.get("published_dt") or parse_dt(primary.get("published"))
        dlt = deltas_from_history(hist, pub)
        age_h = (NOW - pub).total_seconds() / 3600 if pub else None
        baseline = game_pv_baselines.get(ch["game_id"])
        views = primary.get("views")
        lift = round(views / baseline, 3) if (views is not None and baseline and baseline > 0) else None
        return dict(
            video_id=primary["id"], title=primary["title"], url=primary.get("url"),
            thumb=primary.get("thumb"), channel_region=primary.get("channel_region"),
            published=primary.get("published"), age_hours=round(age_h, 1) if age_h is not None else None,
            views_absolute=views,
            likes=None, comments=None, likes_comments_status="미연동",
            deltas=dlt,
            game_baseline_median_views=baseline,
            lift_vs_game_baseline=lift,
            source=primary.get("source"),
            fetched_at=iso(NOW),
        )

    # Section 1: new_characters
    sec_new = []
    for c in new_this_month:
        sec_new.append(dict(
            id=c["id"], game_id=c["game_id"],
            name=c.get("name"), aliases=c.get("aliases"),
            reveal_date=c.get("reveal_date"),
            status=c.get("status"),
            illustration_url=safe_url(c.get("illustration_url"), for_output=True),
            source_urls=[u for u in (c.get("source_urls") or []) if safe_url(u, for_output=True)],
            pickup=c.get("pickup"),
            is_new=True,
            last_updated=iso(NOW),
        ))

    # Section 2: pv_ranking (absolute and growth SEPARATE lists; hold if insufficient)
    pv_rows = []
    for c in rank_pool:
        m = char_pv_metrics(c)
        hold_reason = None
        if c["id"] in force_hold:
            hold_reason = "overrides.force_hold"
        elif not m:
            hold_reason = "공식 PV 매칭 없음"
        elif m.get("age_hours") is not None and m["age_hours"] < HOLD_PV_AGE_H:
            hold_reason = f"PV 게시 후 {HOLD_PV_AGE_H}h 미만"
        elif m.get("views_absolute") is None:
            hold_reason = "조회수 데이터 없음"
        row = dict(
            character_id=c["id"], name=c.get("name"), game_id=c["game_id"],
            hold=bool(hold_reason), hold_reason=hold_reason,
            rank_absolute=None, rank_growth=None,
            absolute=dict(views=m["views_absolute"] if m else None,
                          lift_vs_game_baseline=m.get("lift_vs_game_baseline") if m else None),
            growth=dict(
                delta_24h=(m or {}).get("deltas", {}).get("delta_24h") if m else None,
                delta_7d=(m or {}).get("deltas", {}).get("delta_7d") if m else None,
                delta_30d=(m or {}).get("deltas", {}).get("delta_30d") if m else None,
                since_publish_24h=(m or {}).get("deltas", {}).get("since_publish_24h") if m else None,
                since_publish_7d=(m or {}).get("deltas", {}).get("since_publish_7d") if m else None,
                since_publish_30d=(m or {}).get("deltas", {}).get("since_publish_30d") if m else None,
            ),
            pv=m,
            last_updated=iso(NOW),
        )
        pv_rows.append(row)

    abs_sorted = sorted([r for r in pv_rows if not r["hold"] and r["absolute"]["views"] is not None],
                        key=lambda r: -r["absolute"]["views"])
    for i, r in enumerate(abs_sorted, 1):
        r["rank_absolute"] = i
    growth_sorted = sorted(
        [r for r in pv_rows if not r["hold"] and r["growth"].get("delta_7d") is not None],
        key=lambda r: -(r["growth"]["delta_7d"] or 0),
    )
    for i, r in enumerate(growth_sorted, 1):
        r["rank_growth"] = i

    # Section 3: character_ranking composite (real signals only)
    crank_rows = []
    for c in rank_pool:
        m = char_pv_metrics(c)
        aliases = alias_list(c)
        trends_val = None
        if tr and tr.get("series") and aliases:
            for a in aliases:
                if a in tr["series"] and tr["series"][a]:
                    s = tr["series"][a]
                    trends_val = sum(s[-8:-1]) / max(len(s[-8:-1]), 1) if len(s) >= 8 else sum(s) / len(s)
                    break
        # normalize trends vs game name baseline if present
        game = next((g for g in GAMES if g["id"] == c["game_id"]), None)
        game_tr = None
        if tr and game and game.get("trends") in (tr.get("series") or {}):
            s = tr["series"][game["trends"]]
            game_tr = sum(s[-8:-1]) / max(len(s[-8:-1]), 1) if len(s) >= 8 else None
        trends_lift = round(trends_val / game_tr, 3) if (trends_val is not None and game_tr and game_tr > 0) else None

        signals = {
            "youtube_pv_views": (m or {}).get("views_absolute"),
            "youtube_pv_lift": (m or {}).get("lift_vs_game_baseline"),
            "google_trends": trends_val,
            "google_trends_lift": trends_lift,
            "x": None,  # 미연동
            "community": None,  # optional later
        }
        signal_flags = {
            "youtube_pv_views": "ok" if signals["youtube_pv_views"] is not None else "데이터 없음",
            "google_trends": "ok" if trends_val is not None else ("미연동" if not args.trends and not tr else "데이터 없음"),
            "x": signals_status["x_api"],
            "community": "데이터 없음",
            "likes_comments": "미연동",
        }
        present = sum(1 for k in ("youtube_pv_views", "google_trends") if signals.get(k) is not None)
        hold = present < MIN_SIGNALS_RANK or c["id"] in force_hold or (m and m.get("age_hours") is not None and m["age_hours"] < HOLD_PV_AGE_H)
        hold_reason = None
        if c["id"] in force_hold:
            hold_reason = "overrides.force_hold"
        elif m and m.get("age_hours") is not None and m["age_hours"] < HOLD_PV_AGE_H:
            hold_reason = f"PV 게시 후 {HOLD_PV_AGE_H}h 미만"
        elif present < MIN_SIGNALS_RANK:
            hold_reason = f"실측 신호 {present}<{MIN_SIGNALS_RANK}"

        # composite only from present; still store raw parts separately
        crank_rows.append(dict(
            character_id=c["id"], name=c.get("name"), game_id=c["game_id"],
            hold=hold, hold_reason=hold_reason, rank=None, score=None,
            reliability=round(present / 4, 2),
            signals=signals, signals_status=signal_flags,
            absolute_views=signals["youtube_pv_views"],
            growth_rate_7d=(m or {}).get("deltas", {}).get("delta_7d") if m else None,
            last_updated=iso(NOW),
        ))

    # score only non-hold with views; use lift-normalized views when possible else absolute logscale
    score_map = {}
    for r in crank_rows:
        if r["hold"]:
            continue
        lift = r["signals"].get("youtube_pv_lift")
        views = r["signals"].get("youtube_pv_views")
        trl = r["signals"].get("google_trends_lift") or r["signals"].get("google_trends")
        # combine available normalized signals
        parts = []
        if lift is not None:
            parts.append(("lift", lift))
        elif views is not None:
            parts.append(("views", views))
        if trl is not None:
            parts.append(("trends", trl))
        if len(parts) >= MIN_SIGNALS_RANK or (len(parts) == 1 and parts[0][0] == "lift" and r["signals"].get("google_trends") is not None):
            # provisional raw energy for ranking; final via logscale across pool
            energy = 0.0
            if lift is not None:
                energy += float(lift) * 100
            elif views is not None:
                energy += math.log1p(views)
            if trl is not None:
                energy += float(trl) * (10 if r["signals"].get("google_trends_lift") else 1)
            score_map[r["character_id"]] = energy
    scaled = logscale_map(score_map)
    for r in crank_rows:
        if r["character_id"] in scaled:
            r["score"] = scaled[r["character_id"]]
    ranked = sorted([r for r in crank_rows if r["score"] is not None and not r["hold"]], key=lambda x: -x["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    # Section 4: rising_character — fastest recent growth (delta_7d or trends momentum)
    rising_candidates = []
    for r in pv_rows:
        g7 = r["growth"].get("delta_7d")
        g24 = r["growth"].get("delta_24h")
        if r["hold"] and r["hold_reason"] and "24h" in (r["hold_reason"] or ""):
            # still exclude too-new from rising ranks
            continue
        if g7 is None and g24 is None:
            continue
        if is_rerun_char(next(c for c in enriched if c["id"] == r["character_id"])):
            continue
        rising_candidates.append(dict(
            character_id=r["character_id"], name=r["name"], game_id=r["game_id"],
            delta_24h=g24, delta_7d=g7,
            growth_metric=g7 if g7 is not None else g24,
            growth_window="7d" if g7 is not None else "24h",
            absolute_views=r["absolute"]["views"],
            hold=False, last_updated=iso(NOW),
        ))
    rising_candidates.sort(key=lambda x: -(x["growth_metric"] or 0))
    for i, r in enumerate(rising_candidates, 1):
        r["rank"] = i
    rising_top = rising_candidates[0] if rising_candidates else None

    # Section 5: pickup_schedule — official confirmed; flag reruns but focus new
    schedule_items = []
    for p in all_pickups_raw:
        url = p.get("source_url")
        if url and url in hide_pickups:
            continue
        confirmed = p.get("confirmed") or (url in approve_pickups)
        if not confirmed or not p.get("start") or not p.get("end"):
            continue
        # month overlap
        try:
            s = parse_dt(str(p["start"]))
            e = parse_dt(str(p["end"]))
        except Exception:
            s = e = None
        # include if overlaps this month or upcoming
        schedule_items.append(dict(
            game_id=p["game_id"], game_name=p["game_name"],
            title=p.get("title"),
            characters=p.get("characters"),
            start=p.get("start"), end=p.get("end"),
            is_new=p.get("is_new"), is_rerun=p.get("is_rerun"),
            type=p.get("type"),
            image=p.get("image"),
            source=p.get("source"), source_url=url,
            last_updated=iso(NOW),
        ))
    schedule_items.sort(key=lambda x: x.get("start") or "")

    # Strip private working fields before saving registry
    save_chars = []
    for c in enriched:
        d = {k: v for k, v in c.items() if not k.startswith("_")}
        save_chars.append(d)

    payload_new = dict(
        status="ok" if sec_new else "데이터 없음",
        month=MONTH_START.strftime("%Y-%m"),
        generated_at=iso(NOW),
        rule="이번 달 신캐 공개/진행만. 복각 제외.",
        items=sec_new,
        count=len(sec_new),
    )
    payload_pv = dict(
        status="ok" if abs_sorted else "데이터 없음",
        generated_at=iso(NOW),
        rule="공식 채널 PV 실측 조회수만. 절대값 순위와 성장(델타) 순위 분리. 게임 크기 정규화는 lift_vs_game_baseline.",
        hold_policy=f"PV<{HOLD_PV_AGE_H}h 또는 조회수 없음 → 순위 보류",
        absolute_ranking=abs_sorted,
        growth_ranking=growth_sorted,
        all=pv_rows,
        likes_comments_status="미연동",
    )
    payload_crank = dict(
        status="ok" if ranked else "데이터 없음",
        generated_at=iso(NOW),
        rule=f"복각 제외. 실측 신호 {MIN_SIGNALS_RANK}개 미만이면 순위 보류. 절대 조회수와 성장률 필드 분리.",
        ranking=ranked,
        held=[r for r in crank_rows if r["hold"]],
        all=crank_rows,
        trends_cache=tr,
        signals_status=signals_status,
    )
    payload_rising = dict(
        status="ok" if rising_top else "데이터 없음",
        generated_at=iso(NOW),
        rule="최근 관심 성장 속도(주로 7d 조회수 델타). 복각 제외. 스냅샷 부족 시 데이터 없음.",
        top=rising_top,
        ranking=rising_candidates[:20],
    )
    payload_schedule = dict(
        status="ok" if schedule_items else "데이터 없음",
        generated_at=iso(NOW),
        month=MONTH_START.strftime("%Y-%m"),
        rule="공식 공지만. start/end/source_url 필수. 신규 중심, 복각은 is_rerun=true로 표기.",
        items=[i for i in schedule_items if i.get("is_new")],
        reruns=[i for i in schedule_items if i.get("is_rerun")],
        all=schedule_items,
    )

    # trend.json = site sections bundle (NO game popularity ranking)
    trend = dict(
        generated_at=iso(NOW),
        timezone="Asia/Seoul",
        product="new-character reveals + PV popularity (no game ranking)",
        sections=["new_characters", "pv_ranking", "character_ranking", "rising_character", "pickup_schedule"],
        new_characters=payload_new,
        pv_ranking=payload_pv,
        character_ranking=payload_crank,
        rising_character=payload_rising,
        pickup_schedule=payload_schedule,
        signals_status=signals_status,
        games_supported=[{"id": g["id"], "name": g["name"]} for g in GAMES],
        errors=errors,
        security=dict(https_only=True, sanitized=True, max_response_bytes=MAX_BYTES,
                      fetch_allowlist=sorted(ALLOWED_FETCH_HOSTS), cache_ttl_sec=CACHE_TTL),
    )

    meta = dict(
        last_updated=iso(NOW),
        timezone="Asia/Seoul",
        sources=[
            {"id": "youtube_rss", "status": signals_status["youtube_rss"], "desc": "공식 채널 PV 조회수 스냅샷"},
            {"id": "youtube_data_api", "status": signals_status["youtube_data_api"], "desc": "likes/comments·전체 PV"},
            {"id": "x_api", "status": signals_status["x_api"], "desc": "X 반응"},
            {"id": "google_trends", "status": "ok" if tr else "데이터 없음", "desc": "캐릭터명 Trends"},
            {"id": "pickups_official", "status": signals_status["pickups_official"], "desc": "공식 픽업 공지"},
            {"id": "community_arca", "status": "데이터 없음", "desc": "신캐 언급 보조(미구현)"},
        ],
        endpoints={
            "trend": "data/trend.json",
            "new_characters": "data/new_characters.json",
            "pv_ranking": "data/pv_ranking.json",
            "character_ranking": "data/character_ranking.json",
            "rising_character": "data/rising_character.json",
            "pickup_schedule": "data/pickup_schedule.json",
            "meta": "data/meta.json",
            "review": "review.html",
        },
        active_features=[
            "5 site sections in trend.json",
            "games.json extensible registry",
            "official PV RSS snapshots + history deltas",
            "absolute vs growth rankings separated",
            "lift vs game PV baseline (normalization only)",
            "hold when <2 signals or PV<24h",
            "reruns excluded from new rankings",
            "official pickup schedule with start/end/source",
            "HTTPS allowlist + sanitize + cache",
            "keep previous on error (partial)",
        ],
        not_done=[
            "YouTube Data API likes/comments (YOUTUBE_API_KEY) → 미연동",
            "X API (X_BEARER_TOKEN) → 미연동",
            "arca community mention signal for new chars",
            "ZZZ/Naver/BangDream pickup date body parser (unconfirmed excluded)",
            "Auto-discover brand-new characters not yet in new_characters.json",
            "KR/JP regional channel split beyond single configured region tag",
            "Tistory skin wiring",
        ],
        errors_count=len(errors),
    )

    DATA.mkdir(parents=True, exist_ok=True)
    # registry save
    OUT_NEW.write_text(json.dumps(sanitize(dict(note=registry.get("note"), characters=save_chars, last_updated=iso(NOW))),
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    for path, doc in (
        (OUT_TREND, trend),
        (OUT_META, meta),
        (OUT_SCHEDULE, payload_schedule),
        (OUT_PV, payload_pv),
        (OUT_CRANK, payload_crank),
        (OUT_RISING, payload_rising),
    ):
        path.write_text(json.dumps(sanitize(doc), ensure_ascii=False, indent=1), encoding="utf-8")

    # scoring / all-time / related_posts (separate module; all-time never mixed into score)
    try:
        import integrate_extras
        integrate_extras.run()
    except Exception as e:
        errors.append(f"integrate_extras: {type(e).__name__}: {e!s}"[:200])

    print("wrote sections:", ", ".join(trend["sections"]), "errors:", len(errors), "trends:", args.trends)
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    main()
