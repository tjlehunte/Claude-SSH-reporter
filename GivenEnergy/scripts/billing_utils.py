"""Bill estimate helpers: fixed import tariff + live Octopus Agile Outgoing export rates.

Update IMPORT_UNIT_RATE_PENCE_PER_KWH / IMPORT_STANDING_CHARGE_PENCE_PER_DAY here
if the import tariff changes - these are plain constants, not fetched from
anywhere, since the import side is a fixed-rate tariff (unlike the export side,
which genuinely varies by half-hour and is fetched live).
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RATES_FILE = ROOT / "data" / "octopus_export_rates.jsonl"

# Fixed import tariff (unit rate + standing charge). Confirmed with the user
# as the current rate - not sourced from any API.
IMPORT_UNIT_RATE_PENCE_PER_KWH = 26.49
IMPORT_STANDING_CHARGE_PENCE_PER_DAY = 58.71

# Agile Outgoing Octopus May 2019, region G (North West England - confirmed
# via Octopus's public grid-supply-points lookup for the M5 postcode area).
# This product/tariff code pair is public info, not a secret - the rates
# endpoint itself requires no API key.
OCTOPUS_PRODUCT_CODE = "AGILE-OUTGOING-19-05-13"
OCTOPUS_TARIFF_CODE = "E-1R-AGILE-OUTGOING-19-05-13-G"

LOCAL_TZ = "Europe/London"


def load_export_rates():
    """Return export rates as a DataFrame with a tz-aware UTC `valid_from` column."""
    if not RATES_FILE.exists():
        return pd.DataFrame(columns=["valid_from", "rate_pence_per_kwh"])
    rows = []
    with RATES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        return pd.DataFrame(columns=["valid_from", "rate_pence_per_kwh"])
    df = pd.DataFrame(rows)
    df["valid_from"] = pd.to_datetime(df["valid_from"], utc=True)
    return df.sort_values("valid_from").reset_index(drop=True)


def calculate_import_cost(import_kwh, num_days):
    """Fixed-rate import cost in pounds: unit rate + standing charge for num_days."""
    pence = import_kwh * IMPORT_UNIT_RATE_PENCE_PER_KWH + num_days * IMPORT_STANDING_CHARGE_PENCE_PER_DAY
    return pence / 100.0


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
