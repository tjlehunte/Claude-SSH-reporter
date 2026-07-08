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

## No fallback data source

Unlike Monnit (which falls back to a Render proxy), there's no secondary source for GivenEnergy data — if the API is unreachable or the token (repo secret `GIVENERGY_BEARER`, read inside the script as `GIVENERGY_BEARER_TOKEN`) is missing/expired, `fetch_and_append.py` raises rather than silently succeeding with stale or partial data.
