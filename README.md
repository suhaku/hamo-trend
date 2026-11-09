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

## Add a game
One object in `games.json`. Character aliases in `data/new_characters.json`.

## Security
HTTPS allowlist, size limit, sanitize, cache, Actions `contents:write` only, SHA-pinned actions, no secrets in repo.
