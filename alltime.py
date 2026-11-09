"""Append-only all-time records (SQLite). Never mixes into current composite score."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  character_id TEXT NOT NULL,
  game_id TEXT NOT NULL,
  kind TEXT NOT NULL,           -- pv_views | trends | lift | score | rank
  window TEXT,                  -- now | 24h | 7d | 30d | peak | null
  value REAL,
  meta_json TEXT,
  UNIQUE(at, character_id, kind, window)
);
CREATE INDEX IF NOT EXISTS idx_snap_char ON snapshots(character_id, kind, at);
CREATE TABLE IF NOT EXISTS character_meta (
  character_id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL,
  name_json TEXT,
  is_new INTEGER,
  is_rerun INTEGER,
  reveal_date TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS pickup_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id TEXT,
  game_id TEXT NOT NULL,
  start TEXT,
  end TEXT,
  is_rerun INTEGER,
  source_url TEXT,
  title TEXT,
  recorded_at TEXT NOT NULL,
  UNIQUE(game_id, source_url, start)
);
"""


class AllTimeDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert_character(self, character_id, game_id, name, is_new, is_rerun, reveal_date, at):
        cur = self.conn.execute("SELECT first_seen_at FROM character_meta WHERE character_id=?", (character_id,))
        row = cur.fetchone()
        first = row[0] if row else at
        self.conn.execute(
            """INSERT INTO character_meta(character_id,game_id,name_json,is_new,is_rerun,reveal_date,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(character_id) DO UPDATE SET
                 game_id=excluded.game_id,
                 name_json=excluded.name_json,
                 is_new=excluded.is_new,
                 is_rerun=excluded.is_rerun,
                 reveal_date=COALESCE(excluded.reveal_date, character_meta.reveal_date),
                 last_seen_at=excluded.last_seen_at
            """,
            (character_id, game_id, json.dumps(name, ensure_ascii=False),
             1 if is_new else 0, 1 if is_rerun else 0, reveal_date, first, at),
        )

    def add_snapshot(self, at, character_id, game_id, kind, window, value, meta=None):
        if value is None:
            return
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO snapshots(at,character_id,game_id,kind,window,value,meta_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (at, character_id, game_id, kind, window, float(value),
                 json.dumps(meta, ensure_ascii=False) if meta else None),
            )
        except sqlite3.Error:
            pass

    def add_pickup(self, *, character_id, game_id, start, end, is_rerun, source_url, title, recorded_at):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO pickup_history(character_id,game_id,start,end,is_rerun,source_url,title,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (character_id, game_id, start, end, 1 if is_rerun else 0, source_url, title, recorded_at),
            )
        except sqlite3.Error:
            pass

    def commit(self):
        self.conn.commit()

    def alltime_for_game(self, game_id: str) -> dict:
        """Verifiable aggregates only. Unknowable past windows => omitted/null."""
        chars = self.conn.execute(
            "SELECT character_id,name_json,is_new,is_rerun,reveal_date FROM character_meta WHERE game_id=? AND is_new=1",
            (game_id,),
        ).fetchall()
        ranking = []
        for cid, name_json, is_new, is_rerun, reveal in chars:
            peak_views = self.conn.execute(
                "SELECT MAX(value) FROM snapshots WHERE character_id=? AND kind='pv_views' AND window='now'",
                (cid,),
            ).fetchone()[0]
            peak_lift = self.conn.execute(
                "SELECT MAX(value) FROM snapshots WHERE character_id=? AND kind='lift' AND window='now'",
                (cid,),
            ).fetchone()[0]
            peak_trends = self.conn.execute(
                "SELECT MAX(value) FROM snapshots WHERE character_id=? AND kind='trends' AND (window='peak' OR window='now')",
                (cid,),
            ).fetchone()[0]
            # historical 24h/7d/30d only if we stored them at the time — never backfill inventively
            d24 = self.conn.execute(
                "SELECT value FROM snapshots WHERE character_id=? AND kind='pv_views' AND window='delta_24h' ORDER BY at DESC LIMIT 1",
                (cid,),
            ).fetchone()
            d7 = self.conn.execute(
                "SELECT value FROM snapshots WHERE character_id=? AND kind='pv_views' AND window='delta_7d' ORDER BY at DESC LIMIT 1",
                (cid,),
            ).fetchone()
            d30 = self.conn.execute(
                "SELECT value FROM snapshots WHERE character_id=? AND kind='pv_views' AND window='delta_30d' ORDER BY at DESC LIMIT 1",
                (cid,),
            ).fetchone()
            ranking.append(dict(
                character_id=cid,
                name=json.loads(name_json) if name_json else None,
                is_new=bool(is_new), is_rerun=bool(is_rerun),
                reveal_date=reveal,
                peak_pv_views=peak_views,
                peak_lift=peak_lift,
                peak_trends=peak_trends,
                recorded_delta_24h=d24[0] if d24 else None,
                recorded_delta_7d=d7[0] if d7 else None,
                recorded_delta_30d=d30[0] if d30 else None,
                note="과거 윈도우 값은 당시 스냅샷이 있을 때만. 소급 추정 없음.",
            ))
        ranking.sort(key=lambda x: -(x["peak_pv_views"] or -1))
        for i, r in enumerate(ranking, 1):
            r["alltime_rank"] = i if r["peak_pv_views"] is not None else None
        top = ranking[0] if ranking and ranking[0].get("peak_pv_views") is not None else None
        pickups = [dict(character_id=r[0], start=r[1], end=r[2], is_rerun=bool(r[3]), source_url=r[4], title=r[5])
                   for r in self.conn.execute(
                       "SELECT character_id,start,end,is_rerun,source_url,title FROM pickup_history WHERE game_id=? ORDER BY start DESC",
                       (game_id,),
                   ).fetchall()]
        return dict(
            game_id=game_id,
            alltime_new_character_ranking=ranking,
            alltime_top_character=top,
            pickup_rerun_history=pickups,
        )

    def character_detail(self, character_id: str) -> dict:
        meta = self.conn.execute(
            "SELECT game_id,name_json,is_new,is_rerun,reveal_date,first_seen_at,last_seen_at FROM character_meta WHERE character_id=?",
            (character_id,),
        ).fetchone()
        if not meta:
            return dict(character_id=character_id, status="데이터 없음")
        game_id = meta[0]
        rows = self.conn.execute(
            "SELECT at,kind,window,value,meta_json FROM snapshots WHERE character_id=? ORDER BY at",
            (character_id,),
        ).fetchall()
        snaps = [(a, k, w, v) for a, k, w, v, _m in rows]

        def _vid(m):
            try:
                return (json.loads(m) or {}).get("video_id") if m else None
            except Exception:
                return None
        # every PV point carries its video_id; a change of tracked PV starts a new series
        pv_series = [dict(at=a, video_id=_vid(m), views=v) for a, k, w, v, m in rows if k == "pv_views" and w == "now"]
        pv_series_by_video = []
        for pt in pv_series:
            if not pv_series_by_video or pv_series_by_video[-1]["video_id"] != pt["video_id"]:
                pv_series_by_video.append(dict(video_id=pt["video_id"], started_at=pt["at"], points=[]))
            pv_series_by_video[-1]["points"].append(dict(at=pt["at"], views=pt["views"]))
        trends_series = [dict(at=a, value=v) for a, k, w, v in snaps if k == "trends" and w in ("now", "peak")]
        # past chars comparison: same game, peak views
        peers = self.alltime_for_game(game_id)["alltime_new_character_ranking"]
        me = next((p for p in peers if p["character_id"] == character_id), None)
        return dict(
            character_id=character_id,
            game_id=game_id,
            name=json.loads(meta[1]) if meta[1] else None,
            is_new=bool(meta[2]), is_rerun=bool(meta[3]),
            reveal_date=meta[4], first_seen_at=meta[5], last_seen_at=meta[6],
            alltime_rank=me.get("alltime_rank") if me else None,
            peak_pv_views=me.get("peak_pv_views") if me else None,
            peak_lift=me.get("peak_lift") if me else None,
            peak_trends=me.get("peak_trends") if me else None,
            recorded_deltas=dict(
                d24h=me.get("recorded_delta_24h") if me else None,
                d7d=me.get("recorded_delta_7d") if me else None,
                d30d=me.get("recorded_delta_30d") if me else None,
            ),
            pv_growth_series=pv_series,
            pv_series_by_video=pv_series_by_video,
            pv_series_note="video_id가 바뀌면 새 시리즈. video_id=null은 video_id 기록 이전 스냅샷(다른 PV와 섞어 그리지 말 것).",
            trends_series=trends_series,
            comparison_to_past_new_chars=[
                dict(character_id=p["character_id"], name=p["name"], peak_pv_views=p["peak_pv_views"],
                     alltime_rank=p["alltime_rank"])
                for p in peers[:15]
            ],
            note="all-time는 현재 점수에 합산하지 않음. 스냅샷 없는 과거 윈도우는 null.",
        )

    def export_alltime_json(self, out_path: Path, game_ids: list[str]):
        doc = dict(
            generated_note="All-time records. Never added into current composite score.",
            games={gid: self.alltime_for_game(gid) for gid in game_ids},
        )
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
