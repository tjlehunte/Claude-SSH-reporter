#!/usr/bin/env python3
"""Generate a self-contained daily HTML report from data/history.jsonl.

Reports on the most recently *completed* local calendar day (relative to the
latest data on hand, not wall-clock "now") - i.e. "yesterday" from the point
of view of a run shortly after midnight. Produces a time-series chart of all
7 energy flows, a bar chart of the day's totals, and a short text summary.
"""
import base64
import io
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from billing_utils import calculate_export_revenue, calculate_import_cost, load_export_rates
from energy_utils import (
    FLOW_NAMES,
    flow_totals,
    load_history_wide,
    most_recent_complete_day,
    self_consumption_pct,
    self_sufficiency_pct,
    total_consumption,
    total_export,
    total_generation,
    total_import,
)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "reports" / "daily"
LATEST_FILE = ROOT / "reports" / "latest.html"

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


def plot_flows(df):
    fig, ax = plt.subplots(figsize=(10, 5))
    for flow in FLOW_NAMES:
        ax.plot(df["start"], df[flow], label=flow, linewidth=1.3, color=FLOW_COLORS[flow])
    ax.set_title("Energy flows through the day")
    ax.set_ylabel("kWh per half hour")
    ax.margins(x=0)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.autofmt_xdate()
    return fig


def plot_totals_bar(totals):
    flows = list(totals.keys())
    values = [totals[f] for f in flows]
    colors = [FLOW_COLORS[f] for f in flows]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(flows, values, color=colors)
    ax.set_title("Total energy by flow, today")
    ax.set_ylabel("kWh")
    ax.tick_params(axis="x", rotation=30)
    return fig


def build_insights(df, totals, num_days, rates_df):
    lines = []
    generation = float(total_generation(df).sum())
    consumption = float(total_consumption(df).sum())
    imported = float(total_import(df).sum())
    exported = float(total_export(df).sum())
    lines.append(f"Total solar generated: {generation:.2f} kWh.")
    lines.append(f"Total home consumption: {consumption:.2f} kWh.")
    lines.append(f"Imported from grid: {imported:.2f} kWh; exported to grid: {exported:.2f} kWh.")
    sc = self_consumption_pct(generation, totals["PV to Home"], totals["PV to Battery"])
    if sc is not None:
        lines.append(f"Self-consumption: {sc:.0f}% of generated solar was used on-site rather than exported.")
    ss = self_sufficiency_pct(consumption, imported)
    if ss is not None:
        lines.append(f"Self-sufficiency: {ss:.2f}% of home consumption was met without drawing from the grid.")

    import_cost = calculate_import_cost(imported, num_days)
    export_revenue, coverage_pct = calculate_export_revenue(df, rates_df)
    net = import_cost - export_revenue
    lines.append(f"Estimated import cost: £{import_cost:.2f} (unit rate + standing charge).")
    coverage_note = "" if coverage_pct >= 99 else f" (rate data covered {coverage_pct:.0f}% of intervals)"
    lines.append(f"Estimated export revenue: £{export_revenue:.2f} at your Agile Outgoing rate{coverage_note}.")
    if net >= 0:
        lines.append(f"Estimated net cost: £{net:.2f}.")
    else:
        lines.append(f"Estimated net credit: £{abs(net):.2f}.")
    return lines


def main():
    df_wide = load_history_wide()
    if df_wide.empty:
        print("[report] no history yet, skipping report generation")
        return

    latest_ts = df_wide["start"].max()
    start, end = most_recent_complete_day(latest_ts)
    window_df = df_wide[(df_wide["start"] >= start) & (df_wide["start"] < end)]

    if window_df.empty:
        # No fully-completed day yet (pipeline just started) - fall back to
        # whatever partial data exists in the current in-progress day.
        print("[report] no completed calendar day yet; reporting partial current day instead")
        start, end = end, end + timedelta(days=1)
        window_df = df_wide[(df_wide["start"] >= start) & (df_wide["start"] < end)]

    if window_df.empty:
        print("[report] no data available for any window; skipping report generation")
        return

    totals = flow_totals(window_df)
    flows_chart = fig_to_base64(plot_flows(window_df))
    totals_chart = fig_to_base64(plot_totals_bar(totals))
    num_days = (end - start).days
    rates_df = load_export_rates()
    insights = build_insights(window_df, totals, num_days, rates_df)

    report_date = start.strftime("%Y-%m-%d")
    display_end = window_df["end"].max()

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>GivenEnergy report - {report_date}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ margin-bottom: 0.2rem; }}
.subtitle {{ color: #666; margin-top: 0; }}
img {{ max-width: 100%; height: auto; display: block; margin: 1.5rem 0; }}
ul {{ line-height: 1.6; }}
</style></head>
<body>
<h1>Daily energy report</h1>
<p class="subtitle">Window: {start.strftime('%Y-%m-%d %H:%M')} &ndash; {display_end.strftime('%Y-%m-%d %H:%M')} (local time)</p>
<h2>Summary</h2>
<ul>{''.join(f'<li>{line}</li>' for line in insights)}</ul>
<h2>Energy flows</h2>
<img src="data:image/png;base64,{flows_chart}" alt="Energy flows through the day">
<h2>Daily totals</h2>
<img src="data:image/png;base64,{totals_chart}" alt="Total energy by flow">
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
