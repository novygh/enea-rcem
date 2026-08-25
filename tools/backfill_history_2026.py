#!/usr/bin/env python3
"""One-time guarded history backfill for Enea RCEm.

Pinned to Home Assistant Core 2026.8.3 and the accepted 2024-06-12..2026-08-25
migration. Uses supported Recorder WebSocket APIs only; no direct SQL.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc
START_LOCAL = datetime(2024, 6, 12, 0, 0, tzinfo=TZ)
START_UTC = START_LOCAL.astimezone(UTC)
BOUNDARY = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
REQUIRED_HA_VERSION = "2026.8.3"

IMPORT_SOURCE = "sensor.miernik_energii_elektrycznej_energy"
EXPORT_SOURCE = "sensor.miernik_energii_elektrycznej_produced_energy"
TARGET_IMPORT = "sensor.enea_rcem_import_zbilansowany"
TARGET_EXPORT = "sensor.enea_rcem_eksport_zbilansowany"
TARGET_COST = "sensor.enea_rcem_koszt_importu"
TARGET_COMP = "sensor.enea_rcem_rekompensata_eksportu"
TARGETS = (TARGET_IMPORT, TARGET_EXPORT, TARGET_COST, TARGET_COMP)

UNITS = {
    TARGET_IMPORT: "kWh",
    TARGET_EXPORT: "kWh",
    TARGET_COST: "PLN",
    TARGET_COMP: "PLN",
}
UNIT_CLASSES = {
    TARGET_IMPORT: "energy",
    TARGET_EXPORT: "energy",
    TARGET_COST: None,
    TARGET_COMP: None,
}

API = "http://supervisor/core/api"
WS_HOST = "supervisor"
WS_PORT = 80
WS_PATH = "/core/websocket"
CONFIG_ENTRIES = Path("/config/.storage/core.config_entries")
EXPECTED_IMPORT_CORRECTION = 2.8811
EXPECTED_EXPORT_CORRECTION = -0.2019
BATCH_SIZE = 2000

BENCHMARKS = {
    "2024-06..2025-05": {
        "months": (
            "2024-06", "2024-07", "2024-08", "2024-09", "2024-10", "2024-11",
            "2024-12", "2025-01", "2025-02", "2025-03", "2025-04", "2025-05",
        ),
        "import": 3149.365,
        "export": 4756.936,
        "cost_net": 2767.61,
    },
    "2025-06..2026-05": {
        "months": (
            "2025-06", "2025-07", "2025-08", "2025-09", "2025-10", "2025-11",
            "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
        ),
        "import": 3330.632,
        "export": 4659.064,
        "cost_net": 3079.05,
    },
}


@dataclass(frozen=True, slots=True)
class Tariff:
    start: date
    end: date | None
    energy: float
    commercial: float
    variable_network: float
    quality: float
    oze: float
    cogeneration: float
    fixed_network: float
    capacity: float
    subscription: float
    transition: float
    vat: float = 23.0

    def contains(self, value: date) -> bool:
        return value >= self.start and (self.end is None or value < self.end)

    @property
    def variable_net(self) -> float:
        return (
            self.energy
            + self.variable_network
            + self.quality
            + self.oze
            + self.cogeneration
        )

    @property
    def fixed_net(self) -> float:
        return (
            self.commercial
            + self.fixed_network
            + self.capacity
            + self.subscription
            + self.transition
        )


TARIFFS = (
    Tariff(date(2024, 6, 12), date(2024, 7, 1), 0.6980, 14.31, 0.2486, 0.0314, 0.0000, 0.00618, 10.14, 14.90, 0.32, 0.33),
    Tariff(date(2024, 7, 1), date(2025, 1, 1), 0.5050, 10.24, 0.2486, 0.0314, 0.0000, 0.00618, 10.14, 0.00, 0.32, 0.33),
    Tariff(date(2025, 1, 1), date(2025, 7, 1), 0.5050, 10.24, 0.2456, 0.0321, 0.0035, 0.0030, 10.14, 0.00, 0.32, 0.33),
    Tariff(date(2025, 7, 1), date(2026, 1, 1), 0.5050, 10.24, 0.2456, 0.0321, 0.0035, 0.0030, 10.14, 16.01, 0.32, 0.33),
    Tariff(date(2026, 1, 1), date(2026, 2, 1), 0.4879, 10.24, 0.2456, 0.0331, 0.0073, 0.0030, 10.41, 24.05, 0.32, 0.00),
    Tariff(date(2026, 2, 1), None, 0.4879, 10.24, 0.2456, 0.0332, 0.0073, 0.0030, 10.41, 24.05, 0.32, 0.00),
)
JUNE_2024_FIXED_NET = 6.42 + 0.21 + 9.44 + 0.32 + 14.31


def fail(message: str) -> None:
    raise SystemExit(f"\nBLAD: {message}")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def month_bounds_local(month: str) -> tuple[datetime, datetime]:
    year, mon = map(int, month.split("-"))
    start = datetime(year, mon, 1, tzinfo=TZ)
    end = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1, tzinfo=TZ)
    return start, end


def month_hours(month: str) -> float:
    start, end = month_bounds_local(month)
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600.0


def tariff_for_date(value: date) -> Tariff:
    for tariff in TARIFFS:
        if tariff.contains(value):
            return tariff
    fail(f"brak taryfy dla {value.isoformat()}")


def prosumer_factor(month: str) -> float:
    year, mon = map(int, month.split("-"))
    return 1.00 if date(year, mon, 1) < date(2025, 2, 1) else 1.23


def fixed_hour_net(local_hour: datetime) -> float:
    month = local_hour.strftime("%Y-%m")
    if month == "2024-06":
        return JUNE_2024_FIXED_NET / (19 * 24)
    return tariff_for_date(local_hour.date()).fixed_net / month_hours(month)


def get_token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        fail("brak SUPERVISOR_TOKEN")
    return token


TOKEN = get_token()


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def get_statistics(
    statistic_ids: list[str] | tuple[str, ...],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    root = post_json(
        f"{API}/services/recorder/get_statistics?return_response",
        {
            "statistic_ids": list(statistic_ids),
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "period": "hour",
            "types": ["state", "sum"],
        },
    )
    try:
        return root["service_response"]["statistics"]
    except (KeyError, TypeError):
        fail("nieoczekiwana odpowiedz recorder/get_statistics")


def load_config() -> tuple[float, float, dict[str, dict[str, Any]]]:
    with CONFIG_ENTRIES.open("r", encoding="utf-8") as handle:
        root = json.load(handle)
    entries = [
        item for item in root.get("data", {}).get("entries", [])
        if item.get("domain") == "enea_rcem"
    ]
    if len(entries) != 1:
        fail(f"oczekiwano 1 config entry enea_rcem, znaleziono {len(entries)}")
    entry = entries[0]
    settings: dict[str, Any] = {}
    settings.update(entry.get("data", {}))
    settings.update(entry.get("options", {}))
    import_correction = float(settings.get("import_correction_percent", 0.0))
    export_correction = float(settings.get("export_correction_percent", 0.0))

    store_path = Path(f"/config/.storage/enea_rcem.{entry['entry_id']}")
    if not store_path.exists():
        fail(f"brak Store {store_path}")
    with store_path.open("r", encoding="utf-8") as handle:
        store = json.load(handle)
    prices = {}
    for month, item in store.get("data", {}).get("rcem_prices", {}).items():
        try:
            prices[month] = {
                "price": float(item["price_pln_mwh"]),
                "published": str(item["published"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    if not prices:
        fail("brak RCEm w Store")
    return import_correction, export_correction, prices


class WebSocket:
    def __init__(self) -> None:
        self.sock = socket.create_connection((WS_HOST, WS_PORT), timeout=30)
        self.sock.settimeout(120)
        self.buffer = b""
        self.next_id = 1
        self.ha_version = self._handshake_and_auth()

    def _read(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("WebSocket zamkniety")
        return chunk

    def _exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self.buffer += self._read()
        result = self.buffer[:count]
        self.buffer = self.buffer[count:]
        return result

    def _recv_frame(self) -> str:
        while True:
            first, second = self._exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._exact(4) if masked else None
            payload = self._exact(length)
            if masked and mask is not None:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise ConnectionError("WebSocket zamkniety przez serwer")
            if opcode == 0x1:
                return payload.decode("utf-8")

    def _send_frame(self, payload: str | bytes, opcode: int = 0x1) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_json(self) -> dict[str, Any]:
        return json.loads(self._recv_frame())

    def _handshake_and_auth(self) -> str:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {WS_HOST}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self._read()
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if " 101 " not in header.decode("latin-1"):
            fail("handshake WebSocket nie zwrocil HTTP 101")
        required = self._recv_json()
        if required.get("type") != "auth_required":
            fail(f"oczekiwano auth_required, dostano {required!r}")
        self._send_frame(json.dumps({"type": "auth", "access_token": TOKEN}))
        result = self._recv_json()
        if result.get("type") != "auth_ok":
            fail(f"autoryzacja WebSocket nieudana: {result!r}")
        return str(result.get("ha_version", ""))

    def call(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        self._send_frame(json.dumps({"id": msg_id, **message}, separators=(",", ":")))
        while True:
            result = self._recv_json()
            if result.get("type") == "result" and result.get("id") == msg_id:
                if not result.get("success"):
                    fail(f"{message.get('type')} zwrocil blad: {result.get('error')}")
                return result

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        self.sock.close()


def reconstruct(rows: list[dict[str, Any]], label: str) -> dict[datetime, float]:
    rows = [
        row for row in rows
        if parse_dt(row["end"]).astimezone(UTC) <= BOUNDARY
    ]
    rows.sort(key=lambda row: parse_dt(row["end"]))
    if len(rows) < 2:
        fail(f"{label}: za malo rekordow zrodlowych")
    result: dict[datetime, float] = defaultdict(float)
    gaps = 0
    tiny = []
    serious = []
    previous = rows[0]
    for row in rows[1:]:
        previous_sum = previous.get("sum")
        current_sum = row.get("sum")
        if previous_sum is None or current_sum is None:
            previous = row
            continue
        start = parse_dt(previous["end"]).astimezone(UTC)
        end = parse_dt(row["end"]).astimezone(UTC)
        elapsed = (end - start).total_seconds() / 3600.0
        if elapsed <= 0:
            previous = row
            continue
        hours = round(elapsed)
        if hours < 1 or abs(elapsed - hours) > 0.01:
            fail(f"{label}: nietypowy odstep {start} -> {end}: {elapsed} h")
        if hours > 1:
            gaps += 1
        delta = float(current_sum) - float(previous_sum)
        if delta < 0:
            if abs(delta) <= 0.10:
                tiny.append((start, end, delta))
                delta = 0.0
            else:
                serious.append((start, end, delta))
                previous = row
                continue
        share = delta / hours
        bucket = start
        for _ in range(hours):
            result[bucket] += share
            bucket += timedelta(hours=1)
        previous = row
    if serious:
        for start, end, delta in serious:
            print(f"DUZY RESET {label}: {start} -> {end} {delta:+.6f}")
        fail(f"{label}: wykryto duzy reset")
    print(f"{label}: rekordy={len(rows)}, luki>1h={gaps}, drobne_rebasy={len(tiny)}")
    for start, end, delta in tiny:
        print(f"  rebase {start.isoformat()} -> {end.isoformat()} {delta:+.3f} kWh")
    if max(result, default=START_UTC) < BOUNDARY - timedelta(hours=1):
        fail(f"{label}: rekonstrukcja nie dochodzi do granicy")
    return dict(result)


def build_history(import_hour, export_hour, import_mult, export_mult, rcem_prices):
    monthly = defaultdict(lambda: {"import": 0.0, "export": 0.0, "cost_net": 0.0})
    hourly = []
    hour = START_UTC
    while hour < BOUNDARY:
        local = hour.astimezone(TZ)
        month = local.strftime("%Y-%m")
        imp = max(float(import_hour.get(hour, 0.0)), 0.0)
        exp = max(float(export_hour.get(hour, 0.0)), 0.0)
        balanced_import = max(imp - exp, 0.0) * import_mult
        balanced_export = max(exp - imp, 0.0) * export_mult
        tariff = tariff_for_date(local.date())
        cost_net = balanced_import * tariff.variable_net + fixed_hour_net(local)
        cost_gross = cost_net * (1.0 + tariff.vat / 100.0)
        monthly[month]["import"] += balanced_import
        monthly[month]["export"] += balanced_export
        monthly[month]["cost_net"] += cost_net
        hourly.append((hour, balanced_import, balanced_export, cost_gross))
        hour += timedelta(hours=1)

    comp_add = {}
    for month, values in monthly.items():
        price = rcem_prices.get(month)
        if price is None:
            continue
        _, month_end = month_bounds_local(month)
        month_end_utc = month_end.astimezone(UTC)
        if month_end_utc > BOUNDARY:
            continue
        comp_add[month_end_utc - timedelta(hours=1)] = (
            values["export"] * float(price["price"]) / 1000.0 * prosumer_factor(month)
        )

    rows = {target: [] for target in TARGETS}
    totals = {target: 0.0 for target in TARGETS}
    for hour, imp, exp, cost in hourly:
        totals[TARGET_IMPORT] += imp
        totals[TARGET_EXPORT] += exp
        totals[TARGET_COST] += cost
        totals[TARGET_COMP] += comp_add.get(hour, 0.0)
        for target in TARGETS:
            value = totals[target]
            rows[target].append({"start": hour.isoformat(), "state": value, "sum": value})
    return rows, totals, dict(monthly)


def validate_benchmarks(monthly) -> None:
    print("\n=== BENCHMARK GATE ===")
    for label, benchmark in BENCHMARKS.items():
        months = benchmark["months"]
        actual_import = sum(monthly[m]["import"] for m in months)
        actual_export = sum(monthly[m]["export"] for m in months)
        actual_cost = sum(monthly[m]["cost_net"] for m in months)
        print(f"{label}: import={actual_import:.3f}, export={actual_export:.3f}, koszt_net={actual_cost:.2f}")
        if abs(actual_import - benchmark["import"]) > 0.02:
            fail(f"benchmark {label}: import sie nie zgadza")
        if abs(actual_export - benchmark["export"]) > 0.02:
            fail(f"benchmark {label}: eksport sie nie zgadza")
        if abs(actual_cost - benchmark["cost_net"]) > 0.05:
            fail(f"benchmark {label}: koszt sie nie zgadza")
    print("Benchmark: PASS")


def metadata_for(target: str) -> dict[str, Any]:
    return {
        "has_sum": True,
        "mean_type": 0,
        "name": None,
        "source": "recorder",
        "statistic_id": target,
        "unit_class": UNIT_CLASSES[target],
        "unit_of_measurement": UNITS[target],
    }


def validate_metadata(ws: WebSocket) -> None:
    result = ws.call({"type": "recorder/get_statistics_metadata", "statistic_ids": list(TARGETS)})
    found = {item["statistic_id"]: item for item in result.get("result", [])}
    for target in TARGETS:
        item = found.get(target)
        if not item:
            fail(f"brak metadata {target}")
        if not item.get("has_sum") or item.get("source") != "recorder":
            fail(f"zle metadata {target}")
        if item.get("statistics_unit_of_measurement") != UNITS[target]:
            fail(f"zla jednostka {target}")
        if item.get("unit_class") != UNIT_CLASSES[target]:
            fail(f"zly unit_class {target}")
    print("Metadata: PASS")


def boundary_rows():
    stats = get_statistics(TARGETS, BOUNDARY, BOUNDARY + timedelta(hours=2))
    result = {}
    for target in TARGETS:
        for row in stats.get(target, []):
            if parse_dt(row["start"]).astimezone(UTC) == BOUNDARY:
                result[target] = row
                break
        if target not in result:
            fail(f"brak wiersza granicznego dla {target}")
    return result


def validate_boundary_states(rows) -> None:
    for target, row in rows.items():
        if abs(float(row.get("state") or 0.0)) > 1e-6:
            fail(f"{target}: state na granicy nie jest 0")
    print("Boundary state anchors: PASS")


def import_history(ws: WebSocket, rows) -> None:
    print("\n=== IMPORT HISTORY ===")
    for target in TARGETS:
        data = rows[target]
        batches = (len(data) + BATCH_SIZE - 1) // BATCH_SIZE
        for index, start in enumerate(range(0, len(data), BATCH_SIZE), 1):
            batch = data[start:start + BATCH_SIZE]
            ws.call({"type": "recorder/import_statistics", "metadata": metadata_for(target), "stats": batch})
            print(f"{target}: batch {index}/{batches} ({len(batch)} wierszy) queued")


def wait_for_imported_tail(offsets, timeout=120.0) -> None:
    expected_start = BOUNDARY - timedelta(hours=1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = get_statistics(TARGETS, expected_start, BOUNDARY)
        ready = True
        for target in TARGETS:
            match = next((row for row in stats.get(target, []) if parse_dt(row["start"]).astimezone(UTC) == expected_start), None)
            if match is None or abs(float(match.get("sum") or 0.0) - offsets[target]) > 0.01:
                ready = False
                break
        if ready:
            print("Import history committed: PASS")
            return
        time.sleep(2)
    fail("timeout oczekiwania na zapis historii")


def adjust_live_sums(ws: WebSocket, offsets) -> None:
    print("\n=== ADJUST LIVE SUMS ===")
    current = boundary_rows()
    for target in TARGETS:
        current_sum = float(current[target].get("sum") or 0.0)
        desired = offsets[target]
        adjustment = desired - current_sum
        print(f"{target}: current={current_sum:.6f}, target={desired:.6f}, adjustment={adjustment:+.6f}")
        if abs(adjustment) <= 1e-9:
            continue
        ws.call({
            "type": "recorder/adjust_sum_statistics",
            "statistic_id": target,
            "start_time": BOUNDARY.isoformat(),
            "adjustment": adjustment,
            "adjustment_unit_of_measurement": UNITS[target],
        })


def wait_for_boundary_offsets(offsets, timeout=60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = boundary_rows()
        if all(abs(float(current[target].get("sum") or 0.0) - offsets[target]) <= 0.01 for target in TARGETS):
            print("Live sum offsets committed: PASS")
            return
        time.sleep(1)
    fail("timeout oczekiwania na przesuniecie live sum")


def final_validation(ws: WebSocket, offsets) -> None:
    print("\n=== FINAL VALIDATION ===")
    first = get_statistics(TARGETS, START_UTC, START_UTC + timedelta(hours=2))
    tail = get_statistics(TARGETS, BOUNDARY - timedelta(hours=1), datetime.now(UTC) + timedelta(hours=2))
    for target in TARGETS:
        if not first.get(target) or not tail.get(target):
            fail(f"brak historii po imporcie dla {target}")
        boundary = next((row for row in tail[target] if parse_dt(row["start"]).astimezone(UTC) == BOUNDARY), None)
        if boundary is None:
            fail(f"brak granicy po imporcie dla {target}")
        boundary_sum = float(boundary.get("sum") or 0.0)
        if abs(boundary_sum - offsets[target]) > 0.01:
            fail(f"zly sum na granicy dla {target}")
        latest = tail[target][-1]
        print(f"{target}")
        print(f"  first    {first[target][0]['start']} sum={float(first[target][0].get('sum') or 0.0):.6f}")
        print(f"  boundary {boundary['start']} state={float(boundary.get('state') or 0.0):.6f} sum={boundary_sum:.6f}")
        print(f"  latest   {latest['start']} state={float(latest.get('state') or 0.0):.6f} sum={float(latest.get('sum') or 0.0):.6f}")

    issues = ws.call({"type": "recorder/validate_statistics"}).get("result", {})
    relevant = {key: value for key, value in issues.items() if key in TARGETS}
    print("Recorder validation issues dla Enea:")
    print(json.dumps(relevant, ensure_ascii=False, indent=2))
    if relevant:
        fail("Recorder zglosil problem dla statystyk Enea")
    print("\nFAZA 7 BACKFILL: PASS")


def main() -> None:
    print("=" * 72)
    print("ENEA RCEm - FAZA 7 / HISTORY BACKFILL")
    print("=" * 72)
    print(f"Start historii : {START_LOCAL.isoformat()}")
    print(f"Granica live   : {BOUNDARY.isoformat()}")
    print("Tryb           : Recorder API, bez SQL")

    import_correction, export_correction, rcem_prices = load_config()
    print(f"Import corr.   : {import_correction:+.4f}%")
    print(f"Export corr.   : {export_correction:+.4f}%")
    print(f"RCEm months    : {len(rcem_prices)}")
    if abs(import_correction - EXPECTED_IMPORT_CORRECTION) > 0.00005:
        fail("korekta importu rozni sie od zaakceptowanej")
    if abs(export_correction - EXPECTED_EXPORT_CORRECTION) > 0.00005:
        fail("korekta eksportu rozni sie od zaakceptowanej")

    source = get_statistics(
        (IMPORT_SOURCE, EXPORT_SOURCE),
        datetime(2024, 6, 11, 0, 0, tzinfo=TZ),
        BOUNDARY + timedelta(hours=1),
    )
    if IMPORT_SOURCE not in source or EXPORT_SOURCE not in source:
        fail("brak zrodlowych LTS")
    import_hour = reconstruct(source[IMPORT_SOURCE], "IMPORT")
    export_hour = reconstruct(source[EXPORT_SOURCE], "EXPORT")
    rows, offsets, monthly = build_history(
        import_hour,
        export_hour,
        1.0 + import_correction / 100.0,
        1.0 + export_correction / 100.0,
        rcem_prices,
    )
    validate_benchmarks(monthly)

    print("\n=== HISTORY OFFSETS AT LIVE BOUNDARY ===")
    for target in TARGETS:
        print(f"{target}: {offsets[target]:.6f} {UNITS[target]}")
    expected_rows = int((BOUNDARY - START_UTC).total_seconds() // 3600)
    if any(len(rows[target]) != expected_rows for target in TARGETS):
        fail("nieprawidlowa liczba godzin historii")
    print(f"Hourly rows per sensor: {expected_rows}")

    ws = WebSocket()
    try:
        print(f"WebSocket HA    : {ws.ha_version}")
        if ws.ha_version != REQUIRED_HA_VERSION:
            fail(f"migrator wymaga HA {REQUIRED_HA_VERSION}")
        validate_metadata(ws)
        anchors = boundary_rows()
        validate_boundary_states(anchors)

        print("\n=== CURRENT BOUNDARY SUMS ===")
        for target in TARGETS:
            current_sum = float(anchors[target].get("sum") or 0.0)
            print(f"{target}: current={current_sum:.6f}, desired={offsets[target]:.6f}, delta={offsets[target]-current_sum:+.6f}")

        print("\nPRE-FLIGHT: PASS")
        print("Nastepny krok zapisze historie do Recorder. SQL nie bedzie uzywany.")
        if input("Wpisz dokladnie BACKFILL aby wykonac zapis: ").strip() != "BACKFILL":
            print("Anulowano. Nic nie zapisano.")
            return

        import_history(ws, rows)
        wait_for_imported_tail(offsets)
        adjust_live_sums(ws, offsets)
        wait_for_boundary_offsets(offsets)
        final_validation(ws, offsets)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
