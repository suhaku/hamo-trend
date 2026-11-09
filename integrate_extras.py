"""Post-process trend sections: composite scoring, all-time DB, related_posts from Tistory RSS."""
from __future__ import annotations
import json, re, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import feedparser
import requests

from scoring import WEIGHTS, composite_score, log_norm
from alltime import AllTimeDB
from games import GAMES

KST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
BLOG_RSS = "https://news84034.tistory.com/rss"
ALLOWED_POST_HOST = "news84034.tistory.com"
UA = {"User-Agent": "Mozilla/5.0 (HamoTrendBot; +https://news84034.tistory.com)"}


def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")


def safe_post_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme != "https" or (p.hostname or "").lower() != ALLOWED_POST_HOST:
        return None
    return url


def fetch_blog_posts():
    try:
        r = requests.get(BLOG_RSS, headers=UA, timeout=20)
        r.raise_for_status()
        if len(r.content) > 2_000_000:
            return []
        f = feedparser.parse(r.content)
        posts = []
        for e in f.entries[:50]:
            link = safe_post_url(getattr(e, "link", None))
            if not link:
                continue
            posts.append(dict(
                title=(e.title or "")[:200],
                url=link,
                published=getattr(e, "published", None),
            ))
        return posts
    except Exception as e:
        return []


def related_for(name_obj, aliases, game_name, posts):
    keys = []
    if isinstance(name_obj, dict):
        keys.extend([name_obj.get("ko"), name_obj.get("en"), name_obj.get("ja")])
    if isinstance(aliases, dict):
        for lang in ("ko", "en", "ja"):
            keys.extend(aliases.get(lang) or [])
    if game_name:
        keys.append(game_name)
    keys = [k for k in keys if k and len(str(k)) >= 2]
    out = []
    for p in posts:
        title = p["title"]
        if any(str(k).lower() in title.lower() for k in keys):
            out.append(p)
    return out[:5]


X_STATUS_OK = "연동(일 1회)"
_XURL = re.compile(r"^https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/\d{5,25}$")


def _num(v):
    try:
        f = float(v)
        return f if f >= 0 and f == f else None
    except Exception:
        return None


def load_x_signals():
    """Read-only. data/x_signals.json is written by a separate daily job; never written here."""
    path = DATA / "x_signals.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    chars = doc.get("characters") if isinstance(doc, dict) else None
    if not isinstance(chars, dict):
        return None
    out = {}
    for cid, e in chars.items():
        if not isinstance(e, dict):
            continue
        t = e.get("totals") or {}
        likes, reposts, replies = _num(t.get("likes")), _num(t.get("reposts")), _num(t.get("replies"))
        if likes is None and reposts is None and replies is None:
            continue
        posts = [p for p in (e.get("posts") or []) if isinstance(p, dict) and isinstance(p.get("url"), str) and _XURL.match(p["url"])]
        top = max(posts, key=lambda p: (_num(p.get("likes")) or 0) + 2 * (_num(p.get("reposts")) or 0), default=None)
        out[str(cid)[:80]] = dict(
            engagement=(likes or 0) + 2 * (reposts or 0) + (replies or 0),
            totals=dict(likes=likes, reposts=reposts, replies=replies, views=_num(t.get("views"))),
            post_count=len(posts),
            top_post_url=top["url"] if top else None,
            status=str(e.get("status") or "ok")[:40],
        )
    return dict(generated_at=str(doc.get("generated_at") or "")[:40], source=str(doc.get("source") or "")[:120], chars=out)


def x_peer_pool(db, game_id: str, cid: str, current: dict) -> tuple[list[float], str]:
    """Same game's past new characters (recorded x_engagement); fall back to this month's peers."""
    rows = db.conn.execute(
        "SELECT character_id, MAX(value) FROM snapshots WHERE game_id=? AND kind='x_engagement' "
        "AND character_id<>? GROUP BY character_id", (game_id, cid)).fetchall()
    hist = [float(v) for _c, v in rows if v is not None]
    if len(hist) >= 2:
        return hist, "same_game_history"
    return [v["engagement"] for v in current.values()], "month_peers"


def run():
    trend_path = DATA / "trend.json"
    if not trend_path.exists():
        # create minimal shell so first CI run can still write sections after collect
        print("no trend.json yet — extras will run after collect")
        return
    trend = json.loads(trend_path.read_text(encoding="utf-8"))
    posts = fetch_blog_posts()
    game_name = {g["id"]: g["name"] for g in GAMES}

    # Gather peer pools for normalization from pv_ranking absolute views / trends
    pv_all = (trend.get("pv_ranking") or {}).get("all") or []
    crank = (trend.get("character_ranking") or {}).get("all") or []

    # peer pools by signal across current new-char pool
    peer_views = [r.get("absolute", {}).get("views") for r in pv_all if r.get("absolute", {}).get("views") is not None]
    peer_trends = []
    for r in crank:
        v = (r.get("signals") or {}).get("google_trends")
        if v is not None:
            peer_trends.append(v)

    db = AllTimeDB(DATA / "alltime.sqlite")
    at = now_iso()
    xs = load_x_signals()
    month_ids = {r["character_id"] for r in crank}
    x_cur = {k: v for k, v in ((xs or {}).get("chars") or {}).items() if k in month_ids}
    ss_all = trend.setdefault("signals_status", {})
    if xs is not None:
        ss_all["x_api"] = X_STATUS_OK
        ss_all["x_generated_at"] = xs["generated_at"]
    else:
        ss_all.setdefault("x_api", "데이터 없음")

    # Build new character_ranking with composite /100
    new_crank_all = []
    for r in crank:
        cid = r["character_id"]
        gid = r["game_id"]
        pv_row = next((p for p in pv_all if p.get("character_id") == cid), None)
        vid = ((pv_row or {}).get("pv") or {}).get("video_id")
        vmeta = {"video_id": vid} if vid else None
        sig_raw = {
            "youtube_pv": (r.get("signals") or {}).get("youtube_pv_views"),
            "google_trends": (r.get("signals") or {}).get("google_trends"),
            "x": (x_cur.get(cid) or {}).get("engagement"),  # missing → None (not 0)
            "community": None,
        }
        xinfo = x_cur.get(cid)
        # also try lift as youtube_pv alternative peer-normalized already
        lift = (r.get("signals") or {}).get("youtube_pv_lift")
        # prefer absolute views in peer log-norm; lift stored separately
        ss = trend.get("signals_status") or {}
        available = ["youtube_pv"]
        if ss.get("google_trends") != "미연동":
            available.append("google_trends")
        if xinfo is not None:
            available.append("x")
        x_pool, x_basis = (x_peer_pool(db, gid, cid, x_cur) if xinfo else ([], None))
        if xinfo and x_pool and xinfo["engagement"] not in x_pool:
            x_pool = x_pool + [xinfo["engagement"]]
        if ss.get("community_arca") == "ok" and sig_raw.get("community") is not None:
            available.append("community")
        scored = composite_score(
            sig_raw,
            peer_pools={
                "youtube_pv": peer_views,
                "google_trends": peer_trends if peer_trends else ([sig_raw["google_trends"]] if sig_raw["google_trends"] is not None else []),
                "x": x_pool,
            },
            available=available,
        )
        # merge hold reasons
        hold = bool(r.get("hold")) or scored["hold"]
        hold_reason = r.get("hold_reason") or scored.get("hold_reason")
        if scored.get("provisional") and not hold:
            provisional = True
        else:
            provisional = bool(scored.get("provisional"))

        name = r.get("name")
        # find aliases from new_characters registry
        reg = json.loads((DATA / "new_characters.json").read_text(encoding="utf-8"))
        ch = next((c for c in reg.get("characters") or [] if c.get("id") == cid), {})
        rel = related_for(name or ch.get("name"), ch.get("aliases"), game_name.get(gid), posts)

        row = dict(r)
        row.update(dict(
            score=None if hold else scored.get("score"),
            score_raw=scored.get("score_raw"),
            coverage=scored.get("coverage"),
            reliability=scored.get("reliability"),
            provisional=provisional if not hold else False,
            hold=hold,
            hold_reason=hold_reason,
            used_weights=scored.get("used_weights"),
            present_signals=scored.get("present_signals"),
            signal_scores_0_100=scored.get("signal_scores_0_100"),
            weights_config=WEIGHTS,
            lift_vs_game_baseline=lift,
            absolute_views=sig_raw["youtube_pv"],
            pv_video_id=vid,
            related_posts=rel,
            last_updated=at,
        ))
        sigs = dict(row.get("signals") or {})
        sigs["x"] = xinfo["totals"] if xinfo else None
        row["signals"] = sigs
        sst = dict(row.get("signals_status") or {})
        sst["x"] = X_STATUS_OK if xinfo else "데이터 없음"
        row["signals_status"] = sst
        row["x"] = dict(status=X_STATUS_OK, generated_at=xs["generated_at"], engagement=xinfo["engagement"],
                        normalized_against=x_basis, post_count=xinfo["post_count"],
                        top_post_url=xinfo["top_post_url"]) if xinfo else dict(status="데이터 없음")
        new_crank_all.append(row)
        if xinfo:
            db.add_snapshot(at, cid, gid, "x_engagement", "now", xinfo["engagement"],
                            meta={"source": "x_signals.json", "generated_at": xs["generated_at"]})

        # all-time snapshots (never into current score)
        is_rerun = False
        is_new = True
        pk = ch.get("pickup") or {}
        if pk.get("is_rerun") or ch.get("is_new") is False:
            is_rerun = True
            is_new = False
        db.upsert_character(cid, gid, name or ch.get("name"), is_new, is_rerun, ch.get("reveal_date"), at)
        if sig_raw["youtube_pv"] is not None:
            db.add_snapshot(at, cid, gid, "pv_views", "now", sig_raw["youtube_pv"], meta=vmeta)
        if lift is not None:
            db.add_snapshot(at, cid, gid, "lift", "now", lift)
        if sig_raw["google_trends"] is not None:
            db.add_snapshot(at, cid, gid, "trends", "now", sig_raw["google_trends"])
            db.add_snapshot(at, cid, gid, "trends", "peak", sig_raw["google_trends"])
        # recorded deltas if present on pv row
        if pv_row:
            g = pv_row.get("growth") or {}
            for win, key in (("delta_24h", "delta_24h"), ("delta_7d", "delta_7d"), ("delta_30d", "delta_30d")):
                if g.get(key) is not None:
                    db.add_snapshot(at, cid, gid, "pv_views", win, g[key], meta=vmeta)
        if scored.get("score_raw") is not None:
            db.add_snapshot(at, cid, gid, "score", "now", scored["score_raw"],
                            meta={"coverage": scored.get("coverage"), "not_in_alltime_mix": True})

    # attach related_posts to new_characters + pv rows too
    for item in (trend.get("new_characters") or {}).get("items") or []:
        ch = next((c for c in (json.loads((DATA/"new_characters.json").read_text(encoding="utf-8")).get("characters") or [])
                   if c.get("id") == item.get("id")), {})
        item["related_posts"] = related_for(item.get("name"), ch.get("aliases"), game_name.get(item.get("game_id")), posts)
        xi = x_cur.get(item.get("id"))
        item["x_top_post_url"] = xi["top_post_url"] if xi else None
        item["x_status"] = X_STATUS_OK if xi else "데이터 없음"

    for r in pv_all:
        ch = next((c for c in (json.loads((DATA/"new_characters.json").read_text(encoding="utf-8")).get("characters") or [])
                   if c.get("id") == r.get("character_id")), {})
        r["related_posts"] = related_for(r.get("name"), ch.get("aliases"), game_name.get(r.get("game_id")), posts)

    # pickups into alltime history
    for p in (trend.get("pickup_schedule") or {}).get("all") or []:
        db.add_pickup(
            character_id=None, game_id=p["game_id"], start=p.get("start"), end=p.get("end"),
            is_rerun=bool(p.get("is_rerun")), source_url=p.get("source_url"), title=p.get("title"),
            recorded_at=at,
        )

    # rank by score
    ranked = sorted([r for r in new_crank_all if r.get("score") is not None and not r.get("hold")],
                    key=lambda x: -x["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
        db.add_snapshot(at, r["character_id"], r["game_id"], "rank", "now", i)

    db.commit()

    # all-time export + per-character detail pages
    alltime_path = DATA / "alltime.json"
    db.export_alltime_json(alltime_path, [g["id"] for g in GAMES])
    detail_dir = DATA / "characters"
    detail_dir.mkdir(parents=True, exist_ok=True)
    for r in new_crank_all:
        cid = r["character_id"]
        detail = db.character_detail(cid)
        # merge current score block (separate from all-time)
        detail["current"] = dict(
            score=r.get("score"), score_raw=r.get("score_raw"),
            coverage=r.get("coverage"), reliability=r.get("reliability"),
            provisional=r.get("provisional"), hold=r.get("hold"),
            hold_reason=r.get("hold_reason"),
            absolute_views=r.get("absolute_views"),
            lift_vs_game_baseline=r.get("lift_vs_game_baseline"),
            growth_rate_7d=r.get("growth_rate_7d"),
            related_posts=r.get("related_posts"),
            last_updated=at,
            note="current score does not include all-time records",
        )
        # same-game past new chars at comparable windows: only recorded snapshots
        detail_path = detail_dir / f"{cid}.json"
        detail_path.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")

    trend["character_ranking"] = dict(
        status="ok" if ranked else "데이터 없음",
        generated_at=at,
        rule="100점 만점 · 없는 지표는 0점 처리 없이 나머지로 다시 계산 · 수집률이 낮으면 잠정 또는 보류 · 신뢰도 따로 표시 · 복각 제외 · 역대 기록은 점수에 안 넣음",
        weights=WEIGHTS,
        ranking=ranked,
        held=[r for r in new_crank_all if r.get("hold")],
        all=new_crank_all,
        signals_status=trend.get("signals_status"),
    )
    # strip heavy trends_cache from public if present
    if "trends_cache" in (trend.get("character_ranking") or {}):
        pass

    trend["alltime"] = dict(
        status="ok",
        detail_url="data/alltime.json",
        characters_dir="data/characters/",
        note="올타임 기록은 현재 점수에 합산하지 않음. 스냅샷 없는 과거 윈도우=null.",
    )
    trend["generated_at"] = at
    trend_path.write_text(json.dumps(trend, ensure_ascii=False, indent=1), encoding="utf-8")

    # also refresh split files
    (DATA / "character_ranking.json").write_text(json.dumps(trend["character_ranking"], ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "pv_ranking.json").write_text(json.dumps(trend.get("pv_ranking"), ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "new_characters_month.json").write_text(json.dumps(trend.get("new_characters"), ensure_ascii=False, indent=1), encoding="utf-8")

    # update meta
    meta_path = DATA / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["last_updated"] = at
    meta.setdefault("endpoints", {})
    meta["endpoints"].update({
        "alltime": "data/alltime.json",
        "character_detail_dir": "data/characters/{id}.json",
        "new_characters_month": "data/new_characters_month.json",
    })
    af = list(meta.get("active_features") or [])
    for feat in [
        "composite score /100 with renormalized weights",
        "coverage % + provisional/hold + separate reliability",
        "all-time SQLite append-only (not in current score)",
        "per-character detail JSON",
        "related_posts from news84034 RSS (read-only)",
    ]:
        if feat not in af:
            af.append(feat)
    meta["active_features"] = af
    nd = list(meta.get("not_done") or [])
    for x in [
        "X/community signals still 미연동 → coverage lower until secrets/impl",
        "Past 24h/7d/30d before first snapshot remain null (unknowable)",
        "Same-game past-new-char window comparison densifies as history accrues",
        "Tistory main-page analytics area (skin) — not in this repo",
    ]:
        if x not in nd:
            nd.append(x)
    meta["not_done"] = nd
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("integrate_extras OK", "ranked", len(ranked), "posts", len(posts))


if __name__ == "__main__":
    run()
