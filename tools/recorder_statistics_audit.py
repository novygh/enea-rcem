#!/usr/bin/env python3
"""Read-only audit helper for Enea RCEm Recorder statistics.

This tool never writes to Home Assistant. It queries Recorder statistics through
Home Assistant's service API and reports suspicious state/sum continuity.

Typical use from a Home Assistant add-on terminal::

    python3 tools/recorder_statistics_audit.py \
      --start 2026-08-25T00:00:00+00:00 \
      --end 2026-08-26T00:00:00+00:00 \
      --period hour

Use ``--period 5minute`` when investigating the short-term aggregation layer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Any
from urllib.request import Request, urlopen

API = "http://supervisor/core/api"
DEFAULT_STATISTIC_IDS = (
    "sensor.enea_rcem_import_zbilansowany",
    "sensor.enea_rcem_eksport_zbilansowany",
    "sensor.enea_rcem_koszt_importu",
    "sensor.enea_rcem_rekompensata_eksportu",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only continuity audit for Enea RCEm Recorder statistics"
    )
    parser.add_argument("--start", required=True, help="ISO-8601 start time")
    parser.add_argument("--end", required=True, help="ISO-8601 end time")
    parser.add_argument(
        "--period",
        choices=("hour", "5minute"),
        default="hour",
        help="Recorder aggregation period (default: hour)",
    )
    parser.add_argument(
        "--statistic-id",
        action="append",
        dest="statistic_ids",
        help="Statistic ID to audit; repeat to override the Enea defaults",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.001,
        help="Tolerance for continuity checks (default: 0.001)",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Print every row in addition to the anomaly summary",
    )
    return parser.parse_args()


def token() -> str:
    value = os.environ.get("SUPERVISOR_TOKEN")
    if not value:
        raise SystemExit("BLAD: brak SUPERVISOR_TOKEN")
    return value


def normalize_iso(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def get_statistics(
    statistic_ids: tuple[str, ...],
    start: str,
    end: str,
    period: str,
) -> dict[str, list[dict[str, Any]]]:
    payload = {
        "start_time": normalize_iso(start),
        "end_time": normalize_iso(end),
        "statistic_ids": list(statistic_ids),
        "period": period,
        "types": ["state", "sum", "change"],
        "units": {"energy": "kWh"},
    }
    request = Request(
        f"{API}/services/recorder/get_statistics?return_response",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=120) as response:
        root = json.load(response)
    try:
        return root["service_response"]["statistics"]
    except (KeyError, TypeError) as err:
        raise SystemExit(f"BLAD: nieoczekiwana odpowiedz Recorder: {root!r}") from err


def number(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def row_flags(
    row: dict[str, Any],
    previous: dict[str, Any] | None,
    tolerance: float,
) -> list[str]:
    flags: list[str] = []
    state = number(row.get("state"))
    total = number(row.get("sum"))
    change = number(row.get("change"))

    if state is None:
        flags.append("STATE_NULL")
    if total is None:
        flags.append("SUM_NULL")

    # This catches the exact class of migration/stitch error that motivated the
    # tool: a small relative state living next to a large cumulative sum.
    if state is not None and total is not None and abs(state) < 100 and abs(total) > 1000:
        flags.append("RELATIVE_STATE_SUSPECT")

    if previous is None:
        return flags

    prev_state = number(previous.get("state"))
    prev_sum = number(previous.get("sum"))
    if state is not None and prev_state is not None and state < prev_state - tolerance:
        flags.append("STATE_DROP")
    if total is not None and prev_sum is not None and total < prev_sum - tolerance:
        flags.append("SUM_DROP")

    if total is not None and prev_sum is not None and change is not None:
        expected_change = total - prev_sum
        if abs(expected_change - change) > tolerance:
            flags.append("CHANGE_MISMATCH")

    return flags


def format_row(statistic_id: str, row: dict[str, Any], flags: list[str]) -> str:
    suffix = f"  [{','.join(flags)}]" if flags else ""
    return (
        f"{statistic_id}\t{row.get('start')}\t"
        f"state={row.get('state')}\tsum={row.get('sum')}\t"
        f"change={row.get('change')}{suffix}"
    )


def audit(
    rows_by_id: dict[str, list[dict[str, Any]]],
    statistic_ids: tuple[str, ...],
    tolerance: float,
    show_all: bool,
) -> int:
    anomaly_count = 0
    print("=== RECORDER AUDIT ===")
    for statistic_id in statistic_ids:
        rows = rows_by_id.get(statistic_id, [])
        print(f"\n{statistic_id}: rows={len(rows)}")
        if not rows:
            print("  BRAK DANYCH")
            anomaly_count += 1
            continue

        first = rows[0]
        last = rows[-1]
        print(
            "  first:",
            f"{first.get('start')} state={first.get('state')} sum={first.get('sum')}",
        )
        print(
            "  last :",
            f"{last.get('start')} state={last.get('state')} sum={last.get('sum')}",
        )

        previous: dict[str, Any] | None = None
        for row in rows:
            flags = row_flags(row, previous, tolerance)
            if flags:
                anomaly_count += 1
                print("  !", format_row(statistic_id, row, flags))
            elif show_all:
                print("   ", format_row(statistic_id, row, flags))
            previous = row

    print(f"\n=== PODSUMOWANIE: anomalies={anomaly_count} ===")
    return anomaly_count


def main() -> None:
    args = parse_args()
    statistic_ids = tuple(args.statistic_ids or DEFAULT_STATISTIC_IDS)
    rows = get_statistics(statistic_ids, args.start, args.end, args.period)
    audit(rows, statistic_ids, args.tolerance, args.show_all)


if __name__ == "__main__":
    main()
