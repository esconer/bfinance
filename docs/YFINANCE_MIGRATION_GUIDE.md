# `yfinance` to `bfinance` Migration Guide

This guide is for developers migrating existing Python codebases from `yfinance` (versions 0.2.x up to 1.7.0+) to `bfinance` for Indian equities (NSE & BSE).

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
| `yf.Sector(name)` | `bf.Sector(name)` | `yfinance 1.4.0+` sector constituent explorer |
| `yf.Industry(name)` | `bf.Industry(name)` | `yfinance 1.4.0+` industry constituent explorer |
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
* statements self-consistent (Assets ≈ Liab+Equity ≤5%); `to_yfinance()` bridges
  orientation/units (₹Cr → absolute, Mar-YYYY → DatetimeIndex)
* dividends amounts within 3% (FY-end vs ex-date conventions differ — see table)

Deliberate extras (never silent): `freq="quarterly"` on income uses real quarterly
P&L; native statement orientation stays metric-row/₹Cr; `Ticker("X", True)` bool
second positional is treated as yfinance's `multi_level_index`.
