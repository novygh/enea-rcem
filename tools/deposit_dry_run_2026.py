#!/usr/bin/env python3
"""Read-only dry-run of the Enea RCEm prosumer deposit ledger.

The model follows the user's RCEm settlement:
- export value for month N becomes a deposit lot in month N+1,
- deposit covers only purchase of active energy, not distribution/commercial fees,
- oldest deposit lots are consumed first,
- each lot is usable for 12 following calendar months,
- at expiry up to 20% of the original lot can be refunded; the rest expires,
- latest corrected RCEm statistics already stored in Home Assistant are used.

No Home Assistant data is modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import os
import urllib.request
from zoneinfo import ZoneInfo

API = "http://supervisor/core/api"
TOKEN = os.environ.get("SUPERVISOR_TOKEN")
TZ = ZoneInfo("Europe/Warsaw")

IMPORT_STAT = "sensor.enea_rcem_import_zbilansowany"
COMP_STAT = "sensor.enea_rcem_rekompensata_eksportu"

START_MONTH = "2024-06"
REFUND_LIMIT = 0.20


@dataclass
class Lot:
    source_month: str
    assigned_month: str
    expiry_month: str
    original: float
    remaining: float


def month_add(month: str, delta: int) -> str:
    year, mon = map(int, month.split("-"))
    absolute = year * 12 + (mon - 1) + delta
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def month_range(start: str, end: str):
    month = start
    while month <= end:
        yield month
        month = month_add(month, 1)


def month_date(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1)


def active_energy_net(month: str) -> float:
    value = month_date(month)
    if value < date(2024, 7, 1):
        return 0.6980
    if value < date(2026, 1, 1):
        return 0.5050
    return 0.4879


def active_energy_gross(month: str) -> float:
    return active_energy_net(month) * 1.23


def get_monthly_statistics(end_month_exclusive: str) -> dict:
    if not TOKEN:
        raise SystemExit("BLAD: brak SUPERVISOR_TOKEN")

    end_year, end_mon = map(int, end_month_exclusive.split("-"))
    end_time = datetime(end_year, end_mon, 1, 0, 0, tzinfo=TZ).isoformat()

    payload = json.dumps({
        "statistic_ids": [IMPORT_STAT, COMP_STAT],
        "start_time": "2024-06-01T00:00:00+02:00",
        "end_time": end_time,
        "period": "month",
        "types": ["change", "sum"],
    }).encode()

    req = urllib.request.Request(
        API + "/services/recorder/get_statistics?return_response",
        data=payload,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)["service_response"]["statistics"]


def changes_by_month(rows: list[dict]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        start = datetime.fromisoformat(
            str(row["start"]).replace("Z", "+00:00")
        ).astimezone(TZ)
        change = row.get("change")
        if change is not None:
            result[start.strftime("%Y-%m")] = float(change)
    return result


def main() -> None:
    now = datetime.now(TZ)
    current_month = now.strftime("%Y-%m")
    next_month = month_add(current_month, 1)

    stats = get_monthly_statistics(next_month)
    imports = changes_by_month(stats.get(IMPORT_STAT, []))
    compensation = changes_by_month(stats.get(COMP_STAT, []))

    lots: list[Lot] = []
    total_refund = 0.0
    total_expired = 0.0
    total_used = 0.0
    current_uncovered = 0.0
    ledger: list[dict] = []

    for month in month_range(START_MONTH, current_month):
        refund = 0.0
        expired = 0.0
        survivors: list[Lot] = []

        # A lot assigned in month M is usable for M..M+11.
        # In M+12 the unused part reaches the refund/expiry stage.
        for lot in lots:
            if lot.expiry_month == month:
                lot_refund = min(lot.remaining, lot.original * REFUND_LIMIT)
                lot_expired = max(lot.remaining - lot_refund, 0.0)
                refund += lot_refund
                expired += lot_expired
            else:
                survivors.append(lot)
        lots = survivors

        source_month = month_add(month, -1)
        assigned = float(compensation.get(source_month, 0.0))
        if assigned > 0:
            lots.append(
                Lot(
                    source_month=source_month,
                    assigned_month=month,
                    expiry_month=month_add(month, 12),
                    original=assigned,
                    remaining=assigned,
                )
            )

        import_kwh = float(imports.get(month, 0.0))
        energy_purchase = import_kwh * active_energy_gross(month)

        remaining_obligation = energy_purchase
        used = 0.0

        for lot in lots:
            if remaining_obligation <= 1e-12:
                break
            amount = min(lot.remaining, remaining_obligation)
            lot.remaining -= amount
            remaining_obligation -= amount
            used += amount

        lots = [lot for lot in lots if lot.remaining > 1e-9]
        balance = sum(lot.remaining for lot in lots)

        total_refund += refund
        total_expired += expired
        total_used += used

        if month == current_month:
            current_uncovered = remaining_obligation

        ledger.append({
            "month": month,
            "import_kwh": import_kwh,
            "energy_purchase": energy_purchase,
            "assigned": assigned,
            "used": used,
            "balance": balance,
            "refund": refund,
            "expired": expired,
            "uncovered": remaining_obligation,
        })

    print("=" * 112)
    print("ENEA RCEm - DEPOZYT PROSUMENCKI / DRY-RUN TYLKO ODCZYT")
    print("=" * 112)
    print(
        f"{'Miesiac':<9}"
        f"{'Import kWh':>12}"
        f"{'Energia PLN':>13}"
        f"{'Nowy dep.':>12}"
        f"{'Uzyto':>11}"
        f"{'Saldo':>11}"
        f"{'Zwrot':>10}"
        f"{'Przepadlo':>11}"
        f"{'Do zapl.':>11}"
    )
    print("-" * 112)

    for row in ledger:
        print(
            f"{row['month']:<9}"
            f"{row['import_kwh']:12.3f}"
            f"{row['energy_purchase']:13.2f}"
            f"{row['assigned']:12.2f}"
            f"{row['used']:11.2f}"
            f"{row['balance']:11.2f}"
            f"{row['refund']:10.2f}"
            f"{row['expired']:11.2f}"
            f"{row['uncovered']:11.2f}"
        )

    print()
    print("=" * 112)
    print("STAN BIEZACY")
    print("=" * 112)

    balance = sum(lot.remaining for lot in lots)
    current = ledger[-1]

    print(f"Saldo depozytu:                 {balance:10.2f} PLN")
    print(f"Nowy depozyt w tym miesiacu:   {current['assigned']:10.2f} PLN")
    print(f"Uzyto w tym miesiacu:           {current['used']:10.2f} PLN")
    print(f"Energia czynna do zaplaty:      {current_uncovered:10.2f} PLN")
    print(f"Zwroty lacznie:                 {total_refund:10.2f} PLN")
    print(f"Srodki wygasle lacznie:         {total_expired:10.2f} PLN")
    print(f"Depozyt wykorzystany lacznie:   {total_used:10.2f} PLN")

    print()
    if lots:
        oldest = min(lots, key=lambda lot: (lot.assigned_month, lot.source_month))
        print(
            "Najstarsza aktywna pula:         "
            f"{oldest.source_month} -> przypisana {oldest.assigned_month}, "
            f"wygasa {oldest.expiry_month}"
        )
        print(f"Pozostalo w najstarszej puli:   {oldest.remaining:10.2f} PLN")
        print(
            f"Maks. zwrot z tej puli:         "
            f"{min(oldest.remaining, oldest.original * REFUND_LIMIT):10.2f} PLN"
        )
    else:
        print("Brak aktywnych pul depozytu.")

    print()
    print("MODEL:")
    print(" - depozyt miesiaca N jest dostepny od miesiaca N+1")
    print(" - pokrywa tylko zakup energii czynnej, nie dystrybucje ani oplate handlowa")
    print(" - najstarsze srodki sa zuzywane pierwsze")
    print(" - po 12 miesiacach: zwrot max 20% wartosci pierwotnej, reszta wygasa")
    print(" - wartosci RCEm pochodza z aktualnych statystyk HA (najnowsze korekty PSE)")
    print("=" * 112)


if __name__ == "__main__":
    main()
