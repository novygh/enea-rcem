# Advanced tools

The files in this directory are **not loaded by the Home Assistant integration**.
They are manual diagnostic or migration helpers.

## `recorder_statistics_audit.py`

Read-only continuity audit for Enea RCEm Recorder statistics. It can inspect the
hourly long-term layer or the 5-minute short-term layer and flag suspicious state
resets, sum drops, change mismatches, and relative-state/cumulative-sum mixtures.

Example:

```bash
python3 tools/recorder_statistics_audit.py \
  --start 2026-08-25T00:00:00+00:00 \
  --end 2026-08-26T00:00:00+00:00 \
  --period hour
```

Use `--period 5minute` for the short-term Recorder layer. The tool only reads
Home Assistant data.

## `deposit_dry_run_2026.py`

Read-only reconstruction of the prosumer-deposit ledger used while validating
the 2024-2026 historical settlement model. It contains tariff assumptions that
must be reviewed before reusing it for a later period.

## `backfill_history_2026.py`

One-time historical migration tool used for the accepted 2024-06-12 through
2026-08-25 backfill. It is intentionally pinned to the diagnosed migration and
Home Assistant version and **must not be treated as a generic repair command**.
Make a full Home Assistant backup and inspect the script before any reuse.

## Completed 2026-08-25 incident repairs

The temporary Home Assistant services that repaired the 2026-08-25 migration
boundary were removed from the production integration in version 1.1.0. They
were installation-specific and could be dangerous if accidentally invoked on a
healthy database.

Their source remains recoverable from Git history; the final pre-cleanup tree is
commit `5232791d68069c7137e613a8d83973711753bd45`.

For future incidents, start with the read-only audit tool and create a new,
guarded repair only after the exact failure mode and affected Recorder layer are
proven.

## `subscription_migration_plan.py`

Read-only phase of the October 2026 subscription repair. Run in the HA add-on terminal with access to /config and SUPERVISOR_TOKEN:

```bash
python3 tools/subscription_migration_plan.py
```

Compares the actual hourly Recorder cost and import changes against the legacy and new fee model, reads the runtime Store and retained alignment targets, and saves a private JSON plan. It never writes to Recorder or the integration Store. Unknown hours, gaps, changed tariff options, incomplete aggregation and mismatches must be reviewed before applying any repair. Do not run the old backfill tool to repair this incident. A plan is not an executable SQL migration; application requires inspection of actual results and a full backup.

## Subscription repair service (1.1.5)

After a full HA and Recorder database backup, install 1.1.5 and restart HA.
Call `enea_rcem.repair_subscription_2026` in Developer Tools > Actions with
`apply: false` (default), then review the returned report. If it is correct,
call with `apply: true`. Run between minute 15 and 45 of the hour, to allow
Recorder alignment to complete and avoid the hourly boundary. The service
checks every completed hour in the affected interval against both models and
refuses missing, ambiguous or unexpected values. It fixes retained targets
and uses Recorder's existing sum adjustment API, which affects both hourly
and short-term cumulative sums. Raw sensor state and the cumulative runtime
baseline intentionally stay unchanged, preventing a second adjustment when
Recorder sees the next sensor state. The Energy Dashboard uses corrected sums.
A persistent pending marker prevents blind retries after interruption; a completed
marker does not prevent a new read-only audit. No energy or export data is written.
