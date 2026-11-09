# hamo-trend

Public JSON feed for [Hamo's.](https://news84034.tistory.com) game-trend dashboard.

**Live data:** https://suhaku.github.io/hamo-trend/data/trend.json

## Rules
- Real fetched data only. Missing → `null` (UI shows 데이터 없음).
- Every item carries `source` / `source_url` and `fetched_at` / `generated_at`.
- No secrets or API keys in this repo. Optional keys (YouTube Data / X) go in Actions secrets only if added later.

## Collector
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python collect.py            # reuse cached Trends
python collect.py --trends   # refresh Google Trends (slow; once/day in CI)
```

Sources: YouTube RSS, Google Trends (KR), arca.live, Naver Game Lounge GM notices, HoYoverse / HoYoLAB notice APIs, BanG Dream JP WordPress JSON.

## Security
- HTTPS-only fetches to an allowlisted hostname set; 2 MiB response cap; timeouts.
- Output JSON is recursively sanitized (HTML/scripts stripped; URLs must be https + allowlisted).
- Workflow: `permissions: contents: write` only; `actions/checkout` and `actions/setup-python` pinned by full commit SHA; no third-party actions.
- Issues / wiki / projects disabled. Actions restricted to GitHub-owned where possible.
- Secret scanning + push protection + Dependabot alerts enabled when available.

## Schedule
GitHub Action every 3 hours (`cron: 17 */3 * * *`). Trends refresh only when UTC hour is `00`.
