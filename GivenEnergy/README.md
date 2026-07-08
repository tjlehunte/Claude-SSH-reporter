# GivenEnergy energy-flow reporter

Automated daily/weekly reporting for a GivenEnergy inverter's energy flows (solar generation, battery charge/discharge, grid import/export, home consumption). Part of the [Claude-SSH-reporter](../README.md) repo — see the root README for how this fits alongside the Monnit pipeline.

## What it does

- **Daily** ([`givenergy-daily-report.yml`](../.github/workflows/givenergy-daily-report.yml), 06:00 UTC): fetches new half-hourly energy-flow readings, dedups into [`data/history.jsonl`](data/history.jsonl), and generates [`reports/latest.html`](reports/latest.html) for the previous local calendar day. Runs later than Monnit's 01:00 UTC deliberately — GivenEnergy's own data has some lag, so the later run gives the previous day's readings time to fully land before fetching.
- **Weekly** ([`givenergy-weekly-report.yml`](../.github/workflows/givenergy-weekly-report.yml), Monday 06:00 UTC): aggregates the previous Monday–Sunday local week into [`reports/weekly/latest.html`](reports/weekly/latest.html) — raw and daily-total flow charts, self-consumption/self-sufficiency stats, best/worst generation day, and an estimated bill.
- **Bill estimate**: both reports include an estimated import cost, export revenue, and net cost/credit in £, using a fixed import tariff and live half-hourly Octopus Agile Outgoing export rates. See [`scripts/billing_utils.py`](scripts/billing_utils.py).
- **AI insights**: a locally scheduled Claude Desktop routine (`givenergy-weekly-ai-insights`, not part of this repo, not a GitHub Action) reads the weekly report's compact `latest_stats.json` shortly after the weekly workflow runs, and writes a short grounded narrative paragraph into the report — same pattern as Monnit's routine.

Dated snapshots accumulate in `reports/daily/` and `reports/weekly/` as a permanent history; `reports/latest.html` and `reports/weekly/latest.html` always point at the most recent report.

## Data sources

- **Energy flows**: fetched directly from the GivenEnergy Cloud API (`POST /v1/inverter/{serial}/energy-flows`, bearer token in the `GIVENERGY_BEARER` repo secret, mapped to the `GIVENERGY_BEARER_TOKEN` env var inside the workflow). See [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py). No fallback data source exists — if the API is unreachable, the fetch fails loudly rather than silently skipping.
- **Export rates**: fetched from the public Octopus Energy API (Agile Outgoing Octopus tariff, region G) — no API key needed, since tariff standard-rate lookups are public once you know the product/tariff code. See [`scripts/fetch_octopus_rates.py`](scripts/fetch_octopus_rates.py) and [`scripts/billing_utils.py`](scripts/billing_utils.py) for the exact codes and fixed import-tariff constants.

## Scripts

- [`scripts/fetch_and_append.py`](scripts/fetch_and_append.py) — fetch, dedup, append energy-flow history
- [`scripts/fetch_octopus_rates.py`](scripts/fetch_octopus_rates.py) — fetch, dedup, append Octopus export-rate history
- [`scripts/generate_report.py`](scripts/generate_report.py) — daily report
- [`scripts/generate_weekly_report.py`](scripts/generate_weekly_report.py) — weekly report + AI-insights stats
- [`scripts/energy_utils.py`](scripts/energy_utils.py) — shared helpers: history loading, calendar windows, generation/consumption/self-sufficiency calculations
- [`scripts/billing_utils.py`](scripts/billing_utils.py) — tariff constants + import cost / export revenue calculations

No ongoing manual intervention or Claude API usage is required for the mechanical pipeline — it runs autonomously. AI-narrative commentary is added by the locally scheduled routine described above.
