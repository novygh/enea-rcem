"""Versioned Enea G11 tariff history used by historical billing.

The values below are based on the user's Enea invoices covering
2024-06-12..2025-05-31 and 2025-06-01..2026-05-31. They are deliberately
kept separate from the editable current-rate options used by the live runtime.

Historical backfill must select the period that was valid at the timestamp
being reconstructed. Do not use today's tariff to recalculate older energy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class TariffPeriod:
    """One effective Enea/Enea Operator tariff period."""

    start: date
    end: date | None
    energy_price_net: float
    commercial_fee_net: float
    variable_network_net: float
    quality_net: float
    oze_net: float
    cogeneration_net: float
    fixed_network_net: float
    capacity_fee_net: float
    subscription_fee_net: float
    transition_fee_net: float
    vat_rate: float = 23.0

    def contains(self, value: date) -> bool:
        """Return whether the local calendar date belongs to this period."""
        return value >= self.start and (self.end is None or value < self.end)

    @property
    def variable_rate_net(self) -> float:
        """Return the net variable import rate in PLN/kWh."""
        return (
            self.energy_price_net
            + self.variable_network_net
            + self.quality_net
            + self.oze_net
            + self.cogeneration_net
        )

    @property
    def variable_rate_gross(self) -> float:
        """Return the gross variable import rate in PLN/kWh."""
        return self.variable_rate_net * (1.0 + self.vat_rate / 100.0)

    @property
    def fixed_monthly_net(self) -> float:
        """Return the nominal net monthly fixed charges."""
        return (
            self.commercial_fee_net
            + self.fixed_network_net
            + self.capacity_fee_net
            + self.subscription_fee_net
            + self.transition_fee_net
        )

    @property
    def fixed_monthly_gross(self) -> float:
        """Return the nominal gross monthly fixed charges."""
        return self.fixed_monthly_net * (1.0 + self.vat_rate / 100.0)


# Effective periods verified against Enea invoices.
# End dates are exclusive.
HISTORICAL_TARIFFS: tuple[TariffPeriod, ...] = (
    TariffPeriod(
        start=date(2024, 6, 12),
        end=date(2024, 7, 1),
        energy_price_net=0.6980,
        commercial_fee_net=14.31,
        variable_network_net=0.2486,
        quality_net=0.0314,
        oze_net=0.0000,
        cogeneration_net=0.00618,
        fixed_network_net=10.14,
        capacity_fee_net=14.90,
        subscription_fee_net=0.32,
        transition_fee_net=0.33,
    ),
    TariffPeriod(
        start=date(2024, 7, 1),
        end=date(2025, 1, 1),
        energy_price_net=0.5050,
        commercial_fee_net=10.24,
        variable_network_net=0.2486,
        quality_net=0.0314,
        oze_net=0.0000,
        cogeneration_net=0.00618,
        fixed_network_net=10.14,
        capacity_fee_net=0.00,
        subscription_fee_net=0.32,
        transition_fee_net=0.33,
    ),
    TariffPeriod(
        start=date(2025, 1, 1),
        end=date(2025, 7, 1),
        energy_price_net=0.5050,
        commercial_fee_net=10.24,
        variable_network_net=0.2456,
        quality_net=0.0321,
        oze_net=0.0035,
        cogeneration_net=0.0030,
        fixed_network_net=10.14,
        capacity_fee_net=0.00,
        subscription_fee_net=0.32,
        transition_fee_net=0.33,
    ),
    TariffPeriod(
        start=date(2025, 7, 1),
        end=date(2026, 1, 1),
        energy_price_net=0.5050,
        commercial_fee_net=10.24,
        variable_network_net=0.2456,
        quality_net=0.0321,
        oze_net=0.0035,
        cogeneration_net=0.0030,
        fixed_network_net=10.14,
        capacity_fee_net=16.01,
        subscription_fee_net=0.32,
        transition_fee_net=0.33,
    ),
    TariffPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 2, 1),
        energy_price_net=0.4879,
        commercial_fee_net=10.24,
        variable_network_net=0.2456,
        quality_net=0.0331,
        oze_net=0.0073,
        cogeneration_net=0.0030,
        fixed_network_net=10.41,
        capacity_fee_net=24.05,
        subscription_fee_net=0.32,
        transition_fee_net=0.00,
    ),
    TariffPeriod(
        start=date(2026, 2, 1),
        end=None,
        energy_price_net=0.4879,
        commercial_fee_net=10.24,
        variable_network_net=0.2456,
        quality_net=0.0332,
        oze_net=0.0073,
        cogeneration_net=0.0030,
        fixed_network_net=10.41,
        capacity_fee_net=24.05,
        subscription_fee_net=0.32,
        transition_fee_net=0.00,
    ),
)


# June 2024 is a special partial billing month. Enea prorated the network,
# transition and capacity charges to 19/30 of the month, but charged the
# subscription fee and the first commercial fee in full. Historical billing
# must therefore use the invoice total below instead of simply multiplying the
# nominal monthly fixed-rate sum by the elapsed fraction of June.
HISTORICAL_FIXED_NET_OVERRIDES: dict[str, float] = {
    "2024-06": 6.42 + 0.21 + 9.44 + 0.32 + 14.31,
}


# The 1.23 prosumer-deposit uplift entered into force for settlements from
# 2025-02-01. Earlier RCEm months use factor 1.00. This is intentionally
# versioned separately from RCEm itself because PSE publishes the market price,
# while the uplift is a statutory settlement rule applied by the seller.
PROSUMER_FACTOR_PERIODS: tuple[tuple[date, date | None, float], ...] = (
    (date(2024, 6, 12), date(2025, 2, 1), 1.00),
    (date(2025, 2, 1), None, 1.23),
)


def tariff_for_date(value: date) -> TariffPeriod | None:
    """Return the historical tariff period for a local calendar date."""
    for period in HISTORICAL_TARIFFS:
        if period.contains(value):
            return period
    return None


def tariff_for_datetime(value: datetime) -> TariffPeriod | None:
    """Return the historical tariff period for a timezone-aware local datetime."""
    return tariff_for_date(value.date())


def fixed_invoice_net_override(month: str) -> float | None:
    """Return a verified invoice fixed-charge override for YYYY-MM, if any."""
    return HISTORICAL_FIXED_NET_OVERRIDES.get(month)


def prosumer_factor_for_date(value: date) -> float:
    """Return the statutory deposit uplift factor valid for a local date."""
    for start, end, factor in PROSUMER_FACTOR_PERIODS:
        if value >= start and (end is None or value < end):
            return factor
    raise ValueError(f"No prosumer factor configured for {value.isoformat()}")


def prosumer_factor_for_month(month: str) -> float:
    """Return the unambiguous statutory deposit factor overlapping YYYY-MM."""
    year_text, month_text = month.split("-", 1)
    year = int(year_text)
    month_number = int(month_text)
    month_start = date(year, month_number, 1)
    if month_number == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month_number + 1, 1)

    factors = {
        factor
        for start, end, factor in PROSUMER_FACTOR_PERIODS
        if start < month_end and (end is None or end > month_start)
    }
    if len(factors) == 1:
        return factors.pop()
    if not factors:
        raise ValueError(f"No prosumer factor configured for {month}")
    raise ValueError(f"Multiple prosumer factors overlap {month}")
