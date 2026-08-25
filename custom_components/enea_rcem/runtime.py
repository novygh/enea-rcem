"""Runtime engine for Enea RCEm."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CAPACITY_FEE_NET,
    CONF_COGENERATION_NET,
    CONF_COMMERCIAL_FEE_NET,
    CONF_ENERGY_PRICE_NET,
    CONF_EXPORT_ENTITY,
    CONF_FIXED_NETWORK_NET,
    CONF_IMPORT_ENTITY,
    CONF_OZE_NET,
    CONF_QUALITY_NET,
    CONF_SUBSCRIPTION_FEE_NET,
    CONF_TRANSITION_FEE_NET,
    CONF_VARIABLE_NETWORK_NET,
    CONF_VAT_RATE,
    DEFAULT_CAPACITY_FEE_NET,
    DEFAULT_COGENERATION_NET,
    DEFAULT_COMMERCIAL_FEE_NET,
    DEFAULT_ENERGY_PRICE_NET,
    DEFAULT_FIXED_NETWORK_NET,
    DEFAULT_OZE_NET,
    DEFAULT_QUALITY_NET,
    DEFAULT_SUBSCRIPTION_FEE_NET,
    DEFAULT_TRANSITION_FEE_NET,
    DEFAULT_VARIABLE_NETWORK_NET,
    DEFAULT_VAT_RATE,
    DOMAIN,
    PROSUMER_DEPOSIT_FACTOR,
    RCEM_REFRESH_HOURS,
    STORAGE_VERSION,
)
from .rcem import RcemClient, RcemPrice

_LOGGER = logging.getLogger(__name__)


def _number(state: State | None) -> float | None:
    if state is None or state.state in ("unknown", "unavailable", "none", ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class EneaRcemRuntime:
    """Persistent billing and hourly balancing runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.import_entity = entry.data[CONF_IMPORT_ENTITY]
        self.export_entity = entry.data[CONF_EXPORT_ENTITY]
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True
        )
        self._client = RcemClient(async_get_clientsession(hass))
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._data: dict[str, Any] = {}
        self.rcem_prices: dict[str, RcemPrice] = {}
        self.last_rcem_error: str | None = None
        self._recovery_pending = False

    @property
    def options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    def _rate(self, key: str, default: float) -> float:
        try:
            return float(self.options.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def vat_multiplier(self) -> float:
        return 1.0 + self._rate(CONF_VAT_RATE, DEFAULT_VAT_RATE) / 100.0

    @property
    def variable_rate_gross(self) -> float:
        net = (
            self._rate(CONF_ENERGY_PRICE_NET, DEFAULT_ENERGY_PRICE_NET)
            + self._rate(CONF_VARIABLE_NETWORK_NET, DEFAULT_VARIABLE_NETWORK_NET)
            + self._rate(CONF_QUALITY_NET, DEFAULT_QUALITY_NET)
            + self._rate(CONF_OZE_NET, DEFAULT_OZE_NET)
            + self._rate(CONF_COGENERATION_NET, DEFAULT_COGENERATION_NET)
        )
        return net * self.vat_multiplier

    @property
    def fixed_monthly_gross(self) -> float:
        net = (
            self._rate(CONF_COMMERCIAL_FEE_NET, DEFAULT_COMMERCIAL_FEE_NET)
            + self._rate(CONF_FIXED_NETWORK_NET, DEFAULT_FIXED_NETWORK_NET)
            + self._rate(CONF_CAPACITY_FEE_NET, DEFAULT_CAPACITY_FEE_NET)
            + self._rate(CONF_SUBSCRIPTION_FEE_NET, DEFAULT_SUBSCRIPTION_FEE_NET)
            + self._rate(CONF_TRANSITION_FEE_NET, DEFAULT_TRANSITION_FEE_NET)
        )
        return net * self.vat_multiplier

    @property
    def balanced_import_total(self) -> float:
        return float(self._data.get("balanced_import_total", 0.0))

    @property
    def balanced_export_total(self) -> float:
        return float(self._data.get("balanced_export_total", 0.0))

    @property
    def import_cost_total(self) -> float:
        return float(self._data.get("import_cost_total", 0.0))

    @property
    def export_compensation_total(self) -> float:
        values = self._data.get("monthly_compensation", {})
        return float(sum(float(value) for value in values.values()))

    @property
    def gap_count(self) -> int:
        return int(self._data.get("gap_count", 0))

    @property
    def latest_rcem(self) -> RcemPrice | None:
        if not self.rcem_prices:
            return None
        return self.rcem_prices[max(self.rcem_prices)]

    @property
    def current_month_export(self) -> float:
        month = dt_util.now().strftime("%Y-%m")
        return float(self._data.get("monthly_export", {}).get(month, 0.0))

    @property
    def current_month_export_estimate(self) -> float | None:
        latest = self.latest_rcem
        if latest is None:
            return None
        return (
            self.current_month_export
            * latest.price_pln_mwh
            / 1000.0
            * PROSUMER_DEPOSIT_FACTOR
        )

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        now = dt_util.now()
        hour = self._hour_key(now)
        self._data = stored if stored else self._fresh_data(hour)

        # Storage written by releases before 0.1.3 did not distinguish a
        # complete hourly bucket from the partial hour in which the integration
        # was first loaded.
        self._data.setdefault("bucket_valid", False)

        self._load_cached_rcem()
        self._resume_source_totals(now)

        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self.import_entity, self.export_entity],
                self._async_source_changed,
            )
        )
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._async_hour_tick, minute=0, second=2
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_rcem_tick,
                timedelta(hours=RCEM_REFRESH_HOURS),
            )
        )

        await self.async_refresh_rcem()
        self._schedule_save()

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._serialize())

    def _fresh_data(self, hour: str) -> dict[str, Any]:
        return {
            "bucket_hour": hour,
            "bucket_valid": False,
            "bucket_import": 0.0,
            "bucket_export": 0.0,
            "last_import_total": None,
            "last_export_total": None,
            "balanced_import_total": 0.0,
            "balanced_export_total": 0.0,
            "import_cost_total": 0.0,
            "monthly_export": {},
            "monthly_import": {},
            "monthly_compensation": {},
            "rcem_prices": {},
            "gap_count": 0,
        }

    def _load_cached_rcem(self) -> None:
        cached = self._data.get("rcem_prices", {})
        for month, item in cached.items():
            try:
                self.rcem_prices[month] = RcemPrice(
                    month=month,
                    price_pln_mwh=float(item["price_pln_mwh"]),
                    published=str(item["published"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def _serialize(self) -> dict[str, Any]:
        data = dict(self._data)
        data["rcem_prices"] = {
            month: {
                "price_pln_mwh": item.price_pln_mwh,
                "published": item.published,
            }
            for month, item in self.rcem_prices.items()
        }
        return data

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._serialize, 30)

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @staticmethod
    def _hour_key(now: datetime) -> str:
        return now.replace(minute=0, second=0, microsecond=0).isoformat()

    def _resume_source_totals(self, now: datetime) -> None:
        current_hour = self._hour_key(now)
        stored_hour = self._data.get("bucket_hour")
        import_now = _number(self.hass.states.get(self.import_entity))
        export_now = _number(self.hass.states.get(self.export_entity))

        if stored_hour != current_hour:
            if self._recover_gap(
                current_hour=current_hour,
                import_now=import_now,
                export_now=export_now,
            ):
                self._recovery_pending = False
                return

            # Keep the old baseline until both physical meter totals are
            # available. The first later source update will retry recovery,
            # so cumulative kWh are never silently discarded.
            self._recovery_pending = True
            _LOGGER.debug(
                "Waiting for both source meters before reconstructing gap "
                "from %s to %s",
                stored_hour,
                current_hour,
            )
            return

        self._recovery_pending = False
        self._resume_one("import", import_now)
        self._resume_one("export", export_now)

    def _recover_gap(
        self,
        *,
        current_hour: str,
        import_now: float | None,
        export_now: float | None,
    ) -> bool:
        stored_hour = self._data.get("bucket_hour")
        if not stored_hour or stored_hour == current_hour:
            return True
        if import_now is None or export_now is None:
            return False

        previous_import = self._data.get("last_import_total")
        previous_export = self._data.get("last_export_total")
        if previous_import is None or previous_export is None:
            # There is no trustworthy baseline to reconstruct from. Rebase
            # without inventing energy, but keep the gap visible.
            self._data["gap_count"] = int(self._data.get("gap_count", 0)) + 1
            self._data["bucket_hour"] = current_hour
            self._data["bucket_valid"] = False
            self._data["bucket_import"] = 0.0
            self._data["bucket_export"] = 0.0
            self._data["last_import_total"] = import_now
            self._data["last_export_total"] = export_now
            return True

        delta_import = import_now - float(previous_import)
        delta_export = export_now - float(previous_export)
        if delta_import < -1e-9 or delta_export < -1e-9:
            _LOGGER.warning(
                "Source meter decreased across restart; rebasing without "
                "reconstructing the missing interval"
            )
            self._data["gap_count"] = int(self._data.get("gap_count", 0)) + 1
            self._data["bucket_hour"] = current_hour
            self._data["bucket_valid"] = False
            self._data["bucket_import"] = 0.0
            self._data["bucket_export"] = 0.0
            self._data["last_import_total"] = import_now
            self._data["last_export_total"] = export_now
            return True

        hour_keys = self._hour_keys(stored_hour, current_hour)
        if not hour_keys:
            return False

        # The meters preserve cumulative totals while Home Assistant is down.
        # Split only the unobserved increase equally across every affected hour.
        # Any energy already observed before shutdown remains in the old bucket.
        share_import = max(delta_import, 0.0) / len(hour_keys)
        share_export = max(delta_export, 0.0) / len(hour_keys)

        old_import = max(float(self._data.get("bucket_import", 0.0)), 0.0)
        old_export = max(float(self._data.get("bucket_export", 0.0)), 0.0)

        for index, hour_key in enumerate(hour_keys):
            if index == 0:
                imp = old_import + share_import
                exp = old_export + share_export
            else:
                imp = share_import
                exp = share_export

            if hour_key == current_hour:
                self._data["bucket_hour"] = current_hour
                self._data["bucket_valid"] = True
                self._data["bucket_import"] = imp
                self._data["bucket_export"] = exp
                break

            # A reconstructed historical hour is deliberately approximate, but
            # preserving cumulative kWh is preferable to dropping energy.
            self._finalize_values(hour_key, imp, exp)

        self._data["last_import_total"] = import_now
        self._data["last_export_total"] = export_now
        self._data["gap_count"] = int(self._data.get("gap_count", 0)) + 1

        _LOGGER.warning(
            "Reconstructed Home Assistant data gap from %s to %s across %d "
            "hour buckets; distributed %.6f kWh import and %.6f kWh export "
            "equally",
            stored_hour,
            current_hour,
            len(hour_keys),
            max(delta_import, 0.0),
            max(delta_export, 0.0),
        )
        self._recalculate_compensation()
        self._schedule_save()
        self._notify()
        return True

    def _hour_keys(self, start_key: str, end_key: str) -> list[str]:
        start = dt_util.parse_datetime(start_key)
        end = dt_util.parse_datetime(end_key)
        if start is None or end is None:
            return []
        if start.tzinfo is None or end.tzinfo is None:
            return []

        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        current_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if current_utc > end_utc:
            return []

        keys: list[str] = []
        while current_utc <= end_utc:
            keys.append(self._hour_key(current_utc.astimezone(tz)))
            current_utc += timedelta(hours=1)
        return keys

    def _resume_one(self, kind: str, current: float | None) -> None:
        if current is None:
            return
        key = f"last_{kind}_total"
        previous = self._data.get(key)
        if previous is not None:
            delta = current - float(previous)
            if delta >= 0:
                self._data[f"bucket_{kind}"] = (
                    float(self._data.get(f"bucket_{kind}", 0.0)) + delta
                )
            elif abs(delta) > 1e-9:
                self._data["gap_count"] = int(self._data.get("gap_count", 0)) + 1
                self._data["bucket_valid"] = False
        self._data[key] = current

    @callback
    def _async_source_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state: State | None = event.data.get("new_state")
        value = _number(new_state)
        if value is None:
            return

        now = dt_util.now()

        if self._recovery_pending:
            self._resume_source_totals(now)
            if self._recovery_pending:
                return
            # Recovery already incorporated the current physical meter values.
            # Do not count this state change a second time.
            return

        self._roll_to(now)
        kind = "import" if entity_id == self.import_entity else "export"
        key = f"last_{kind}_total"
        previous = self._data.get(key)

        if previous is None:
            self._data[key] = value
            self._schedule_save()
            return

        delta = value - float(previous)
        self._data[key] = value
        if delta < -1e-9:
            _LOGGER.warning(
                "%s source meter decreased from %.6f to %.6f; "
                "rebasing without adding delta",
                kind,
                float(previous),
                value,
            )
            self._data["gap_count"] = int(self._data.get("gap_count", 0)) + 1
            self._data["bucket_valid"] = False
        elif delta > 0:
            self._data[f"bucket_{kind}"] = (
                float(self._data.get(f"bucket_{kind}", 0.0)) + delta
            )

        self._schedule_save()
        self._notify()

    @callback
    def _async_hour_tick(self, now: datetime) -> None:
        if self._recovery_pending:
            self._resume_source_totals(now)
            if self._recovery_pending:
                return
        self._roll_to(now)

    @callback
    def _roll_to(self, now: datetime) -> None:
        target = self._hour_key(now)
        current = self._data.get("bucket_hour")
        if current == target:
            return

        if self._data.get("bucket_valid"):
            self._finalize_bucket(current)
        else:
            _LOGGER.debug("Discarding incomplete hourly bucket %s", current)

        self._data["bucket_hour"] = target
        # Once Home Assistant crosses an hour boundary while this runtime is
        # active, the new bucket starts at the real boundary and is complete.
        self._data["bucket_valid"] = True
        self._data["bucket_import"] = 0.0
        self._data["bucket_export"] = 0.0
        self._schedule_save()
        self._notify()

    def _finalize_bucket(self, hour_key: str | None) -> None:
        if not hour_key:
            return
        imp = max(float(self._data.get("bucket_import", 0.0)), 0.0)
        exp = max(float(self._data.get("bucket_export", 0.0)), 0.0)
        self._finalize_values(hour_key, imp, exp)

    def _finalize_values(self, hour_key: str, imp: float, exp: float) -> None:
        balanced_import = max(imp - exp, 0.0)
        balanced_export = max(exp - imp, 0.0)

        self._data["balanced_import_total"] = (
            self.balanced_import_total + balanced_import
        )
        self._data["balanced_export_total"] = (
            self.balanced_export_total + balanced_export
        )

        month = hour_key[:7]
        monthly_import = dict(self._data.get("monthly_import", {}))
        monthly_export = dict(self._data.get("monthly_export", {}))
        monthly_import[month] = (
            float(monthly_import.get(month, 0.0)) + balanced_import
        )
        monthly_export[month] = (
            float(monthly_export.get(month, 0.0)) + balanced_export
        )
        self._data["monthly_import"] = monthly_import
        self._data["monthly_export"] = monthly_export

        # Fixed charges accrue per calendar hour. Reconstructed historical hours
        # therefore keep their share of monthly fixed fees after an HA outage.
        fixed_hour = self.fixed_monthly_gross / self._hours_in_month(hour_key)
        variable = balanced_import * self.variable_rate_gross
        self._data["import_cost_total"] = (
            self.import_cost_total + fixed_hour + variable
        )

    def _hours_in_month(self, hour_key: str) -> float:
        year = int(hour_key[0:4])
        month = int(hour_key[5:7])
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        start = datetime(year, month, 1, tzinfo=tz)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            end = datetime(year, month + 1, 1, tzinfo=tz)
        return (
            end.astimezone(UTC) - start.astimezone(UTC)
        ).total_seconds() / 3600.0

    @callback
    def _async_rcem_tick(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_refresh_rcem())

    async def async_refresh_rcem(self) -> None:
        try:
            prices = await self._client.async_fetch()
        except (ConnectionError, ValueError) as err:
            self.last_rcem_error = str(err)
            _LOGGER.warning("RCEm refresh failed: %s", err)
            self._notify()
            return

        changed = prices != self.rcem_prices
        self.rcem_prices = prices
        self.last_rcem_error = None
        if changed:
            self._recalculate_compensation()
            self._schedule_save()
            self._notify()

    def _recalculate_compensation(self) -> None:
        monthly_export = self._data.get("monthly_export", {})
        compensation: dict[str, float] = {}
        for month, export_kwh in monthly_export.items():
            price = self.rcem_prices.get(month)
            if price is None:
                continue
            compensation[month] = (
                float(export_kwh)
                * price.price_pln_mwh
                / 1000.0
                * PROSUMER_DEPOSIT_FACTOR
            )
        self._data["monthly_compensation"] = compensation
