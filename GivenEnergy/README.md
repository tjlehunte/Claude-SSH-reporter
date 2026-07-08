# GivenEnergy energy-flow reporter

Automated daily/weekly reporting for a GivenEnergy inverter's energy flows (solar generation, battery charge/discharge, grid import/export, home consumption). Part of the [Claude-SSH-reporter](../README.md) repo — see the root README for how this fits alongside the Monnit pipeline.

## What it does

- **Daily** ([`givenergy-daily-report.yml`](../.github/workflows/givenergy-daily-report.yml), 02:00 UTC): fetches new half-hourly energy-flow readings, dedups into [`data/history.jsonl`](data/history.jsonl), and generates [`reports/latest.html`](reports/latest.html) for the previous local calendar day.
- **Weekly** ([`givenergy-weekly-report.yml`](../.github/workflows/givenergy-weekly-report.yml), Monday 02:00 UTC): aggregates the previous Monday–Sunday local week into [`reports/weekly/latest.html`](reports/weekly/latest.html) — raw and daily-total flow charts, self-consumption/self-sufficiency stats, best/worst generation day.
- **AI insights**: not built yet. A weekly `<!-- AI_INSIGHTS_PLACEHOLDER -->` slot exists in the report (same convention as Monnit) for a future locally-scheduled routine to fill in, once the mechanical pipeline has run for a while and is trusted.

Dated snapshots accumulate in `reports/daily/` and `reports/weekly/` as a permanent history; `reports/latest.html` and `reports/weekly/latest.html` always point at the most recent report.

## Data source

Fetches directly from the GivenEnergy Cloud API (`POST /v1/inverter/{serial}/energy-flows`, bearer token in the `GIVENERGY_BEARER` repo secret, mapped to the `GIVENERGY_BEARER_TOKEN` env var inside the workflow). See [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py). No fallback data source exists — if the API is unreachable, the fetch fails loudly rather than silently skipping.

## Scripts

- [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py) — fetch, dedup, append to history
- [`scripts/generate_report.py`](scripts/generate_report.py) — daily report
- [`scripts/generate_weekly_report.py`](scripts/generate_weekly_report.py) — weekly report + AI-insights stats
- [`scripts/energy_utils.py`](scripts/energy_utils.py) — shared helpers: history loading, calendar windows, generation/consumption/self-sufficiency calculations

No ongoing manual intervention or Claude API usage is required for the mechanical pipeline — it runs autonomously.
