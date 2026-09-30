# `yfinance` to `bfinance` Migration Guide

This guide is for developers migrating existing Python codebases from `yfinance` (versions 0.2.x up to 1.7.0+) to `bfinance` for Indian equities (NSE & BSE).

Scope: written against `bfinance` 0.2.0. The 0.1.3 -> 0.2.0 release is a **breaking** change and
the two that will hit a migrating user hardest are in section 3 (a `history(period=...)` that
cannot be served now raises instead of returning six years of bars) and section 3.D (the sector
and industry names were removed, not renamed). `docs/RELEASE_0.2.0.md` is the full list.

---

## 1. Zero-Code-Change Drop-in Migration

`bfinance` is designed with strict **1:1 API signature parity** with `yfinance 1.7.0+`. In 99% of cases, migration requires changing only your import statement:

### Before (`yfinance`):
```python
import yfinance as yf

ticker = yf.Ticker("RELIANCE.NS")
print(ticker.fast_info.last_price)
hist = ticker.history(period="1mo")
financials = ticker.financials
```

### After (`bfinance`):
```python
import bfinance as yf  # Alias as yf or bf

# Supports plain symbol, .NS, or .BO
ticker = yf.Ticker("RELIANCE")
print(ticker.fast_info.last_price)
hist = ticker.history(period="1mo")
financials = ticker.financials  # Now returns 10+ years instead of 4 years!
```

---

## 2. Direct API Compatibility Mapping Table

| `yfinance` Method / Property | `bfinance` Equivalent | Notes & Behavioral Enhancements |
| :--- | :--- | :--- |
| `yf.Ticker(symbol)` | `bf.Ticker(symbol)` | Supports `'RELIANCE'`, `'RELIANCE.NS'`, `'500325.BO'` |
| `ticker.fast_info` | `ticker.fast_info` | Scalar properties (`last_price`, `market_cap`, `year_high`, `year_low`, `shares`, `currency='INR'`, `exchange='NSE'`) |
| `ticker.info` | `ticker.info` | 180+ key dictionary matching standard Yahoo schema + Indian extras (`marketCapInCr`, `roce`) |
| `ticker.history(...)` | `ticker.history(...)` | Identical DataFrame schema (`Open`, `High`, `Low`, `Close`, `Adj Close`, `Volume`, `Dividends`, `Stock Splits`) |
| `ticker.get_history_metadata()` | `ticker.get_history_metadata()` | `yfinance 1.7.0` metadata dictionary |
| `ticker.financials` / `income_stmt` | `ticker.financials` / `income_stmt` | **Upgraded**: 10–13 years Ind AS statements instead of Yahoo's 4-year limit |
| `ticker.quarterly_income_stmt` | `ticker.quarterly_income_stmt` | **Upgraded**: 12–16 historical quarters instead of 4 quarters |
| `ticker.balance_sheet` | `ticker.balance_sheet` | **Upgraded**: 10–13 years audited annual balance sheet |
| `ticker.quarterly_balance_sheet` | raises `NotImplementedError` | No quarterly-BS source upstream; annual only (yfinance has quarterly — use it if you need it) |
| `ticker.cash_flow` / `cashflow` | `ticker.cash_flow` / `cashflow` | **Upgraded**: 10–13 years cash flow statements (CFO, CFI, CFF) |
| `ticker.quarterly_cash_flow` | raises `NotImplementedError` | No quarterly-CF source upstream; annual only |
| `ticker.dividends` | `ticker.dividends` | Per-share amounts match yfinance (≤3%); dates are FY-end (Mar-31), yfinance uses ex-dates |
| `ticker.splits` | `ticker.splits` | Same ratios; bonus issues represented as split factors on FY-end dates |
| `ticker.actions` | `ticker.actions` | Dividends + splits merged frame, yfinance-shaped columns |
| `ticker.capital_gains` | `ticker.capital_gains` | Empty like yfinance for Indian equities |
| `ticker.major_holders` | `ticker.major_holders` | Numeric `Value` column (fraction), yfinance-shaped |
| `ticker.options` | `ticker.options` | `()` for NSE underlyings — identical to live yfinance 1.7.0 (verified 2026-09-04) |
| `ticker.option_chain(date)` | `ticker.option_chain(date)` | Empty yfinance-shaped chain for NSE (no options feed either side) |
| `ticker.calendar` | `ticker.calendar` | Earnings/dividend dates where disclosed; no analyst estimates |
| `ticker.analyst_price_targets` | `None` | No analyst feed upstream (was fabricated pre-0.1.3; now honest) |
| `ticker.valuation_measures` | `ticker.valuation_measures` | `yfinance 1.3.0+` valuation table |
| `yf.Tickers(list)` | `bf.Tickers(list)` | Multi-ticker container matching `yf.Tickers` |
| `yf.Sector(name)` / `yf.Industry(name)` | **no equivalent** | **removed in 0.2.0** -- not renamed. Read section 3.D before writing any replacement |
| `yf.download(...)` | `bf.download(...)` | Concurrent batch downloader with `group_by='column'` and `group_by='ticker'` |

---

## 3. Key Differences & Improvements

### A. Statement History: 10–13 Years vs 4 Years
Yahoo Finance only provides 4–5 years of annual statements for Indian stocks. `bfinance` pulls 10 to 13+ years of audited Ind AS statements:

```python
# In yfinance: shape is (25, 4)
# In bfinance: shape is (12, 13)
print(ticker.financials.shape)
```

### B. Indian Ticker Formats
`yfinance` requires appending `.NS` or `.BO` to every symbol (e.g. `RELIANCE.NS`). `bfinance` understands raw symbols, BSE scrip codes, and suffixes:
* `'RELIANCE'` $\rightarrow$ Defaults to NSE
* `'RELIANCE.NS'` $\rightarrow$ NSE
* `'500325.BO'` or `'500325'` $\rightarrow$ BSE

### C. Added Superpowers (Not Available in `yfinance`)
Migrating to `bfinance` unlocks features that `yfinance` does not possess:
* `ticker.concalls`: 40+ earnings calls with direct **audio MP3 streams** and PDF transcripts.
* `ticker.shareholding`: 12Q quarterly & 11Y annual institutional holding trends (FIIs, DIIs, Promoters).
* `ticker.piotroski_score` & `ticker.graham_number`: Built-in quantitative models.
* `ticker.to_excel("model.xlsx")`: 1-line multi-tab financial model export.
* `ticker.to_ai_context()`: Native LLM data feed engine.

### D. Sector and Industry: removed in 0.2.0, and not renamed

`yfinance 1.4.0+` exposes `yf.Sector(name)` and `yf.Industry(name)`, which take a sector or
industry **name** and hand back its constituent tickers.

**`bfinance` has no equivalent, and the two names it shipped in 0.1.3 were a fabrication.**
They are gone. Verified in this tree:

```
hasattr(bfinance, "Sector")               -> False
hasattr(bfinance, "Industry")             -> False
from bfinance.sector import Sector        -> ModuleNotFoundError
```

`src/bfinance/sector.py` is deleted. It was a **hardcoded 4-sector / 20-company table with
hardcoded prices and P/Es and no HTTP call anywhere in the file**, so every number it
returned was invented. The deletion is deliberate and test-guarded:
`tests/test_exhaustive_screener_and_yfinance.py` asserts `not hasattr(bfinance, "Sector")`
with the message "bfinance.Sector must stay deleted", and separately asserts the module itself
cannot be imported. `docs/RELEASE_0.2.0.md` section 1 is the release note, and it is where the
wider 0.2.0 shape change is written up.

**What a migrating user hits:** an `AttributeError` on the first call, with no deprecation
shim and no intermediate warning. The `AttributeError` is the intended signal, not a bug to
report.

#### The replacement is NOT a rename, and that distinction is the whole trap

`bfinance.trendlyne.sectors.TrendlyneGroup` is the real destination, and it does not take a
name. Verified signature:

```
TrendlyneGroup(kind: str, group_id: int, slug: str, name: str)
```

`__post_init__` rejects any `kind` other than `"sector"` or `"industry"`, and any
`group_id <= 0`. `group_id` is the integer path segment on trendlyne: sector 20 is
`/equity/sector/20/automobiles-auto-components/`. **No id in that module is hardcoded and
none is guessed** -- every id, slug and display name is read out of a page, and the module
docstring says so.

**So there is no constructor path from a bare name, and a migration written as if there were
one will not work.** You resolve a name to a `TrendlyneGroup` first, and that resolution
costs a request.

#### What to write instead

**Starting from a symbol** (the common case) needs no extra request. The stock page this
package already fetches for the OHLC token carries the sector and industry as schema.org
microdata, so classification is free:

```python
from bfinance.trendlyne import TrendlyneClient

client = TrendlyneClient()
groups = client.classification("RELIANCE")   # {"sector": TrendlyneGroup, "industry": TrendlyneGroup}
autos = groups["sector"]
members = client.constituents(autos)        # DataFrame, one row per member
```

The same two values are on every OHLC frame without a second call:

```python
from bfinance.trendlyne import ohlc

frame = ohlc("RELIANCE")
frame.attrs["bfinance_trendlyne_sector"]     # {"id", "slug", "name"} mapping, or None
frame.attrs["bfinance_trendlyne_industry"]   # {"id", "slug", "name"} mapping, or None
```

**Starting from a bare name** ("automobiles") needs a bootstrap group before anything else,
because `taxonomy` reads the vocabulary off a group page:

```python
vocab = client.taxonomy(autos)   # {"sector:20": TrendlyneGroup, "industry:9": TrendlyneGroup, ...}
```

That is the whole shape of the problem: the vocabulary is one request, but it is keyed off
a group you must already hold. Resolve the first group by symbol (`classification`) or from
an id you have read off a page yourself, then the rest of the vocabulary costs nothing more.

Key the mapping on `TrendlyneGroup.key` (`"sector:20"`), not on `name`. trendlyne itself is
inconsistent about case -- the same sector renders `AUTOMOBILES & AUTO COMPONENTS` on the
stock-page breadcrumb and `Automobiles & Auto Components` in the taxonomy select, for
sector id 20 -- and both spellings are preserved verbatim rather than normalised, because
folding them would invent a spelling the site does not use.

Four behaviours to plan for, all verified:

* `classification()` is the **strict** door: a page with no `SECTOR :` / `INDUSTRY :`
  breadcrumb raises `TrendlyneSchemaError` rather than handing you `{}` and leaving you to
  guess whether the stock is unclassified or the markup moved. It costs nothing beyond the
  token fetch, whose page it shares. The two OHLC `attrs` are the lenient version and do read
  `None`, because there the price series is the product and the classification is a bonus.
* `constituents(group)` takes either kind. `industry_aggregate(sector)` takes a **sector
  only** and raises `ValueError` for an industry; it is the per-industry block beside the
  constituent list, and its per-industry company counts must sum to the list's row count,
  which is a server-side cross-check for a truncated list.
* The market-cap **column unit is not asserted** -- no unit label appears anywhere on the
  page. `attrs["bfinance_trendlyne_market_cap_unit"] == "unverified"`. Use the `Weight %`
  column, which is exact and unit-free (market cap over the sector's own total).
* `bfinance.trendlyne` is an **opt-in, isolated third-party package**. Nothing else in
  `bfinance` imports it, `import bfinance` does not pull it in, and it must be imported
  explicitly. It is not an exchange source and it is not in the price cascade. It also sits
  on trendlyne.com, not on an exchange, so "sector" means trendlyne's sector taxonomy,
  which is not Yahoo's and need not agree with it.

---

## 4. Handling `yfinance 1.7.0` Breaking Changes

If your code relies on `yfinance 1.7.0` features like `history_metadata` or `valuation_measures`, `bfinance` implements them natively:

```python
import bfinance as yf

ticker = yf.Ticker("TCS")

# 1. yfinance 1.7.0 history metadata
meta = ticker.get_history_metadata()
print("Trading Timezone:", meta["timezone"])
print("Valid Ranges:", meta["validRanges"])

# 2. yfinance valuation measures table
print(ticker.valuation_measures)
```

---

## 5. Verified Parity Status (v0.1.3, yfinance 1.7.0 baseline, live-checked 2026-09-04)

`tests/test_dropin_*.py` + `tests/test_parity_*.py` run the same calls against both
libraries (`pytest -m live`). Measured on RELIANCE.NS:

* history Close within 0.6% (1mo/1y), tz `Asia/Kolkata`, weekly intervals resampled,
  `auto_adjust`/`actions` flags honored like yfinance 1.7 defaults
* info values within 2.5% (price, mcap, EPS, float, 50d/200d averages, prev close exact)
* statements self-consistent (Assets ≈ Liab+Equity ≤5%); the accessors return the yfinance
  shape (metric-row, generic labels, newest-first `DatetimeIndex`, absolute ₹ rather than
  ₹Cr), and `to_dataframe()` remains the native metric-row/₹Cr frame
* dividends amounts within 3% (FY-end vs ex-date conventions differ — see table)

**The crore-to-rupee conversion applies to the money rows only.** screener.in states a row's
unit in its label, so a row is converted only if it is actually an amount in crore: a
trailing `%` (`OPM %`, `Tax Rate`, `Dividend Payout %`, `ROCE %`) is a percentage, a per-share
row is already rupees (`Diluted EPS`), and a ratio / days / count row (`CFO/OP`,
`Debtor Days`, `No. of Shareholders`) is dimensionless. All three pass through **unscaled**,
so `income_stmt.loc["OPM %"]` is `35.0` and not `350000000.0`. This was a real defect on the
accessor's primary return path and is pinned by `tests/test_statement_row_units.py`, which
also asserts the money rows in the same frame are still converted. Two consequences for a
migrating user: **there is no `TTM` column** (a trailing-twelve-months total has no month-end
date, so it is dropped by name and the decision is published in
`attrs["bfinance_dropped_period_columns"]`), and the native crore values are one call away at
`FinancialStatement.to_dataframe()`.

Deliberate extras (never silent): `freq="quarterly"` on income uses real quarterly
P&L; the native ₹Cr statement is reached through `FinancialStatement.to_dataframe()`,
not through the accessors; `Ticker("X", True)` bool
second positional is treated as yfinance's `multi_level_index`.
