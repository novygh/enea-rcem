# Enea RCEm

Custom Home Assistant integration for Polish prosumer billing with **Enea / Enea Operator G11** and **PSE RCEm**.

> Early alpha. Verify calculations against your invoice before relying on them for payments.

## What v0.1 does

- selects existing cumulative grid import/export energy sensors from Home Assistant,
- performs hourly import/export balancing locally,
- fetches official monthly **RCEm** values from PSE,
- detects later RCEm corrections and recalculates monthly export compensation,
- applies the prosumer deposit factor `1.23`,
- calculates gross import cost from configurable net energy/distribution rates plus VAT,
- accrues fixed monthly fees across the month,
- stores its internal counters persistently without creating Home Assistant helpers.

## Sensors

- PSE RCEm
- PSE RCEm Prosumer
- Hourly-balanced import
- Hourly-balanced export
- Import cost
- Export compensation (only months for which RCEm is already published)
- Current month estimated export compensation
- Data gaps (diagnostic)

## Installation with HACS

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/novygh/enea-rcem` as type **Integration**.
3. Download **Enea RCEm**.
4. Restart Home Assistant.
5. Settings → Devices & services → Add integration → **Enea RCEm**.

## Initial rates

The setup flow contains 2026 Enea Operator G11 defaults. The two seller-specific values must be verified against your own Enea invoice:

- active energy price (net PLN/kWh),
- commercial fee (net PLN/month).

All rates remain editable under the integration's **Configure** action.

## Important limitations in v0.1

- v0.1 starts accounting from installation time; historical reconstruction is not performed automatically yet.
- changing rates currently applies from save time; versioned contract/tariff history is planned.
- if Home Assistant is offline across an hourly boundary, the integration refuses to guess the hourly split and increments `Data gaps` instead.
- finalized RCEm compensation is recalculated when PSE publishes/corrects RCEm, but retroactive Energy Dashboard timestamp repair is planned for the history/statistics stage.

## Planned

- versioned Enea seller and Enea Operator rate tables,
- retroactive long-term-statistics reconstruction,
- correct backdating of RCEm publications/corrections in Energy Dashboard,
- optional invoice-assisted rate change detection/approval.

## Data sources

- local Home Assistant energy sensors: import/export kWh,
- PSE: RCEm,
- configured Enea seller and Enea Operator rates.

This project is independent and is not affiliated with Enea S.A., Enea Operator, or PSE.
