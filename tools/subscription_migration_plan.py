#!/usr/bin/env python3
"""Read-only audit of the October 2026 Enea subscription change.

No writes to HA or Recorder. Local JSON report only, with restrictive mode.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from recorder_statistics_audit import get_statistics

UTC = timezone.utc
TZ = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 10, 1, tzinfo=TZ)
END = datetime(2026, 12, 2, tzinfo=TZ)
DEFAULTS = {
    "energy_price_net": 0.4879,
    "variable_network_net": 0.2456,
    "quality_net": 0.0332,
    "oze_net": 0.0073,
    "cogeneration_net": 0.0030,
    "commercial_fee_net": 10.24,
    "fixed_network_net": 10.41,
    "capacity_fee_net": 24.05,
    "transition_fee_net": 0.0,
    "subscription_fee_net": 0.32,
    "vat_rate": 23.0,
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp without timezone")
    return dt.astimezone(UTC)


def month_hours(hour):
    local = hour.astimezone(TZ)
    first = datetime(local.year, local.month, 1, tzinfo=TZ)
    last = datetime(local.year + (local.month == 12),
                    local.month % 12 + 1, 1, tzinfo=TZ)
    return (last.astimezone(UTC) - first.astimezone(UTC)).total_seconds() / 3600


def classify(cost, energy, hour, rates, tolerance):
    if cost is None or energy is None:
        return "missing", None, None
    cost, energy = float(cost), float(energy)
    if not math.isfinite(cost) or not math.isfinite(energy) or energy < 0:
        return "unknown", None, None
    vat = 1 + rates["vat_rate"] / 100
    variable = sum(rates[k] for k in (
        "energy_price_net", "variable_network_net", "quality_net",
        "oze_net", "cogeneration_net")) * vat
    fixed = sum(rates[k] for k in (
        "commercial_fee_net", "fixed_network_net", "capacity_fee_net",
        "transition_fee_net")) * vat / month_hours(hour)
    new = energy * variable + fixed
    old = new + rates["subscription_fee_net"] * vat / month_hours(hour)
    old_match = abs(cost - old) <= tolerance
    new_match = abs(cost - new) <= tolerance
    if old_match and not new_match:
        return "legacy", old, new
    if new_match and not old_match:
        return "correct", old, new
    return ("ambiguous" if old_match and new_match else "unknown"), old, new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/config"))
    parser.add_argument("--output", type=Path, default=Path("enea_subscription_plan.json"))
    parser.add_argument("--tolerance", type=float, default=0.0001)
    args = parser.parse_args()
    if not 0 < args.tolerance <= 0.0002:
        parser.error("Tolerance must be >0 and <=0.0002 PLN")
    storage = args.config / ".storage"
    entries = [e for e in read(storage / "core.config_entries")["data"]["entries"]
               if e["domain"] == "enea_rcem"]
    if len(entries) != 1:
        raise SystemExit("Expected exactly one Enea RCEm entry")
    entry = entries[0]
    settings = {**entry["data"], **entry.get("options", {})}
    rates = {k: float(settings.get(k, v)) for k, v in DEFAULTS.items()}
    if any(not math.isfinite(v) or v < 0 for v in rates.values()):
        raise SystemExit("Invalid configured rates")
    store_path = storage / ("enea_rcem." + entry["entry_id"])
    before = read(store_path)["data"]
    registry = read(storage / "core.entity_registry")["data"]["entities"]
    ids = {}
    for suffix in ("balanced_import", "import_cost"):
        matches = [e["entity_id"] for e in registry
                   if e.get("platform") == "enea_rcem"
                   and e.get("unique_id") == entry["entry_id"] + "_" + suffix]
        if len(matches) != 1:
            raise SystemExit("Cannot resolve unique statistic: " + suffix)
        ids[suffix] = matches[0]

    # Allow Recorder's hourly aggregation and alignment to finish. Never include
    # the current or immediately preceding hour in the correction estimate.
    now = datetime.now(UTC)
    cutoff = min(END.astimezone(UTC),
                 now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
                 parse(before["bucket_hour"]))
    start = START.astimezone(UTC)
    if cutoff <= start:
        raise SystemExit("No completed hours in migration range")
    indexed = {key: {} for key in ids}
    cursor = start
    while cursor < cutoff:
        stop = min(cursor + timedelta(days=7), cutoff)
        stats = get_statistics(tuple(ids.values()), cursor.isoformat(),
                               stop.isoformat(), "hour")
        for key, sid in ids.items():
            for row in stats.get(sid, []):
                stamp = parse(row["start"])
                if cursor <= stamp < stop:
                    if stamp in indexed[key]:
                        raise SystemExit("Duplicate statistics row")
                    indexed[key][stamp] = row
        cursor = stop

    counts = Counter()
    monthly = defaultdict(lambda: {"legacy_hours": 0, "correction_pln": 0.0})
    rows = []
    cursor = start
    while cursor < cutoff:
        costrow = indexed["import_cost"].get(cursor, {})
        importrow = indexed["balanced_import"].get(cursor, {})
        cost, energy = costrow.get("change"), importrow.get("change")
        kind, old, new = classify(cost, energy, cursor, rates, args.tolerance)
        counts[kind] += 1
        adjustment = new - old if kind == "legacy" else 0.0
        if kind == "legacy":
            m = monthly[cursor.astimezone(TZ).strftime("%Y-%m")]
            m["legacy_hours"] += 1
            m["correction_pln"] += adjustment
        rows.append({"hour_utc": cursor.isoformat(), "hour_local": cursor.astimezone(TZ).isoformat(),
                     "class": kind, "import_kwh": energy, "cost_change": cost,
                     "old_expected": old, "new_expected": new,
                     "proposed_adjustment": adjustment,
                     "cost_state": costrow.get("state"), "cost_sum": costrow.get("sum")})
        cursor += timedelta(hours=1)
    after = read(store_path)["data"]
    targets = after.get("statistics_targets", {})
    target_report = []
    for stamp, target in sorted(targets.items()):
        hour = parse(stamp)
        if start <= hour < END.astimezone(UTC):
            cost, energy = target.get("import_cost_pln"), target.get("import_kwh")
            kind, old, new = classify(cost, energy, hour, rates, args.tolerance)
            target_report.append({"hour": stamp, "class": kind, "cost": cost,
                                  "import_kwh": energy, "old_expected": old,
                                  "new_expected": new})
    total = sum(r["proposed_adjustment"] for r in rows)
    report = {"mode": "read_only", "start": start.isoformat(),
              "cutoff_exclusive": cutoff.isoformat(), "rates": rates,
              "statistics_ids": ids, "counts": dict(counts),
              "monthly": dict(monthly), "candidate_recorder_correction_pln": total,
              "runtime_before": {k: before.get(k) for k in ("bucket_hour", "import_cost_total", "gap_count")},
              "runtime_after": {k: after.get(k) for k in ("bucket_hour", "import_cost_total", "gap_count")},
              "retained_targets": target_report, "rows": rows,
              "warnings": [
                  "Current configured rates may not match historical rates.",
                  "Recorder changes are not proof of runtime charges; do not subtract this total blindly from Store.",
                  "Recent hours deliberately excluded; short-term layer and boundary require separate inspection.",
                  "No Recorder or Store changes were made.",
              ]}
    # Exclusive creation prevents accidental overwriting of prior evidence.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
    print("READ ONLY:", args.output)
    print("Hours:", dict(counts))
    for month, summary in sorted(monthly.items()):
        print(month, summary["legacy_hours"], "hours,", f'{summary["correction_pln"]:.6f} PLN')
    print(f"Candidate Recorder correction: {total:.6f} PLN")
    print("Unknown/missing/ambiguous hours:", sum(counts[k] for k in ("unknown", "missing", "ambiguous")))
    print("No data changed. Review plan before any migration.")


if __name__ == "__main__":
    main()
