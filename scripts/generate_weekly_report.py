#!/usr/bin/env python3
"""Generate a self-contained weekly HTML report from data/history.jsonl.

Reports on the most recently *completed* Monday-Sunday calendar week
(relative to the latest data on hand, not wall-clock "now"), not a rolling
7-day-from-now window: daily mean temperature per room, weekly average
humidity per room, and the weekly worst-case (minimum) condensation-risk
margin per room.
"""
import base64
import io
import json
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from sensor_utils import (
    condensation_margin,
    detect_sharp_changes,
    find_collective_events,
    load_history_wide,
    rank_thermal_comfort,
    room_category_color,
    room_order,
    to_long,
    CONDENSATION_HIGHLIGHT_EXCLUDE,
    HOUSE_INTERIOR_EXCLUDE,
    HUMIDITY_HIGHLIGHT_EXCLUDE,
    PEAK_TEMPERATURE_EXCLUDE,
    SHARP_CHANGE_WINDOW_MINUTES,
    SHARP_HUMIDITY_THRESHOLD_PCT,
    SHARP_TEMPERATURE_THRESHOLD_C,
)

ROOT = Path(__file__).resolve().parent.parent
WEEKLY_DIR = ROOT / "reports" / "weekly"
LATEST_FILE = WEEKLY_DIR / "latest.html"
WINDOW_DAYS = 7


def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def plot_raw_series(long_df, rooms, metric, ylabel, title):
    fig, ax = plt.subplots(figsize=(10, 5))
    for room in rooms:
        series = long_df[(long_df["Room"] == room) & (long_df["Metric"] == metric)].sort_values(
            "MessageDate"
        )
        if series.empty:
            continue
        ax.plot(series["MessageDate"], series["Value"], label=room, linewidth=0.8, alpha=0.85)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.margins(x=0)
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %Hh"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig


def plot_daily_mean_series(long_df, rooms, metric, ylabel, title):
    sub = long_df[long_df["Metric"] == metric].copy()
    sub["Day"] = pd.to_datetime(sub["MessageDate"].dt.date)
    daily_mean = sub.groupby(["Day", "Room"])["Value"].mean().reset_index()

    fig, ax = plt.subplots(figsize=(10, 5))
    for room in rooms:
        series = daily_mean[daily_mean["Room"] == room].sort_values("Day")
        if series.empty:
            continue
        ax.plot(series["Day"], series["Value"], marker="o", label=room, linewidth=1.3)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.margins(x=0.02)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig


def most_recent_complete_week(latest_ts):
    """Monday 00:00 .. next Monday 00:00 (exclusive) of the week before latest_ts's week."""
    this_monday = (latest_ts - timedelta(days=latest_ts.weekday())).normalize()
    return this_monday - timedelta(days=7), this_monday


def plot_bar(values_by_room, rooms, title, ylabel, color_fn=None):
    """color_fn, if given, receives (room, value) and returns a hex color."""
    values = [values_by_room.get(room, float("nan")) for room in rooms]
    colors = [color_fn(room, v) if color_fn else "#4a7ab5" for room, v in zip(rooms, values)]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(rooms, values, color=colors)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", rotation=60)
    return fig


def main():
    df_wide = load_history_wide()
    if df_wide.empty:
        print("[weekly] no history yet, skipping report generation")
        return

    latest_ts = df_wide["MessageDate"].max()
    week_start, week_end = most_recent_complete_week(latest_ts)
    window_df = df_wide[(df_wide["MessageDate"] >= week_start) & (df_wide["MessageDate"] < week_end)]

    if window_df.empty:
        # No fully-completed Mon-Sun week yet (pipeline just started) - fall
        # back to whatever partial data exists in the current in-progress week.
        print("[weekly] no completed calendar week yet; reporting partial current week instead")
        week_start, week_end = week_end, week_end + timedelta(days=WINDOW_DAYS)
        window_df = df_wide[(df_wide["MessageDate"] >= week_start) & (df_wide["MessageDate"] < week_end)]

    # Use the actual first/last readings present (e.g. Monday 00:10, Sunday
    # 23:50) for display and stats, while week_start/week_end above stay as
    # the exact calendar boundaries used for filtering.
    start = window_df["MessageDate"].min() if not window_df.empty else week_start
    end = window_df["MessageDate"].max() if not window_df.empty else week_end
    long_df = to_long(window_df)
    rooms = room_order(long_df)

    avg_humidity = long_df[long_df["Metric"] == "Humidity"].groupby("Room")["Value"].mean().to_dict()
    avg_temperature = long_df[long_df["Metric"] == "Temperature"].groupby("Room")["Value"].mean().to_dict()

    margin_df = condensation_margin(long_df)
    worst_margin = margin_df.groupby("Room")["Margin"].min().to_dict() if not margin_df.empty else {}

    comfort_ranking = rank_thermal_comfort(avg_temperature, avg_humidity)

    raw_temp_chart = fig_to_base64(
        plot_raw_series(long_df, rooms, "Temperature", "°C", "Raw temperature readings by room")
    )
    daily_temp_chart = fig_to_base64(
        plot_daily_mean_series(long_df, rooms, "Temperature", "°C", "Daily mean temperature by room")
    )
    raw_humidity_chart = fig_to_base64(
        plot_raw_series(long_df, rooms, "Humidity", "% RH", "Raw humidity readings by room")
    )
    daily_humidity_chart = fig_to_base64(
        plot_daily_mean_series(long_df, rooms, "Humidity", "% RH", "Daily mean humidity by room")
    )
    margin_chart = fig_to_base64(
        plot_bar(
            worst_margin, rooms, "Weekly worst-case condensation risk margin", "°C (minimum seen)",
            color_fn=lambda room, v: room_category_color(room),
        )
    )

    insights = []
    temp_series = long_df[long_df["Metric"] == "Temperature"]
    # Outside/lofts would win peak & lowest every single week (they're always
    # the most extreme by nature), which makes the figure meaningless as a
    # comment on the house - so restrict this specific figure to living/
    # utility rooms. They stay fully visible in the charts.
    temp_series_indoor = temp_series[~temp_series["Room"].isin(PEAK_TEMPERATURE_EXCLUDE)]
    if not temp_series_indoor.empty:
        hottest_row = temp_series_indoor.loc[temp_series_indoor["Value"].idxmax()]
        coldest_row = temp_series_indoor.loc[temp_series_indoor["Value"].idxmin()]
        insights.append(
            f"Peak temperature this week: {hottest_row['Value']:.1f}°C in {hottest_row['Room']} "
            f"on {hottest_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )
        insights.append(
            f"Lowest temperature this week: {coldest_row['Value']:.1f}°C in {coldest_row['Room']} "
            f"on {coldest_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )

    humidity_series = long_df[long_df["Metric"] == "Humidity"]
    # Same living-space-only restriction as temperature above - excluded
    # from this figure only; still fully visible (and averaged) elsewhere.
    humidity_series_indoor = humidity_series[~humidity_series["Room"].isin(HUMIDITY_HIGHLIGHT_EXCLUDE)]
    if not humidity_series_indoor.empty:
        most_humid_row = humidity_series_indoor.loc[humidity_series_indoor["Value"].idxmax()]
        least_humid_row = humidity_series_indoor.loc[humidity_series_indoor["Value"].idxmin()]
        insights.append(
            f"Peak humidity this week: {most_humid_row['Value']:.1f}% in {most_humid_row['Room']} "
            f"on {most_humid_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )
        insights.append(
            f"Lowest humidity this week: {least_humid_row['Value']:.1f}% in {least_humid_row['Room']} "
            f"on {least_humid_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )

    # Whole-house means cover the house interior only - lofts and outside
    # aren't part of the living space, so they're excluded here even though
    # this isn't a max/min figure. The network cupboard stays included.
    temp_series_house = temp_series[~temp_series["Room"].isin(HOUSE_INTERIOR_EXCLUDE)]
    humidity_series_house = humidity_series[~humidity_series["Room"].isin(HOUSE_INTERIOR_EXCLUDE)]
    if not temp_series_house.empty:
        insights.append(
            f"Mean temperature in the SSH this week was {temp_series_house['Value'].mean():.1f}°C."
        )
    if not humidity_series_house.empty:
        insights.append(
            f"Mean humidity in the SSH this week was {humidity_series_house['Value'].mean():.1f}%."
        )

    # Outside's condensation margin is always going to differ from the indoor
    # rooms in a way that isn't a useful "worst room" comment - excluded from
    # this specific figure only; it stays fully visible in the chart. Not
    # rendered as a Summary bullet (the Condensation risk margin section
    # below covers it), but still exposed in stats JSON for the AI insights.
    indoor_margin = {k: v for k, v in worst_margin.items() if k not in CONDENSATION_HIGHLIGHT_EXCLUDE}

    if "Current - Cumulative Amp.hours" in window_df.columns:
        series = window_df["Current - Cumulative Amp.hours"].dropna()
        if len(series) >= 2:
            insights.append(f"Amp-hours consumed this week: {series.iloc[-1] - series.iloc[0]:.2f} Ah.")

    # Daily house-interior mean, for spotting a pattern across the week
    # cheaply from a handful of numbers instead of re-reading raw history or
    # visually inspecting the chart images. Same room scope as the whole-house
    # means above (excludes lofts/outside, keeps the network cupboard).
    daily_indoor_temp = (
        temp_series_house
        .assign(Day=lambda d: d["MessageDate"].dt.strftime("%Y-%m-%d"))
        .groupby("Day")["Value"].mean()
    )
    daily_indoor_humidity = (
        humidity_series_house
        .assign(Day=lambda d: d["MessageDate"].dt.strftime("%Y-%m-%d"))
        .groupby("Day")["Value"].mean()
    )

    # Every short-window swing per metric (e.g. a window/door being opened),
    # restricted to actual living-space rooms - outdoor and loft readings
    # swing with the weather and aren't informative here. Also grouped into
    # "collective" events where several rooms swing the same way at once,
    # which is stronger evidence of a whole-house event than any single room.
    temp_swing_events = detect_sharp_changes(
        temp_series, SHARP_CHANGE_WINDOW_MINUTES, SHARP_TEMPERATURE_THRESHOLD_C
    )
    humidity_swing_events = detect_sharp_changes(
        humidity_series, SHARP_CHANGE_WINDOW_MINUTES, SHARP_HUMIDITY_THRESHOLD_PCT
    )
    collective_temp_events = find_collective_events(temp_swing_events)
    collective_humidity_events = find_collective_events(humidity_swing_events)

    report_label = f"{start.strftime('%Y-%m-%d')}_to_{end.strftime('%Y-%m-%d')}"

    # Compact machine-readable stats, meant for a separate lightweight process
    # (e.g. a local scheduled Claude routine) to turn into narrative commentary
    # without needing to re-read the full history file.
    stats = {
        "report_label": report_label,
        "window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "rooms": rooms,
        "avg_temperature_by_room": {k: round(v, 1) for k, v in avg_temperature.items()},
        "avg_humidity_by_room": {k: round(v, 1) for k, v in avg_humidity.items()},
        "worst_condensation_margin_by_room": {k: round(v, 1) for k, v in worst_margin.items()},
        "mean_temperature_c": (
            round(float(temp_series_house["Value"].mean()), 1) if not temp_series_house.empty else None
        ),
        "mean_humidity_pct": (
            round(float(humidity_series_house["Value"].mean()), 1) if not humidity_series_house.empty else None
        ),
        "daily_mean_temperature_indoor": [
            {"date": d, "value": round(v, 1)} for d, v in daily_indoor_temp.items()
        ],
        "daily_mean_humidity_indoor": [
            {"date": d, "value": round(v, 1)} for d, v in daily_indoor_humidity.items()
        ],
        "comfort_standard": {
            "name": "CIBSE Guide A",
            "target_temperature_living_areas_c": 21.0,
            "target_temperature_bedrooms_c": 18.0,
            "comfortable_humidity_range_pct_rh": [40.0, 60.0],
        },
        "thermal_comfort_ranking": comfort_ranking,
        "temperature_swing_events": temp_swing_events,
        "humidity_swing_events": humidity_swing_events,
        "collective_temperature_events": collective_temp_events,
        "collective_humidity_events": collective_humidity_events,
        "insights": insights,
    }
    if comfort_ranking:
        stats["best_comfort_room"] = comfort_ranking[0]
        stats["worst_comfort_room"] = comfort_ranking[-1]
    if not temp_series_indoor.empty:
        stats["peak_temperature"] = {
            "value": round(float(hottest_row["Value"]), 1),
            "room": hottest_row["Room"],
            "timestamp": hottest_row["MessageDate"].strftime("%Y-%m-%d %H:%M:%S"),
        }
        stats["lowest_temperature"] = {
            "value": round(float(coldest_row["Value"]), 1),
            "room": coldest_row["Room"],
            "timestamp": coldest_row["MessageDate"].strftime("%Y-%m-%d %H:%M:%S"),
        }
    if not humidity_series_indoor.empty:
        stats["peak_humidity"] = {
            "value": round(float(most_humid_row["Value"]), 1),
            "room": most_humid_row["Room"],
            "timestamp": most_humid_row["MessageDate"].strftime("%Y-%m-%d %H:%M:%S"),
        }
        stats["lowest_humidity"] = {
            "value": round(float(least_humid_row["Value"]), 1),
            "room": least_humid_row["Room"],
            "timestamp": least_humid_row["MessageDate"].strftime("%Y-%m-%d %H:%M:%S"),
        }
    if indoor_margin:
        tightest_room = min(indoor_margin, key=indoor_margin.get)
        stats["tightest_condensation_margin"] = {
            "value": round(indoor_margin[tightest_room], 1),
            "room": tightest_room,
        }
    if "Current - Cumulative Amp.hours" in window_df.columns:
        series = window_df["Current - Cumulative Amp.hours"].dropna()
        if len(series) >= 2:
            stats["amp_hours_used"] = round(float(series.iloc[-1] - series.iloc[0]), 2)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Weekly sensor report - {report_label}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ margin-bottom: 0.2rem; }}
.subtitle {{ color: #666; margin-top: 0; }}
img {{ max-width: 100%; height: auto; display: block; margin: 1.5rem 0; }}
ul {{ line-height: 1.6; }}
#ai-insights {{ background: #f4f6fb; border-left: 3px solid #4a7ab5; padding: 0.1rem 1rem; border-radius: 4px; }}
#ai-insights .ai-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #4a7ab5; font-weight: 600; }}
</style></head>
<body>
<h1>Weekly sensor report</h1>
<p class="subtitle">Window: {start.strftime('%Y-%m-%d %H:%M')} &ndash; {end.strftime('%Y-%m-%d %H:%M')} (UTC)</p>
<h2>Summary</h2>
<ul>{''.join(f'<li>{line}</li>' for line in insights)}</ul>
<h2>AI insights</h2>
<div id="ai-insights"><!-- AI_INSIGHTS_PLACEHOLDER --><p><em>Not yet generated.</em></p></div>
<h2>Temperature</h2>
<h3>Raw readings</h3>
<img src="data:image/png;base64,{raw_temp_chart}" alt="Raw temperature readings by room">
<h3>Daily mean</h3>
<img src="data:image/png;base64,{daily_temp_chart}" alt="Daily mean temperature by room">
<h2>Humidity</h2>
<h3>Raw readings</h3>
<img src="data:image/png;base64,{raw_humidity_chart}" alt="Raw humidity readings by room">
<h3>Daily mean</h3>
<img src="data:image/png;base64,{daily_humidity_chart}" alt="Daily mean humidity by room">
<h2>Condensation risk margin</h2>
<p>Worst-case (minimum) margin seen per room over the week. Below 3&deg;C is elevated risk, below 1&deg;C is high risk.
Bars are colored by sensor type &mdash; <span style="color:#4a7ab5">rooms</span>, <span style="color:#c2793d">lofts</span>,
and <span style="color:#2e8b57">outside</span> &mdash; since these swing very differently and shouldn't be judged on the same footing.</p>
<img src="data:image/png;base64,{margin_chart}" alt="Weekly worst-case condensation risk margin by room">
</body></html>
"""

    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = WEEKLY_DIR / f"{report_label}.html"
    dated_path.write_text(html, encoding="utf-8")
    LATEST_FILE.write_text(html, encoding="utf-8")

    stats_path = WEEKLY_DIR / f"{report_label}_stats.json"
    stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    (WEEKLY_DIR / "latest_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    print(f"[weekly] wrote {dated_path}, {LATEST_FILE}, and {stats_path}")


if __name__ == "__main__":
    main()
