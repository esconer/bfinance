# INFO key gap: what yfinance has that bfinance does not, measured

Measured 2026-09-30 against yfinance 1.7.0 (installed in this venv) and the bfinance
working tree, on `RELIANCE.NS`. Every number below came out of a live call; every module
claim is a path you can open.

The rule this report is written under is the repo's own: **absent beats faked.** A key that
would carry a fabricated value or a permanent `None` is a cost, not a parity win. So the
question for each of the 102 keys is not "can we make the count match" but "can we produce a
number that is true".

> **Update 2026-10-23 — read this before the 2026-09-30 counts below.**
> The body of this report is the 2026-09-30 measurement of `RELIANCE.NS`; it is kept as the
> record of that measurement. Four things have changed, and every stale sentence below is
> superseded by this block:
>
> 1. **`INFO_VALUE_UNITS` is now 80 keys**, not 76. Four keys from this report's reachable
>    list were added and are declared with units: `regularMarketChange`, `lastFiscalYearEnd`,
>    `nextFiscalYearEnd`, `mostRecentQuarter`. All four are **conditional** — omitted, never
>    `None`, when their source is absent. The returned dict now measures **76–90 keys**
>    depending on the profile (five profiles measured 2026-10-23: 76, 80, 89, 90, 90).
> 2. **`Ticker.splits` is real**, not always empty: an ex-dated Series from NSE's
>    corporate-actions filings, factor = the filing's `SplitRatio`, selected on that ratio and
>    not on the `Action` kind. An empty series can also mean the feed failed —
>    `attrs["bfinance_actions_source"] == "none"`.
> 3. **`Ticker.actions` and `Ticker.isin` are real.** `actions` carries `Dividends` and
>    `Stock Splits` from that same NSE feed; `actions["Dividends"]` is **not** `Ticker.dividends`
>    (Screener FY-end basis). `isin` returns `str` or `None`, refuses a frame whose rows
>    disagree, and falls back to archived index memberships.
> 4. **The six corporate-action `info` keys stay deliberately excluded**: `dividendRate`,
>    `exDividendDate`, `lastDividendValue`, `lastDividendDate`, `lastSplitDate`,
>    `lastSplitFactor`. Adding them would put one corporate-actions request on the most-called,
>    batch-used entry point. The same values are reachable through `splits` / `actions` / `isin`
>    and the NSE feed directly. Measured 2026-10-23: none of the six is in the returned `info`
>    dict and none is declared in `INFO_VALUE_UNITS`.
>
> `docs/YFINANCE_PARITY.md` is the current measured surface; this file remains the key-by-key
> audit of the gap.

---

## 1. The counts, live, both sides

```
yfinance 1.7.0   yf.Ticker("RELIANCE.NS").info   ->  163 keys
bfinance         Ticker("RELIANCE.NS").info      ->   86 keys
bfinance         len(INFO_VALUE_UNITS)           ->   76 keys (the declared set)
```

86, not 76: `QuoteEngine.build_info_dict` injects 10 extra labels from
`TopRatios.custom_ratios` at the end of the dict (`quotes.py:371-374`), which is the
pass-through of every raw `screener.in` top-ratios label. Those 10 are `Book Value`,
`Current Price`, `Dividend Yield`, `Face Value`, `High`, `Low`, `Market Cap`, `ROCE`,
`ROE`, `Stock P/E`.

Intersection arithmetic, all live:

| set | count |
|---|---|
| keys in both | **61** |
| yfinance only | **102** |
| bfinance only | **25** |

So the headline gap is **102, not 87**. The 87 in the task brief came from comparing 163
against `INFO_VALUE_UNITS` (76) and ignoring the 10 injected `custom_ratios` labels plus the
15 declared keys yfinance does not have. Comparing the two *dicts* is the only comparison
that means something, and it gives 102.

Also live, and worth knowing before anyone plans a fill: of the 163 yfinance keys,
**exactly one is `None`** on RELIANCE.NS -- `trailingPegRatio`. On `TCS.NS` (169 keys, read
in the same pass) **none** are `None`. So yfinance is not padding these 102 with empty
shells; it is carrying real values, and most of them are values this package can compute or
can already hold in a different key.

### 1a. The 25 keys only bfinance has

All 15 declared-but-not-in-yfinance keys are Indian-specific and are exactly the ones
yfinance cannot express:

`bseCode`, `nseSymbol`, `promoterHolding`, `promoterPledged`, `isConsolidated`,
`returnOnCapitalEmployed` (ROCE, raw percent), `marketCapInCr`, `screenerUrl`,
`dividendYieldPercent`, `faceValue`, `cagrs`, `pros`, `cons`, `returnOnEquity`,
`returnOnAssets`.

Plus the 10 injected `custom_ratios` labels listed above.

### 1b. The 25 keys bfinance has but leaves permanently `None`

Measured on the live RELIANCE dict -- these are **not** in the 102, they are already in the
dict as `None`, and four of them have arithmetic available today (section 3a):

```
address1, address2, city, country, fax, phone, zip,   # screener.in prints none of these
beta,                                                 # no beta source in the package
forwardEps, forwardPE,                                # no estimates feed
earningsGrowth, revenueGrowth,                        # fillable: see section 3
pegRatio, trailingPegRatio,                           # no estimates feed (no growth denominator)
debtToEquity,                                         # fillable: CustomRatiosCalculator already computes it
heldPercentInstitutions,                              # fillable: screener.in shareholding has FIIs + DIIs
irWebsite, fullTimeEmployees,                         # no source
promoterPledged,                                      # screener.in top-ratios prints no pledge row
open, dayHigh, dayLow, regularMarketOpen,
regularMarketDayHigh, regularMarketDayLow              # intraday: EOD history cannot carry these
```

---

## 2. The 102 keys, classified

Buckets:

- **REACHABLE** -- a source this package already reads can supply a real value. "divergent"
  means the arithmetic exists and produces a true number, but the measured result does not
  land on yfinance's number; the divergence is stated.
- **REACHABLE -- ONLY AS None** -- a source exists, but the only value it can carry today is
  `None`. Do not add.
- **NOT REACHABLE -- UPSTREAM LIMIT** -- the data is published somewhere; this package has no
  reader. The missing reader is named.
- **NOT REACHABLE -- DOES NOT EXIST FOR INDIA** -- genuinely not a published concept here.
- **STRUCTURAL** -- Yahoo plumbing or a flattening artifact. Matching means reproducing a wart.

| key | bucket | evidence / which module |
|---|---|---|
| `52WeekChange` | STRUCTURAL | Capital-S duplicate of `fiftyTwoWeekChangePercent`; live both are the identical number `-0.13640684`. Reproducing a key that exists only because of a Yahoo naming inconsistency. |
| `SandP52WeekChange` | NOT REACHABLE -- DOES NOT EXIST FOR INDIA | S&P 500's 52-week return, live `0.14299083`. The Indian analogue is NIFTY 50 and this package *does* have it (`nse/macro.py`, `VALUATION_FIELDS`, dated index closes), but under a different name and a different meaning. |
| `allTimeHigh` | NOT REACHABLE -- UPSTREAM LIMIT | Needs an intraday-high series over the whole life. `nse/bhavcopy.py` measured to start 2024-01-01 on this venv, so `period="max"` cannot reach it; `nse/marketlens.py` 30Y gives **close only** (measured max close 1600.9 vs yfinance 1611.8). Missing reader: a full-history OHLC route. |
| `allTimeLow` | NOT REACHABLE -- UPSTREAM LIMIT | Worse than unreachable: yfinance's live `5.428648` is split/bonus-adjusted back to 1996. `nse/marketlens.py` 30Y min close is `49.01` as-traded, and `market/ohlcv.py` declares its back-adjustment **incomplete** and never claims yfinance parity. A "real" number here would be a different basis, not a better one. |
| `ask` | NOT REACHABLE -- UPSTREAM LIMIT | Live `0.0` -- yfinance itself carries no book. NSE depth lives on `quote-equity`, which `nse/marketlens.py` records as **403 from datacenter IPs**. Missing reader: a depth route. |
| `askSize` | NOT REACHABLE -- UPSTREAM LIMIT | Same route; live `0`. |
| `averageAnalystRating` | NOT REACHABLE -- UPSTREAM LIMIT | Broker consensus rendered as a string; live `"1.3 - Strong Buy"`. Same missing feed as `recommendationMean`. |
| `averageDailyVolume10Day` | REACHABLE | Mean of the last 10 `Volume` values in the 1y frame `Ticker.info` already fetches. Measured **13,315,234** vs yfinance `12,679,954` (5%). No extra request. |
| `averageDailyVolume3Month` | REACHABLE | Mean of the last ~63 bars: measured **11,694,908** vs yfinance `11,643,731` (0.4%). |
| `averageVolume` | REACHABLE | yfinance sets it to the identical number as `averageDailyVolume3Month` (`11643731` both). Same computation. |
| `averageVolume10days` | REACHABLE | Same 10-day mean as `averageDailyVolume10Day`; yfinance carries the pair as two names. |
| `bid` | NOT REACHABLE -- UPSTREAM LIMIT | Same depth route as `ask`; live `0.0`. |
| `bidSize` | NOT REACHABLE -- UPSTREAM LIMIT | Same route as `askSize`; live `0`. |
| `companyOfficers` | NOT REACHABLE -- UPSTREAM LIMIT | Live: 10 director records with `age`, `yearBorn`, `totalPay`. NSE/BSE publish board data; `trendlyne/client.py` `ENDPOINTS` has 4 routes and none is an officers page, and its own docstring says the section routes were deleted rather than wired on a table signature that could not tell them apart. Missing reader: a board-of-directors route. |
| `compensationAsOfEpochDate` | NOT REACHABLE -- UPSTREAM LIMIT | Only meaningful next to `companyOfficers`; live `1798675200`. |
| `corporateActions` | STRUCTURAL | Live `[]` -- for a stock the NSE feed gives **22 dividends and 3 bonuses** (measured, `nse/corporate_actions.py`). Yahoo's own artifact is empty; copying the emptiness is copying a Yahoo bug. |
| `cryptoTradeable` | STRUCTURAL | Live `False`. A crypto flag on an Indian equity. |
| `customPriceAlertConfidence` | STRUCTURAL | Live `"HIGH"`. A Yahoo alerting UI string. |
| `dividendRate` | REACHABLE | `nse/corporate_actions.py`: last non-zero `Dividends` = **6.0** on **2026-06-05**. yfinance live `6.0`. Exact. |
| `earningsCallTimestampEnd` | NOT REACHABLE -- UPSTREAM LIMIT | Concall schedule; `ticker.py` `calendar` already returns `None` for every earnings field with the reason stated. |
| `earningsCallTimestampStart` | NOT REACHABLE -- UPSTREAM LIMIT | Same; live `1745592300`. |
| `earningsQuarterlyGrowth` | NOT REACHABLE -- UPSTREAM LIMIT | Estimates-derived; live `-0.224`, and it is the identical number to yfinance's own `earningsGrowth`. Two keys, one Yahoo estimate. |
| `earningsTimestamp` | NOT REACHABLE -- UPSTREAM LIMIT | Results-announcement schedule; live `1784282400`. No calendar feed in this package. |
| `earningsTimestampEnd` | NOT REACHABLE -- UPSTREAM LIMIT | Same feed; live `1792144800`. |
| `earningsTimestampStart` | NOT REACHABLE -- UPSTREAM LIMIT | Same feed; live `1792144800`. |
| `ebitda` | REACHABLE (divergent) | `market/ratios.py` already builds EBITDA as Operating Profit + Depreciation from the P&L. Measured TTM: 183,561 + 58,946 = **242,507 Cr** vs yfinance `180,636 Cr` (**+34%**). A true number under this package's stated definition; not yfinance parity. |
| `ebitdaMargins` | REACHABLE (divergent) | Same legs over `Sales` TTM 1,123,055 Cr: **0.2159** vs yfinance `0.15991`. Inherits the EBITDA divergence. |
| `enterpriseToEbitda` | NOT REACHABLE -- UPSTREAM LIMIT | `market/ratios.py` measured `enterprise_value_cr = None` and `ev_to_ebitda` absent, with the reason recorded in `bfinance_omitted_metrics`: the cash leg is unsourceable. Measured `bfinance_cash_cr = None`, `bfinance_debt_cr = 402,962.0`. |
| `enterpriseToRevenue` | NOT REACHABLE -- UPSTREAM LIMIT | Same cash leg. Live yfinance `1.707`. |
| `enterpriseValue` | NOT REACHABLE -- UPSTREAM LIMIT | Same cash leg. Live yfinance `19,279,934,128,128`. `screener/in`'s balance sheet has **no itemised cash row** -- measured row list is exactly `Equity Capital, Reserves, Borrowings, Other Liabilities, Total Liabilities, Fixed Assets, CWIP, Investments, Other Assets, Total Assets`. |
| `epsCurrentYear` | NOT REACHABLE -- UPSTREAM LIMIT | Forward estimate. Live `63.95286`. |
| `epsForward` | NOT REACHABLE -- UPSTREAM LIMIT | Forward estimate; same number as yfinance's `forwardEps`. |
| `epsTrailingTwelveMonths` | REACHABLE | Already in the dict as `trailingEps`: measured bfinance **55.22** vs yfinance `55.2`. Adding it is a pure duplicate of a key that exists -- yfinance itself carries both `trailingEps` and `epsTrailingTwelveMonths`. |
| `esgPopulated` | STRUCTURAL | Live `False`. A Yahoo data-availability flag. |
| `exDividendDate` | REACHABLE | `nse/corporate_actions.py` ex-date 2026-06-05 = epoch `1780617600`; yfinance live `1780617600`. Exact. |
| `exchangeDataDelayedBy` | STRUCTURAL | Live `15`. A Yahoo feed-delay constant, not a property of the stock. |
| `executiveTeam` | STRUCTURAL | Live `[]` while yfinance's own `companyOfficers` on the same call has 10 entries. Same source, one populated and one empty -- the empty one is the artifact. |
| `fiftyDayAverageChange` | REACHABLE | `quotes.py` already computes `fiftyDayAverage` from the 1y frame. Measured: `1187.0 - 1282.478` = **-95.478** vs yfinance `-96.88794` (window/alignment, same definition). |
| `fiftyDayAverageChangePercent` | REACHABLE | Same two numbers, divided. |
| `fiftyTwoWeekChangePercent` | REACHABLE | Needs the close ~1y back, which the already-fetched 1y frame has (247 bars, first close `1357.72`). Measured `1187.0/1357.72 - 1` = `-0.125742` vs yfinance `-0.13640684` (different 52-week anchor). |
| `fiftyTwoWeekHighChange` | REACHABLE | `currentPrice - fiftyTwoWeekHigh`, both already in the dict. Measured **-425.0** vs yfinance `-424.80005`. |
| `fiftyTwoWeekHighChangePercent` | REACHABLE | **Divide by the high, not by the price.** Measured `(1187.0 - 1612.0) / 1612.0` = **-0.263647** vs yfinance `-0.2635563`. Yahoo's own basis is inconsistent across this pair: the *low* key divides by the low (`5.300049 / 1181.7 = 0.004485`, matching its published `fiftyTwoWeekLowChangePercent`), the *high* key divides by the high. Copying one rule for both is how this pair gets silently wrong. |
| `fiftyTwoWeekLowChange` | REACHABLE | `currentPrice - fiftyTwoWeekLow`. Measured **+5.0** vs yfinance `+5.300049`. |
| `fiftyTwoWeekLowChangePercent` | REACHABLE | Measured `0.00423` vs yfinance `0.004485105`. |
| `fiftyTwoWeekRange` | REACHABLE | Formatted string over `fiftyTwoWeekHigh`/`fiftyTwoWeekLow`, both already held (`"1181.7 - 1611.8"` upstream). |
| `firstTradeDateMilliseconds` | NOT REACHABLE -- UPSTREAM LIMIT | Live `820467900000` = **1996-01-01**, the listing date. The deepest real frame in this package, `nse/marketlens.py` 30Y, starts **1996-10-01**. That is 8 months of listing before the first observable price, so "first price we can see" is not "first trade". Missing reader: a listing-date field. |
| `fiveYearAvgDividendYield` | REACHABLE | `nse/corporate_actions.py` (measured first event 2005-05-12) + a 5y price frame. Live yfinance `0.44`. Needs a defined 5-year window; the value is real but window-dependent. |
| `fulldayChange` | STRUCTURAL | Yahoo's US session aggregation. Live `5.0`, identical to its own `regularMarketChange`. |
| `fulldayChangePercent` | STRUCTURAL | Same; live `0.42301184`, identical to its own `regularMarketChangePercent`. |
| `fulldayPrice` | STRUCTURAL | Live `1187.0`, identical to its own `regularMarketPrice`. Three names, one number. |
| `grossMargins` | NOT REACHABLE -- UPSTREAM LIMIT | Live `0.33837`. `screener.in`'s P&L has no cost-of-goods row: `Sales - Expenses` measured 1,123,055 - 939,494 = **183,561 Cr**, which is exactly the `Operating Profit` row, not gross profit (yfinance `382,228 Cr`). |
| `grossProfits` | NOT REACHABLE -- UPSTREAM LIMIT | Same missing row; live `3,822,279,983,104`. |
| `hasPrePostMarketData` | STRUCTURAL | Live `False`. NSE has no pre/post session for cash equities, so this is a constant, not a datum. |
| `industryDisp` | STRUCTURAL | A display copy of `industry`, which the dict already carries. |
| `isEarningsDateEstimate` | NOT REACHABLE -- UPSTREAM LIMIT | Estimates feed; live `True`. |
| `lastDividendDate` | REACHABLE | Same ex-date as `exDividendDate`: measured `1780617600` vs yfinance live `1780617600`. Exact. |
| `lastDividendValue` | REACHABLE | Measured **6.0** vs yfinance live `6.0`. Exact. |
| `lastFiscalYearEnd` | REACHABLE | `profit_loss.headers` last annual column is `"Mar 2026"` -> 2026-03-31 = `1774915200`; yfinance live `1774915200`. Exact. |
| `lastSplitDate` | REACHABLE | `nse/corporate_actions.py` `Stock Splits` = 2.0 on **2024-10-28** = `1730073600`; yfinance live `1730073600`. Exact. |
| `lastSplitFactor` | REACHABLE | Same event, rendered `"2:1"`. yfinance live `"2:1"`. Exact. |
| `language` | STRUCTURAL | Live `"en-US"`. Yahoo's own locale, and the same key on every symbol in every market -- it describes the caller, not the listing. |
| `market` | STRUCTURAL | Live `"in_market"` -- a literal restatement of the fact that a ticker exists. |
| `marketState` | STRUCTURAL | Live `"POSTPOST"`, from Yahoo's US session state machine. `build_history_metadata` already publishes real IST `currentTradingPeriod` seconds instead. |
| `maxAge` | STRUCTURAL | Live `86400`. A Yahoo cache TTL. |
| `messageBoardId` | STRUCTURAL | Live `"finmb_878373"`. Yahoo's internal forum id. |
| `mostRecentQuarter` | REACHABLE | `quarters.headers[-1]` is `"Jun 2026"` -> 2026-06-30 = `1782777600`; yfinance live `1782777600`. Exact. |
| `netIncomeToCommon` | REACHABLE (divergent, needs a decision) | Two candidate legs disagree: P&L `TTM` `Net Profit` = **88,167 Cr**, but `TTM` `EPS in Rs` 55.22 x `sharesOutstanding` 13,509,679,865 = **74,600 Cr**, and yfinance live is `74,727 Cr`. The EPS leg matches; the Net Profit row does not, because the TTM EPS is a pre-bonus share base. Whoever fills this must pick the EPS leg and say why. |
| `nextFiscalYearEnd` | REACHABLE | Indian FY is calendar-year by statute, so `"Mar 2026"` + 1y = 2027-03-31 = `1806451200`; yfinance live `1806451200`. Exact. |
| `nonDilutedMarketCap` | NOT REACHABLE -- DOES NOT EXIST FOR INDIA | Live `16,888,525,847,232`. Diluted vs non-diluted share count is a US-GAAP construct; NSE publishes one share count and `screener.in` publishes one. |
| `numberOfAnalystOpinions` | NOT REACHABLE -- UPSTREAM LIMIT, ON THE DEFAULT PATH | Broker consensus count; live `26`. On a default `Ticker(...)`, `ticker.py` `analyst_price_targets` returns `None` by design, after a synthesised-targets implementation was removed in 0.1.3. **That default is no longer the whole answer**: `Ticker(..., trendlyne_sections=True)` derives the targets from trendlyne's research-report `Target` column and publishes the row count it computed them from as **`n_reports`**. It is still not the same field — Yahoo's 26 is a broker-consensus count for a rating distribution, `n_reports` is how many report rows this package averaged — so this row's verdict stands for the default path, and the opt-in is the honest partial reach. See `docs/YFINANCE_MIGRATION_GUIDE.md` section 3.E |
| `operatingMargins` | REACHABLE (divergent) | P&L `Operating Profit` TTM 183,561 / `Sales` TTM 1,123,055 = **0.16345**. yfinance `0.12331`, which implies an operating income of ~138,477 Cr against our 183,561 Cr -- a definitional gap, not a rounding one. Real value, not parity. |
| `payoutRatio` | REACHABLE | Trailing 12m dividend sum from `nse/corporate_actions.py` = **11.5**, over `EPS in Rs` TTM **55.22** = **0.20826**. yfinance live `0.2083`. Matches to 4 dp. |
| `priceEpsCurrentYear` | NOT REACHABLE -- UPSTREAM LIMIT | Forward estimate. Live `18.560545`. |
| `priceHint` | STRUCTURAL | Live `2`. A tick-size/display hint; `build_history_metadata` already publishes `priceHint`. |
| `priceToSalesTrailing12Months` | REACHABLE | `marketCap` 16,035,990,000,000 / (`Sales` TTM 1,123,055 Cr x 1e7 = 11,230,550,000,000) = **1.42808**. yfinance `1.42200`. Same definition, 0.4% apart. |
| `profitMargins` | REACHABLE | Using the EPS leg (see `netIncomeToCommon`): 74,600 / 1,123,055 = **0.06642**; yfinance `0.06615`. Using the `Net Profit` row instead gives 0.0785. Pick the EPS leg. |
| `quoteSourceName` | STRUCTURAL | Live `"Delayed Quote"`. Describes Yahoo's pipe, not the stock. |
| `recommendationKey` | NOT REACHABLE -- UPSTREAM LIMIT | Broker consensus. Live `"strong_buy"`. |
| `recommendationMean` | NOT REACHABLE -- UPSTREAM LIMIT | Same source; live `1.30769`. |
| `region` | STRUCTURAL | Live `"US"` **for an NSE-listed Indian equity**. A Yahoo geo bug. Reproducing it would be reproducing a wrong value. |
| `regularMarketChange` | REACHABLE | `currentPrice - previousClose`, both already in the dict: measured **5.0**; yfinance live `5.0`. Exact. |
| `regularMarketChangePercent` | REACHABLE | `1187.0/1182.0 - 1` = **0.42301**; yfinance live `0.42301184`. Exact. |
| `regularMarketDayRange` | REACHABLE -- ONLY AS None | Formatted `"1181.7 - 1196.5"` upstream, from intraday high/low. `quotes.py` sets `regularMarketDayHigh`/`dayHigh` to `None` from EOD history on purpose, and `test_parity_info.py` pins that. This key can only ever carry `None`. Do not add. |
| `regularMarketTime` | STRUCTURAL | Live `1790761504`. `build_history_metadata` already publishes `regularMarketTime`; here it would be `datetime.now()`, i.e. the call time, not a quote time. |
| `revenuePerShare` | REACHABLE | `Sales` TTM 1,123,055 Cr x 1e7 / 13,509,679,865 shares = **831.30**; yfinance `834.734`. 0.4% apart, same definition. |
| `sectorDisp` | STRUCTURAL | A display copy of `sector`, already in the dict. |
| `sourceInterval` | STRUCTURAL | Live `15`. A Yahoo polling interval. |
| `targetHighPrice` | NOT REACHABLE -- UPSTREAM LIMIT | Broker consensus; live `1890.0`. |
| `targetLowPrice` | NOT REACHABLE -- UPSTREAM LIMIT | Same; live `1350.0`. |
| `targetMeanPrice` | NOT REACHABLE -- UPSTREAM LIMIT | Same; live `1676.8462`. |
| `targetMedianPrice` | NOT REACHABLE -- UPSTREAM LIMIT | Same; live `1690.0`. |
| `totalCash` | NOT REACHABLE -- UPSTREAM LIMIT | Measured: `screener.in` balance sheet has **no** cash row (`has_cash_row: false`); `market/ratios.py` names this in `_CASH_ROW_LABELS` and records the omission per call. Live yfinance `2,581,470,117,888`. |
| `totalCashPerShare` | NOT REACHABLE -- UPSTREAM LIMIT | Same missing cash row. |
| `totalDebt` | REACHABLE (divergent) | `Borrowings` at `Mar 2026` = **402,962 Cr**; yfinance live `397,999.99 Cr`. 1.2% apart -- yfinance counts borrowings plus something else this balance sheet does not itemise. |
| `totalRevenue` | REACHABLE | `Sales` TTM 1,123,055 Cr x 1e7 = **11,230,550,000,000**; yfinance `11,296,050,249,728`. 0.58% apart, same definition. |
| `tradeable` | STRUCTURAL | Live `False`. A per-session Yahoo permission flag. |
| `trailingAnnualDividendRate` | STRUCTURAL | Live **`0.0`**. yfinance carries nothing here. The NSE feed gives a trailing 12m sum of **11.5** -- filling this key would produce a true number that can never match yfinance. Do not fill for parity. |
| `trailingAnnualDividendYield` | STRUCTURAL | Live **`0.0`**, same reason. |
| `triggerable` | STRUCTURAL | Live `True`. A Yahoo alerting capability flag. |
| `typeDisp` | STRUCTURAL | A display copy of `quoteType`, already in the dict. |
| `twoHundredDayAverageChange` | REACHABLE | `quotes.py` already computes `twoHundredDayAverage`: measured `1187.0 - 1362.5216` = **-175.52** vs yfinance `-169.756`. |
| `twoHundredDayAverageChangePercent` | REACHABLE | Same pair over `currentPrice`. |

### 2a. Bucket totals

| bucket | count |
|---|---|
| REACHABLE (incl. the 5 divergent) | **37** |
| REACHABLE -- ONLY AS None | **1** |
| NOT REACHABLE -- UPSTREAM LIMIT | **34** |
| NOT REACHABLE -- DOES NOT EXIST FOR INDIA | **2** |
| STRUCTURAL | **28** |
| total | **102** |

So of 102 missing keys: **37 can carry a real value**, **1 can only ever carry `None`**,
**34 have no source in this package**, **2 are not Indian concepts**, and **28 are Yahoo
plumbing**. Fewer than a third of the gap is fillable at all, and every one of the 37 has a
measured number behind it in this document rather than an inference.

---

## 3. The ranked REACHABLE list

Ranking is by likely caller impact, and the honest caveat first: **not one of these 37 keys
is touched by anything in this repo today.** Checked:

- `tests/test_dropin_info.py` `TOP40` -- **46 keys** (the name is historical), and all 46
  are in the "both" set. `HONEST_NONE_KEYS` is 15 keys, all of them already-present keys that
  the suite explicitly accepts as `None`.
- `tests/test_parity_info.py` -- asserts sector/industry presence and `fiftyDayAverage` /
  `twoHundredDayAverage` / `previousClose`, all already in both dicts.
- `src/bfinance/screens.py` reads `shortName`, `currentPrice`, `marketCapInCr`, `trailingPE`,
  `returnOnCapitalEmployed`, `returnOnEquity`, `dividendYield`, `bookValue`, `cagrs`,
  `debtToEquity` -- all already in the dict.
- `tests/test_ratio_units.py`, `test_zero_value_orphaning.py`, `test_custom_ratios.py`,
  `test_ticker_yfinance_compat.py` -- same keys.

The impact ranking below is therefore a judgement about **external** yfinance callers, not a
measurement of this repo's usage. Any key already in the TOP40 or in a screens gate would
have shown up in the grep; none did.

Ranked:

1. **`dividendRate`, `exDividendDate`, `lastDividendValue`, `lastDividendDate`,
   `payoutRatio`** -- income keys, the most commonly read group in any `info` consumer. Four
   land **digit-exact** on yfinance and `payoutRatio` agrees to 4 dp, all off
   `nse/corporate_actions.py`, which `Ticker.dividends` already calls. No new fetch is
   needed. Not covered by any test.
2. **`averageVolume`, `averageDailyVolume3Month`, `averageVolume10days`,
   `averageDailyVolume10Day`** -- liquidity, read by nearly every screen. Computable from the
   1y frame `Ticker.info` **already fetches** (`history(period="1y")`), so cost is zero:
   measured 3-month mean **11,694,908** vs yfinance `11,643,731` (0.4%), 10-day mean
   **13,315,234** vs yfinance `12,679,954` (5%). Not covered by any test.
3. **`regularMarketChange`, `regularMarketChangePercent`** -- the change column of every price
   table built on `info`. Two subtractions on values already in the dict: measured `5.0`
   exact, and `0.42301` vs yfinance `0.42301184`. Not covered.
4. **`fiftyTwoWeekChangePercent`, `fiftyTwoWeekHighChange(+Percent)`,
   `fiftyTwoWeekLowChange(+Percent)`, `fiftyTwoWeekRange`** -- the 52-week band, already half
   present (`fiftyTwoWeekHigh`/`fiftyTwoWeekLow`). Completing it is cheap and coherent, but
   read the basis note on `fiftyTwoWeekHighChangePercent` first: yfinance divides that one by
   the high and its low twin by the low. Not covered.
5. **`totalRevenue`, `revenuePerShare`, `priceToSalesTrailing12Months`, `profitMargins`** --
   the P&L size keys, all measured within 0.6% of yfinance on the same definition.
   `profitMargins` needs the EPS leg, not the `Net Profit` row (see the `netIncomeToCommon`
   note). Not covered.
6. **`totalDebt`** -- measured 1.2% from yfinance; a divergence to document, not a blocker.
   Not covered.
7. **`lastFiscalYearEnd`, `nextFiscalYearEnd`, `mostRecentQuarter`** -- period bookkeeping,
   **all three exact** off `profit_loss.headers` / `quarters.headers`. Trivially correct and
   the kind of key a caller uses to align periods. Not covered.
8. **`lastSplitFactor`, `lastSplitDate`** -- exact off the same NSE feed; matters for any
   caller reconstructing share count. Not covered.
9. **`epsTrailingTwelveMonths`** -- agrees to 0.04% but **is a duplicate of `trailingEps`**,
   which the dict already carries at 55.22. Lowest-value item on this list; add only if a
   caller is known to ask for the long name. Not covered.
10. **`netIncomeToCommon`, `operatingMargins`, `ebitda`, `ebitdaMargins`** -- reachable and
    true, but 25-34% away from yfinance on a definitional gap. Fill these only with the
    definition printed next to the number, or they become a second source of silent
    disagreement. Not covered.
11. **`fiftyDayAverageChange(+Percent)`, `twoHundredDayAverageChange(+Percent)`** -- same
    arithmetic as group 4 on the two MAs the dict already carries. Cosmetic. Not covered.
12. **`fiveYearAvgDividendYield`** -- real, but window-dependent; needs a stated 5-year
    definition before it is a number rather than a guess. Not covered.

### 3a. Five keys already in the dict, left `None`, that the same evidence touches

Not part of the 102, but the same evidence applies and they are cheaper than anything above.
Four have arithmetic available today; the fifth should stay `None`.

- **`heldPercentInstitutions`** -- `shareholding` rows `FIIs` **17.19** + `DIIs` **21.10** at
  `Jun 2026` = 38.29% (yfinance live `0.28053`, so a real divergence, disclosed). Also gives
  the package the institutional series yfinance cannot.
- **`debtToEquity`** -- `market/ratios.py` already computes it: **0.45**, against yfinance
  `36.653`. That is an ~80x definitional gap (yfinance divides by a much smaller equity base:
  `402,962 / 36.653` implies ~10,994 Cr of equity against our `Reserves + Equity Capital` =
  `904,030 Cr`). **Do not fill this key.** The value belongs under a name that says which
  definition it is; yfinance's is not India's.
- **`earningsGrowth`, `revenueGrowth`** -- `quarters` gives Jun-2026 vs Jun-2025:
  `Net Profit` 23,196 vs 30,783 = **-0.2465** (yfinance `earningsGrowth` `-0.224`);
  `Sales` 309,468 vs 243,632 = **+0.2702** (yfinance `revenueGrowth` `0.297`). Close but not
  parity, and both are None today. Note that `test_dropin_info.py` lists both in
  `HONEST_NONE_KEYS`, so filling them changes what that test asserts -- a decision, not a
  patch.
- **`promoterPledged`** -- reachable in principle via `nse/ownership.py` `probe_pledge`, but
  that probe is documented as **permanently empty with an unverified row schema**
  (`row_schema_verified = False`). Correctly `None`. Leave it.

---

## 4. The structural waste -- keys we should deliberately NOT reproduce

Reproducing any of these means copying a Yahoo internal, a Yahoo bug, or a key that exists
only because of how yfinance flattens its payload. 28 of the 102, and none of them is a fact
about an Indian company.

**Yahoo's own fields about Yahoo's own pipe** -- no datum about the security:
`quoteSourceName`, `sourceInterval`, `exchangeDataDelayedBy`, `maxAge`, `priceHint`,
`messageBoardId`, `customPriceAlertConfidence`, `triggerable`, `tradeable`, `market`,
`marketState`, `region`, `language`, `hasPrePostMarketData`, `cryptoTradeable`,
`esgPopulated`.

**Yahoo's geo bug**: `region` = `"US"` on an NSE listing. Copying it would put a wrong value
in the dict.

**Flat duplicates of keys we already carry**: `sectorDisp` (`sector`), `industryDisp`
(`industry`), `typeDisp` (`quoteType`), `52WeekChange` (`fiftyTwoWeekChangePercent`,
identical number), `fulldayPrice` / `fulldayChange` / `fulldayChangePercent` (the
`regularMarket*` values under US session names), `epsTrailingTwelveMonths` (`trailingEps`).

**Empty-list artifacts**: `corporateActions` (`[]` on a stock with 25 measured NSE events),
`executiveTeam` (`[]` beside a populated `companyOfficers` on the same call).

**Session/clock**: `regularMarketTime`. `build_history_metadata` publishes it from
`datetime.now()`, which is the call time. In `info` it would be the same non-fact under a
second name.

**Keys yfinance fills with a zero it does not mean**: `trailingAnnualDividendRate` and
`trailingAnnualDividendYield`, both live `0.0`. The NSE feed gives 11.5. A true value here
cannot ever match yfinance, and matching means writing `0.0` -- a fabricated zero, which is
the exact failure class this repo deletes code over.

**Already refused elsewhere in the package, keep refusing**: `option_chain` returns a zero-row
frame; `beta`, `forwardPE`, `forwardEps` are declared `"unknown"` in `INFO_VALUE_UNITS`. Those are
the same decisions, already made.

**And one that is a default, not a refusal: `analyst_price_targets`.** It returns `None` on a
default `Ticker(...)` after the 0.1.3 removal of synthesised targets — but that is the *off* state
of an opt-in, not a permanent refusal. `Ticker(..., trendlyne_sections=True)` fills `low` / `high`
/ `mean` / `median` from trendlyne's research-report `Target` column, plus `n_reports` and
`bfinance_analyst_source`, and still returns `None` for `current` because that feed carries no
today's price. So for this file's purpose — an `info` key that must carry a real broker consensus
count — keep the gap; but do not describe the property as having no upstream feed.

---

## 5. Verification

```
$ uv run python -c "import yfinance as yf; print(len(yf.Ticker('RELIANCE.NS').info))"
163
```

```
$ uv run pytest tests/test_no_lying_docs.py -q
............                                                             [100%]
```

(The `-q` summary line is suppressed by this repo's pytest config; run without `-q` for the
count:)

```
$ uv run pytest tests/test_no_lying_docs.py
............                                                             [100%]
12 passed in 0.94s
```

```
$ git status --porcelain
 M docs/API_REFERENCE.md
 M src/bfinance/download.py
 M src/bfinance/market/ohlcv.py
 M src/bfinance/ticker.py
 M src/bfinance/tickers.py
 M tests/test_adjust_default_drift.py
 M tests/test_bar_interval_honesty.py
 M tests/test_bhavcopy_parity_adjust.py
 M tests/test_corporate_actions_wiring.py
 M tests/test_mirror_integration.py
 M tests/test_nse_bhavcopy_wiring.py
 M tests/test_ohlcv_auto_adjust_true.py
 M tests/test_ohlcv_honesty.py
 M tests/test_ohlcv_price_sources.py
 M tests/test_ohlcv_window_honesty.py
 M tests/test_silent_fallbacks.py
?? .scratch/
?? docs/INFO_KEY_GAP.md
?? plans/
```

Every `M` line above is the concurrent agent's work in `ohlcv.py` / `ticker.py` /
`download.py` / `tickers.py` / `API_REFERENCE.md` / `test_adjust_default_drift.py` and the
tests that follow them. The only line this report owns is
`?? docs/INFO_KEY_GAP.md` -- new, untracked, 33,344 bytes, no BOM, LF only, pure ASCII.

## 6. What was not done, and why

- **No key was added.** `src/bfinance/market/quotes.py` is untouched. Of the 37 REACHABLE
  keys, **10 match yfinance to the digit** on the same definition (`dividendRate`,
  `exDividendDate`, `lastDividendDate`, `lastDividendValue`, `lastSplitDate`,
  `lastSplitFactor`, `lastFiscalYearEnd`, `nextFiscalYearEnd`, `mostRecentQuarter`,
  `regularMarketChange`) and **10 more agree within 1%** (`payoutRatio`,
  `regularMarketChangePercent`, `epsTrailingTwelveMonths`, `averageVolume`,
  `averageDailyVolume3Month`, `fiftyTwoWeekHighChange(+Percent)`, `totalRevenue`,
  `revenuePerShare`, `priceToSalesTrailing12Months`, `profitMargins`). But every one of them
  changes a dict that `screens.py` and three test modules read, and the choice of which
  `netIncomeToCommon` / `profitMargins` leg to publish is a definition decision that belongs
  to the operator, not to a report.
- **`debtToEquity` was deliberately not proposed**, despite `market/ratios.py` already
  computing 0.45. yfinance's 36.653 is a different denominator; filling the yfinance-named
  key with the Indian number would make the dict look right and be wrong.
- **`earningsGrowth` / `revenueGrowth` were not proposed for filling** even though the
  arithmetic exists, because `tests/test_dropin_info.py` `HONEST_NONE_KEYS` currently asserts
  they are honestly `None`. Filling them is a change to what the suite claims, which is a
  decision.
- **Only RELIANCE.NS was enumerated.** A second symbol (`TCS.NS`, 169 keys) was read once to
  establish that the yfinance key set is symbol-dependent -- it adds `currentRatio`,
  `freeCashflow`, `operatingCashflow`, `quickRatio`, `returnOnAssets`, `returnOnEquity` --
  so a strict "102" is a RELIANCE.NS statement, not a universal one. Every yfinance call in
  this report was made at >= 1.2 s spacing.
- **Not investigated**: whether `TCS`-only keys (`currentRatio`, `quickRatio`,
  `freeCashflow`, `operatingCashflow`) are reachable, since they did not appear in the
  RELIANCE key set. `operatingCashflow` and `freeCashflow` look reachable from
  `cash_flow` rows (`Cash from Operating Activity`, `Free Cash Flow` both present);
  `currentRatio` / `quickRatio` need `Current Assets` / `Current Liabilities`, which the
  measured `screener.in` balance-sheet row list does **not** contain. That is the natural
  next measurement, and it is not a claim this report makes.
