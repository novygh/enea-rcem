"""Guarded one-shot repair using Recorder's established sum adjustment API."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import math

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_SUBSCRIPTION_FEE_NET, DEFAULT_SUBSCRIPTION_FEE_NET
from .runtime import EneaRcemRuntime
from .statistics_alignment import StatisticsAligner, _timestamp
from .tariffs import DISTRIBUTION_SUBSCRIPTION_END, SELLER_SUBSCRIPTION_START

_LOGGER = logging.getLogger(__name__)
SERVICE = "repair_subscription_2026"
MARKER = "2026-subscription-v1"
TOL = 0.0001


def _classification(actual, expected, legacy_delta):
    actual = float(actual)
    if not math.isfinite(actual):
        raise HomeAssistantError("Non-finite cost; repair refused")
    old = abs(actual - expected - legacy_delta) <= TOL
    new = abs(actual - expected) <= TOL
    if old == new:
        raise HomeAssistantError(f"Unexpected or ambiguous cost: {actual:.9f}, "
                                 f"expected {expected:.9f} or {expected + legacy_delta:.9f}")
    return "legacy" if old else "correct"


def register_subscription_repair(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE):
        return
    lock = asyncio.Lock()

    async def handle(call: ServiceCall):
        apply = call.data.get("apply", False)
        if type(apply) is not bool:
            raise HomeAssistantError("apply must be a boolean")
        if lock.locked():
            raise HomeAssistantError("Repair already running")
        async with lock:
            return await _repair(hass, apply)

    hass.services.async_register(DOMAIN, SERVICE, handle,
                                 supports_response=SupportsResponse.OPTIONAL)


async def _repair(hass, apply):
    runtimes = [e.runtime_data for e in hass.config_entries.async_entries(DOMAIN)
                if isinstance(getattr(e, "runtime_data", None), EneaRcemRuntime)]
    if len(runtimes) != 1:
        raise HomeAssistantError("Expected exactly one loaded Enea runtime")
    runtime = runtimes[0]
    aligner = getattr(runtime, "statistics_aligner", None)
    if not isinstance(aligner, StatisticsAligner) or not aligner.cost_stat_id or not aligner.import_stat_id:
        raise HomeAssistantError("Statistics IDs not ready")
    if hass.config.time_zone != "Europe/Warsaw":
        raise HomeAssistantError("Unexpected timezone; repair refused")
    if runtime.gap_count != 0:
        raise HomeAssistantError("Runtime has data gaps; repair refused")
    now = dt_util.now()
    if not 15 <= now.minute <= 45:
        raise HomeAssistantError("Run between minute 15 and 45 of the hour")
    if aligner._running:
        raise HomeAssistantError("Statistics alignment running; try again later")
    marker = runtime._data.get("repair_markers", {}).get(MARKER, {})
    if apply and marker.get("status") == "pending":
        raise HomeAssistantError("Previous repair pending; inspect backup and logs, do not retry blindly")
    legacy = runtime._rate(CONF_SUBSCRIPTION_FEE_NET, DEFAULT_SUBSCRIPTION_FEE_NET)
    if abs(legacy - 0.32) > 1e-8 or abs(runtime.vat_multiplier - 1.23) > 1e-8:
        raise HomeAssistantError("Unexpected subscription or VAT; repair refused")
    if abs(runtime.variable_rate_gross - 0.955710) > 1e-6:
        raise HomeAssistantError("Unexpected variable rate; repair refused")
    tz = dt_util.get_time_zone(hass.config.time_zone)
    start = datetime.combine(DISTRIBUTION_SUBSCRIPTION_END, datetime.min.time(), tz).astimezone(UTC)
    end = datetime.combine(SELLER_SUBSCRIPTION_START, datetime.min.time(), tz).astimezone(UTC)
    bucket = runtime._data.get("bucket_hour")
    cutoff = min(end, datetime.fromisoformat(bucket).astimezone(UTC))
    if cutoff <= start or cutoff > now.astimezone(UTC).replace(minute=0, second=0, microsecond=0):
        raise HomeAssistantError("Unexpected runtime hour")
    if abs(runtime.fixed_monthly_gross_for_date(start.astimezone(tz).date()) - 54.981) > 1e-6:
        raise HomeAssistantError("Unexpected fixed rates; repair refused")
    instance = get_instance(hass)
    stat_ids = {aligner.import_stat_id, aligner.cost_stat_id}

    async def read():
        result = await instance.async_add_executor_job(
            recorder_statistics.statistics_during_period, hass, start, cutoff,
            stat_ids, "hour", {"energy": "kWh"}, {"change", "sum", "state"})
        return {sid: {int(round(_timestamp(row["start"]))): row for row in rows}
                for sid, rows in result.items()}

    def expected_for(hour, energy):
        local = hour.astimezone(tz)
        hours = runtime._hours_in_month(local.isoformat())
        expected = (runtime.fixed_monthly_gross_for_date(local.date()) / hours
                    + float(energy) * runtime.variable_rate_gross)
        return expected, legacy * runtime.vat_multiplier / hours

    # Suppress regular alignment while the plan is computed and verified.
    # The runtime continues normal metering; reject an unexpected hour rollover
    # before any write. The raw cumulative state deliberately remains unchanged.
    aligner._running = True
    try:
        rows = await read()
        changes = []
        expected_by_hour = {}
        monthly = {}
        hour = start
        while hour < cutoff:
            stamp = int(hour.timestamp())
            imp = rows.get(aligner.import_stat_id, {}).get(stamp, {}).get("change")
            cost = rows.get(aligner.cost_stat_id, {}).get(stamp, {}).get("change")
            if imp is None or cost is None or not math.isfinite(float(imp)) or float(imp) < 0:
                raise HomeAssistantError(f"Missing/invalid hour {hour.isoformat()}; repair refused")
            expected, delta = expected_for(hour, imp)
            kind = _classification(cost, expected, delta)
            expected_by_hour[stamp] = expected
            if kind == "legacy":
                changes.append((hour, -delta))
                month = hour.astimezone(tz).strftime("%Y-%m")
                monthly[month] = monthly.get(month, 0.0) - delta
            hour += timedelta(hours=1)

        targets = {}
        for key, target in runtime._data.get("statistics_targets", {}).items():
            item = dict(target)
            hour = datetime.fromisoformat(key).astimezone(UTC)
            if start <= hour < end:
                expected, delta = expected_for(hour, item["import_kwh"])
                if _classification(item["import_cost_pln"], expected, delta) == "legacy":
                    item["import_cost_pln"] = expected
                    targets[key] = item

        report = {"mode": "apply" if apply else "dry_run",
                  "hours_checked": len(expected_by_hour),
                  "hours_to_correct": len(changes),
                  "targets_to_correct": len(targets),
                  "correction_pln": sum(delta for _, delta in changes),
                  "monthly_correction_pln": monthly,
                  "raw_runtime_counter_unchanged": True,
                  "cutoff_exclusive": cutoff.isoformat()}
        if not apply or not (changes or targets):
            return report
        if runtime._data.get("bucket_hour") != bucket or dt_util.now().minute > 50:
            raise HomeAssistantError("Hour changed during planning; no writes performed")
        markers = dict(runtime._data.get("repair_markers", {}))
        markers[MARKER] = {**report, "status": "pending",
                           "started_at": datetime.now(UTC).isoformat()}
        runtime._data["repair_markers"] = markers
        runtime._data["statistics_targets"] = {
            **runtime._data.get("statistics_targets", {}), **targets}
        await runtime._store.async_save(runtime._serialize())

        # API adjusts both cumulative sum tables from each hour forward.
        # Do not change raw state, which would be counted again on next update.
        for hour, delta in changes:
            instance.async_adjust_statistics(aligner.cost_stat_id, hour, delta, "PLN")

        verified = False
        for _ in range(60):
            await asyncio.sleep(2)
            check = await read()
            costs = check.get(aligner.cost_stat_id, {})
            if all(costs.get(stamp, {}).get("change") is not None
                   and abs(float(costs[stamp]["change"]) - expected) <= TOL
                   for stamp, expected in expected_by_hour.items()):
                verified = True
                break
        if not verified:
            raise HomeAssistantError("Repair queued but verification failed; pending marker retained. Do not retry blindly.")
        markers = dict(runtime._data.get("repair_markers", {}))
        markers[MARKER] = {**markers[MARKER], "status": "verified",
                           "verified_at": datetime.now(UTC).isoformat()}
        runtime._data["repair_markers"] = markers
        await runtime._store.async_save(runtime._serialize())
        report["verified"] = True
        _LOGGER.warning("Subscription repair verified: %s", report)
        return report
    finally:
        aligner._running = False
        if aligner._rerun_requested:
            aligner._rerun_requested = False
            hass.async_create_task(aligner.async_reconcile())
