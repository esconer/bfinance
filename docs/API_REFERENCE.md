# `bfinance` Complete API Reference Manual

This document provides a comprehensive, exhaustive reference for every class, method, property, and helper utility available in `bfinance`.

---

## Table of Contents

1. [Top-Level Package Functions](#1-top-level-package-functions)
2. [`bfinance.Ticker` Class Reference](#2-bfinanceticker-class-reference)
   - [Constructor](#21-constructor)
   - [Market Data & yfinance Core Properties](#22-market-data--yfinance-core-properties)
   - [10-Year Financial Statements](#23-10-year-financial-statements)
   - [Indian Corporate Superpowers](#24-indian-corporate-superpowers)
   - [Custom Ratios & Quantitative Scoring](#25-custom-ratios--quantitative-scoring)
   - [Historical Valuation Timeseries](#26-historical-valuation-timeseries)
   - [Document & Excel Exporters](#27-document--excel-exporters)
   - [AI & LLM Context Engine](#28-ai--llm-context-engine)
3. [`bfinance.Tickers` Multi-Ticker Collection](#3-bfinancetickers-multi-ticker-collection)
4. [`bfinance.Sector` & `bfinance.Industry` (removed in 0.2.0)](#4-bfinancesector--bfinanceindustry-removed-in-020)
5. [`bfinance.screens` Institutional Screener Registry](#5-bfinancescreens-institutional-screener-registry)
6. [`bfinance.ratios` Ratio Search Client](#6-bfinanceratios-ratio-search-client)
7. [`bfinance.ai` Module & Agent Tools](#7-bfinanceai-module--agent-tools)
8. [Data Models & Schema Reference](#8-data-models--schema-reference)
9. [Exceptions Hierarchy](#9-exceptions-hierarchy)

---

## 1. Top-Level Package Functions

### `bfinance.download(tickers, ...)`
Concurrent batch historical OHLCV data downloader matching `yfinance.download()`.

```python
bfinance.download(
    tickers: Union[str, List[str]],
    start: Optional[Union[str, datetime]] = None,
    end: Optional[Union[str, datetime]] = None,
    period: str = "1mo",
    interval: str = "1d",
    group_by: Literal["column", "ticker"] = "column",
    auto_adjust: bool = True,
    actions: bool = False,
    threads: bool = True,
    proxy: Optional[str] = None,
    timeout: int = 10,
) -> pd.DataFrame
```

The listing above is the commonly-used subset, not the full signature; `inspect.signature`
is authoritative for the rest.

#### Parameters:
* **`tickers`** (*str | List[str]*): Single symbol (e.g. `'RELIANCE'`), space-delimited string (e.g. `'RELIANCE TCS INFY'`), or list of symbols.
* **`period`** (*str*): Lookback period: `'1d'`, `'5d'`, `'1mo'`, `'3mo'`, `'6mo'`, `'1y'`, `'2y'`, `'5y'`, `'10y'`, `'ytd'`, `'max'`. Default is `'1mo'`.
* **`interval`** (*str*): Data granularity. Default is `'1d'`.
* **`group_by`** (*'column' | 'ticker'*):
  - `'column'` (default): MultiIndex columns where level 0 is Price type (`'Close'`), level 1 is Ticker (`'RELIANCE'`).
  - `'ticker'`: MultiIndex columns where level 0 is Ticker (`'RELIANCE'`), level 1 is Price type (`'Close'`).
* **`actions`** (*bool*): If `True`, includes `'Dividends'` and `'Stock Splits'` columns. Default `False`.
* **`auto_adjust`** (*bool*): **Default `True`, matching `yfinance` 1.7**, and that default is
  pinned by `tests/test_adjust_default_drift.py` so it cannot drift away from the inner history
  path unnoticed. `download()`, `Tickers.history`, `Ticker.history` and `OHLCVEngine.fetch_history`
  all agree on it. `True` scales `Open/High/Low/Close` by `Adj Close / Close` and drops
  `Adj Close`; pass `False` for the strictly richer frame that also carries `Adj Close`. It is
  independent of `actions`: the corporate-actions feed is resolved either way because the factor
  comes from it, and `actions` alone decides whether the two action columns are published.
  `True` raises `AdjustmentNotAvailableError` where `Adj Close` is not a real adjusted series
  (a derived-O/H/L frame, an empty frame, no ex-dated actions feed, or the Screener.in
  fiscal-year-end approximation) -- measured live, `MAFANG` is in that state, because the NSE
  corporate-actions feed does not index it.
* **`threads`** (*bool*): ~~`Optional[int]`, default `None`.~~ **It is a `bool`, default `True`**,
  and it is read as a switch: `max_concurrency=10 if threads else 1`. It is **not** a thread count.
* **`timeout`** (*int*): ~~`15`.~~ **`10`**, matching `Ticker.history`.
* **`proxy`** (*str, optional*): HTTP/HTTPS/SOCKS5 proxy URL.

---

## 2. `bfinance.Ticker` Class Reference

The primary unified facade representing an equity asset listed on the National Stock Exchange (NSE) or Bombay Stock Exchange (BSE).

### 2.1 Constructor

```python
bf.Ticker(
    ticker: str,
    cache_ttl_hours: float = 24.0,
    timeout: float = 15.0,
    proxy: Optional[str] = None,
    raise_errors: bool = False,
    session: Optional[Any] = None,
)
```

#### Arguments:
* **`ticker`** (*str*): Ticker symbol with optional exchange suffix (e.g., `'RELIANCE'`, `'RELIANCE.NS'`, `'500325.BO'`).
* **`cache_ttl_hours`** (*float*): Local SQLite cache time-to-live in hours. Set to `0.0` to force live network requests. Default is `24.0`.
* **`timeout`** (*float*): Maximum network request timeout in seconds. Default is `15.0`.
* **`proxy`** (*str, optional*): Proxy connection URL.
* **`raise_errors`** (*bool*): If `False` (default), invalid tickers or missing data log warnings and return safe empty DataFrames/dictionaries. If `True`, raises `TickerNotFoundError` or `UpstreamServiceError`.

---

### 2.2 Market Data & yfinance Core Properties

#### `ticker.fast_info` -> `FastInfo`
Container for quick scalar attributes matching `yfinance.Ticker.fast_info`:
* `fast_info.last_price`: Current market price in ₹ (float).
* `fast_info.market_cap`: Market capitalization in absolute ₹ (float).
* `fast_info.year_high` / `fast_info.year_low`: 52-week high and low in ₹.
* `fast_info.shares`: Total shares outstanding.
* `fast_info.currency`: Always `'INR'`.
* `fast_info.timezone`: Always `'Asia/Kolkata'`.
* `fast_info.exchange`: `'NSE'` or `'BSE'`.

#### `ticker.info` -> `Dict[str, Any]`
~~Comprehensive 180+ key dictionary matching `yfinance.Ticker.info` schema.~~ **Corrected: it
is a 76-key dictionary, not 180+.** That count is measured, not estimated: the returned dict has
exactly the keys of `bfinance.market.quotes.INFO_VALUE_UNITS`, which is a 76-entry mapping and
is the authoritative list -- read the key set there rather than trusting a summary.

* Key fields include: `shortName`, `symbol`, `currentPrice`, `marketCap`, `marketCapInCr`,
  `trailingPE`, `bookValue`, `dividendYield`, `returnOnCapitalEmployed`, `returnOnEquity`,
  `debtToEquity`, `pegRatio`, `currency`, `sector`, `industry`.
* ~~`52WeekChange`~~ **is not a key and never was.** `info["52WeekChange"]` is a `KeyError`. The
  52-week fields are `fiftyTwoWeekHigh` and `fiftyTwoWeekLow`.
* **Units are not uniform.** Every key's unit is declared in `INFO_VALUE_UNITS`, and they are
  deliberately mixed: `returnOnEquity` and `returnOnAssets` are decimal fractions, while
  `returnOnCapitalEmployed` and `dividendYield` are raw percents. Check the map before you
  format one of them.

#### `ticker.history(...)` -> `pd.DataFrame`
Historical OHLCV data matching `yfinance.Ticker.history()`:
```python
ticker.history(
    period="1mo",
    interval="1d",
    start=None,
    end=None,
    actions=True,
    auto_adjust=True,
    back_adjust=False,
    rounding=False,
)
```
* **`auto_adjust`** defaults to **`True`**, matching `yfinance` 1.7. The default was `False`
  until 2026-09-30 and was changed because it made bfinance column-incompatible with the
  library it exists to stand in for: a bare `history()` returned
  `['Open','High','Low','Close','Adj Close','Volume',...]` where `yfinance` returns
  `['Open','High','Low','Close','Volume',...]`. `False` is still the way to ask for `Adj Close`.
  The default is pinned by `tests/test_adjust_default_drift.py`, which asserts the same value on
  `Ticker.history`, `Tickers.history`, `download()` and `OHLCVEngine.fetch_history` so the batch
  wrappers cannot drift away from the inner path.
* **Columns**: `['Open', 'High', 'Low', 'Close', 'Volume']` on the default `auto_adjust=True`
  path (+ `['Dividends', 'Stock Splits']` if `actions=True`); `['Open', 'High', 'Low', 'Close',
  'Adj Close', 'Volume']` with `auto_adjust=False`.
* **Index**: `pd.DatetimeIndex` (localized to Asia/Kolkata).

#### `ticker.history_metadata` / `ticker.get_history_metadata()` -> `Dict[str, Any]`
Metadata dictionary matching `yfinance 1.7.0+` including `timezone`, `currency`, `exchangeName`, `currentTradingPeriod`, and `validRanges`.

#### `ticker.valuation_measures` -> `pd.DataFrame`
Valuation summary table matching `yfinance 1.3.0+` with columns `['Metric', 'Value']`.

#### `ticker.dividends` -> `pd.Series`
Pandas Series of historical corporate dividend distributions indexed by Date.

#### `ticker.splits` -> `pd.Series`
Pandas Series of historical stock split ratios indexed by Date.

#### `ticker.options` -> `Tuple[str, ...]`
~~Tuple of available derivatives expiry dates (e.g. `('2026-09-24', '2026-10-29')`).~~
**This is `()` for every symbol, today.** Expiries are returned only for a symbol in a verified
F&O membership list, and `Ticker` supplies no membership list, so the empty tuple is what every
caller gets -- which is also what live `yfinance 1.7.0` returns for NSE underlyings. Branch on
`if not ticker.options:` rather than indexing it.

#### `ticker.option_chain(date: str)` -> `OptionChain`
~~Object containing `calls` and `puts` DataFrames with columns
`['contractSymbol', 'strike', 'lastPrice', 'openInterest', 'impliedVolatility']`.~~
**Always an empty chain.** `calls` and `puts` are both **zero-row `(0, 13)` frames** carrying
the full yfinance column set -- `bfinance.market.derivatives.YF_CHAIN_COLUMNS`:
`contractSymbol`, `strike`, `lastPrice`, `bid`, `ask`, `change`, `percentChange`, `volume`,
`openInterest`, `impliedVolatility`, `inTheMoney`, `contractSize`, `currency` -- so you can index
a column without a `KeyError`. Every cell is null and `len(chain.calls) == 0`. The fabricator
that used to synthesise strikes, Greeks, volume and open interest here has been **deleted**;
those were random numbers shaped like an exchange quote. **Do not read a quote out of this.**

---

### 2.3 10-Year Financial Statements

~~All financial statements return standard Pandas DataFrames where rows are financial line items
and columns are fiscal period end dates (e.g. `'Mar 2022'`, `'Mar 2023'`, `'Mar 2024'`,
`'Mar 2025'`, `'Mar 2026'`, `'TTM'`).~~ **Both halves of that are now false for the `Ticker`
accessors below.** They return `FinancialStatement.to_yfinance()` -- the yfinance shape:

* **Columns are a `pd.DatetimeIndex` of month-end timestamps** (`Timestamp('2026-03-31')`), **not**
  strings like `'Mar 2022'`. `df["Mar 2026"]` is a `KeyError`.
* **Columns are ordered newest-first**, as yfinance orders them, so `df.columns[0]` is the latest
  period rather than the oldest.
* **There is no `'TTM'` column.** A trailing-twelve-months total is not a fiscal period and has no
  month-end date, so it is dropped **by name**; the dropped headers are published on the frame as
  `df.attrs['bfinance_dropped_period_columns']`. The TTM values are not lost, just not here.
* **Values are absolute rupees, not Rs Crore -- but only on the monetary rows.** screener.in
  states a row's unit in its label, so the `1e7` crore-to-rupee conversion is applied only to rows
  that really are amounts. Percentages (`OPM %`, `Tax Rate`, `Dividend Payout %`, `ROCE %`),
  per-share quantities (`Diluted EPS`) and ratio / days / count rows (`CFO/OP`, `Debtor Days`,
  `No. of Shareholders`) pass through **unscaled**. So `income_stmt.loc["OPM %"]` is `35.0`, not
  `350000000.0`.
* **Row labels are yfinance's generic names**: `Sales` -> `Total Revenue`, `Net Profit` ->
  `Net Income`, `Expenses` -> `Total Expenses`, `Operating Profit` -> `Operating Income`. Rows with
  no mapping keep their Indian label.

**The native shape was not removed.** `FinancialStatement.to_dataframe()` is unchanged and still
returns the string headers, the Indian labels, Rs Crore and the `TTM` column. It is reached
through the profile rather than through these accessors: `Ticker._ensure_profile()` returns a
`CompanyProfile` whose `.profit_loss`, `.quarters`, `.balance_sheet` and `.cash_flow` are
`FinancialStatement` models. `docs/RELEASE_0.2.0.md` section 6 is the release note for the break.

* **`ticker.financials`** / **`ticker.income_stmt`**: 10 to 13+ Years Audited Annual Ind AS Income Statement.
* **`ticker.quarterly_financials`** / **`ticker.quarterly_income_stmt`**: 12 to 16+ Historical
  Quarterly Results. Quarterly *income* is served, so these are the two quarterly accessors that
  return a frame; they raise `NotImplementedError` only if the profile carries no quarters at all.
* **`ticker.balance_sheet`**: 10 to 13+ Years Audited Annual Balance Sheet.
* **`ticker.quarterly_balance_sheet`**: ~~Historical Quarterly Balance Sheets.~~ **Raises
  `NotImplementedError`.** There is no quarterly-balance-sheet source upstream, so this is annual
  only. The property is a one-line delegate to `get_balance_sheet(freq="quarterly")` and **the
  raise is inside that method, not in the property body** -- so calling
  `ticker.get_balance_sheet(freq="quarterly")` directly raises the same exception.
  **A caller gets an exception, not a frame**; `df["Total Assets"]` never gets the chance to fail.
* **`ticker.cashflow`** / **`ticker.cash_flow`**: 10 to 13+ Years Annual Cash Flow Statement (CFO, CFI, CFF).
* **`ticker.quarterly_cashflow`** / **`ticker.quarterly_cash_flow`**: ~~Historical Quarterly Cash
  Flow.~~ **Raises `NotImplementedError`**, annual only, for the same reason. The raise is in
  `get_cash_flow(freq="quarterly")` rather than in either property body. **A caller gets an
  exception, not a frame.**

---

### 2.4 Indian Corporate Superpowers

#### `ticker.sector` -> `Optional[str]`
Macro sector name (e.g., `'Energy'`, `'Information Technology'`).

#### `ticker.industry_group` -> `Optional[str]`
Industry group (e.g., `'Oil, Gas & Consumable Fuels'`).

#### `ticker.industry` -> `Optional[str]`
Specific industry name (e.g., `'Petroleum Products'`, `'Computers - Software & Consulting'`).

#### `ticker.sub_industry` -> `Optional[str]`
Micro sub-industry (e.g., `'Refineries & Marketing'`).

#### `ticker.indices` -> `List[str]`
List of official benchmark indices the equity belongs to (e.g. `['Nifty 50', 'BSE Sensex', 'BSE 500', 'Nifty Energy']`).

#### `ticker.shareholding` -> `pd.DataFrame`
12+ Quarters institutional shareholding distribution table (% Promoter, FII, DII, Government, Public, and Number of Shareholders).

#### `ticker.shareholding_yearly` -> `pd.DataFrame`
11+ Years historical annual shareholding pattern trends.

#### `ticker.ratios_history` -> `pd.DataFrame`
10-Year historical operational efficiency metrics (ROCE %, Debtor Days, Inventory Turnover, Working Capital Days, Cash Conversion Cycle).

#### `ticker.cagrs` -> `Dict[str, Dict[str, str]]`
Compounded growth rates across 10Y, 5Y, 3Y, 1Y for:
* Compounded Sales Growth
* Compounded Profit Growth
* Stock Price CAGR
* Return on Equity (ROE)

#### `ticker.pros_cons` -> `Dict[str, List[str]]`
Algorithmic qualitative insights with `'pros'` and `'cons'` bullet points.

#### `ticker.concalls` -> `List[Concall]`
List of 40+ historical quarterly earnings calls. Each `Concall` object has:
* `call.date`: Quarter date (e.g. `'Jul 2026'`)
* `call.title`: Event title
* `call.transcript_url`: Direct BSE/NSE PDF transcript link
* `call.audio_url`: Direct streamable **audio MP3 URL**
* `call.presentation_url`: Investor presentation deck PDF/PPT link

#### `ticker.annual_reports` -> `List[Dict[str, str]]`
List of 15+ years of Annual Report PDF download URLs (`[{'year': '2024', 'url': '...'}]`).

#### `ticker.credit_ratings` -> `List[Dict[str, str]]`
Credit rating agency filings and rationales (CRISIL, ICRA, CARE, India Ratings).

#### `ticker.peers` -> `pd.DataFrame`
Live industry peer comparison table including CMP, P/E, Market Cap in ₹ Cr, ROCE %, and quarterly profit/sales variances.

---

### 2.5 Custom Ratios & Quantitative Scoring

#### `ticker.custom_ratios` -> `Dict[str, Any]`
Dictionary containing all computed advanced ratios:
* `piotroski_score`: 0 to 9 integer
* `piotroski_breakdown`: Boolean map of the 9 Piotroski criteria
* `graham_number`: Fair value in ₹
* `graham_upside_%`: Percentage upside/downside to Graham number
* `enterprise_value_cr`: EV in ₹ Crores, or **`None`** when a leg of `mcap + debt - cash` is not
  in hand. The screener.in balance sheet does not itemise cash — it reports cash inside an
  undifferentiated `Other Assets` bucket — so the cash leg is normally unsourceable and this key
  is normally `None` rather than a number. It is not computed from an assumed zero.
* `cash_and_equivalents_cr`: itemised cash in ₹ Crores, or `None` for the same reason
* `ev_to_ebitda`: Enterprise Value / EBITDA, or `None` — it follows the same omission as
  `enterprise_value_cr`, and is never computed from an EV that was withheld
* `interest_coverage`: EBIT / Interest Expense
* `debt_to_equity`: Debt / Net Worth
* `cfo_to_pat`: Quality of earnings ratio (Operating Cash Flow / Net Profit)
* `free_cash_flow_cr`: CFO minus Capex in ₹ Crores

**Provenance keys**, always present in the returned dict, whether or not anything was omitted:

* `bfinance_enterprise_value_omitted` (`str | None`): why EV is `None` — names the missing leg
* `bfinance_debt_cr` (`float | None`): the debt leg on its own
* `bfinance_cash_cr` (`float | None`): the cash leg on its own
* `bfinance_omitted_metrics` (`List[str]`): one line per metric that was left out and why

`ticker.valuation_measures` publishes the same key names on `df.attrs`, so the omission reason is
spelled identically across both Enterprise Value surfaces.

#### Scalar Properties:
* `ticker.piotroski_score` -> `int` (0 to 9)
* `ticker.graham_number` -> `Optional[float]` (in ₹)
* `ticker.enterprise_value` -> `Optional[float]` (in ₹ Cr). `None` when a leg of
  `mcap + debt - cash` is not in hand; see `custom_ratios` above for why the cash leg normally is
* `ticker.ev_to_ebitda` -> `Optional[float]`, `None` whenever `enterprise_value` is
* `ticker.interest_coverage` -> `Optional[float]`

---

### 2.6 Historical Valuation Timeseries

#### `ticker.valuation_history(metric="pe", days=1825)` -> `pd.DataFrame`
Fetches multi-year historical valuation charts from Screener's charting engine:
* **`metric`** (*str*):
  - `'pe'`: Historical Stock P/E vs Median P/E & EPS.
  - `'margins'`: Historical Gross Profit Margin, Operating Margin (OPM %), Net Margin (NPM %).
  - `'ev_ebitda'`: Historical EV / EBITDA multiple.
  - `'pb'`: Price to Book Value history.
  - `'mcap_sales'`: Market Cap to Sales multiple.
* **`days`** (*int*): Lookback days (30, 180, 365, 1095, 1825, 3652, 10000).

---

### 2.7 Document & Excel Exporters

#### `ticker.to_excel(filepath: str)` -> `str`
Exports a complete, formatted 8-sheet financial model to an `.xlsx` workbook:
* Sheet 1: `Overview` (Metadata, live CMP, 52W H/L, P/E, ROCE, ROE, Book Value)
* Sheet 2: `Profit & Loss` (10-13 Years Annual Ind AS Statements)
* Sheet 3: `Quarters` (12-16 Quarters Results)
* Sheet 4: `Balance Sheet` (10-13 Years Balance Sheet)
* Sheet 5: `Cash Flow` (10-13 Years Cash Flow Statement)
* Sheet 6: `Shareholding` (Quarterly & Annual Trends)
* Sheet 7: `Ratios History` (10-Year ROCE, Debtor Days, Working Capital)
* Sheet 8: `Peers` (Live Peer Group Matrix)

#### `ticker.download_concall_audio(index=0, dest_path=".")` -> `str`
Downloads the concall audio recording (.mp3) directly to local disk.

#### `ticker.download_concall_transcript(index=0, dest_path=".")` -> `str`
Downloads the concall PDF transcript directly to local disk.

#### `ticker.download_annual_report(index=0, dest_path=".")` -> `str`
Downloads the annual report PDF directly to local disk.

---

### 2.8 AI & LLM Context Engine

#### `ticker.to_ai_context(format="markdown"|"json", sections=None)` -> `str | dict`
Generates a token-optimized financial dossier formatted specifically for LLM context windows.

#### `ticker.to_investment_memo_prompt(custom_instructions="")` -> `str`
Generates a ready-to-run initiation coverage investment memo prompt.

#### `ticker.to_forensic_audit_prompt()` -> `str`
Generates a forensic accounting check prompt auditing cash flow divergence, working capital stress, and promoter pledging.

#### `ticker.to_concall_analyst_prompt()` -> `str`
Generates an equity research prompt extracting management forward guidance and Q&A takeaways.

---

## 3. `bfinance.Tickers` Multi-Ticker Collection

```python
group = bf.Tickers(["RELIANCE", "TCS", "INFY"])
```
* **`group.tickers`**: Dictionary of individual `Ticker` instances (`group['TCS']`).
* **`group.history(period="1mo")`**: Fetches combined historical prices in a single call.

---

## 4. `bfinance.Sector` & `bfinance.Industry` (removed in 0.2.0)

~~Compatible with `yfinance 1.4.0+`:~~

```python
sec = bf.Sector("technology")
print("Top Constituents:\n", sec.top_companies)
print("ETF Constituents:\n", sec.top_etfs)
```

**The code above does not work. `bf.Sector` and `bf.Industry` do not exist, and were not
renamed to anything.** Verified in this tree: `hasattr(bfinance, "Sector")` is `False`,
`hasattr(bfinance, "Industry")` is `False`, and `from bfinance.sector import Sector` raises
`ModuleNotFoundError`.

They were removed on correctness grounds, not deprecation grounds: the old
`bfinance/sector.py` was a **hardcoded 4-sector / 20-company table with hardcoded prices and
P/Es and no HTTP call anywhere in the file**, so every number it returned was invented. A
`AttributeError` on first call is the intended signal, not a bug to report, and there is no
deprecation shim.

For a real sector or industry membership use
`ScreenerClient.get_industry_constituents`, which reads a leaf industry's own page -- and which
is itself **partial by construction**, because screener.in's `robots.txt` disallows the
`?page=` / `?limit=` a full list would need. `docs/YFINANCE_MIGRATION_GUIDE.md` section 3.D is
the long form; `docs/RELEASE_0.2.0.md` section 1 is the release note.

---

## 5. `bfinance.screens` Institutional Screener Registry

Pre-built institutional strategies:

* **`bf.screens.coffee_can.run(universe=None, max_stocks=10)`**: 10Y ROCE > 15% & ROE > 15%.
* **`bf.screens.magic_formula.run(...)`**: High ROCE + Low P/E.
* **`bf.screens.debt_free_compounders.run(...)`**: ROCE > 20% with negligible debt.
* **`bf.screens.high_dividend_yield.run(...)`**: Yield > 2.5% backed by earnings.
* **`bf.screens.undervalued_growth.run(...)`**: P/E < 22 and ROE > 15%.
* **`bf.screens.custom(name, filter_fn)`**: Custom quantitative filter predicate.

---

## 6. `bfinance.ratios` Ratio Search Client

Search Screener.in's 500+ ratio catalog:

```python
results = bf.ratios.search("graham")
# Returns: [{'name': 'Graham', 'description': '...', 'unit': 'Rs.'}]
```

---

## 7. `bfinance.ai` Module & Agent Tools

### `BFinanceAITools.get_openai_tools()` -> `List[Dict[str, Any]]`
Returns native OpenAI / Gemini / LiteLLM function calling schemas for:
* `get_stock_dossier`
* `run_institutional_screen`

### `BFinanceAITools.execute_tool(name: str, arguments: dict)` -> `str`
Executes an agent tool call and returns the formatted response.

---

## 8. Data Models & Schema Reference

All data models are defined using strict **Pydantic v2** models in `bfinance.models`:
* `CompanyProfile`: Root container holding 100% of corporate data.
* `TopRatios`: Valuation and quality ratios card.
* `FinancialStatement`: Statement model supporting `.empty`, `.to_dataframe()` and
  `.to_yfinance()`. `.to_dataframe()` is the **native** shape (string headers, Indian labels,
  Rs Crore, `TTM` column); `.to_yfinance()` is the **yfinance** shape (newest-first
  `pd.DatetimeIndex` columns, generic labels, absolute rupees on the money rows, no `TTM`), and
  it is the one the `Ticker` statement accessors return. See section 2.3.
* `Concall`: Conference call audio, transcript, and PPT links.
* `PeerStock`: Peer comparison item.
* `OptionChain` / `OptionContract`: Derivatives chain data.

---

## 9. Exceptions Hierarchy

All exceptions inherit from `bfinance.BFinanceError`:

```
BFinanceError
├── TickerNotFoundError      # Symbol not found or delisted
├── UpstreamServiceError     # Network failure or upstream 5xx
└── RateLimitExceededError   # Exceeded maximum retry attempts on 429
```
