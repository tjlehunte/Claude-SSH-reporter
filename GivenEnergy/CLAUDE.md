# CLAUDE.md

Operational notes specific to the GivenEnergy pipeline — not a description of what the code does (see README.md and the scripts themselves for that), just the gotchas that aren't obvious from reading the code. See [`../CLAUDE.md`](../CLAUDE.md) for repo-wide notes (verifying changes via workflow_dispatch, the Windows git-push gotcha, the shared concurrency group) — this file only covers what's GivenEnergy-specific.

## Timestamps are local time, not UTC

The API's `start`/`end` fields are naive **Europe/London local time** (confirmed against a real response — values line up with BST/GMT wall-clock, not UTC). This is deliberate throughout the pipeline:

- `fetch_and_append.py` builds its `start_time`/`end_time` request dates from the local calendar, not UTC, so requested windows actually align with what the API returns.
- `energy_utils.most_recent_complete_day`/`most_recent_complete_week` do pure calendar arithmetic on whatever timestamp they're given — they don't know or care that it's local time rather than UTC (unlike Monnit's equivalent functions, which operate on UTC). Don't naively copy Monnit's "(UTC)" label into this pipeline's report text — it's already correctly labeled "(local time)" instead.
- On the two clock-change days per year, the half-hour grid will have 46 or 50 rows instead of the usual 48. This is expected and does not need special handling — sums and charts are unaffected either way.

## Flow ordering is positional, not named (`scripts/fetch_and_append.py`)

The API's `data` payload per interval is an array of 7 values with no self-describing keys (inferred from the source R script, which manually renames them positionally rather than trusting JSON key names). `FLOW_NAMES` in `fetch_and_append.py` is the single source of truth for that order — every other script imports it from there (via `energy_utils.py`) rather than re-declaring it. If GivenEnergy ever changes the API to return named keys, or reorders the array, every downstream number silently mislabels itself with no error — this is the single most fragile assumption in the pipeline.

## No highlight/exclusion constants (unlike Monnit's `sensor_utils.py`)

Monnit needed room-exclusion constants because it has a fleet of *interchangeable, comparable* sensors where one (Outside) would trivially win every peak/lowest comparison. GivenEnergy has no such fleet — it's a single site with 7 *different-meaning* flow types that were never comparable to begin with, so there's nothing structurally analogous to exclude. Don't add exclusion-style constants here by default just because Monnit has them; if a genuinely misleading figure shows up once real data is visible (e.g. a "best generation day" landing on a day with a data gap), that's a data-quality filter to add deliberately, not a room-exclusion-style pattern to port over.

## Backfill chunking

`CHUNK_DAYS = 7` in `fetch_and_append.py` applies to every fetch, not just backfill mode — the user believes the API can handle large date ranges in a single call, but chunking anyway costs nothing and matches Monnit's defensive pattern (skip a malformed chunk's response rather than let it crash the whole fetch).

## No fallback data source

Unlike Monnit (which falls back to a Render proxy), there's no secondary source for GivenEnergy data — if the API is unreachable or the token (repo secret `GIVENERGY_BEARER`, read inside the script as `GIVENERGY_BEARER_TOKEN`) is missing/expired, `fetch_and_append.py` raises rather than silently succeeding with stale or partial data.
