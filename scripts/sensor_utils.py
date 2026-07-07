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
