#!/usr/bin/env python3
"""Generate a self-contained weekly HTML report from data/history.jsonl.

Reports on the most recently *completed* Monday-Sunday calendar week
(relative to the latest data on hand, not wall-clock "now"): raw energy-flow
time series, daily totals per flow, and self-consumption/self-sufficiency
stats. Also writes a compact stats.json for a later AI-insights routine to
turn into narrative commentary.
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

from billing_utils import calculate_export_revenue, calculate_import_cost, load_export_rates
from energy_utils import (
    FLOW_NAMES,
    flow_totals,
    load_history_wide,
    most_recent_complete_week,
    self_consumption_pct,
    self_sufficiency_pct,
    total_consumption,
    total_export,
    total_generation,
    total_import,
)

ROOT = Path(__file__).resolve().parent.parent
WEEKLY_DIR = ROOT / "reports" / "weekly"
LATEST_FILE = WEEKLY_DIR / "latest.html"
WINDOW_DAYS = 7

# 7 of the 8 hues from the categorical palette (blue/aqua/yellow/green/violet/
# red/orange - magenta dropped), reordered so each pair that's always visually
# adjacent (legend order, stacked-bar segments) sits far apart on the hue
# wheel - the previous amber/brown and green/sea-green pairs were too close.
FLOW_COLORS = {
    "PV to Home": "#eda100",
    "PV to Battery": "#2a78d6",
    "PV to Grid": "#eb6834",
    "Grid to Home": "#1baf7a",
    "Grid to Battery": "#e34948",
    "Battery to Home": "#008300",
    "Battery to Grid": "#4a3aa7",
}


def fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def plot_raw_flows(df):
    fig, ax = plt.subplots(figsize=(10, 5))
    for flow in FLOW_NAMES:
        ax.plot(df["start"], df[flow], label=flow, linewidth=0.8, alpha=0.85, color=FLOW_COLORS[flow])
    ax.set_title("Raw energy flows across the week")
    ax.set_ylabel("kWh per half hour")
    ax.margins(x=0)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig


def plot_daily_totals(df):
    daily = df.copy()
    daily["Day"] = pd.to_datetime(daily["start"].dt.date)
    daily_totals = daily.groupby("Day")[FLOW_NAMES].sum().reset_index()

    fig, ax = plt.subplots(figsize=(10, 5))
    bottom = None
    for flow in FLOW_NAMES:
        values = daily_totals[flow]
        ax.bar(daily_totals["Day"], values, bottom=bottom, label=flow, color=FLOW_COLORS[flow])
        bottom = values if bottom is None else bottom + values
    ax.set_title("Daily totals by flow")
    ax.set_ylabel("kWh")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig, daily_totals


def main():
    df_wide = load_history_wide()
    if df_wide.empty:
        print("[weekly] no history yet, skipping report generation")
        return

    latest_ts = df_wide["start"].max()
    week_start, week_end = most_recent_complete_week(latest_ts)
    window_df = df_wide[(df_wide["start"] >= week_start) & (df_wide["start"] < week_end)]

    if window_df.empty:
        # No fully-completed Mon-Sun week yet (pipeline just started) - fall
        # back to whatever partial data exists in the current in-progress week.
        print("[weekly] no completed calendar week yet; reporting partial current week instead")
        week_start, week_end = week_end, week_end + timedelta(days=WINDOW_DAYS)
        window_df = df_wide[(df_wide["start"] >= week_start) & (df_wide["start"] < week_end)]

    if window_df.empty:
        print("[weekly] no data available for any window; skipping report generation")
        return

    start = window_df["start"].min()
    end = window_df["end"].max()
    # The last interval's *end* can roll into the next calendar day (e.g. the
    # 23:30-00:00 interval on the window's last day) - report_label should
    # still name the last day actually covered, not that rollover day.
    last_day = window_df["start"].max()

    totals = flow_totals(window_df)
    generation = float(total_generation(window_df).sum())
    consumption = float(total_consumption(window_df).sum())
    imported = float(total_import(window_df).sum())
    exported = float(total_export(window_df).sum())
    sc = self_consumption_pct(generation, totals["PV to Home"], totals["PV to Battery"])
    ss = self_sufficiency_pct(consumption, imported)

    num_days = (end - start).days
    rates_df = load_export_rates()
    import_cost = calculate_import_cost(imported, num_days)
    export_revenue, rate_coverage_pct = calculate_export_revenue(window_df, rates_df)
    net_bill = import_cost - export_revenue

    raw_flows_chart = fig_to_base64(plot_raw_flows(window_df))
    daily_totals_fig, daily_totals_df = plot_daily_totals(window_df)
    daily_totals_chart = fig_to_base64(daily_totals_fig)

    daily_generation = (
        window_df.assign(Day=window_df["start"].dt.date)
        .assign(Generation=lambda d: d["PV to Home"] + d["PV to Battery"] + d["PV to Grid"])
        .groupby("Day")["Generation"].sum()
    )

    insights = [
        f"Total solar generated this week: {generation:.1f} kWh.",
        f"Total home consumption this week: {consumption:.1f} kWh.",
        f"Imported from grid: {imported:.1f} kWh; exported to grid: {exported:.1f} kWh.",
    ]
    if sc is not None:
        insights.append(f"Self-consumption: {sc:.0f}% of generated solar was used on-site rather than exported.")
    if ss is not None:
        insights.append(f"Self-sufficiency: {ss:.2f}% of home consumption was met without drawing from the grid.")
    if not daily_generation.empty:
        best_day = daily_generation.idxmax()
        worst_day = daily_generation.idxmin()
        insights.append(
            f"Best generation day: {best_day.strftime('%Y-%m-%d')} ({daily_generation[best_day]:.1f} kWh); "
            f"lowest: {worst_day.strftime('%Y-%m-%d')} ({daily_generation[worst_day]:.1f} kWh)."
        )

    insights.append(f"Estimated import cost: £{import_cost:.2f} (unit rate + standing charge).")
    coverage_note = "" if rate_coverage_pct >= 99 else f" (rate data covered {rate_coverage_pct:.0f}% of intervals)"
    insights.append(f"Estimated export revenue: £{export_revenue:.2f} at your Agile Outgoing rate{coverage_note}.")
    if net_bill >= 0:
        insights.append(f"Estimated net cost: £{net_bill:.2f}.")
    else:
        insights.append(f"Estimated net credit: £{abs(net_bill):.2f}.")

    report_label = f"{start.strftime('%Y-%m-%d')}_to_{last_day.strftime('%Y-%m-%d')}"

    # Compact machine-readable stats, meant for a later local AI-insights
    # routine to turn into narrative commentary without re-reading full history.
    stats = {
        "report_label": report_label,
        "window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "window_end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "flow_totals_kwh": totals,
        "total_generation_kwh": round(generation, 2),
        "total_consumption_kwh": round(consumption, 2),
        "total_import_kwh": round(imported, 2),
        "total_export_kwh": round(exported, 2),
        "self_consumption_pct": round(sc, 1) if sc is not None else None,
        "self_sufficiency_pct": round(ss, 2) if ss is not None else None,
        "estimated_import_cost_gbp": round(import_cost, 2),
        "estimated_export_revenue_gbp": round(export_revenue, 2),
        "estimated_net_bill_gbp": round(net_bill, 2),
        "export_rate_coverage_pct": round(rate_coverage_pct, 1),
        "daily_generation_kwh": [
            {"date": d.strftime("%Y-%m-%d"), "value": round(v, 2)} for d, v in daily_generation.items()
        ],
        "insights": insights,
    }
    if not daily_generation.empty:
        stats["best_generation_day"] = {
            "date": best_day.strftime("%Y-%m-%d"),
            "value": round(float(daily_generation[best_day]), 2),
        }
        stats["worst_generation_day"] = {
            "date": worst_day.strftime("%Y-%m-%d"),
            "value": round(float(daily_generation[worst_day]), 2),
        }

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Weekly GivenEnergy report - {report_label}</title>
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
<h1>Weekly energy report</h1>
<p class="subtitle">Window: {start.strftime('%Y-%m-%d %H:%M')} &ndash; {end.strftime('%Y-%m-%d %H:%M')} (local time)</p>
<h2>Summary</h2>
<ul>{''.join(f'<li>{line}</li>' for line in insights)}</ul>
<h2>AI insights</h2>
<div id="ai-insights"><!-- AI_INSIGHTS_PLACEHOLDER --><p><em>Not yet generated.</em></p></div>
<h2>Energy flows</h2>
<h3>Raw readings</h3>
<img src="data:image/png;base64,{raw_flows_chart}" alt="Raw energy flows across the week">
<h3>Daily totals</h3>
<img src="data:image/png;base64,{daily_totals_chart}" alt="Daily totals by flow">
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
