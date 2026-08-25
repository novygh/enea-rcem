# Enea RCEm

Home Assistant custom integration for Polish prosumer settlement with **Enea / Enea Operator G11** and monthly **PSE RCEm**.

**Current stable version: 1.0.2**

> This project is intended to reproduce and monitor settlement logic in Home Assistant. Always verify tariff values and final billing against your contract and invoice.

## Scope

Enea RCEm is designed for prosumers settled using **monthly RCEm**, not hourly RCE. It uses existing cumulative grid import/export meters in Home Assistant and builds billing-oriented sensors locally.

The integration:

- performs hourly import/export balancing,
- applies independent post-balance import/export calibration,
- fetches official monthly RCEm values from PSE,
- detects later PSE RCEm corrections,
- keeps export compensation assigned to the month in which the energy was exported,
- exposes the latest fully settled month with its import cost and export compensation for dashboards,
- exposes day-by-day cost and export-value series for that settled month,
- applies historical prosumer-factor rules,
- calculates gross import cost from configurable Enea / Enea Operator rates and VAT,
- accrues fixed monthly charges across the month,
- reconstructs trustworthy cumulative meter deltas after Home Assistant restarts,
- distributes a recovered multi-hour delta over the missing hourly buckets instead of dropping energy,
- only increments the **Data gaps** diagnostic when a source baseline is actually missing or untrustworthy,
- reconstructs the prosumer deposit from Recorder statistics,
- keeps persistent runtime counters in Home Assistant storage without additional helpers.

## Sensors

### Energy

- **Balanced import** — cumulative hourly-balanced import; native calculation in kWh, suggested display in MWh with 3 decimals.
- **Balanced export** — cumulative hourly-balanced export; native calculation in kWh, suggested display in MWh with 3 decimals.

### Prices and billing

- **PSE RCEm** — latest published monthly RCEm.
- **PSE RCEm Prosumer** — RCEm with the currently applicable prosumer factor.
- **Import cost** — cumulative gross import cost.
- **Export compensation** — cumulative value of settled export for months with published RCEm.
- **Current month estimated export compensation** — estimate based on the latest available RCEm until the current month is officially published.

The **Export compensation** sensor also exposes dashboard-oriented attributes for the latest closed month that has both an official RCEm publication and Recorder billing data:

- `last_settled_month`,
- `last_settled_import_cost_pln`,
- `last_settled_export_compensation_pln`,
- `last_settled_daily_month`,
- `last_settled_daily`.

`last_settled_daily` contains one item for every calendar day of the selected month with:

- `date`,
- `import_cost_pln`,
- `export_compensation_pln`,
- `export_kwh`.

The daily export value is reconstructed from that day's balanced export and the official RCEm/factor for the selected settlement month. This avoids assigning the whole monthly RCEm reconciliation adjustment to a single day.

If the immediately preceding calendar month is not settled yet, the integration automatically falls back to the newest earlier month that is settled.

### Prosumer deposit

- **Prosumer deposit balance** — currently available deposit balance.
- **Deposit assigned this month** — value assigned to the deposit in the current month from the previous settlement month.
- **Deposit used this month** — deposit already used against active-energy purchase in the current month.
- **Active energy due this month** — active-energy amount still payable after deposit use.

### Diagnostics

- **Data gaps** — counts only cases where at least one cumulative source-meter baseline is missing or untrustworthy. A normal restart with recoverable cumulative deltas does not count as a gap.

## Energy Dashboard

For grid configuration, use the integration's own statistics:

- grid import: **Balanced import**,
- grid export: **Balanced export**,
- import cost: **Import cost**,
- export compensation: **Export compensation**.

Do not attach a separate current-price entity when using the cumulative cost/compensation sensors above.

## Installation with HACS

1. Open **HACS**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/novygh/enea-rcem` as type **Integration**.
4. Download **Enea RCEm**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration → Enea RCEm**.
7. Select the existing cumulative grid-import and grid-export sensors.

## Configuration

The setup/configuration flow contains Enea / Enea Operator G11 tariff values used by the integration. Values remain editable through the integration's **Configure** action.

In particular, verify against your own contract/invoice:

- active-energy price,
- commercial fee,
- distribution and statutory tariff components,
- VAT,
- import/export calibration values if used.

Calibration is applied **after hourly net balancing**, so changing an import correction does not alter export balancing and vice versa.

## Restart and missing-data recovery

The physical source sensors are expected to be cumulative meters that retain their totals while Home Assistant is offline.

If Home Assistant restarts across one or more hourly boundaries and both source baselines remain trustworthy, Enea RCEm:

1. reads the new cumulative totals,
2. calculates the missing import/export delta,
3. distributes that delta over the elapsed hourly buckets,
4. continues hourly balancing,
5. preserves the total recovered kWh,
6. does **not** increment `Data gaps`.

If a baseline is missing or a source meter decreases/reset unexpectedly, the integration rebases the affected source, preserves any trustworthy delta from the other source, and increments `Data gaps`.

## RCEm corrections

PSE can publish corrected RCEm values after the original publication. The integration periodically reconciles Recorder long-term statistics so a later correction is reflected in the original export month instead of being posted as a new amount in the month when the correction was published.

Recorder calculations explicitly request energy in **kWh**, so changing the display unit of the energy sensors to MWh does not affect settlement mathematics.

## Prosumer deposit

The deposit model is reconstructed from monthly Recorder statistics. Export value is assigned to the following settlement month and deposit lots are consumed oldest-first according to the implemented settlement rules.

Deposit sensors are current snapshots; the cumulative historical accounting remains in Recorder statistics and the cumulative import/export/cost/compensation sensors.

## Historical data

Version 1.0.2 does **not** automatically invent pre-installation history. Advanced migration/backfill tooling is included in `tools/` for installations where trustworthy historical cumulative meter statistics already exist.

Historical writes to Recorder should be treated as an advanced operation: make a Home Assistant backup first and validate the resulting long-term statistics after migration.

## Data sources

- local Home Assistant cumulative import/export energy sensors,
- PSE monthly RCEm publication,
- configured Enea seller / Enea Operator tariff values,
- Home Assistant Recorder long-term statistics for reconciliation, daily settlement detail, and deposit reconstruction.

## Disclaimer

This project is independent and is not affiliated with Enea S.A., Enea Operator, PSE, or Home Assistant. It is not a substitute for an electricity invoice or official settlement statement.
