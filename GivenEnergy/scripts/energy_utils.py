"""Shared helpers for turning data/history.jsonl into report-ready energy figures."""
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.jsonl"

# The API returns these keyed by string index ("0".."6"), not named keys (see
# fetch_and_append.py's FLOW_NAMES comment) - this is the same order used
# throughout the pipeline, from fetch to charts to stats.json.
FLOW_NAMES = [
    "PV to Home",
    "PV to Battery",
    "PV to Grid",
    "Grid to Home",
    "Grid to Battery",
    "Battery to Home",
    "Battery to Grid",
]


def load_history_wide():
    """Return the full history as a wide DataFrame, one row per half-hour interval."""
    if not HISTORY_FILE.exists():
        return pd.DataFrame()
    rows = []
    with HISTORY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    return df.sort_values("start").reset_index(drop=True)


def most_recent_complete_day(latest_ts):
    """Midnight of latest_ts's day, and the midnight before it (yesterday). Pure
    calendar arithmetic - works the same regardless of what timezone latest_ts's
    naive timestamps actually represent (here, Europe/London local time)."""
    day_start = latest_ts.normalize()
    return day_start - timedelta(days=1), day_start


def most_recent_complete_week(latest_ts):
    """Monday 00:00 .. next Monday 00:00 (exclusive) of the week before latest_ts's week."""
    this_monday = (latest_ts - timedelta(days=latest_ts.weekday())).normalize()
    return this_monday - timedelta(days=7), this_monday


def total_generation(df):
    return df["PV to Home"] + df["PV to Battery"] + df["PV to Grid"]


def total_consumption(df):
    return df["PV to Home"] + df["Battery to Home"] + df["Grid to Home"]


def total_import(df):
    return df["Grid to Home"] + df["Grid to Battery"]


def total_export(df):
    return df["PV to Grid"] + df["Battery to Grid"]


def self_consumption_pct(generation_kwh, pv_to_home_kwh, pv_to_battery_kwh):
    """Share of generated solar used on-site (home or battery) rather than exported."""
    if generation_kwh <= 0:
        return None
    return 100.0 * (pv_to_home_kwh + pv_to_battery_kwh) / generation_kwh


def self_sufficiency_pct(consumption_kwh, grid_drawn_kwh):
    """Share of home consumption not funded by grid draw. Counts grid energy
    used to charge the battery as grid-drawn too, not just grid-to-home
    directly - charging the battery from the grid isn't self-sufficient just
    because the battery discharges to the home later. `grid_drawn_kwh` is
    expected to be total_import() (Grid to Home + Grid to Battery)."""
    if consumption_kwh <= 0:
        return None
    return 100.0 * (consumption_kwh - grid_drawn_kwh) / consumption_kwh


def flow_totals(df):
    """Sum of each of the 7 flows over the given window, in kWh (rows are
    already 0.5h-average kW readings per interval, so summing per-interval
    values yields kWh directly - same convention the source R script uses)."""
    return {flow: round(float(df[flow].sum()), 2) for flow in FLOW_NAMES}
