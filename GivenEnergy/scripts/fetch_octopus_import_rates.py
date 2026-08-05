#!/usr/bin/env python3
"""Fetch the full unit-rate and standing-charge history for the import tariff
(Flexible Octopus / VAR, region G) and write it to data/octopus_import_rates.jsonl.

Unlike fetch_octopus_rates.py (export side), this deliberately does NOT use a
high-water-mark incremental fetch. Two reasons:
  1. Scale: Agile export rates are half-hourly (thousands of immutable rows),
     so incremental append matters there. The VAR import tariff has only
     repriced a handful of times since 2022 - the entire history is a dozen-ish
     rows, so a full refetch every run costs nothing.
  2. Correctness: VAR rate rows are MUTABLE. The currently-active rate has
     valid_to = null; when Octopus reprices, that same row's valid_to gets
     closed with a date. A watermark keyed on valid_from would never re-fetch
     that row, leaving the cache permanently claiming the old rate is still
     open-ended. Full refetch-and-overwrite self-corrects.

Region note: the tariff code here is region G (North West England, the real
house's M5 postcode area) - deliberately NOT region B, which is what the test
account is actually signed up to (its address had to be set near Leeds during
account creation). The pipeline models what the Manchester house would pay,
not what the test account is billed. The rates endpoint is public - no API
key needed - so this works regardless of what region the account itself is in.
"""
import json
import sys
from pathlib import Path

import requests

from billing_utils import (
    IMPORT_PRODUCT_CODE,
    IMPORT_TARIFF_CODE,
    IMPORT_RATES_FILE,
)

API_BASE = "https://api.octopus.energy/v1"
REQUEST_TIMEOUT = 30

# Octopus publishes TWO prices per validity period for VAR tariffs, split by
# payment method (confirmed against the raw API response: each period has a
# DIRECT_DEBIT and a NON_DIRECT_DEBIT row, differing by ~5.6% on unit rate
# and ~17.5% on standing charge as of Aug 2026). Without filtering to one,
# the downstream rate join is ambiguous. DIRECT_DEBIT is the assumed default
# for the modelled Manchester household (also the price Ofgem cap figures
# quote) - flip this single constant if the account pays on receipt of bill.
PAYMENT_METHOD = "DIRECT_DEBIT"

# The two endpoints under the same product/tariff pair. Standing charges are
# fetched too (the old hardcoded-constant approach bundled both, and the
# standing charge is the value that had drifted furthest - 58.71 hardcoded vs
# 44.32 actual for region G when this was written).
ENDPOINTS = {
    "unit_rate": "standard-unit-rates",
    "standing_charge": "standing-charges",
}


def fetch_all_pages(endpoint):
    """Fetch every page of one endpoint (no period filters - we want the full
    history precisely so that closed-off valid_to values get picked up)."""
    url = f"{API_BASE}/products/{IMPORT_PRODUCT_CODE}/electricity-tariffs/{IMPORT_TARIFF_CODE}/{endpoint}/"
    params = {"page_size": 1500}
    rows = []
    while url:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results")
        if not isinstance(results, list):
            print(f"[octopus-import] unexpected response shape: {payload!r}", file=sys.stderr)
            break
        for item in results:
            if not isinstance(item, dict) or "valid_from" not in item or "value_inc_vat" not in item:
                print(f"[octopus-import] skipping malformed entry: {item!r}", file=sys.stderr)
                continue
            # Drop the other payment method's row (see PAYMENT_METHOD comment) -
            # keeping both would give the rate join two candidates per timestamp.
            if item.get("payment_method") != PAYMENT_METHOD:
                continue
            rows.append(
                {
                    "valid_from": item["valid_from"],
                    # valid_to is None for the currently-active row - keep it
                    # as null in the JSONL, the loader treats null as open-ended.
                    "valid_to": item.get("valid_to"),
                    "value_inc_vat_pence": item["value_inc_vat"],
                }
            )
        url = payload.get("next")
        params = None  # the `next` link already has query params baked in
    return rows


def main():
    all_rows = []
    for charge_type, endpoint in ENDPOINTS.items():
        rows = fetch_all_pages(endpoint)
        for row in rows:
            row["charge_type"] = charge_type
        all_rows.extend(rows)
        print(f"[octopus-import] fetched {len(rows)} {charge_type} row(s)")

    if not all_rows:
        # Refuse to clobber an existing (possibly still-useful) cache with an
        # empty file if the API returned nothing/garbage.
        print("[octopus-import] no rows fetched; leaving existing cache untouched", file=sys.stderr)
        sys.exit(1)

    all_rows.sort(key=lambda r: (r["charge_type"], r["valid_from"]))

    IMPORT_RATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with IMPORT_RATES_FILE.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    print(f"[octopus-import] wrote {len(all_rows)} row(s) to {IMPORT_RATES_FILE}")


if __name__ == "__main__":
    main()
