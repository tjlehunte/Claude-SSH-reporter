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

    from_date = since_utc.strftime("%Y/%m/%d %H:%M:%S")
    to_date = datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S")

    per_timestamp = {}  # MessageDate string -> row dict

    for sensor in sensors:
        sensor_id = sensor.get("SensorID")
        sensor_name = sensor.get("SensorName", "")
        is_humidity = "Humidity" in sensor_name
        is_current = "Current" in sensor_name
        if not (is_humidity or is_current):
            continue  # unrecognized sensor type, skip (matches R's keep() filtering)

        resp = requests.get(
            f"{GATEWAY_BASE}/SensorDataMessages",
            params={"SensorID": sensor_id, "fromDate": from_date, "toDate": to_date},
            headers=headers,
            verify=False,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json().get("Result") or []
        if not result:
            continue

        for message in result:
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
    if watermark:
        since_utc = datetime.strptime(watermark, "%Y-%m-%d %H:%M:%S")
    else:
        since_utc = datetime.utcnow() - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

    source = "gateway"
    try:
        new_rows = fetch_gateway(since_utc)
        if not new_rows:
            raise RuntimeError("gateway returned zero rows for the requested window")
    except Exception as exc:
        print(f"[fetch] direct gateway unavailable ({exc}); falling back to Render proxy", file=sys.stderr)
        source = "proxy"
        new_rows = fetch_proxy()

    existing_dates = {row["MessageDate"] for row in history}
    appended = [row for row in new_rows if row["MessageDate"] not in existing_dates]
    if watermark:
        appended = [row for row in appended if row["MessageDate"] > watermark]
    appended.sort(key=lambda r: r["MessageDate"])

    if not appended:
        print(f"[fetch] source={source}: no new records since {watermark or 'beginning'}")
        return

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        for row in appended:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"[fetch] source={source}: appended {len(appended)} record(s), latest={appended[-1]['MessageDate']}")


if __name__ == "__main__":
    main()
