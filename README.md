# hamo-trend

Public JSON for Hamo's (news84034) **new-character** analytics area on the Tistory main page.
**No game-level popularity ranking. No Reddit. Skin edits are out of scope for this repo.**

Live: https://suhaku.github.io/hamo-trend/data/trend.json  
Review: https://suhaku.github.io/hamo-trend/review.html

## Five sections (`data/trend.json`)
1. `new_characters` — revealed/active **new** chars this month (reruns excluded)
2. `pv_ranking` — official YouTube PV views; **absolute** and **growth** rankings separate; game-size lift separate
3. `character_ranking` — composite **/100** (YT PV 35, Trends 25, X 20, community 20). Missing ≠ 0 → renormalize; `coverage` %; low coverage → provisional/hold; `reliability` separate
4. `rising_character` — fastest recent growth
5. `pickup_schedule` — official pickups with start/end/source; new vs rerun flagged

Also: `data/alltime.json` + `data/alltime.sqlite` + `data/characters/{id}.json` (all-time **not** mixed into current score).  
Optional `related_posts` from https://news84034.tistory.com/rss (read-only).

## New vs rerun (verification)
- Per-game known roster: `data/rosters/{game_id}.json` — names already playable are **never** auto-promoted to NEW.
- A name becomes NEW only when an official notice marks it (featured debut not in roster, or explicit markers like 신규 / Character Demo / Genshin "세상 여행 일반 기원에 추가").
- Unsure extractions go to `data/pending_review.json` (not shown on the main widget).
- Weapon banners and junk tokens (열쇠, 상점, …) are skipped.

## Auto-discovery of new reveals
- HoYo games: official HoYoWiki catalog (`sg-wiki-api-static.hoyolab.com`, read-only). New entry vs `data/rosters/wiki_seen/{game}.json` → NEW candidate. HoYo pickup names not in the catalog (light cones, W-engines) are dropped.
- All games: official channel titles (YouTube RSS, HoYoLAB KR, Naver lounge GM) matching character-demo formats (`REVEAL_PATTERNS` in collect.py, e.g. 캐릭터 트레일러 / 캐릭터 프리뷰 / 에이전트 파일 / 공명자 화면 / 오퍼레이터 전투 시연 / 【NIKKE 소개】 / 신규 사도).
- Roster `revealed_unreleased` = revealed earlier, not yet released (not a rerun). After a debut month ends, the name is auto-appended to `auto_added`.
- Month rule: revealed this month OR released / debut pickup this month.
- Manual: `overrides.json → verified_new_characters` (each must carry official `source_urls`).

## X signal (daily file, read-only here)
`data/x_signals.json` is written by a separate daily job. The collector only reads it: X weight 20% from `totals` (likes + 2×reposts + replies), log-normalized against the same game's past new characters (`x_engagement` in alltime.sqlite) or this month's peers when history < 2. Status `연동(일 1회)` + `x_generated_at`. Missing file/entry → `데이터 없음`, weights renormalized. The 3-hourly Action never stages or deletes it.

## Source links
`source_url` is always a reader page (HoYoLAB article, Naver lounge post, official YouTube/X). Raw APIs go to `api_source` and are never linked; `source_label` is shown as text when no page exists.

## Add a game
One object in `games.json`. Character aliases in `data/new_characters.json`.

## Security
HTTPS allowlist, size limit, sanitize, cache, Actions `contents:write` only, SHA-pinned actions, no secrets in repo.
