#!/usr/bin/env python3
"""Fetch half-hourly Agile Outgoing export rates and cache them to
data/octopus_export_rates.jsonl.

The rates endpoint (product + tariff code) is public - no API key needed,
confirmed against a real request. Mirrors fetch_and_append.py's shape: a
high-water-mark for the normal incremental case, and the same
GIVENERGY_BACKFILL_DAYS env var for a one-time historical pull, so a backfill
of energy history and a backfill of rates always cover the same window.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from billing_utils import OCTOPUS_PRODUCT_CODE, OCTOPUS_TARIFF_CODE, RATES_FILE

API_BASE = "https://api.octopus.energy/v1"
RATES_PATH = f"products/{OCTOPUS_PRODUCT_CODE}/electricity-tariffs/{OCTOPUS_TARIFF_CODE}/standard-unit-rates/"
LOCAL_TZ = ZoneInfo("Europe/London")
REQUEST_TIMEOUT = 30


def load_history():
    if not RATES_FILE.exists():
        return []
    rows = []
    with RATES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def high_water_mark(rows):
    if not rows:
        return None
    return max(row["valid_from"] for row in rows)


def fetch_rates(period_from, period_to):
    """Fetch all pages of rates in [period_from, period_to) (UTC datetimes)."""
    params = {
        "period_from": period_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_to": period_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 1500,
    }
    url = f"{API_BASE}/{RATES_PATH}"
    rows = []
    while url:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results")
        if not isinstance(results, list):
            print(f"[octopus] unexpected response shape: {payload!r}", file=sys.stderr)
            break
        for item in results:
            if not isinstance(item, dict) or "valid_from" not in item or "value_inc_vat" not in item:
                print(f"[octopus] skipping malformed rate entry: {item!r}", file=sys.stderr)
                continue
            rows.append({"valid_from": item["valid_from"], "rate_pence_per_kwh": item["value_inc_vat"]})
        url = payload.get("next")
        params = None  # the `next` link already has query params baked in
    return rows


def main():
    history = load_history()
    watermark = high_water_mark(history)
    backfill_days = os.environ.get("GIVENERGY_BACKFILL_DAYS") or None

    today = datetime.now(LOCAL_TZ).date()
    if backfill_days:
        start_date = today - timedelta(days=int(backfill_days))
        print(f"[octopus] backfill mode: requesting rates since {start_date} ({backfill_days} day(s))")
    elif watermark:
        start_date = datetime.fromisoformat(watermark.replace("Z", "+00:00")).date() - timedelta(days=1)
    else:
        start_date = today - timedelta(days=1)

    period_from = datetime(start_date.year, start_date.month, start_date.day, tzinfo=ZoneInfo("UTC"))
    period_to = datetime.now(ZoneInfo("UTC")) + timedelta(days=1)

    new_rows = fetch_rates(period_from, period_to)

    combined = {row["valid_from"]: row for row in history}
    added = sum(1 for row in new_rows if row["valid_from"] not in combined)
    combined.update({row["valid_from"]: row for row in new_rows})
    all_rows = sorted(combined.values(), key=lambda r: r["valid_from"])

    if added == 0:
        print(f"[octopus] no new rates since {watermark or 'beginning'}")
        return

    RATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RATES_FILE.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"[octopus] added {added} new rate(s) (total {len(all_rows)}), latest={all_rows[-1]['valid_from']}")


if __name__ == "__main__":
    main()
