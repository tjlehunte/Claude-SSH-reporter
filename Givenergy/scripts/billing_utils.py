"""Bill estimate helpers: live Octopus rates for BOTH directions.

Import side: Flexible Octopus (VAR-22-11-01), region G - fetched by
fetch_octopus_import_rates.py into data/octopus_import_rates.jsonl. This
replaces the old hardcoded IMPORT_UNIT_RATE_PENCE_PER_KWH /
IMPORT_STANDING_CHARGE_PENCE_PER_DAY constants, which had silently drifted
(26.49/58.71 hardcoded vs 26.13/44.32 live for region G when checked
2026-08-05) - the VAR product reprices with the price cap, so any snapshot
goes stale. Import cost is now matched per-interval against whichever rate
was actually valid at that time, same philosophy as the export side.

Export side: Agile Outgoing, region G - unchanged, fetched live by
fetch_octopus_rates.py.

Region note (applies to both sides): region G is the real house's region
(M5 postcode, North West England). The test Octopus account is actually
signed up in region B because its address had to be set near Leeds at
account creation - the pipeline deliberately prices the Manchester house,
not the test account's paper bill. All rates endpoints used here are public
(no API key), so the account's own region is irrelevant to fetching.
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RATES_FILE = ROOT / "data" / "octopus_export_rates.jsonl"
IMPORT_RATES_FILE = ROOT / "data" / "octopus_import_rates.jsonl"

# Export: Agile Outgoing May 2019, region G.
OCTOPUS_PRODUCT_CODE = "AGILE-OUTGOING-19-05-13"
OCTOPUS_TARIFF_CODE = "E-1R-AGILE-OUTGOING-19-05-13-G"

# Import: Flexible Octopus (the standard variable tariff), region G. Product
# vintage matches the account's actual current agreement (VAR-22-11-01,
# confirmed via the authenticated account endpoint 2026-08-05 - the account's
# agreement is the -B variant; -G is the deliberate region substitution).
IMPORT_PRODUCT_CODE = "VAR-22-11-01"
IMPORT_TARIFF_CODE = "E-1R-VAR-22-11-01-G"

LOCAL_TZ = "Europe/London"


def _load_jsonl(path):
    """Shared JSONL reader - both rate files are one-JSON-object-per-line."""
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_export_rates():
    """Return export rates as a DataFrame with a tz-aware UTC `valid_from` column."""
    rows = _load_jsonl(RATES_FILE)
    if not rows:
        return pd.DataFrame(columns=["valid_from", "rate_pence_per_kwh"])
    df = pd.DataFrame(rows)
    df["valid_from"] = pd.to_datetime(df["valid_from"], utc=True)
    return df.sort_values("valid_from").reset_index(drop=True)


def load_import_rates():
    """Return import rates as a DataFrame with tz-aware UTC valid_from/valid_to.

    Unlike export rows (point-in-time half-hourly), import rows are validity
    RANGES: valid_to is NaT for the currently-active rate (open-ended). The
    `charge_type` column distinguishes 'unit_rate' (pence/kWh) from
    'standing_charge' (pence/day) rows - both live in the same file.
    """
    rows = _load_jsonl(IMPORT_RATES_FILE)
    if not rows:
        return pd.DataFrame(
            columns=["charge_type", "valid_from", "valid_to", "value_inc_vat_pence"]
        )
    df = pd.DataFrame(rows)
    df["valid_from"] = pd.to_datetime(df["valid_from"], utc=True)
    # null valid_to (currently-active rate) becomes NaT, treated as open-ended
    # by the matchers below.
    df["valid_to"] = pd.to_datetime(df["valid_to"], utc=True)
    return df.sort_values(["charge_type", "valid_from"]).reset_index(drop=True)


def _match_rate_asof(times_utc, rate_rows):
    """For each timestamp, find the rate row whose [valid_from, valid_to)
    range contains it. merge_asof gets the latest valid_from <= t; the
    valid_to check then rejects matches where that row had already expired
    (which can only happen if the cache has a gap - flagged via NaN so the
    caller's coverage stat picks it up rather than silently using a stale rate).
    """
    if rate_rows.empty:
        return pd.Series([float("nan")] * len(times_utc), index=times_utc.index)
    lookup = rate_rows.sort_values("valid_from")[["valid_from", "valid_to", "value_inc_vat_pence"]]
    probe = pd.DataFrame({"t": times_utc}).sort_values("t")
    matched = pd.merge_asof(probe, lookup, left_on="t", right_on="valid_from")
    expired = matched["valid_to"].notna() & (matched["t"] >= matched["valid_to"])
    matched.loc[expired, "value_inc_vat_pence"] = float("nan")
    # restore the caller's original row order
    return matched.set_index(probe.index)["value_inc_vat_pence"].reindex(times_utc.index)


def calculate_import_cost(window_df, import_rates_df):
    """Import cost in pounds for a window, matching each half-hour's actual
    import volume (Grid to Home + Grid to Battery) against the unit rate that
    was genuinely valid at that time, plus the standing charge valid on each
    calendar day in the window.

    Replaces the old flat-multiply signature (import_kwh, num_days): the flat
    version applied one snapshot rate to the whole window, which both went
    stale between price-cap repricings and mis-costed any window straddling a
    repricing date.

    Returns (cost_pounds, coverage_pct) - coverage_pct is the share of
    import-carrying intervals that found a valid unit rate, mirroring
    calculate_export_revenue's contract so callers can flag partial estimates
    the same way for both directions.

    window_df["start"] is naive Europe/London local time; rate validity is
    UTC - localized here for the match. Ambiguous/nonexistent local times on
    the two DST-transition days a year are dropped from the match (same
    documented non-issue as elsewhere in this pipeline) rather than raised.
    """
    if window_df.empty:
        return 0.0, 0.0

    unit_rates = import_rates_df[import_rates_df["charge_type"] == "unit_rate"]
    standing = import_rates_df[import_rates_df["charge_type"] == "standing_charge"]

    # ---- unit-rate component: per-interval join, same shape as export ----
    import_kwh = window_df["Grid to Home"] + window_df["Grid to Battery"]
    start_utc = (
        window_df["start"]
        .dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="shift_forward")
        .dt.tz_convert("UTC")
    )
    merged = pd.DataFrame({"start_utc": start_utc, "import_kwh": import_kwh}).dropna(
        subset=["start_utc"]
    )
    merged["rate"] = _match_rate_asof(merged["start_utc"], unit_rates)

    total_intervals = len(merged)
    matched = merged.dropna(subset=["rate"])
    coverage_pct = 100.0 * len(matched) / total_intervals if total_intervals else 0.0
    unit_pence = float((matched["import_kwh"] * matched["rate"]).sum())

    # ---- standing-charge component: one charge per local calendar day ----
    # Matched at each day's local midnight (converted to UTC), because
    # standing charges are quoted per day and Octopus's validity boundaries
    # are set at local-midnight repricing dates.
    days = pd.Series(sorted(window_df["start"].dt.normalize().unique()))
    day_starts_utc = (
        days.dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="shift_forward")
        .dt.tz_convert("UTC")
        .dropna()
    )
    day_rates = _match_rate_asof(day_starts_utc, standing).dropna()
    standing_pence = float(day_rates.sum())

    return (unit_pence + standing_pence) / 100.0, coverage_pct


def calculate_export_revenue(window_df, rates_df):
    """Export revenue in pounds, matching each half-hour's actual export volume
    (PV to Grid + Battery to Grid) against the real Agile Outgoing rate for that
    settlement period. Returns (revenue_pounds, coverage_pct) - coverage_pct is
    the share of intervals that had a matching rate, so a caller can flag a
    partial/unreliable estimate rather than silently understating it.
    window_df["start"] is naive Europe/London local time; rates_df["valid_from"]
    is UTC - localized here for the join. Ambiguous/nonexistent local times on
    the two DST-transition days a year are dropped from the match (same
    documented non-issue as elsewhere in this pipeline) rather than raised.
    """
    if window_df.empty or rates_df.empty:
        return 0.0, 0.0
    export_kwh = window_df["PV to Grid"] + window_df["Battery to Grid"]
    start_utc = (
        window_df["start"]
        .dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="shift_forward")
        .dt.tz_convert("UTC")
    )
    merged = pd.DataFrame({"start_utc": start_utc, "export_kwh": export_kwh})
    joined = merged.merge(rates_df, left_on="start_utc", right_on="valid_from", how="left")
    total_intervals = len(joined)
    matched = joined.dropna(subset=["rate_pence_per_kwh"])
    coverage_pct = 100.0 * len(matched) / total_intervals if total_intervals else 0.0
    revenue_pence = float((matched["export_kwh"] * matched["rate_pence_per_kwh"]).sum())
    return revenue_pence / 100.0, coverage_pct
