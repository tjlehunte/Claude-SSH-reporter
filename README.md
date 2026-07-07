# Claude-SSH-Monnit-reporter

Automated daily/weekly reporting for a 16-room Monnit home sensor network (temperature, humidity, dewpoint, condensation risk, current/power draw).

## What it does

- **Daily** ([`.github/workflows/daily-report.yml`](.github/workflows/daily-report.yml), 01:00 UTC): fetches new sensor readings, dedups into [`data/history.jsonl`](data/history.jsonl), and generates [`reports/latest.html`](reports/latest.html) for the previous UTC calendar day.
- **Weekly** ([`.github/workflows/weekly-report.yml`](.github/workflows/weekly-report.yml), Monday 01:00 UTC): aggregates the previous Monday–Sunday week into [`reports/weekly/latest.html`](reports/weekly/latest.html) — temperature/humidity charts, CIBSE Guide A thermal comfort scoring, and detection of sharp or house-wide temperature/humidity swings (e.g. windows being opened).
- **AI insights**: a locally scheduled Claude Desktop routine reads the weekly report's compact `latest_stats.json` (not the raw history) shortly after the weekly workflow runs, and writes a short grounded narrative paragraph into the report.

Dated snapshots accumulate in `reports/daily/` and `reports/weekly/` as a permanent history; `reports/latest.html` and `reports/weekly/latest.html` always point at the most recent report.

## Data source

Fetches directly from the Monnit gateway (self-signed cert, API keys in the `MONNIT_API_KEY_ID`/`MONNIT_API_SECRET_KEY` repo secrets), falling back to a personal Render proxy if the gateway is unreachable. See [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py).

## Scripts

- [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py) — fetch, dedup, append to history
- [`scripts/generate_report.py`](scripts/generate_report.py) — daily report
- [`scripts/generate_weekly_report.py`](scripts/generate_weekly_report.py) — weekly report + AI-insights stats
- [`scripts/sensor_utils.py`](scripts/sensor_utils.py) — shared helpers: room exclusions, comfort scoring, swing detection

No ongoing manual intervention or Claude API usage is required — the whole pipeline runs autonomously.
