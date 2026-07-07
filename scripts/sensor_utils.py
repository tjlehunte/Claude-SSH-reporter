"""Shared helpers for turning data/history.jsonl into tidy per-room data."""
import json
import re
from datetime import datetime, timedelta
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


# Rooms excluded from "sharp change" detection (e.g. window-opening events) -
# outdoor and unheated loft readings swing naturally with the weather and
# aren't informative about occupant behaviour the way living-space rooms are.
SHARP_CHANGE_EXCLUDE = {"Outside", "Loft 1", "Loft 2"}
SHARP_CHANGE_WINDOW_MINUTES = 30
SHARP_TEMPERATURE_THRESHOLD_C = 1.0
SHARP_HUMIDITY_THRESHOLD_PCT = 5.0
SHARP_CHANGE_MAX_EVENTS = 20  # safety cap on the returned list, sorted by magnitude
COLLECTIVE_MIN_ROOMS = 3      # >= this many distinct rooms, same direction, same time bucket
COLLECTIVE_BUCKET_MINUTES = 30


def detect_sharp_changes(metric_long_df, window_minutes, threshold, exclude=SHARP_CHANGE_EXCLUDE):
    """Find every distinct >=threshold change within `window_minutes`, in
    either direction, across all in-scope rooms. Assumes readings are on
    Monnit's 10-minute grid. Consecutive rolling-window hits within the same
    room are collapsed into a single event (the point of peak magnitude),
    since one real dip/rise otherwise triggers many overlapping detections.
    Returns a list sorted by magnitude (largest first), capped at
    SHARP_CHANGE_MAX_EVENTS.
    """
    window_steps = max(1, window_minutes // 10)
    events = []
    for room, sub in metric_long_df.groupby("Room"):
        if room in exclude:
            continue
        sub = sub.sort_values("MessageDate").reset_index(drop=True)
        if len(sub) <= window_steps:
            continue
        values = sub["Value"].to_numpy()
        times = sub["MessageDate"]
        diffs = values[window_steps:] - values[:-window_steps]
        flagged = [i for i in range(len(diffs)) if abs(diffs[i]) >= threshold]

        clusters = []
        for i in flagged:
            if clusters and i - clusters[-1][-1] <= window_steps:
                clusters[-1].append(i)
            else:
                clusters.append([i])

        for cluster in clusters:
            peak_idx = max(cluster, key=lambda i: abs(diffs[i]))
            change = diffs[peak_idx]
            events.append(
                {
                    "room": room,
                    "change": round(float(change), 1),
                    "direction": "drop" if change < 0 else "rise",
                    "from_time": times.iloc[peak_idx].strftime("%Y-%m-%d %H:%M:%S"),
                    "from_value": round(float(values[peak_idx]), 1),
                    "to_time": times.iloc[peak_idx + window_steps].strftime("%Y-%m-%d %H:%M:%S"),
                    "to_value": round(float(values[peak_idx + window_steps]), 1),
                }
            )

    events.sort(key=lambda e: -abs(e["change"]))
    return events[:SHARP_CHANGE_MAX_EVENTS]


def find_collective_events(events, bucket_minutes=COLLECTIVE_BUCKET_MINUTES, min_rooms=COLLECTIVE_MIN_ROOMS):
    """Group same-direction events that start within the same time bucket
    across multiple distinct rooms - evidence of a whole-house event (e.g.
    several windows opened) rather than one room's isolated behaviour.
    """
    buckets = {}
    for e in events:
        dt = datetime.strptime(e["from_time"], "%Y-%m-%d %H:%M:%S")
        bucket_dt = dt - timedelta(minutes=dt.minute % bucket_minutes, seconds=dt.second)
        buckets.setdefault((bucket_dt, e["direction"]), []).append(e)

    collective = []
    for (bucket_dt, direction), group in buckets.items():
        rooms = sorted({g["room"] for g in group})
        if len(rooms) < min_rooms:
            continue
        changes = [g["change"] for g in group]
        collective.append(
            {
                "time": bucket_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "direction": direction,
                "rooms": rooms,
                "room_count": len(rooms),
                "avg_change": round(sum(changes) / len(changes), 1),
                "largest_change": max(changes, key=abs),
            }
        )
    collective.sort(key=lambda c: -c["room_count"])
    return collective


# Rooms excluded when picking the "peak"/"lowest" (or warmest/coolest)
# temperature figure - Outside, the unheated lofts, and the network cupboard
# (full of heat-generating equipment) will essentially always win those
# slots, which makes the figure meaningless as a comment on the house
# itself. Same set as NON_LIVING_ROOMS; they stay fully visible in charts.
PEAK_TEMPERATURE_EXCLUDE = NON_LIVING_ROOMS

# Condensation risk is dominated by Outside's naturally different profile;
# excluded from the single "tightest margin" figure (charts still show it).
CONDENSATION_HIGHLIGHT_EXCLUDE = {"Outside"}

# Same reasoning as PEAK_TEMPERATURE_EXCLUDE, applied to humidity max/min -
# lofts, the network cupboard, and outside are excluded from peak/lowest
# humidity figures.
HUMIDITY_HIGHLIGHT_EXCLUDE = NON_LIVING_ROOMS

# Rooms excluded from "house interior" whole-house averages (e.g. the
# overall mean temperature/humidity for the week) - lofts and outside aren't
# part of the house's living space, unlike the network cupboard which stays
# included here.
HOUSE_INTERIOR_EXCLUDE = {"Loft 1", "Loft 2", "Outside"}


def room_category(room):
    if room == "Outside":
        return "outside"
    if room in {"Loft 1", "Loft 2"}:
        return "loft"
    return "room"


ROOM_CATEGORY_COLORS = {
    "outside": "#2e8b57",  # green
    "loft": "#c2793d",     # brown
    "room": "#4a7ab5",     # blue
}


def room_category_color(room):
    return ROOM_CATEGORY_COLORS[room_category(room)]


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
