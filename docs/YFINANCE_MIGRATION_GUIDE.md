# `yfinance` to `bfinance` Migration Guide

This guide is for developers migrating existing Python codebases from `yfinance` (versions 0.2.x up to 1.7.0+) to `bfinance` for Indian equities (NSE & BSE).

Scope: written against `bfinance` 0.2.0. The 0.1.3 -> 0.2.0 release is a **breaking** change and
the two that will hit a migrating user hardest are in section 3 (a `history(period=...)` that
cannot be served now raises instead of returning six years of bars) and section 3.D (the sector
and industry names were removed, not renamed). `docs/RELEASE_0.2.0.md` is the full list.

---

## 1. Migrating an existing codebase

`bfinance` implements a **documented subset** of the `yfinance` 1.7.0 `Ticker` API, measured on
2026-10-23: **44 of 98 public `Ticker` members present with a data path, 14 present-but-empty, 40
absent by name**. Most call sites move by changing the import; this is **not** a 100% drop-in.
The measured table is `docs/YFINANCE_PARITY.md`.

Three breaks to check before you start:

1. **`Ticker("X", True)` raises.** The second positional is `session`, so a stray bool is a
   `NotImplementedError` before any request. Write `Ticker("X", multi_level_index=True)`.
2. **`Ticker(..., session=...)` raises.** The transport is `httpx`; a `requests`/`httpx` session
   cannot be honoured. Omit the argument.
3. **Every Trendlyne-backed yfinance-named member requires `trendlyne_sections=True`.** With the
   flag off, `recommendations`, `recommendations_summary`, `earnings_estimate`, `revenue_estimate`,
   `earnings_history`, `growth_estimates` and `insider_transactions` raise `NotImplementedError`
   naming the flag.

The calls in the example below are unchanged:

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
| `yf.Ticker(symbol)` | `bf.Ticker(symbol)` | Supports `'RELIANCE'`, `'RELIANCE.NS'`, `'500325.BO'`. **Second positional is `session`, not `multi_level_index`**: `Ticker("X", True)` and `Ticker(..., session=...)` both raise `NotImplementedError` naming `httpx`. Use `Ticker("X", multi_level_index=True)` |
| `ticker.fast_info` | `ticker.fast_info` | Scalar properties (`last_price`, `market_cap`, `year_high`, `year_low`, `shares`, `currency='INR'`, `exchange='NSE'`); **also a 21-key mapping**, so `dict(t.fast_info)`, `in`, `len()` work |
| `ticker.info` | `ticker.info` | **76–90 keys depending on the profile** (measured across five profiles; `INFO_VALUE_UNITS` declares 80). Four declared keys are omitted, never `None`, when unsourced: `regularMarketChange`, `lastFiscalYearEnd`, `nextFiscalYearEnd`, `mostRecentQuarter`. Read `bfinance.market.quotes.INFO_VALUE_UNITS` for the unit of every key; the return/yield keys are deliberately mixed-unit |
| `ticker.history(...)` | `ticker.history(...)` | `auto_adjust` defaults to `True` (yfinance 1.7), so the default columns are `Open`, `High`, `Low`, `Close`, `Volume` (+ `Dividends`, `Stock Splits`). Pass `auto_adjust=False` for the `Adj Close` column |
| `ticker.get_history_metadata()` | `ticker.get_history_metadata()` | `yfinance 1.7.0` metadata dictionary |
| `ticker.financials` / `income_stmt` | `ticker.financials` / `income_stmt` | **Upgraded**: 10–13 years Ind AS statements instead of Yahoo's 4-year limit |
| `ticker.quarterly_income_stmt` | `ticker.quarterly_income_stmt` | **Upgraded**: 12–16 historical quarters instead of 4 quarters |
| `ticker.balance_sheet` | `ticker.balance_sheet` | **Upgraded**: 10–13 years audited annual balance sheet |
| `ticker.quarterly_balance_sheet` | raises `NotImplementedError` | No quarterly-BS source upstream; annual only (yfinance has quarterly — use it if you need it) |
| `ticker.cash_flow` / `cashflow` | `ticker.cash_flow` / `cashflow` | **Upgraded**: 10–13 years cash flow statements (CFO, CFI, CFF) |
| `ticker.quarterly_cash_flow` | raises `NotImplementedError` | No quarterly-CF source upstream; annual only |
| `ticker.dividends` | `ticker.dividends` | Per-share amounts match yfinance (≤3%); dates are FY-end (Mar-31), yfinance uses ex-dates |
| `ticker.splits` | `ticker.splits` | **Real ex-dated Series since 2026-10-23**, from NSE's corporate-actions filings; the factor is the filing's own `SplitRatio`, selected on that ratio rather than the `Action` kind. An empty series can also mean the feed failed, not "no split": `attrs["bfinance_actions_source"] == "none"` is the failure shape, `"nse_corporate_actions"` the answered-empty one |
| `ticker.actions` | `ticker.actions` | `Dividends` + `Stock Splits` from the **same** NSE feed, one row per declared ex-date. `actions["Dividends"]` is **not** `ticker.dividends` (Screener FY-end basis, different source). Zero rows can also mean a failed feed: `bfinance_actions_source == "none"` (no `bfinance_empty_feed` key — `True` would assert NSE answered with nothing, which was not observed) |
| `ticker.isin` | `ticker.isin` | `str` or `None`; first from the NSE corporate-actions payload (refused if rows disagree), then from **archived** index memberships. `None` means "not found on this machine", not "no ISIN exists" |
| `ticker.capital_gains` | `ticker.capital_gains` | Empty like yfinance for Indian equities |
| `ticker.major_holders` | `ticker.major_holders` | Numeric `Value` column (fraction), yfinance-shaped |
| `ticker.options` | `ticker.options` | `()` for NSE underlyings — identical to live yfinance 1.7.0 (verified 2026-09-04) |
| `ticker.option_chain(date)` | `ticker.option_chain(date)` | Empty yfinance-shaped chain for NSE (no options feed either side) |
| `ticker.calendar` | `ticker.calendar` | Earnings/dividend dates where disclosed; no analyst estimates |
| `ticker.analyst_price_targets` | `None` by default | **Only true with the opt-in off.** `Ticker(...)` default returns `None`, because the package serves no estimates feed on the default path and the old synthesised implementation (`low = cmp x 0.85`, `high = cmp x 1.35`, `mean = cmp x 1.15`) was deleted in 0.1.3 for producing confident numbers nobody published. **Construct with `Ticker(..., trendlyne_sections=True)` and it is a real derivation**: `low`/`high`/`mean`/`median` from trendlyne's research-reports `Target` column, plus `n_reports` and `bfinance_analyst_source="trendlyne_research_reports"`. Two things it deliberately does **not** give you: `current` is always `None` (the page's `LTP` is the price at each report's publication, not today's), and the vendor's own aggregate *"Consensus Share Price Target"* row is excluded from the statistics. See section 3.E |
| `ticker.valuation_measures` | `ticker.valuation_measures` | `yfinance 1.3.0+` valuation table; `freq`/`periods` accepted and **cannot narrow** (one current snapshot) |
| `ticker.recommendations` | same name | **Requires `trendlyne_sections=True`**; 6×6 measured. `period` is the vendor's `asofdate`, not yfinance's four relative `0m`/`-1m`/`-2m`/`-3m` rows |
| `ticker.recommendations_summary` | same name | Requires the flag; last four rows of the **same** frame, no second fetch |
| `ticker.earnings_estimate` | same name | Requires the flag; index `0q`/`+1q`/`0y`/`+1y` derived from `is_current`/`is_past`. `yearAgoEps` is **`NaN`** where the prior-year row is absent, named in `attrs["bfinance_estimate_note"]` |
| `ticker.revenue_estimate` | same name | Requires the flag; same index/shape from `REVENUE` rows; `yearAgoRevenue` same `NaN` + note contract |
| `ticker.earnings_history` | same name | Requires the flag; index is **fiscal period end** (`attrs["bfinance_date_basis"]`), not the announcement date |
| `ticker.growth_estimates` | same name | Requires the flag; `stock` column only — `industry`/`sector`/`index` absent, not zero |
| `ticker.insider_transactions` | same name | Requires the flag; **500-row server cap**; vendor columns verbatim |
| `ticker.get_*` aliases | same names | 17 exact forwarders: 13 `get_*` accessors plus `incomestmt`, `balancesheet`, `quarterly_incomestmt`, `quarterly_balancesheet`. `period=`/`freq=`/`periods=` are accepted and cannot narrow; `as_dict=` is honoured on the three holder accessors |
| `yf.Search(query)` | `bf.Search(query)` | NSE MarketLens `/api/search`; 7 payload keys passed through verbatim (`id`, `type`, `name`, `ticker`, `description`, `matchScore`, `marketCap`); equities-only; a failure raises |
| `yf.Tickers(list)` | `bf.Tickers(list)` | Multi-ticker container matching `yf.Tickers` |
| `yf.Sector(name)` / `yf.Industry(name)` | **no equivalent** | **removed in 0.2.0** -- not renamed. Read section 3.D before writing any replacement |
| `yf.download(...)` | `bf.download(...)` | Concurrent batch downloader with `group_by='column'` and `group_by='ticker'`. `timeout` reaches the wire; a non-`None` `session` raises `NotImplementedError` naming `httpx`; `ignore_tz=False` raises (only `True`/`None` can produce a frame); `progress=True` emits one `INFO` line per finished symbol |

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

### E. Analyst price targets: `None` by default, a real derivation behind one flag

The table in section 2 says `ticker.analyst_price_targets` → `None`. **That is true of the
default `Ticker` and false of the opt-in**, and the distinction matters because the default
value and "no estimates feed" are not the same statement.

```python
import bfinance as bf

bf.Ticker("TCS").analyst_price_targets
# None   <- default. Nothing is fetched; this is a constant, not a failed lookup.

bf.Ticker("TCS", trendlyne_sections=True).analyst_price_targets
# {'current': None, 'low': ..., 'high': ..., 'mean': ..., 'median': ...,
#  'n_reports': 20, 'bfinance_analyst_source': 'trendlyne_research_reports'}
```

`trendlyne_sections=True` is **off by default and additive**: no existing property changes
behaviour, and a default `Ticker(...)` opens no trendlyne socket. The seven members it adds
are `trendlyne_news`, `trendlyne_reports`, `trendlyne_shareholding`, `trendlyne_deals`,
`trendlyne_buy_sell_zone`, `trendlyne_technicals` and `trendlyne_swot`;
`docs/API_REFERENCE.md` section 2.3.2 documents each one.

Three things about the derived numbers that are **not** yfinance's shape, and are stated here
because they change what the value means:

* **`current` is always `None`.** trendlyne's research-reports table publishes an `LTP`
  column, but that is the price at the moment each report was published. Publishing it under
  a key a caller reads as *today's* price would be exactly the relabelling defect that got
  the pre-0.1.3 synthesiser deleted. **No current price is derivable from this feed.** If you
  need one, read `ticker.fast_info.last_price` and attach it yourself.
* **The vendor's aggregate row is excluded.** The first row of the table is trendlyne's own
  *"Consensus Share Price Target"*, which is an average of the rows beneath it. Averaging the
  `Target` column without excluding it folds the consensus into every statistic. The
  exclusion is on a word match against the `Author` column, not on row position, and the
  count of what remained ships as `n_reports` — a mean of two and a mean of twenty are the
  same number over a very different amount of evidence.
* **`trendlyne_reports()` keeps that aggregate row**, because removing it would be this
  package deciding what you wanted, and it is the row that carries the consensus. If you
  average that frame yourself, exclude it.

The value is `None` — never a synthesised number — when the flag is off, when the reports page
carries no usable `Target`, or when the fetch failed. In the last case the reason is on
`ticker.data_warnings` and the fetch is not retried.

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
  `auto_adjust`/`actions` flags honored like yfinance 1.7 defaults (`auto_adjust=True` was
  adopted on 2026-09-30; it was `False` and column-incompatible with yfinance before that)
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
not through the accessors. The old shim that read `Ticker("X", True)` as
`multi_level_index=True` is **gone**: the second positional is now `session`, so a bool there raises
`NotImplementedError`. Write `Ticker("X", multi_level_index=True)`.
