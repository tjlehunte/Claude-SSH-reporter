#!/usr/bin/env python3
"""Generate a self-contained daily HTML report from data/history.jsonl.

Reports on the most recently *completed* UTC calendar day (00:00-23:50,
relative to the latest data on hand, not wall-clock "now") - i.e. "yesterday"
from the point of view of a run shortly after midnight. Produces three charts
(temperature over time, latest humidity per room, latest condensation-risk
margin per room) plus a short text summary, and writes
reports/daily/<date>.html and reports/latest.html.
"""
import base64
import io
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from sensor_utils import (
    condensation_margin,
    load_history_wide,
    risk_color,
    room_order,
    to_long,
    CONDENSATION_HIGHLIGHT_EXCLUDE,
    HUMIDITY_HIGHLIGHT_EXCLUDE,
    PEAK_TEMPERATURE_EXCLUDE,
)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "reports" / "daily"
LATEST_FILE = ROOT / "reports" / "latest.html"


def most_recent_complete_day(latest_ts):
    """Midnight of latest_ts's day, and the midnight before it (yesterday)."""
    day_start = latest_ts.normalize()
    return day_start - timedelta(days=1), day_start


def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def plot_temperature(long_df, rooms):
    fig, ax = plt.subplots(figsize=(10, 5))
    for room in rooms:
        series = long_df[(long_df["Room"] == room) & (long_df["Metric"] == "Temperature")].sort_values(
            "MessageDate"
        )
        if series.empty:
            continue
        ax.plot(series["MessageDate"], series["Value"], label=room, linewidth=1.3)
    ax.set_title("Temperature by room")
    ax.set_ylabel("°C")
    ax.margins(x=0)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8, ncol=1)
    fig.autofmt_xdate()
    return fig


def plot_latest_bar(latest, rooms, value_col, title, ylabel, color_fn=None):
    values = [latest.get(room, float("nan")) for room in rooms]
    colors = [color_fn(v) if color_fn and v == v else "#4a7ab5" for v, room in zip(values, rooms)]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(rooms, values, color=colors)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="x", rotation=60)
    return fig


def latest_by_room(long_df, metric):
    sub = long_df[long_df["Metric"] == metric]
    if sub.empty:
        return {}
    idx = sub.groupby("Room")["MessageDate"].idxmax()
    latest_rows = sub.loc[idx]
    return dict(zip(latest_rows["Room"], latest_rows["Value"]))


def build_insights(window_df, latest_temp, latest_humidity, latest_margin, rooms):
    lines = []
    # Outside and the unheated lofts would win warmest/coolest every single
    # day by nature, which makes the figure meaningless as a comment on the
    # house - restricted to living/utility rooms, same as the weekly report.
    indoor_temp = {r: v for r, v in latest_temp.items() if r not in PEAK_TEMPERATURE_EXCLUDE}
    if indoor_temp:
        hottest = max(indoor_temp, key=indoor_temp.get)
        coldest = min(indoor_temp, key=indoor_temp.get)
        lines.append(
            f"Warmest room: {hottest} ({indoor_temp[hottest]:.1f}°C); "
            f"coolest room: {coldest} ({indoor_temp[coldest]:.1f}°C)."
        )
    indoor_humidity = {r: v for r, v in latest_humidity.items() if r not in HUMIDITY_HIGHLIGHT_EXCLUDE}
    if indoor_humidity:
        most_humid = max(indoor_humidity, key=indoor_humidity.get)
        least_humid = min(indoor_humidity, key=indoor_humidity.get)
        lines.append(
            f"Most humid room: {most_humid} ({indoor_humidity[most_humid]:.0f}% RH); "
            f"least humid room: {least_humid} ({indoor_humidity[least_humid]:.0f}% RH)."
        )
    if latest_margin:
        at_risk = {r: m for r, m in latest_margin.items() if r not in CONDENSATION_HIGHLIGHT_EXCLUDE and m < 3.0}
        if at_risk:
            details = ", ".join(f"{r} ({m:.1f}°C)" for r, m in sorted(at_risk.items(), key=lambda kv: kv[1]))
            lines.append(f"Condensation risk margin below 3°C in: {details}.")
        else:
            lines.append("No rooms currently below the 3°C condensation-risk margin.")
    current_cols = [c for c in window_df.columns if c.startswith("Current - ")]
    if "Current - Cumulative Amp.hours" in window_df.columns:
        series = window_df["Current - Cumulative Amp.hours"].dropna()
        if len(series) >= 2:
            lines.append(f"Amp-hours consumed in this window: {series.iloc[-1] - series.iloc[0]:.2f} Ah.")
    return lines


def main():
    df_wide = load_history_wide()
    if df_wide.empty:
        print("[report] no history yet, skipping report generation")
        return

    latest_ts = df_wide["MessageDate"].max()
    start, end = most_recent_complete_day(latest_ts)
    window_df = df_wide[(df_wide["MessageDate"] >= start) & (df_wide["MessageDate"] < end)]

    if window_df.empty:
        # No fully-completed day yet (pipeline just started) - fall back to
        # whatever partial data exists in the current in-progress day.
        print("[report] no completed calendar day yet; reporting partial current day instead")
        start, end = end, end + timedelta(days=1)
        window_df = df_wide[(df_wide["MessageDate"] >= start) & (df_wide["MessageDate"] < end)]

    long_df = to_long(window_df)
    rooms = room_order(long_df)

    latest_temp = latest_by_room(long_df, "Temperature")
    latest_humidity = latest_by_room(long_df, "Humidity")
    margin_df = condensation_margin(long_df)
    latest_margin = {}
    if not margin_df.empty:
        idx = margin_df.groupby("Room")["MessageDate"].idxmax()
        latest_margin = dict(zip(margin_df.loc[idx, "Room"], margin_df.loc[idx, "Margin"]))

    temp_chart = fig_to_base64(plot_temperature(long_df, rooms))
    humidity_chart = fig_to_base64(
        plot_latest_bar(latest_humidity, rooms, "Humidity", "Latest humidity by room", "% RH")
    )
    margin_chart = fig_to_base64(
        plot_latest_bar(
            latest_margin, rooms, "Margin", "Condensation risk margin (Temperature − Dewpoint)", "°C",
            color_fn=risk_color,
        )
    )

    insights = build_insights(window_df, latest_temp, latest_humidity, latest_margin, rooms)
    report_date = start.strftime("%Y-%m-%d")
    display_end = window_df["MessageDate"].max() if not window_df.empty else end - timedelta(minutes=10)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Sensor report - {report_date}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ margin-bottom: 0.2rem; }}
.subtitle {{ color: #666; margin-top: 0; }}
img {{ max-width: 100%; height: auto; display: block; margin: 1.5rem 0; }}
ul {{ line-height: 1.6; }}
</style></head>
<body>
<h1>Daily sensor report</h1>
<p class="subtitle">Window: {start.strftime('%Y-%m-%d %H:%M')} &ndash; {display_end.strftime('%Y-%m-%d %H:%M')} (UTC)</p>
<h2>Summary</h2>
<ul>{''.join(f'<li>{line}</li>' for line in insights)}</ul>
<h2>Temperature</h2>
<img src="data:image/png;base64,{temp_chart}" alt="Temperature by room">
<h2>Humidity</h2>
<img src="data:image/png;base64,{humidity_chart}" alt="Latest humidity by room">
<h2>Condensation risk margin</h2>
<p>Margin = Temperature &minus; Dewpoint. Below {3.0}&deg;C is elevated risk, below {1.0}&deg;C is high risk.</p>
<img src="data:image/png;base64,{margin_chart}" alt="Condensation risk margin by room">
</body></html>
"""

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    dated_path = DAILY_DIR / f"{report_date}.html"
    dated_path.write_text(html, encoding="utf-8")
    LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    LATEST_FILE.write_text(html, encoding="utf-8")
    print(f"[report] wrote {dated_path} and {LATEST_FILE}")


if __name__ == "__main__":
    main()
