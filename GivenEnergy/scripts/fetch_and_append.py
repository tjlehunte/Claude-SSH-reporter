#!/usr/bin/env python3
"""Fetch new GivenEnergy energy-flow readings and append them to data/history.jsonl.

Ports the logic of the working `get_givenergy_data()` R function: POST to the
inverter's energy-flows endpoint for a date range, half-hourly grouping
(grouping=0), and unpack each interval's 7 flow values.

Dedup strategy: track the max `start` timestamp already on disk (the
"high-water mark") and only append rows whose `start` isn't already present,
same approach as the Monnit pipeline's MessageDate high-water mark.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.jsonl"

LOCAL_TZ = ZoneInfo("Europe/London")
API_BASE = "https://api.givenergy.cloud"
INVERTER_SERIAL = "ED2052G003"
ENERGY_FLOWS_PATH = f"v1/inverter/{INVERTER_SERIAL}/energy-flows"
REQUEST_TIMEOUT = 30
CHUNK_DAYS = 7  # cheap insurance against oversized-range errors, even though the API may tolerate large ranges

# The API returns each interval's `data` as an object keyed by string indices
# ("0".."6"), not named flow keys - confirmed against a real response, and
# consistent with the source R script, which manually renames them in this
# exact order rather than trusting JSON keys. If GivenEnergy ever changes
# this ordering, every downstream stat silently mislabels itself, so this
# list is the single source of truth other scripts import from.
FLOW_NAMES = [
    "PV to Home",
    "PV to Battery",
    "PV to Grid",
    "Grid to Home",
    "Grid to Battery",
    "Battery to Home",
    "Battery to Grid",
]


def load_history():
    if not HISTORY_FILE.exists():
        return []
    rows = []
    with HISTORY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def high_water_mark(rows):
    if not rows:
        return None
    return max(row["start"] for row in rows)


def parse_ts(raw):
    """The exact raw format the API returns isn't confirmed (the source R
    script's ymd_hm() output always shows seconds, whether or not the API
    sends them) - try with seconds first, fall back to without."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp format: {raw!r}")


def date_chunks(start, end, chunk_days=CHUNK_DAYS):
    """Split [start, end] into <=chunk_days spans (inclusive of a final short one)."""
    step = timedelta(days=chunk_days)
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks or [(start, end)]


def fetch_chunk(bearer_token, start_date, end_date):
    """Fetch one date-range chunk. Returns a list of interval dicts, or an
    empty list if the response isn't the expected shape (logged, not raised) -
    same defensive-skip approach as the Monnit gateway's oversized-range errors."""
    resp = requests.post(
        f"{API_BASE}/{ENERGY_FLOWS_PATH}",
        params={
            "start_time": start_date.isoformat(),
            "end_time": end_date.isoformat(),
            "grouping": 0,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    # The API wraps `data` as an object keyed by string indices ("0", "1", ...)
    # rather than a JSON array, both at the top level and for each interval's
    # own `data` payload - confirmed against a real response, not just
    # inferred from the R script. dict.values() preserves insertion order
    # (guaranteed since Python 3.7), which matches the numeric key order here.
    raw_intervals = payload.get("data") if isinstance(payload, dict) else None
    intervals = list(raw_intervals.values()) if isinstance(raw_intervals, dict) else raw_intervals
    if not isinstance(intervals, list):
        print(
            f"[fetch] unexpected response shape for {start_date}..{end_date}: {payload!r}",
            file=sys.stderr,
        )
        return []

    rows = []
    for interval in intervals:
        if not isinstance(interval, dict) or "start_time" not in interval or "end_time" not in interval:
            print(f"[fetch] skipping malformed interval in {start_date}..{end_date}: {interval!r}", file=sys.stderr)
            continue
        raw_values = interval.get("data")
        values = list(raw_values.values()) if isinstance(raw_values, dict) else raw_values
        if not isinstance(values, list) or len(values) != len(FLOW_NAMES):
            print(f"[fetch] skipping interval with unexpected data shape: {interval!r}", file=sys.stderr)
            continue
        row = {"start": interval["start_time"], "end": interval["end_time"]}
        row.update(dict(zip(FLOW_NAMES, values)))
        rows.append(row)
    return rows


def fetch_range(bearer_token, start_date, end_date):
    all_rows = []
    for chunk_start, chunk_end in date_chunks(start_date, end_date):
        all_rows.extend(fetch_chunk(bearer_token, chunk_start, chunk_end))
    return all_rows


def main():
    bearer_token = os.environ.get("GIVENERGY_BEARER_TOKEN")
    if not bearer_token:
        raise RuntimeError("GIVENERGY_BEARER_TOKEN not set")

    history = load_history()
    watermark = high_water_mark(history)
    backfill_days = os.environ.get("GIVENERGY_BACKFILL_DAYS") or None

    # Use the local (Europe/London) calendar date, not the runner's UTC date -
    # the API's own dates are local, and a manual dispatch near midnight could
    # otherwise request the wrong day.
    today = datetime.now(LOCAL_TZ).date()
    if backfill_days:
        start_date = today - timedelta(days=int(backfill_days))
        print(f"[fetch] backfill mode: requesting history since {start_date} ({backfill_days} day(s))")
    elif watermark:
        # Re-request from the watermark's own day (not day+1) so we naturally
        # re-fetch and dedup the last partial day rather than risk a gap.
        start_date = parse_ts(watermark).date()
    else:
        start_date = today - timedelta(days=1)

    end_date = today + timedelta(days=1)  # matches the source script's Sys.Date() + 1

    new_rows = fetch_range(bearer_token, start_date, end_date)

    combined = {row["start"]: row for row in history}
    added = sum(1 for row in new_rows if row["start"] not in combined)
    combined.update({row["start"]: row for row in new_rows})
    all_rows = sorted(combined.values(), key=lambda r: r["start"])

    if added == 0:
        print(f"[fetch] no new records since {watermark or 'beginning'}")
        return

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"[fetch] added {added} new record(s) (total {len(all_rows)}), latest={all_rows[-1]['start']}")


if __name__ == "__main__":
    main()
