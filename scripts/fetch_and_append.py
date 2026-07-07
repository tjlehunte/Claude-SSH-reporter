#!/usr/bin/env python3
"""Fetch new Monnit sensor readings and append them to data/history.jsonl.

Tries the Monnit gateway directly first (same SensorList -> per-sensor
SensorDataMessages flow as the existing R/Plumber script), then falls back
to the Render proxy (https://monnit-plumber-api.onrender.com/data) if the
gateway is unreachable or credentials aren't configured.

Dedup strategy: track the max MessageDate already on disk (the "high-water
mark") and only append rows with a MessageDate strictly newer than that,
skipping any that somehow already exist.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.jsonl"

GATEWAY_BASE = "https://146.87.171.55/json"
NETWORK_ID = "6"  # SSH gateway, per the existing R script
PROXY_URL = "https://monnit-plumber-api.onrender.com/data"
DEFAULT_LOOKBACK_HOURS = 36
REQUEST_TIMEOUT = 15
PROXY_TIMEOUT = 60  # Render free tier can be slow to wake from sleep
CHUNK_DAYS = 5  # the gateway errors on overly-large SensorDataMessages date ranges

# Order of comma-separated fields Monnit returns for a humidity/temp combo sensor.
METRIC_ORDER = ["Humidity", "Temperature", "Dewpoint", "gpkg", "Heat Index", "Wet Bulb"]
CURRENT_COLUMNS = [
    "Current - Cumulative Amp.hours",
    "Current - Average current",
    "Current - Maximum current",
    "Current - Minimum current",
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
    return max(row["MessageDate"] for row in rows)


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_monnit_date(raw):
    """Monnit returns .NET-style '/Date(1751840400000)/' strings (epoch ms)."""
    match = re.search(r"\d+", raw)
    epoch_ms = int(match.group())
    dt = datetime.utcfromtimestamp(epoch_ms / 1000)
    # Floor to the 10-minute sampling grid, matching the existing R pipeline.
    floored_minute = dt.minute - (dt.minute % 10)
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


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


def fetch_gateway(since_utc):
    key_id = os.environ.get("MONNIT_API_KEY_ID")
    secret = os.environ.get("MONNIT_API_SECRET_KEY")
    if not key_id or not secret:
        raise RuntimeError("MONNIT_API_KEY_ID/MONNIT_API_SECRET_KEY not set")

    headers = {"APIKeyID": key_id, "APISecretKey": secret}

    sensor_list_resp = requests.get(
        f"{GATEWAY_BASE}/SensorList",
        params={"NetworkID": NETWORK_ID},
        headers=headers,
        verify=False,
        timeout=REQUEST_TIMEOUT,
    )
    sensor_list_resp.raise_for_status()
    sensors = sensor_list_resp.json().get("Result") or []
    if not sensors:
        raise RuntimeError("gateway returned no sensors for NetworkID=6")

    now_utc = datetime.utcnow()
    per_timestamp = {}  # MessageDate string -> row dict

    for sensor in sensors:
        sensor_id = sensor.get("SensorID")
        sensor_name = sensor.get("SensorName", "")
        is_humidity = "Humidity" in sensor_name
        is_current = "Current" in sensor_name
        if not (is_humidity or is_current):
            continue  # unrecognized sensor type, skip (matches R's keep() filtering)

        for chunk_start, chunk_end in date_chunks(since_utc, now_utc):
            resp = requests.get(
                f"{GATEWAY_BASE}/SensorDataMessages",
                params={
                    "SensorID": sensor_id,
                    "fromDate": chunk_start.strftime("%Y/%m/%d %H:%M:%S"),
                    "toDate": chunk_end.strftime("%Y/%m/%d %H:%M:%S"),
                },
                headers=headers,
                verify=False,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json().get("Result") or []
            if not isinstance(result, list):
                # The gateway sometimes returns a plain error/status string in
                # Result instead of a list (e.g. for an oversized date range) -
                # skip this chunk rather than let it blow up the whole fetch.
                print(
                    f"[fetch] unexpected response for sensor {sensor_id} ({sensor_name}) "
                    f"{chunk_start}..{chunk_end}: {result!r}",
                    file=sys.stderr,
                )
                continue
            if not result:
                continue

            for message in result:
                if not isinstance(message, dict):
                    continue
                ts = parse_monnit_date(message["MessageDate"])
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                values = message.get("Data", "").split(",")
                row = per_timestamp.setdefault(ts_str, {"MessageDate": ts_str})

                if is_humidity:
                    for metric, value in zip(METRIC_ORDER, values):
                        col_name = (
                            sensor_name
                            if metric == "Humidity"
                            else sensor_name.replace("Humidity - ", f"{metric} - ", 1)
                        )
                        parsed = safe_float(value)
                        if parsed is not None:
                            row[col_name] = parsed
                elif is_current:
                    for col_name, value in zip(CURRENT_COLUMNS, values):
                        parsed = safe_float(value)
                        if parsed is not None:
                            row[col_name] = parsed

    return list(per_timestamp.values())


def fetch_proxy():
    resp = requests.get(PROXY_URL, timeout=PROXY_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def main():
    history = load_history()
    watermark = high_water_mark(history)
    backfill_hours = os.environ.get("BACKFILL_HOURS") or None

    if backfill_hours:
        since_utc = datetime.utcnow() - timedelta(hours=float(backfill_hours))
        print(f"[fetch] backfill mode: requesting history since {since_utc} UTC ({backfill_hours}h)")
    elif watermark:
        since_utc = datetime.strptime(watermark, "%Y-%m-%d %H:%M:%S")
    else:
        since_utc = datetime.utcnow() - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

    source = "gateway"
    try:
        new_rows = fetch_gateway(since_utc)
        if not new_rows:
            raise RuntimeError("gateway returned zero rows for the requested window")
    except Exception as exc:
        if backfill_hours:
            # The Render proxy only ever exposes a ~36h rolling window, so it
            # can't satisfy a multi-day backfill request - fail loudly instead
            # of silently returning a much smaller window than asked for.
            print(f"[fetch] backfill requires the direct gateway; it was unavailable ({exc})", file=sys.stderr)
            raise
        print(f"[fetch] direct gateway unavailable ({exc}); falling back to Render proxy", file=sys.stderr)
        source = "proxy"
        new_rows = fetch_proxy()

    # Merge by MessageDate (last write wins) and rewrite the whole file in
    # order, so a backfill's older rows land in the right place rather than
    # just being tacked on to the end of the file.
    combined = {row["MessageDate"]: row for row in history}
    added = sum(1 for row in new_rows if row["MessageDate"] not in combined)
    combined.update({row["MessageDate"]: row for row in new_rows})
    all_rows = sorted(combined.values(), key=lambda r: r["MessageDate"])

    if added == 0:
        print(f"[fetch] source={source}: no new records since {watermark or 'beginning'}")
        return

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(
        f"[fetch] source={source}: added {added} new record(s) (total {len(all_rows)}), "
        f"latest={all_rows[-1]['MessageDate']}"
    )


if __name__ == "__main__":
    main()
