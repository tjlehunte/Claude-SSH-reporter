# CLAUDE.md

Operational notes specific to the GivenEnergy pipeline — not a description of what the code does (see README.md and the scripts themselves for that), just the gotchas that aren't obvious from reading the code. See [`../CLAUDE.md`](../CLAUDE.md) for repo-wide notes (verifying changes via workflow_dispatch, the Windows git-push gotcha, the shared concurrency group) — this file only covers what's GivenEnergy-specific.

## Timestamps are local time, not UTC

The API's `start_time`/`end_time` fields (remapped to `start`/`end` in our own JSONL rows) are naive **Europe/London local time** (confirmed against a real response — values line up with BST/GMT wall-clock, not UTC). This is deliberate throughout the pipeline:

- `fetch_and_append.py` builds its `start_time`/`end_time` request dates from the local calendar, not UTC, so requested windows actually align with what the API returns.
- `energy_utils.most_recent_complete_day`/`most_recent_complete_week` do pure calendar arithmetic on whatever timestamp they're given — they don't know or care that it's local time rather than UTC (unlike Monnit's equivalent functions, which operate on UTC). Don't naively copy Monnit's "(UTC)" label into this pipeline's report text — it's already correctly labeled "(local time)" instead.
- On the two clock-change days per year, the half-hour grid will have 46 or 50 rows instead of the usual 48. This is expected and does not need special handling — sums and charts are unaffected either way.

## Response shape: objects keyed by string index, not arrays (`scripts/fetch_and_append.py`)

Confirmed against a real response: both the top-level `data` payload (the list of intervals) and each interval's own `data` payload (the 7 flow values) are JSON *objects* keyed by string indices (`"0"`, `"1"`, ... `"6"`), not JSON arrays — `fetch_chunk()` unwraps both via `.values()` (relying on Python 3.7+ dict insertion-order preservation matching the numeric key order). Each interval's timestamp keys are `start_time`/`end_time`, not `start`/`end` (those get remapped to `start`/`end` when building our own JSONL rows).

The 7 values themselves have no self-describing flow names — `FLOW_NAMES` in `fetch_and_append.py` is the single source of truth for that positional order (index 0 = PV to Home ... 6 = Battery to Grid), verified against real data (nonzero overnight battery discharge at index 5, nonzero PV from sunrise at index 0). Every other script imports `FLOW_NAMES` from there (via `energy_utils.py`) rather than re-declaring it. If GivenEnergy ever changes the API to return self-describing keys, or reorders the values, every downstream number silently mislabels itself with no error — this is the single most fragile assumption in the pipeline.

## No highlight/exclusion constants (unlike Monnit's `sensor_utils.py`)

Monnit needed room-exclusion constants because it has a fleet of *interchangeable, comparable* sensors where one (Outside) would trivially win every peak/lowest comparison. GivenEnergy has no such fleet — it's a single site with 7 *different-meaning* flow types that were never comparable to begin with, so there's nothing structurally analogous to exclude. Don't add exclusion-style constants here by default just because Monnit has them; if a genuinely misleading figure shows up once real data is visible (e.g. a "best generation day" landing on a day with a data gap), that's a data-quality filter to add deliberately, not a room-exclusion-style pattern to port over.

## Backfill chunking

`CHUNK_DAYS = 7` in `fetch_and_append.py` applies to every fetch, not just backfill mode — the user believes the API can handle large date ranges in a single call, but chunking anyway costs nothing and matches Monnit's defensive pattern (skip a malformed chunk's response rather than let it crash the whole fetch).

## Self-sufficiency counts grid-to-battery draw, not just grid-to-home

`energy_utils.self_sufficiency_pct(consumption_kwh, grid_drawn_kwh)` takes `grid_drawn_kwh` as `total_import()` (Grid to Home + Grid to Battery), not just Grid to Home. This was a deliberate correction: a day can show `Grid to Home == 0` (mathematically 100% self-sufficient by a narrower definition) while still importing grid energy to charge the battery — that energy isn't really "self-sufficient" just because it's discharged to the home later rather than drawn directly. Because of this, the metric can in principle go negative (heavy grid-charging on a low-consumption day) — that's not clamped to 0, since a negative value is a real signal (grid draw exceeded same-window consumption), not an error.

## Window end must come from the `end` column, not `start`

The displayed "Window: ... to ..." line (and the weekly `window_end` stats field) uses `window_df["end"].max()`, not `window_df["start"].max()`. Using `start` understates the window by one interval — e.g. a full day would show "00:00 to 23:30" instead of "00:00 to 00:00 (next day)". The weekly report's `report_label`/filename deliberately does the opposite: it derives the last-day date from `window_df["start"].max()` (via the `last_day` variable), not `end`, because the last interval's `end` can roll into the next calendar day (the 23:30-00:00 interval) and would otherwise misname the file. Don't "fix" either of these to match the other — they're intentionally different for different reasons.

## Daily/weekly schedule times don't need to change for Octopus's 4pm rate publication

Octopus publishes Agile rates day-ahead (tomorrow's rates go live ~4pm today). This looks like it could matter for the export-rate fetch, but it doesn't: every report covers the *previous fully-completed* day/week, so the rates it needs were already published the *afternoon before that period even started* — at least 24-36 hours before any report runs, regardless of what time of day the workflow fires. The 06:00 UTC schedule exists for a different reason (GivenEnergy's own inverter-cloud data lag, not Octopus). If `export_rate_coverage_pct` ever drops meaningfully below 100% despite this, that points to an actual Octopus outage/delay, not a scheduling problem — don't reach for "run it later in the day" as the fix.

## No fallback data source

Unlike Monnit (which falls back to a Render proxy), there's no secondary source for GivenEnergy data — if the API is unreachable or the token (repo secret `GIVENERGY_BEARER`, read inside the script as `GIVENERGY_BEARER_TOKEN`) is missing/expired, `fetch_and_append.py` raises rather than silently succeeding with stale or partial data.

## Bill estimate: fixed import tariff, live export rates, no secret needed

- **Import** is a fixed tariff (unit rate 26.49p/kWh + standing charge 58.71p/day) — plain constants in `billing_utils.py`. Update them there directly if the tariff changes; there's no API for this side since it doesn't vary.
- **Export** is on Agile Outgoing Octopus (product `AGILE-OUTGOING-19-05-13`, tariff `E-1R-AGILE-OUTGOING-19-05-13-G` — region G, confirmed via Octopus's public grid-supply-points lookup for the M5 postcode area), which genuinely varies by half-hour settlement period. `fetch_octopus_rates.py` fetches and caches these to `data/octopus_export_rates.jsonl`, high-water-marked and backfilled the same way as `fetch_and_append.py` (same `GIVENERGY_BACKFILL_DAYS` env var, so an energy backfill and a rates backfill always cover the same window).
- **The Octopus standard-unit-rates endpoint is public** — no API key/secret required, confirmed against a real request. If the export tariff ever changes to a *different* product (not just a rate revision within Agile Outgoing), `OCTOPUS_PRODUCT_CODE`/`OCTOPUS_TARIFF_CODE` in `billing_utils.py` need updating to match.
- **Matching rates to energy data**: `billing_utils.calculate_export_revenue()` converts each interval's naive Europe/London `start` to UTC to join against the rates file's UTC `valid_from`. On the two DST-transition days a year, ambiguous/nonexistent local times are dropped from the match rather than raised — a small, documented coverage gap, not a bug. A report's `export_rate_coverage_pct` reflects this; the summary line only shows a coverage caveat when it drops below 99%, since 100% (or near enough) is the overwhelmingly common case.
- **Net figure**: `estimated_net_bill_gbp` is import cost minus export revenue — negative means net credit, not an error; reports print "net credit" instead of a negative "net cost" for readability.
