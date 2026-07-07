"""Shared helpers for turning data/history.jsonl into tidy per-room data."""
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.jsonl"

COLUMN_RE = re.compile(
    r"^(?P<room>.+?) - (?P<metric>Temperature|Humidity|Dewpoint|gpkg|Heat Index|Wet Bulb) - (?P<sensor>\d+)\s*$"
)

# Condensation risk margin = Temperature - Dewpoint (how many degrees C
# above the point at which moisture starts condensing on a surface).
RISK_HIGH = 1.0   # margin below this: high risk (red)
RISK_ELEVATED = 3.0  # margin below this: elevated risk (amber)

# CIBSE Guide A recommended operative temperatures for UK dwellings, plus the
# widely used 40-60% RH comfortable-humidity band. Used to mechanically score
# thermal comfort per room so a downstream narrative process has a ready-made
# fact rather than having to judge it itself.
COMFORT_TEMP_BEDROOM_C = 18.0
COMFORT_TEMP_LIVING_C = 21.0
COMFORT_RH_LOW = 40.0
COMFORT_RH_HIGH = 60.0
NON_LIVING_ROOMS = {"Loft 1", "Loft 2", "Network", "Outside"}


def comfort_target_temp(room):
    return COMFORT_TEMP_BEDROOM_C if room.startswith("Bed") else COMFORT_TEMP_LIVING_C


def humidity_deviation(avg_rh):
    if avg_rh < COMFORT_RH_LOW:
        return COMFORT_RH_LOW - avg_rh
    if avg_rh > COMFORT_RH_HIGH:
        return avg_rh - COMFORT_RH_HIGH
    return 0.0


def rank_thermal_comfort(avg_temp_by_room, avg_humidity_by_room, exclude=NON_LIVING_ROOMS):
    """Score living-space rooms by deviation from CIBSE Guide A comfort targets.

    Lower comfort_score = closer to the target temperature and the 40-60% RH
    band. Returns a list sorted best-to-worst; excludes non-living-space
    sensors (lofts, network cupboard, outside) since they aren't spaces
    people occupy.
    """
    scored = []
    for room, avg_temp in avg_temp_by_room.items():
        if room in exclude:
            continue
        avg_rh = avg_humidity_by_room.get(room)
        if avg_rh is None:
            continue
        target_temp = comfort_target_temp(room)
        scored.append(
            {
                "room": room,
                "avg_temperature": round(avg_temp, 1),
                "target_temperature": target_temp,
                "avg_humidity": round(avg_rh, 1),
                "comfort_score": round(abs(avg_temp - target_temp) + humidity_deviation(avg_rh), 2),
            }
        )
    scored.sort(key=lambda r: r["comfort_score"])
    return scored


def load_history_wide():
    """Return the full history as a wide DataFrame, one row per MessageDate."""
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
    df["MessageDate"] = pd.to_datetime(df["MessageDate"])
    return df.sort_values("MessageDate").reset_index(drop=True)


def to_long(df_wide):
    """Melt the wide per-room columns into tidy (MessageDate, Room, Metric, Value) rows."""
    records = []
    for col in df_wide.columns:
        match = COLUMN_RE.match(col)
        if not match:
            continue
        room = match.group("room").strip()
        metric = match.group("metric")
        sub = df_wide[["MessageDate", col]].rename(columns={col: "Value"})
        sub["Room"] = room
        sub["Metric"] = metric
        records.append(sub)
    if not records:
        return pd.DataFrame(columns=["MessageDate", "Room", "Metric", "Value"])
    long_df = pd.concat(records, ignore_index=True)
    return long_df.dropna(subset=["Value"])


def room_order(long_df):
    """Rooms sorted alphabetically, with Outside last (kept as an outdoor reference)."""
    rooms = sorted(r for r in long_df["Room"].unique() if r != "Outside")
    if "Outside" in long_df["Room"].unique():
        rooms.append("Outside")
    return rooms


def condensation_margin(long_df):
    """Per (MessageDate, Room) Temperature - Dewpoint margin."""
    temp = long_df[long_df["Metric"] == "Temperature"][["MessageDate", "Room", "Value"]].rename(
        columns={"Value": "Temperature"}
    )
    dew = long_df[long_df["Metric"] == "Dewpoint"][["MessageDate", "Room", "Value"]].rename(
        columns={"Value": "Dewpoint"}
    )
    merged = temp.merge(dew, on=["MessageDate", "Room"], how="inner")
    merged["Margin"] = merged["Temperature"] - merged["Dewpoint"]
    return merged


def risk_color(margin):
    if margin < RISK_HIGH:
        return "#d64545"  # red
    if margin < RISK_ELEVATED:
        return "#e0a030"  # amber
    return "#3f9142"  # green
