#!/usr/bin/env python3
"""Generate a self-contained weekly HTML report from data/history.jsonl.

Collates the most recent 7 days present in the history file: daily mean
temperature per room, weekly average humidity per room, and the weekly
worst-case (minimum) condensation-risk margin per room.
"""
import base64
import io
import json
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sensor_utils import condensation_margin, load_history_wide, risk_color, room_order, to_long

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


def plot_daily_temperature(long_df, rooms):
    temp = long_df[long_df["Metric"] == "Temperature"].copy()
    temp["Day"] = temp["MessageDate"].dt.date
    daily_mean = temp.groupby(["Day", "Room"])["Value"].mean().reset_index()

    fig, ax = plt.subplots(figsize=(10, 5))
    for room in rooms:
        series = daily_mean[daily_mean["Room"] == room].sort_values("Day")
        if series.empty:
            continue
        ax.plot(series["Day"], series["Value"], marker="o", label=room, linewidth=1.3)
    ax.set_title("Daily mean temperature by room")
    ax.set_ylabel("°C")
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig


def plot_bar(values_by_room, rooms, title, ylabel, color_fn=None):
    values = [values_by_room.get(room, float("nan")) for room in rooms]
    colors = [color_fn(v) if color_fn and v == v else "#4a7ab5" for v in values]
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

    end = df_wide["MessageDate"].max()
    start = end - timedelta(days=WINDOW_DAYS)
    window_df = df_wide[df_wide["MessageDate"] >= start]
    long_df = to_long(window_df)
    rooms = room_order(long_df)

    avg_humidity = long_df[long_df["Metric"] == "Humidity"].groupby("Room")["Value"].mean().to_dict()

    margin_df = condensation_margin(long_df)
    worst_margin = margin_df.groupby("Room")["Margin"].min().to_dict() if not margin_df.empty else {}

    temp_chart = fig_to_base64(plot_daily_temperature(long_df, rooms))
    humidity_chart = fig_to_base64(
        plot_bar(avg_humidity, rooms, "Weekly average humidity by room", "% RH")
    )
    margin_chart = fig_to_base64(
        plot_bar(
            worst_margin, rooms, "Weekly worst-case condensation risk margin", "°C (minimum seen)",
            color_fn=risk_color,
        )
    )

    insights = []
    temp_series = long_df[long_df["Metric"] == "Temperature"]
    if not temp_series.empty:
        hottest_row = temp_series.loc[temp_series["Value"].idxmax()]
        coldest_row = temp_series.loc[temp_series["Value"].idxmin()]
        insights.append(
            f"Peak temperature this week: {hottest_row['Value']:.1f}°C in {hottest_row['Room']} "
            f"on {hottest_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )
        insights.append(
            f"Lowest temperature this week: {coldest_row['Value']:.1f}°C in {coldest_row['Room']} "
            f"on {coldest_row['MessageDate'].strftime('%Y-%m-%d %H:%M')}."
        )
    if avg_humidity:
        most_humid = max(avg_humidity, key=avg_humidity.get)
        insights.append(f"Most humid room on average: {most_humid} ({avg_humidity[most_humid]:.0f}% RH).")
    if worst_margin:
        worst_room = min(worst_margin, key=worst_margin.get)
        insights.append(
            f"Tightest condensation-risk margin this week: {worst_margin[worst_room]:.1f}°C in {worst_room}."
        )
    if "Current - Cumulative Amp.hours" in window_df.columns:
        series = window_df["Current - Cumulative Amp.hours"].dropna()
        if len(series) >= 2:
            insights.append(f"Amp-hours consumed this week: {series.iloc[-1] - series.iloc[0]:.2f} Ah.")

    report_label = f"{start.strftime('%Y-%m-%d')}_to_{end.strftime('%Y-%m-%d')}"

    # Compact machine-readable stats, meant for a separate lightweight process
    # (e.g. a local scheduled Claude routine) to turn into narrative commentary
    # without needing to re-read the full history file.
    stats = {
        "report_label": report_label,
        "window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "rooms": rooms,
        "avg_humidity_by_room": {k: round(v, 1) for k, v in avg_humidity.items()},
        "worst_condensation_margin_by_room": {k: round(v, 1) for k, v in worst_margin.items()},
        "insights": insights,
    }
    if not temp_series.empty:
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
<img src="data:image/png;base64,{temp_chart}" alt="Daily mean temperature by room">
<h2>Humidity</h2>
<img src="data:image/png;base64,{humidity_chart}" alt="Weekly average humidity by room">
<h2>Condensation risk margin</h2>
<p>Worst-case (minimum) margin seen per room over the week. Below 3&deg;C is elevated risk, below 1&deg;C is high risk.</p>
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
