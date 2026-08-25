"""Sensors for Enea RCEm."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PROSUMER_DEPOSIT_FACTOR
from .runtime import EneaRcemRuntime


@dataclass(frozen=True, kw_only=True)
class EneaRcemSensorDescription(SensorEntityDescription):
    """Describe an Enea RCEm sensor."""

    value_fn: Callable[[EneaRcemRuntime], float | int | None]
    attrs_fn: Callable[[EneaRcemRuntime], dict] | None = None


def _latest_attrs(runtime: EneaRcemRuntime) -> dict:
    latest = runtime.latest_rcem
    if latest is None:
        return {"source": "PSE", "error": runtime.last_rcem_error}
    return {
        "source": "PSE",
        "month": latest.month,
        "published": latest.published,
        "error": runtime.last_rcem_error,
    }


def _import_cost_attrs(runtime: EneaRcemRuntime) -> dict:
    return {
        "variable_rate_gross_pln_kwh": round(runtime.variable_rate_gross, 6),
        "fixed_monthly_gross_pln": round(runtime.fixed_monthly_gross, 4),
        "data_gap_count": runtime.gap_count,
    }


def _comp_attrs(runtime: EneaRcemRuntime) -> dict:
    return {
        "settled_months": sorted(runtime._data.get("monthly_compensation", {})),
        "data_gap_count": runtime.gap_count,
        "prosumer_factor": PROSUMER_DEPOSIT_FACTOR,
    }


SENSORS: tuple[EneaRcemSensorDescription, ...] = (
    EneaRcemSensorDescription(
        key="rcem",
        translation_key="rcem",
        native_unit_of_measurement="PLN/MWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: r.latest_rcem.price_pln_mwh if r.latest_rcem else None,
        attrs_fn=_latest_attrs,
    ),
    EneaRcemSensorDescription(
        key="rcem_prosumer",
        translation_key="rcem_prosumer",
        native_unit_of_measurement="PLN/MWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: (
            r.latest_rcem.price_pln_mwh * PROSUMER_DEPOSIT_FACTOR
            if r.latest_rcem
            else None
        ),
        attrs_fn=_latest_attrs,
    ),
    EneaRcemSensorDescription(
        key="balanced_import",
        translation_key="balanced_import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda r: r.balanced_import_total,
        attrs_fn=lambda r: {"data_gap_count": r.gap_count},
    ),
    EneaRcemSensorDescription(
        key="balanced_export",
        translation_key="balanced_export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda r: r.balanced_export_total,
        attrs_fn=lambda r: {"data_gap_count": r.gap_count},
    ),
    EneaRcemSensorDescription(
        key="import_cost",
        translation_key="import_cost",
        native_unit_of_measurement="PLN",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda r: r.import_cost_total,
        attrs_fn=_import_cost_attrs,
    ),
    EneaRcemSensorDescription(
        key="export_compensation",
        translation_key="export_compensation",
        native_unit_of_measurement="PLN",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda r: r.export_compensation_total,
        attrs_fn=_comp_attrs,
    ),
    EneaRcemSensorDescription(
        key="current_month_export_estimate",
        translation_key="current_month_export_estimate",
        native_unit_of_measurement="PLN",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda r: r.current_month_export_estimate,
        attrs_fn=lambda r: {
            "export_kwh": round(r.current_month_export, 4),
            "price_basis_month": r.latest_rcem.month if r.latest_rcem else None,
            "estimated": True,
        },
    ),
    EneaRcemSensorDescription(
        key="data_gaps",
        translation_key="data_gaps",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r.gap_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enea RCEm sensors."""
    runtime: EneaRcemRuntime = entry.runtime_data
    async_add_entities(EneaRcemSensor(runtime, description) for description in SENSORS)


class EneaRcemSensor(SensorEntity):
    """Representation of an Enea RCEm sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: EneaRcemRuntime,
        description: EneaRcemSensorDescription,
    ) -> None:
        self.runtime = runtime
        self.entity_description = description
        self._attr_unique_id = f"{runtime.entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.entry.entry_id)},
            name="Enea RCEm",
            manufacturer="novygh",
            model="G11 / RCEm billing",
        )

    @property
    def native_value(self) -> float | int | None:
        """Return current value."""
        return self.entity_description.value_fn(self.runtime)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.runtime)

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.add_listener(self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
