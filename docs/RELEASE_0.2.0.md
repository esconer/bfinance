# Release readiness: 0.1.3 to 0.2.0

Written 2026-09-29, before any push. Everything below was executed, not inferred.

## The decision

**0.1.3 must become 0.2.0.** The evidence is not a matter of taste:

| | released 0.1.3 | HEAD | delta |
|---|---|---|---|
| `.py` files under `src/bfinance` | 33 | 63 | +30 |
| public names (functions, classes, no leading underscore) | 48 | 340 | **+294** |
| public names **removed** | | | **-2** |

A removal of public API is a breaking change by any reading of semver, and this one is the
sharpest kind available: `bfinance.sector` no longer exists as a module.

```
# released 0.1.3
$ python -c "from bfinance.sector import Sector"     # works

# HEAD
$ python -c "from bfinance.sector import Sector"
ModuleNotFoundError: No module named 'bfinance.sector'
```

There is no deprecation shim and no intermediate warning. A user on 0.1.3 who did
`from bfinance.sector import Sector` gets an `ImportError` at first use and no earlier
signal that anything moved. That is the single most important line in this document.

The removal is **deliberate, not accidental**: `tests/test_exhaustive_screener_and_yfinance.py:421`
asserts `not hasattr(bfinance, "Sector"), "bfinance.Sector must stay deleted"`, and line 428
asserts the module itself cannot be imported. The behaviour is intentional and guarded. That
makes it a *chosen* break, which raises the documentation obligation rather than lowering it.

## Breaking changes a user must be told about

### 1. `bfinance.sector` removed; `Sector` and `Industry` gone

Replaced by `TrendlyneGroup` in `bfinance.trendlyne.sectors`, which carries the sector and
industry taxonomy together with the source that published it, rather than as two bare labels.
Migration is a rename plus reading the new fields. See `docs/YFINANCE_MIGRATION_GUIDE.md` for
the wider shape change.

### 2. `history(period=...)` that cannot be served now raises instead of lying

Released 0.1.3 returned **1,823 days** for `period="1M"` without saying so. That is the
defect this release exists to remove: a caller asking for a month and receiving six years of
bars, silently, with every downstream consumer compounding the error. HEAD raises. Code that
relied on the over-wide return will now fail loudly, which is the intended trade.

### 3. Historical prices change for anyone who cached them

The demerger correction changes split-adjusted history. A user who stored 0.1.3 output and
compares it against 0.2.0 output will see different numbers for the same symbol and date, both
correct on their own terms. This is a data correction, not a regression, and it cannot be
papered over: the old numbers were wrong.

### 4. Nine adapter entry points return a DataFrame, not a coroutine

`screener_chart_history` and eight siblings in `bfinance.sources.bindings` previously handed
sync callers an unresolved coroutine, which failed at first column access rather than at the
call. They now return a `DataFrame`. Any caller that had wrapped the old return in
`asyncio.run` or awaited it will now break, correctly.

### 5. New `df.attrs` on every frame

`bfinance_bar_interval` and `bfinance_bar_interval_mismatch` are published on every frame.
`history(period="2y")` and `period="5y"` deliver **weekly** bars and did not say so; now they
do. This is additive, but a caller doing strict attribute comparison will see new keys.

### 6. The statement accessors change shape (landed)

`get_*_stmt` returned the package's native shape. It now returns the **yfinance** shape
instead: one row per statement, newest first, de-duplicated labels, a plain `DatetimeIndex`
with no date-period column, absolute rupee values rather than crore, and generic row labels.

`FinancialStatement.to_dataframe()` -- the native Indian-label Rs-Cr frame -- is unchanged
and is still what finengine reads.

This landed with the seven ``xfail(strict=True)`` markers that guarded it now converted to
real passes. `tests/test_stmt_accessor_contract.py` had carried **7 `xfail(strict=True)`
markers**, one per property, and the file documented exactly what they were for:

> Each of these fails against 0.1.3. `strict=True` means that when issue 04 lands and they
> start passing, pytest reports XPASS as a *failure*, which is the signal to delete the
> marker. That turns the 0.2.0 break into something that cannot land silently.

There was also a self-check, `_EXPECTED_XFAIL_COUNT = 7`, whose comment read:

> Lowering it to make a run green defeats the only tripwire between 0.1.3 and the 0.2.0
> statement-order break.

Both were replaced rather than deleted, because a strict xfail is *self-arming* and a plain
assertion is not. In their place the file now carries
`test_the_seven_named_tests_are_still_present_in_this_file` (an `ast` check that all seven
named property tests still exist) and `test_the_seven_yfinance_properties_all_hold` (all
seven properties re-asserted together, against a real `Ticker`, independently of the tests
above). Deleting any one of the seven is now a failure rather than a quiet loss.

Two decisions inside the converter, both recorded rather than inferred:

- **`TTM` is still dropped, deliberately.** A trailing-twelve-months total is not a fiscal
  period and has no month-end date, so it cannot occupy a `DatetimeIndex` without a date
  being invented for it. The drop is now made *by name* and published on the frame as
  `attrs["bfinance_dropped_period_columns"]`. The values are not lost: they stay on the
  native shape.
- **Only the money rows are scaled, and the per-share exemption was the first half of it.**
  yfinance's `Diluted EPS` is rupees per share, so the `1e7` scale turned screener's reported
  `16.51` into `165,100,000` -- a per-share number in absolute-rupee units. `models/statements.py`
  carries `PER_SHARE_LABELS` for that.
  ~~Known adjacent defect, **not** fixed here: the percentage rows (`OPM %`, `Tax Rate`,
  `Dividend Payout %`) are still scaled the same way.~~ **That is now fixed too, and the
  note above was false as soon as the accessor was wired.** The percentage rows were scaled
  alongside the money ones, so a recorded `OPM %` of 35.0 came out of `get_income_stmt()` as
  `350000000.0` -- fabricated magnitude on the primary return path, which the 0.1.3 -> 0.2.0
  notes above describe as the point of the release. The scale is now decided per row from the
  label screener.in printed, and **before** the rename, because `Tax %` becomes `Tax Rate` and
  the output label no longer carries a `%`: a trailing `%` is a percentage, a per-share row is
  already rupees, and a ratio / days / count row is dimensionless. `RATIO_LABELS` names the
  latter, every entry a row the committed recordings actually emit. Pinned by
  `tests/test_statement_row_units.py`, which asserts the corrected value for every such row in
  both recordings *and* the money rows in the same frame as the control, so "delete the
  scaling" cannot satisfy it.

## Data honesty changes worth stating plainly

- **The screener.in source fabricates Open, High and Low.** Measured on 1,572 of 1,572 rows:
  `Open[t] == Close[t-1]`, `High == max(O, C) * 1.002`, `Low == min(O, C) * 0.998`. Only
  `Close` is real, and `Volume` is real but up to 28.6% off. This is now surfaced through
  the capability model rather than presented as OHLC.
- **A new bhavcopy mirror** (`bfinance.mirror`) is available and opt-in. It is lossless: 58,050
  cells compared against the live tape across four real trading days, zero mismatches. It is
  not yet wired into `history()` as a default source, and integration is tracked separately.
- **Trendlyne returns bars dated before a security's own listing.** Measured on SBIN: 2,222
  rows totalling 45,561,075,413 volume before its 2015-01-15 listing. The package previously
  asserted the opposite in a caller-facing message. Correcting that is part of this release.

## Release checklist

| gate | state |
|---|---|
| offline suite green | 2126 tests, 0 failures, 0 errors, 8 xfailed |
| `ruff check --select E9,F` on every file this release touched | clean |
| version bumped in `pyproject.toml` **and** `__init__.py` | done -- `0.2.0` in both, asserted to agree |
| breaking changes documented | this file |
| `bfinance.sector` removal documented | this file, section 1 |
| push authorised by the maintainer | **no -- do not push** |

## Do not push

The remote has PyPI auto-build wired to commits. 28 commits are ahead of `origin/main` and
**none has been pushed**. This document exists so the version number is chosen deliberately
before that happens, not discovered afterwards.
