#!/usr/bin/env python3
"""Hamo's Game Trend collector. Real fetched data only; missing -> None ("데이터 없음").

Security:
- HTTPS-only fetches to an allowlisted set of official domains
- Response size limit
- Output JSON is recursively sanitized (HTML/scripts stripped; URLs must be https + allowlisted)
- No secrets/tokens used or written
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import time
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from games import GAMES, WEIGHTS

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
UA = {"User-Agent": "Mozilla/5.0 (HamoTrendBot; +https://news84034.tistory.com)"}
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data", "trend.json")
HIST = os.path.join(BASE, "data", "history")
PICKUP_RE = re.compile(r"기원|워프|채널|픽업|모집|기간 한정|복각|뽑기|헤드헌팅|소환|공명|조달|ガチャ|확률 UP|UP!")
MAX_BYTES = 2_000_000  # 2 MiB per response
errors: list[str] = []

# Strict fetch allowlist (hostnames only). Subdomains must be listed explicitly.
ALLOWED_FETCH_HOSTS = frozenset({
    "www.youtube.com",
    "arca.live",
    "sg-hk4e-api.hoyoverse.com",
    "sg-hkrpg-api.hoyoverse.com",
    "bbs-api-os.hoyolab.com",
    "comm-api.game.naver.com",
    "bang-dream.bushimo.jp",
    # pytrends hits these; we do not wrap pytrends with get(), documented separately
    "trends.google.com",
})

# Hosts permitted in output URL / image fields (https only).
ALLOWED_OUTPUT_HOSTS = frozenset({
    "www.youtube.com",
    "youtu.be",
    "i.ytimg.com",
    "arca.live",
    "sg-hk4e-api.hoyoverse.com",
    "sg-hkrpg-api.hoyoverse.com",
    "bbs-api-os.hoyolab.com",
    "www.hoyolab.com",
    "hoyolab.com",
    "comm-api.game.naver.com",
    "game.naver.com",
    "bang-dream.bushimo.jp",
    # HoYoverse / miHoYo CDN for notice banners
    "webstatic.hoyoverse.com",
    "sdk-os-static.hoyoverse.com",
    "upload-os-bbs.hoyolab.com",
    "img-os-bbs.hoyolab.com",
    "act-webstatic.hoyoverse.com",
    "fastcdn.hoyoverse.com",
    # Naver lounge images
    "ssl.pstatic.net",
    "phinf.pstatic.net",
    "nng-phinf.pstatic.net",
})

SCRIPTISH = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|link|meta|style|svg|math)[^>]*>.*?</\s*\1\s*>|"
    r"<\s*(script|iframe|object|embed|link|meta)\b[^>]*/?\s*>|"
    r"javascript\s*:|"
    r"data\s*:|"
    r"on[a-z]+\s*="
)


def iso(t: dt.datetime) -> str:
    return t.astimezone(KST).isoformat(timespec="seconds")


def _host_ok(host: str, allowed: frozenset[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in allowed:
        return True
    # allow exact subdomain match only if listed; no wildcard
    return False


def safe_url(url: str | None, *, for_output: bool = False) -> str | None:
    """Return url if https + allowlisted host, else None."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme != "https":
        return None
    allowed = ALLOWED_OUTPUT_HOSTS if for_output else ALLOWED_FETCH_HOSTS
    if not _host_ok(p.hostname or "", allowed):
        return None
    # block credentials in URL
    if p.username or p.password:
        return None
    return url


def get(url: str, **kw) -> requests.Response:
    clean = safe_url(url, for_output=False)
    if not clean:
        raise ValueError(f"blocked URL (not https allowlisted): {url[:120]}")
    timeout = kw.pop("timeout", 20)
    headers = {**UA, **kw.pop("headers", {})}
    # stream to enforce size
    with requests.get(clean, headers=headers, timeout=timeout, stream=True, **kw) as r:
        r.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"response too large (>{MAX_BYTES} bytes): {clean[:80]}")
            chunks.append(chunk)
        content = b"".join(chunks)
    # rebuild a simple Response-like object
    out = requests.Response()
    out.status_code = r.status_code
    out.headers = r.headers
    out.url = r.url
    out._content = content
    out.encoding = r.encoding or "utf-8"
    return out


def strip_html(h: str | None) -> str:
    if not h:
        return ""
    text = SCRIPTISH.sub(" ", h)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2000]  # hard cap on text fields


def sanitize(obj):
    """Recursively sanitize JSON-serializable data for safe blog consumption."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        # if it looks like a URL, validate; else strip HTML
        if obj.startswith("http://") or obj.startswith("https://") or obj.startswith("//"):
            return safe_url(obj if obj.startswith("https://") else None, for_output=True)
        return strip_html(obj)
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = strip_html(str(k))[:80]
            if key in ("source_url", "url", "image", "thumb", "link") or key.endswith("_url"):
                out[key] = safe_url(v, for_output=True) if isinstance(v, str) else None
            else:
                out[key] = sanitize(v)
        return out
    return strip_html(str(obj))[:500]


# ---------- YouTube (public RSS: latest 15 videos incl. view counts) ----------
def youtube(g):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={g['yt']}"
    f = feedparser.parse(get(url).content)
    vids = []
    for e in f.entries:
        pub = dt.datetime.fromisoformat(e.published)
        views = int(e.get("media_statistics", {}).get("views", 0))
        vid = getattr(e, "yt_videoid", None) or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
            continue
        link = safe_url(getattr(e, "link", None), for_output=True)
        vids.append(dict(
            id=vid,
            title=strip_html(e.title)[:200],
            published=iso(pub),
            views=views,
            thumb=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            url=link,
        ))
    week = [v for v in vids if NOW - dt.datetime.fromisoformat(v["published"]) <= dt.timedelta(days=7)]
    char_pv = [v for v in vids if re.search(r"캐릭터|PV|티저|예고|Character|Trailer|Teaser", v["title"], re.I)]
    return dict(
        source=url,
        fetched_at=iso(NOW),
        channel=strip_html(f.feed.get("title"))[:120],
        views_7d_new_videos=sum(v["views"] for v in week),
        new_videos_7d=len(week),
        latest=vids[:5],
        character_videos=char_pv[:8],
        note="RSS는 최신 15개 영상만 제공. 조회수는 수집 시점 누적값.",
    )


# ---------- Community (arca.live list page; robots.txt allows /b/) ----------
def community(g):
    slug = g["arca"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", slug):
        raise ValueError("bad arca slug")
    url = f"https://arca.live/b/{slug}"
    s = BeautifulSoup(get(url).text, "html.parser")
    times = []
    for row in s.select("a.vrow.column"):
        if "notice" in (row.get("class") or []):
            continue
        t = row.select_one("time[datetime]")
        if t:
            times.append(dt.datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")))
    times.sort(reverse=True)
    if len(times) < 5:
        return dict(source=url, fetched_at=iso(NOW), posts_per_hour=None, note="글 수 부족")
    span_h = max((times[0] - times[-1]).total_seconds() / 3600, 0.05)
    return dict(
        source=url,
        fetched_at=iso(NOW),
        posts_per_hour=round(len(times) / span_h, 2),
        sample=len(times),
        note="아카라이브 채널 첫 페이지(공지 제외) 글 작성 속도",
    )


# ---------- Google Trends (pytrends, KR, 30 days; batches anchored on 원신) ----------
def trends():
    from pytrends.request import TrendReq

    p = TrendReq(hl="ko-KR", tz=-540, timeout=(10, 30))
    anchor = "원신"
    others = [g["trends"] for g in GAMES if g["trends"] != anchor]
    series: dict = {}
    dates: list[str] = []
    for i in range(0, len(others), 4):
        kws = [anchor] + others[i : i + 4]
        p.build_payload(kws, timeframe="today 1-m", geo="KR")
        df = p.interest_over_time()
        if df.empty:
            continue
        a = df[anchor].astype(float)
        ref = a.mean() or 1.0
        if anchor not in series:
            series[anchor] = df[anchor].tolist()
        series_mean = sum(series[anchor]) / len(series[anchor])
        scale = series_mean / ref if ref else 1
        for k in kws[1:]:
            series[k] = [round(v * scale, 2) for v in df[k].tolist()]
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        time.sleep(3)
    return dict(
        source="Google Trends (pytrends, geo=KR, today 1-m)",
        fetched_at=iso(NOW),
        dates=dates,
        series=series,
        note="상대값(원신 기준으로 묶음 간 스케일 맞춤). 절대 검색량 아님. 마지막 날은 부분 데이터.",
    )


# ---------- Pickups (official sources only) ----------
def hoyo_ann(g, cfg):
    base = {
        "hk4e": ("sg-hk4e-api.hoyoverse.com", "hk4e_global", "os_asia", "100000000"),
        "hkrpg": ("sg-hkrpg-api.hoyoverse.com", "hkrpg_global", "prod_official_asia", "800000000"),
    }[cfg["game"]]
    q = (
        f"game={cfg['game']}&game_biz={base[1]}&lang=ko-kr&bundle_id={base[1]}"
        f"&platform=pc&region={base[2]}&level=60&uid={base[3]}&channel_id=1"
    )
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
        chars = sorted(set(re.findall(r"「([^」]{1,20})」\s*\(", body + " " + title)))
        img = safe_url(a.get("banner") or a.get("img"), for_output=True)
        out.append(dict(
            title=title[:200],
            start=a.get("start_time"),
            end=a.get("end_time"),
            tz="UTC+8 (HoYoverse 서버 공지 기준)",
            characters=chars or None,
            image=img,
            type=("복각" if "복각" in body[:400] else None),
            source=f"HoYoverse 공식 인게임 공지 API ann_id={a['ann_id']}",
            source_url=lst.split("?")[0],  # drop query (no secrets, but shorter/cleaner)
        ))
    return out


def hoyolab_news(g, cfg):
    gid = int(cfg["gid"])
    url = f"https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList?gids={gid}&type=1&page_size=30"
    L = get(url, headers={"x-rpc-language": "ko-kr"}).json()["data"]["list"]
    out = []
    for p in L:
        t = strip_html(p["post"]["subject"])
        if re.search(r"채널|신규 에이전트|확률 UP|독점", t):
            img = safe_url((p.get("image_list") or [{}])[0].get("url"), for_output=True)
            pid = str(p["post"]["post_id"])
            if not pid.isdigit():
                continue
            out.append(dict(
                title=t[:200],
                start=None,
                end=None,
                characters=None,
                image=img,
                posted=iso(dt.datetime.fromtimestamp(int(p["post"]["created_at"]), KST)),
                source="HoYoLAB 공식 계정 공지",
                source_url=f"https://www.hoyolab.com/article/{pid}",
            ))
    return out


DATE_RE = re.compile(r"(\d{4}[.\-/년]\s*)?(\d{1,2})\s*[월./]\s*(\d{1,2})\s*일?")


def naver_lounge(g, cfg):
    lounge = cfg["lounge"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", lounge):
        raise ValueError("bad lounge id")
    out = []
    for b in range(1, 21):
        url = (
            f"https://comm-api.game.naver.com/nng_main/v1/community/lounge/{lounge}/feed"
            f"?boardId={b}&buffFilteringYN=N&limit=15&offset=0&order=NEW"
        )
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
            if NOW - created > dt.timedelta(days=45):
                continue
            body = ""
            try:
                doc = json.loads(f["feed"].get("contents") or "{}")
                body = " ".join(re.findall(r'"value"\s*:\s*"([^"]*)"', json.dumps(doc, ensure_ascii=False)))
                body = strip_html(body)
            except Exception:
                pass
            dates = [m.group(0) for m in DATE_RE.finditer(t + " " + body)][:4]
            feed_link = safe_url((f.get("feedLink") or {}).get("pc"), for_output=True)
            img = safe_url(f["feed"].get("repImageUrl"), for_output=True)
            out.append(dict(
                title=t[:200],
                posted=iso(created),
                dates_found=dates or None,
                start=None,
                end=None,
                type=("복각" if "복각" in t else ("신규" if "신규" in t else None)),
                image=img,
                board=strip_html(f["board"]["boardName"])[:80],
                source="네이버 게임 라운지 공식 GM 공지",
                source_url=feed_link,
            ))
    seen: set = set()
    uniq = []
    for p in out:
        key = p["source_url"] or p["title"]
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def wp_json(g, cfg):
    base = cfg["base"].rstrip("/")
    if not safe_url(base, for_output=False):
        raise ValueError("bad wp base")
    url = f"{base}/wp-json/wp/v2/posts?per_page=10&search=%E3%82%AC%E3%83%81%E3%83%A3&_fields=date,title,link"
    return [
        dict(
            title=strip_html(p["title"]["rendered"])[:200],
            posted=p["date"] + "+09:00",
            start=None,
            end=None,
            characters=None,
            source="BanG Dream! GBP 일본 공식 사이트 뉴스",
            source_url=safe_url(p["link"], for_output=True),
        )
        for p in get(url).json()
    ]


def pickups(g):
    cfg = g["pickup"]
    k = cfg["kind"]
    if k == "none":
        return dict(status="데이터 없음", note=cfg["note"], items=[])
    items = {
        "hoyo_ann": hoyo_ann,
        "hoyolab_news": hoyolab_news,
        "naver_lounge": naver_lounge,
        "wp_json": wp_json,
    }[k](g, cfg)
    return dict(status="ok", fetched_at=iso(NOW), items=items)


# ---------- Scoring ----------
def logscale(vals):
    vs = {k: v for k, v in vals.items() if v is not None and v >= 0}
    if not vs:
        return {}
    m = max(math.log1p(v) for v in vs.values()) or 1
    return {k: round(100 * math.log1p(v) / m, 1) for k, v in vs.items()}


def score(results, tr):
    yt = logscale({g: r["youtube"]["views_7d_new_videos"] if r.get("youtube") else None for g, r in results.items()})
    cm = logscale({g: r["community"]["posts_per_hour"] if r.get("community") else None for g, r in results.items()})
    srch, mom = {}, {}
    if tr:
        avg7 = {}
        for g in GAMES:
            s = tr["series"].get(g["trends"])
            if not s or len(s) < 15:
                continue
            last7, prev7 = s[-8:-1], s[-15:-8]
            avg7[g["id"]] = sum(last7) / 7
            p = sum(prev7) / 7
            if p > 0:
                mom[g["id"]] = round(max(0, min(100, 50 + 50 * (avg7[g["id"]] / p - 1))), 1)
        srch = logscale(avg7)
    sig = dict(youtube=yt, x={}, community=cm, search=srch, momentum=mom)
    out = {}
    for g in GAMES:
        parts = {k: sig[k].get(g["id"]) for k in WEIGHTS}
        have = {k: v for k, v in parts.items() if v is not None}
        if len(have) < 2:
            out[g["id"]] = dict(score=None, signals=parts, used_weights=None)
            continue
        wsum = sum(WEIGHTS[k] for k in have)
        w = {k: round(WEIGHTS[k] / wsum, 3) for k in have}
        out[g["id"]] = dict(score=max(1, round(sum(have[k] * w[k] for k in have))), signals=parts, used_weights=w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trends", action="store_true", help="Fetch Google Trends (slow; once/day in CI)")
    ap.add_argument("--skip-trends", action="store_true", help="Explicitly skip Trends")
    args = ap.parse_args()
    do_trends = args.trends and not args.skip_trends

    results = {}
    for g in GAMES:
        r = {}
        for name, fn in (("youtube", youtube), ("community", community), ("pickups", pickups)):
            try:
                r[name] = fn(g)
            except Exception as e:
                r[name] = None
                errors.append(f"{g['id']}.{name}: {type(e).__name__}: {e!s}"[:200])
            time.sleep(1)
        results[g["id"]] = r

    tr = None
    if do_trends:
        try:
            tr = trends()
        except Exception as e:
            errors.append(f"trends: {type(e).__name__}: {e!s}"[:200])
    else:
        # Preserve previous Trends series from existing trend.json if present
        if os.path.exists(OUT):
            try:
                prev_data = json.load(open(OUT))
                dates = prev_data.get("trends_dates")
                series = {}
                for g in prev_data.get("games") or []:
                    if g.get("trends_series") is not None:
                        # map back via game id -> trends keyword
                        for gg in GAMES:
                            if gg["id"] == g["id"]:
                                series[gg["trends"]] = g["trends_series"]
                                break
                if dates and series:
                    tr = dict(
                        source="Google Trends (cached from previous run)",
                        fetched_at=prev_data.get("generated_at"),
                        dates=dates,
                        series=series,
                        note="캐시된 Trends (일 1회 갱신). " + (prev_data.get("missing_note") or ""),
                    )
            except Exception as e:
                errors.append(f"trends_cache: {type(e).__name__}: {e!s}"[:200])

    sc = score(results, tr)
    prev = None
    yday = os.path.join(HIST, (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%d") + ".json")
    if os.path.exists(yday):
        prev = json.load(open(yday))
    games = []
    for g in GAMES:
        s = sc[g["id"]]
        p = (prev or {}).get("scores", {}).get(g["id"])
        chg = round((s["score"] - p) / p * 100, 1) if (s["score"] and p) else None
        games.append(dict(
            id=g["id"],
            name=g["name"],
            score=s["score"],
            change_vs_prev_day_pct=chg,
            signals=s["signals"],
            used_weights=s["used_weights"],
            **results[g["id"]],
            trends_series=(tr or {}).get("series", {}).get(g["trends"]),
        ))
    ranked = sorted([x for x in games if x["score"]], key=lambda x: -x["score"])
    data = dict(
        generated_at=iso(NOW),
        timezone="Asia/Seoul",
        weights=WEIGHTS,
        signals_available=dict(
            youtube=True,
            x=False,
            community=True,
            search=tr is not None,
            momentum=tr is not None,
        ),
        missing_note="X(트위터) 반응량: API 키 없음 -> 데이터 없음. 가중치는 사용 가능한 신호로 재정규화.",
        trends_dates=(tr or {}).get("dates"),
        top5=[x["id"] for x in ranked[:5]],
        rising=(max(games, key=lambda x: x["signals"].get("momentum") or -1)["id"] if tr else None),
        character_ranking=dict(
            status="데이터 없음",
            reason="캐릭터별 신호(X 언급량/트렌드 캐릭터명 검색) 미구현 단계",
        ),
        games=games,
        errors=errors,
        security=dict(
            https_only=True,
            fetch_allowlist=sorted(ALLOWED_FETCH_HOSTS),
            output_url_allowlist=sorted(ALLOWED_OUTPUT_HOSTS),
            max_response_bytes=MAX_BYTES,
            sanitized=True,
        ),
    )
    data = sanitize(data)
    os.makedirs(HIST, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HIST, NOW.strftime("%Y-%m-%d") + ".json"), "w", encoding="utf-8") as f:
        json.dump(
            dict(generated_at=data["generated_at"], scores={x["id"]: x["score"] for x in games}),
            f,
            ensure_ascii=False,
        )
    print("wrote", OUT, "errors:", len(errors), "trends:", do_trends)
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    main()
