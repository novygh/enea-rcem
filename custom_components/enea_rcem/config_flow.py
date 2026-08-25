"""Config flow for Enea RCEm."""

from __future__ import annotations

from typing import Any, Literal

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CAPACITY_FEE_NET,
    CONF_COGENERATION_NET,
    CONF_COMMERCIAL_FEE_NET,
    CONF_ENERGY_PRICE_NET,
    CONF_EXPORT_CORRECTION_PERCENT,
    CONF_EXPORT_ENTITY,
    CONF_FIXED_NETWORK_NET,
    CONF_IMPORT_CORRECTION_PERCENT,
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
    DEFAULT_EXPORT_CORRECTION_PERCENT,
    DEFAULT_FIXED_NETWORK_NET,
    DEFAULT_IMPORT_CORRECTION_PERCENT,
    DEFAULT_OZE_NET,
    DEFAULT_QUALITY_NET,
    DEFAULT_SUBSCRIPTION_FEE_NET,
    DEFAULT_TRANSITION_FEE_NET,
    DEFAULT_VARIABLE_NETWORK_NET,
    DEFAULT_VAT_RATE,
    DOMAIN,
)


def _number(
    default: float,
    step: float | Literal["any"] = "any",
) -> selector.NumberSelector:
    """Return a box number selector.

    Home Assistant currently requires numeric selector steps to be >= 0.001.
    Energy tariffs need four decimal places, so use step='any' for those fields;
    the box selector still validates the entered value as a float.
    """
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=1000,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _correction_number() -> selector.NumberSelector:
    """Return a signed percentage correction selector."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=-100,
            max=100,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _rates_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_IMPORT_CORRECTION_PERCENT,
                default=values.get(
                    CONF_IMPORT_CORRECTION_PERCENT,
                    DEFAULT_IMPORT_CORRECTION_PERCENT,
                ),
            ): _correction_number(),
            vol.Required(
                CONF_EXPORT_CORRECTION_PERCENT,
                default=values.get(
                    CONF_EXPORT_CORRECTION_PERCENT,
                    DEFAULT_EXPORT_CORRECTION_PERCENT,
                ),
            ): _correction_number(),
            vol.Required(
                CONF_ENERGY_PRICE_NET,
                default=values.get(CONF_ENERGY_PRICE_NET, DEFAULT_ENERGY_PRICE_NET),
            ): _number(DEFAULT_ENERGY_PRICE_NET),
            vol.Required(
                CONF_COMMERCIAL_FEE_NET,
                default=values.get(CONF_COMMERCIAL_FEE_NET, DEFAULT_COMMERCIAL_FEE_NET),
            ): _number(DEFAULT_COMMERCIAL_FEE_NET, 0.01),
            vol.Required(
                CONF_VARIABLE_NETWORK_NET,
                default=values.get(CONF_VARIABLE_NETWORK_NET, DEFAULT_VARIABLE_NETWORK_NET),
            ): _number(DEFAULT_VARIABLE_NETWORK_NET),
            vol.Required(
                CONF_QUALITY_NET,
                default=values.get(CONF_QUALITY_NET, DEFAULT_QUALITY_NET),
            ): _number(DEFAULT_QUALITY_NET),
            vol.Required(
                CONF_OZE_NET,
                default=values.get(CONF_OZE_NET, DEFAULT_OZE_NET),
            ): _number(DEFAULT_OZE_NET),
            vol.Required(
                CONF_COGENERATION_NET,
                default=values.get(CONF_COGENERATION_NET, DEFAULT_COGENERATION_NET),
            ): _number(DEFAULT_COGENERATION_NET),
            vol.Required(
                CONF_FIXED_NETWORK_NET,
                default=values.get(CONF_FIXED_NETWORK_NET, DEFAULT_FIXED_NETWORK_NET),
            ): _number(DEFAULT_FIXED_NETWORK_NET, 0.01),
            vol.Required(
                CONF_CAPACITY_FEE_NET,
                default=values.get(CONF_CAPACITY_FEE_NET, DEFAULT_CAPACITY_FEE_NET),
            ): _number(DEFAULT_CAPACITY_FEE_NET, 0.01),
            vol.Required(
                CONF_SUBSCRIPTION_FEE_NET,
                default=values.get(CONF_SUBSCRIPTION_FEE_NET, DEFAULT_SUBSCRIPTION_FEE_NET),
            ): _number(DEFAULT_SUBSCRIPTION_FEE_NET, 0.01),
            vol.Required(
                CONF_TRANSITION_FEE_NET,
                default=values.get(CONF_TRANSITION_FEE_NET, DEFAULT_TRANSITION_FEE_NET),
            ): _number(DEFAULT_TRANSITION_FEE_NET, 0.01),
            vol.Required(
                CONF_VAT_RATE,
                default=values.get(CONF_VAT_RATE, DEFAULT_VAT_RATE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=0.01,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


class EneaRcemConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enea RCEm."""

    VERSION = 1

    def __init__(self) -> None:
        self._source_data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select source energy sensors."""
        if user_input is not None:
            if user_input[CONF_IMPORT_ENTITY] == user_input[CONF_EXPORT_ENTITY]:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._source_schema(user_input),
                    errors={"base": "same_entity"},
                )
            self._source_data = user_input
            return await self.async_step_rates()

        return self.async_show_form(step_id="user", data_schema=self._source_schema({}))

    def _source_schema(self, values: dict[str, Any]) -> vol.Schema:
        entity_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_IMPORT_ENTITY,
                    default=values.get(CONF_IMPORT_ENTITY),
                ): entity_selector,
                vol.Required(
                    CONF_EXPORT_ENTITY,
                    default=values.get(CONF_EXPORT_ENTITY),
                ): entity_selector,
            }
        )

    async def async_step_rates(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure billing rates."""
        if user_input is not None:
            data = {**self._source_data, **user_input}
            return self.async_create_entry(title="Enea RCEm", data=data)

        return self.async_show_form(step_id="rates", data_schema=_rates_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return EneaRcemOptionsFlow()


class EneaRcemOptionsFlow(config_entries.OptionsFlow):
    """Edit current billing rates."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Edit rates and meter corrections."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_rates_schema(current))
