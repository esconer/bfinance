# yfinance parity: the measured subset

**This is an Indian-equity alternative to `yfinance`, not a drop-in replacement.** `bfinance`
implements a documented subset of `yfinance` 1.7.0's `Ticker` surface, fills it from NSE/BSE and
screener.in data, and **refuses** the rest by name. Of `yfinance` 1.7.0's 98 public `Ticker`
members, **44 are present with a data path, 14 are present but structurally empty, and 40 are
absent** (measured 2026-10-23). "100% drop-in" is not true and is not claimed anywhere after this
document; the old claim in `README.md` was corrected on 2026-10-23.

Everything below was measured on this working tree, in this environment, on **2026-10-23**. Where a
figure was recorded earlier and could not be re-measured today, the row says so. There are no
projected numbers in this file.

---

## 1. How the table was measured

`yfinance` **1.7.0 is importable in this venv** (`uv run python -c "import yfinance"` → `1.7.0`), so
the comparison is against the installed library, not against a remembered baseline.

```text
python                         3.13.15
bfinance                       0.2.0
yfinance                       1.7.0
len([n for n in dir(yf.Ticker) if not n.startswith("_")])   98
len([n for n in dir(bf.Ticker) if not n.startswith("_")])   99
shared names                                                58
yfinance-only                                               40
bfinance-only                                               41
```

The `dir()` diff is only the **membership** measurement. It cannot tell a real member from one that
answers empty, which is why the second tier exists and why it was measured by calling the members.

**Tier rules used here** (stated so the classification is reproducible rather than a judgement):

| tier | rule |
|---|---|
| `present` | the name resolves on `bf.Ticker` **and** at least one constructor configuration yields real data. A forwarding alias is classified by the answer it returns, not by the fact that its body is one line. |
| `present-but-empty` | the name resolves, but **no** configuration yields data: the answer is an empty Series/frame, `()`, or a raise, by design. |
| `absent` | the name does not resolve on `bf.Ticker` at all. |

The fourteen `present-but-empty` members were checked by calling them with the socket blocked, so a
"returns empty" observation cannot have come from a successful fetch. The four quarterly statement
spellings and the two TTM spellings raise the same deliberate `NotImplementedError`; the three
holder/capital-gain `get_*` forwarders answer exactly what their target answers:

```text
capital_gains                     Series, shape (0,)
institutional_holders             DataFrame, shape (0, 0)
mutualfund_holders                DataFrame, shape (0, 0)
get_capital_gains                 Series, shape (0,)
get_institutional_holders         DataFrame, shape (0, 0)
get_mutualfund_holders            DataFrame, shape (0, 0)
options                           ()
option_chain()                    OptionChain, calls (0, 13) and puts (0, 13)
quarterly_balance_sheet           raises NotImplementedError
quarterly_balancesheet            raises NotImplementedError
quarterly_cash_flow               raises NotImplementedError
quarterly_cashflow                raises NotImplementedError
ttm_income_stmt                   raises NotImplementedError
ttm_cash_flow                     raises NotImplementedError
```

`option_chain` is empty structurally — `DerivativesEngine.empty_option_chain()` builds it, and
`tests/test_no_lying_docs.py` pins the `(0, 13)` shape. `options` is `()` because `Ticker.options`
calls `DerivativesEngine.resolve_options(self.ticker)` with no membership list, and that method
returns `()` when `fo_symbols` is falsy.

---

## 2. The three tiers

### 2.1 `present` — 44 members

A real data path. It may still need a constructor flag (`trendlyne_sections=True`), a profile that
carries the row, or a working upstream; those conditions are in the notes column, because "present"
means "has an implementation", not "returns rows for every symbol".

### 2.2 `present-but-empty` — 14 members

The member exists so a `yfinance`-shaped `.empty` check behaves, but it carries no data for any
Indian equity today. Shipping it is a deliberate choice over deleting it: an `AttributeError` in a
migrating caller is a worse failure than an honest empty answer, and the docstrings say why each one
is empty.

### 2.3 `absent` — 40 members

Not defined at all. Each one is either refused for a stated reason (section 4), or is a wrapper
spelling this pass did not add (the last row of §3.3). Nothing is hidden behind `__getattr__`.

---

## 3. The measured member table

### 3.1 `present` (44)

| `yfinance` member | What it returns here | Divergence to know |
|---|---|---|
| `history` | `DataFrame`, tz `Asia/Kolkata` | `auto_adjust=True` by default (yfinance 1.7 default); extra `ohlc`, `source`, `sources`, `requires` keywords |
| `history_metadata`, `get_history_metadata` | `dict` | Built from the requested symbol and the already-fetched frame |
| `fast_info`, `get_fast_info` | `FastInfo` | **21 mapping keys**, `dict(t.fast_info)` works; four keys are new (see §6.2) |
| `info`, `get_info` | `dict` | **76–90 keys depending on the profile** (measured, §6.1); four keys are omitted when unsourced |
| `financials` | `DataFrame` | 10–13 audited fiscal years, ₹ absolute, metric-row / generic-label shape |
| `income_stmt`, `incomestmt` | `DataFrame` | Same; `incomestmt` is the yfinance 1.7 property spelling |
| `quarterly_financials`, `quarterly_income_stmt`, `quarterly_incomestmt` | `DataFrame` | 12–16 quarters; `quarterly_incomestmt` is the 1.7 spelling |
| `get_income_stmt` | `DataFrame` | 13 `get_*` forwarders + 4 property spellings added in this pass (§6.1) |
| `balance_sheet`, `balancesheet` | `DataFrame` | 10–13 fiscal years; yfinance has quarterly here, this package does not |
| `get_balance_sheet` | `DataFrame` | Forwarder; yearly only — `freq="quarterly"` raises (§3.2) |
| `cash_flow`, `cashflow` | `DataFrame` | Annual 10–13 years |
| `get_cash_flow`, `get_cashflow` | `DataFrame` | Forwarders; yearly only — `freq="quarterly"` raises (§3.2) |
| `dividends` | `Series`, tz-aware, name `Dividends` | Screener **fiscal-year-end** basis, not yfinance's ex-dates (see §7) |
| `splits` | `Series`, tz-aware, name `Stock Splits` | **Real since this pass**: NSE corporate-actions `SplitRatio`, ex-dated (§7) |
| `actions` | `DataFrame` with `Dividends`, `Stock Splits` | Both columns from the **same** NSE feed, not `dividends` + `splits` concatenated |
| `get_splits`, `get_actions`, `get_dividends` | Forwarders | `period=` is accepted and **cannot narrow** — the series is the whole declared history |
| `calendar` | `dict` (8 keys) | Earnings + ex-dividend dates where disclosed |
| `major_holders`, `get_major_holders(as_dict=...)` | `DataFrame`, numeric fraction `Value` | `as_dict` honoured |
| `news`, `get_news(count=10)` | `list[dict]`, yfinance `{id, content}` shape | Screener announcements; `count` slices |
| `valuation_measures`, `get_valuation_measures` | `DataFrame`, `Metric`/`Value` | `Value` is `object` dtype on purpose; `freq`/`periods` are accepted and **cannot narrow** (one current snapshot) |
| `analyst_price_targets`, `get_analyst_price_targets` | `dict` **or `None`** | `None` on a default `Ticker`; real only with `trendlyne_sections=True`; `current` is always `None` |
| `recommendations` | `DataFrame`, 6×6 measured | Requires `trendlyne_sections=True`; `period` is the vendor's `asofdate`, not `0m/-1m` |
| `recommendations_summary` | `DataFrame`, 4×6 measured | Last four rows of the **same** frame, no second fetch |
| `earnings_estimate` | `DataFrame`, 4×6 measured | Requires the flag; index `0q/+1q/0y/+1y`; `yearAgoEps` may be `NaN` |
| `revenue_estimate` | `DataFrame`, 4×6 measured | Same index/shape from the vendor's `REVENUE` rows |
| `earnings_history` | `DataFrame`, 3×4 measured | Requires the flag; index is **fiscal period end**, not the announcement date |
| `growth_estimates` | `DataFrame`, 9×1 measured | Requires the flag; column `stock` only — `industry`/`sector`/`index` are absent, not zero |
| `insider_transactions` | `DataFrame`, 174×13 measured | Requires the flag; **500-row server cap**, vendor columns verbatim |

### 3.2 `present-but-empty` (14)

| `yfinance` member | Answer here | Why it is empty rather than absent |
|---|---|---|
| `capital_gains` | `Series`, `(0,)` | An Indian listed equity has no capital-gain distribution; no NSE/Screener route publishes one. Returned, not raised, so `.empty` behaves like a US fund that distributed nothing. |
| `get_capital_gains` | `Series`, `(0,)` | Exact forwarder to `capital_gains`; the answer and the reason are the same. |
| `institutional_holders` | `DataFrame`, `(0, 0)` | yfinance parity shape; Indian institutional ownership is served by `shareholding` / `shareholding_yearly`. |
| `get_institutional_holders` | `DataFrame`, `(0, 0)` | Exact forwarder to `institutional_holders`. |
| `mutualfund_holders` | `DataFrame`, `(0, 0)` | Same: the MF holder table is not the shape this package publishes. |
| `get_mutualfund_holders` | `DataFrame`, `(0, 0)` | Exact forwarder to `mutualfund_holders`. |
| `options` | `()` | No verified F&O membership list is wired into `Ticker`; `resolve_options` returns `()` when `fo_symbols` is falsy. |
| `option_chain(date)` | `OptionChain`, both sides `(0, 13)` | The synthesiser that used to invent strikes/Greeks/OI was deleted; an empty chain is the truthful answer. |
| `quarterly_balance_sheet`, `quarterly_balancesheet` | raise `NotImplementedError` | No quarterly-balance-sheet source upstream; annual only. The `quarterly_balancesheet` alias raises where its target raises. |
| `quarterly_cash_flow`, `quarterly_cashflow` | raise `NotImplementedError` | No quarterly-cash-flow source upstream; annual only. The `quarterly_cashflow` alias raises where its target raises. |
| `ttm_income_stmt` | raises `NotImplementedError` | No TTM statement route. |
| `ttm_cash_flow` | raises `NotImplementedError` | No TTM statement route. |

### 3.3 `absent` (40)

| `yfinance` member | Why it is absent |
|---|---|
| `eps_trend`, `get_eps_trend` | **Refused** — see §4.1. |
| `eps_revisions`, `get_eps_revisions` | **Refused** — same source limit: one current snapshot + a prose hit-rate. |
| `sustainability`, `get_sustainability` | **Refused** — no ESG/BRSR source anywhere in `src/`; see §4.2. |
| `earnings`, `quarterly_earnings`, `get_earnings` | Dead upstream: `yfinance` 1.7.0 returns `None` and emits a `DeprecationWarning` (measured 2026-10-23). An alias to a dead member moves the failure, it does not fix it. |
| `earnings_dates`, `get_earnings_dates` | Refused: no join exists between a board-meeting announcement date and an EPS row; the recorded fixture has no future-dated row. |
| `funds_data`, `get_funds_data` | Refused: the AMFI client is category-flow only; no per-fund portfolio exists. |
| `shares`, `get_shares` | Refused: no NSE route publishes a current share-count field. `yfinance` 1.7.0's own `shares` **raises** `YFNotImplementedError` (measured 2026-10-23). |
| `get_shares_full` | Refused: no NSE route publishes share-count **history**. Unlike `shares`, this one works upstream, so it is a real gap, stated rather than papered over. |
| `insider_roster_holders`, `get_insider_roster_holders` | Refused: a roster is an aggregation of the transactions table into "current holding"; the route publishes no position or title field. |
| `insider_purchases`, `get_insider_purchases` | Refused for the same reason; the raw rows stay reachable at `insider_transactions`. |
| `upgrades_downgrades`, `get_upgrades_downgrades` | Refused: the vendor table has `Author`/`Date`/`Type`/`Target` but no `FromGrade` and no explicit `Action`, so it cannot express "upgrade vs downgrade". Raw data stays at `trendlyne_reports()`. |
| `sec_filings`, `get_sec_filings` | US SEC only; no Indian equivalent. |
| `live` | `yfinance`'s streaming object; there is no streaming route in this package. |
| `valuation` | `yfinance`'s period-grid `ValuationMeasures` object; this package serves one current snapshot at `valuation_measures`. |
| `get_financials`, `get_incomestmt`, `get_balancesheet` | `yfinance`'s older accessor spellings. This package ships `get_income_stmt` / `get_balance_sheet` plus the property spellings `incomestmt` / `balancesheet`, but not these three wrappers. |
| `ttm_financials`, `ttm_incomestmt`, `ttm_cashflow` | Deliberately not added: they would forward to `ttm_income_stmt` / `ttm_cash_flow`, which raise. An alias to a raiser only moves the failure. |
| `get_recommendations`, `get_recommendations_summary`, `get_earnings_estimate`, `get_revenue_estimate`, `get_earnings_history`, `get_growth_estimates`, `get_insider_transactions`, `get_isin` | The **property** is implemented (§3.1); `yfinance`'s `get_*` wrapper for it was not added in this pass. Call the property. |

---

## 4. Refusals (dated 2026-10-23)

A refusal is a decision, not an oversight. Each of these has a source that does not exist in this
tree, so implementing it would mean inventing the number.

### 4.1 `eps_trend` and `eps_revisions` — refused

`yfinance` publishes four columns per row here (`current`, `7daysAgo`, `30daysAgo`, `60daysAgo`,
`90daysAgo`) built from a time series of analyst estimates. **The vendor this package reads publishes
one current snapshot and a prose three-year hit-rate; it does not publish a dated series of past
consensus values.** Emitting the `7/30/60/90-day` columns would therefore mean either repeating
today's number under four labels or differencing a series that does not exist. Both are invention,
so the members are absent and this paragraph is the reason.

What *is* shipped from the same page is the current snapshot: `earnings_estimate`,
`revenue_estimate`, `earnings_history`, `growth_estimates`, `recommendations` and
`recommendations_summary` (§3.1). `eps_revisions` would need the same missing history and is refused
for the same reason.

### 4.2 `sustainability` and `get_sustainability` — refused

`yfinance`'s `sustainability` object carries ESG scores, controversy involvement and a
peer/percentile grid. **There is no ESG or BRSR source anywhere in `src/`** — no client, no parser,
no cached payload, no recorded fixture. A score computed from anything else in this package
(financials, shareholding, governance flags) would be this package's own arithmetic wearing an ESG
label. So the member is absent and the refusal is recorded here rather than the member being
silently omitted from a list.

### 4.3 The rest of the refusals

| refused | reason |
|---|---|
| `funds_data`, `get_funds_data` | The AMFI client is category-flow only; no per-fund portfolio exists. |
| `shares`, `get_shares`, `get_shares_full` | No NSE route publishes share-count history. `yfinance`'s own `shares` raises in 1.7.0; `get_shares_full` does work upstream, so that one is a stated gap. |
| `insider_roster_holders`, `insider_purchases` (+ `get_*`) | Derivation from a 500-capped transactions table; no position/title field upstream. |
| `upgrades_downgrades` | No `FromGrade`, no explicit `Action`; a same-named frame could not express the one thing the name means. |
| `earnings_dates` | No join between a board-meeting announcement date and an EPS row. |
| `sec_filings` | US SEC only. |
| `earnings`, `quarterly_earnings`, `get_earnings` | Dead upstream in 1.7.0 (measured: `None` + `DeprecationWarning`). |
| `Sector`, `Industry` at package root | Removed in 0.2.0; the old implementation was a hardcoded table and a test enforces the deletion. |
| `screen` / `EquityQuery`, `config` / `set_config`, `WebSocket` | An abstraction with one implementation and no caller; `bfinance` already has `Screen`/`screens` with a different vocabulary. |
| 28 structural `info` keys (`region`, `marketState`, `fullday*`, …) | Yahoo plumbing and one geo bug; copying them copies a defect. |
| 6 corporate-action `info` keys (`dividendRate`, `exDividendDate`, `lastDividendValue`, `lastDividendDate`, `lastSplitDate`, `lastSplitFactor`) | Deliberately excluded from `info`: they would add one corporate-actions request to the most-called, batch-used entry point. The same values are reachable directly through `splits` / `actions` / `isin` and the NSE feed. Measured 2026-10-23: none of the six is in the returned `info` dict, and none is declared in `INFO_VALUE_UNITS`. |

---

## 5. Swap-boundary fixes (measured 2026-10-23)

The four defects were **wrong behaviour in members that were already present**, not missing
features. All four are fixed in this tree.

### 5.1 The real constructor signature

```text
inspect.signature(bf.Ticker)
(ticker: str, session: Optional[Any] = None, *, cache_ttl_hours: float = 24.0, timeout: float = 15.0,
 proxy: Optional[str] = None, raise_errors: bool = False, statement_fallback: bool = False,
 trendlyne_sections: bool = False, multi_level_index: bool = True,
 cache: Optional[SQLiteCache] = None, screener_client: Optional[ScreenerClient] = None)
```

`inspect.signature(...).parameters` starts `['ticker', 'session']`, which is `yfinance`'s order. The
old `bfinance/__init__` monkey-patch that reported `(ticker, cache_ttl_hours=24.0, *args, **kwargs)`
is deleted.

### 5.2 A second positional is `session`, and a session is refused

```text
bf.Ticker("TCS", True)            -> NotImplementedError
bf.Ticker("TCS", session=object()) -> NotImplementedError
```

Both raise **before any request**, and the message names `httpx` and says the argument cannot be
honoured. `yfinance`'s second positional is `session`, so mirroring it means a stray bool fails
loudly instead of being read as `cache_ttl_hours`. The old bool shim (which read `Ticker("X", True)`
as `multi_level_index=True`) is gone; use `Ticker("X", multi_level_index=True)`.

**Residual divergence, not fixed:** `Tickers.__init__`'s second positional is still
`cache_ttl_hours`, and a bool there is still shimmed to `multi_level_index` (measured:
`bf.Tickers(["TCS"], True).multi_level_index is True`). `Tickers` is not fixed by this pass.

### 5.3 `dict(t.fast_info)` works

`FastInfo` implements `keys()`, `items()`, `values()`, `get(key, default=None)`, `__contains__`,
`__iter__` and `__len__` over **21 keys** (measured). `dict(...)` returns all 21.

---

## 6. New surface

### 6.1 `Ticker`

**17 aliases added.** Thirteen `get_*` forwarders — `get_info`, `get_fast_info`, `get_calendar`,
`get_news`, `get_dividends`, `get_splits`, `get_actions`, `get_capital_gains`,
`get_major_holders`, `get_institutional_holders`, `get_mutualfund_holders`,
`get_analyst_price_targets`, `get_valuation_measures` — plus the four yfinance 1.7 statement
spellings `incomestmt`, `balancesheet`, `quarterly_incomestmt`, `quarterly_balancesheet`.

Each is a one-line forwarder; no new request, no re-implementation. Three keywords are accepted for
signature parity and **cannot narrow anything**: `period=` on the four corporate-action series
(the series is the whole declared history) and `freq=`/`periods=` on `get_valuation_measures` (one
current snapshot, no period axis). `as_dict=` **is** honoured on the three holder accessors.

**Eight data members added**, plus two that became real:

| member | flag | reference date / index | measured shape | divergence stated |
|---|---|---|---|---|
| `recommendations` | `trendlyne_sections=True` **required** | `period` = the vendor's `asofdate` (`YYYY-MM-DD`) | `(6, 6)` | Six dated snapshots, not yfinance's four relative `0m/-1m/-2m/-3m` rows |
| `recommendations_summary` | required | same | `(4, 6)` | Last four rows of the **same** frame; no second fetch |
| `earnings_estimate` | required | `0q/+1q/0y/+1y`, derived from `is_current` / `is_past` + `period_end` | `(4, 6)` | `yearAgoEps` is the published prior-year `actual`; where absent the cell is **`NaN`** and `attrs["bfinance_estimate_note"]` names the exact cells. Never solved from `growth_pct` |
| `revenue_estimate` | required | same rule | `(4, 6)` | `yearAgoRevenue` same `NaN` + note contract |
| `earnings_history` | required | **fiscal period end**, `attrs["bfinance_date_basis"]="fiscal_period_end"`, index name `quarter` | `(3, 4)` | The vendor publishes no announcement date; a period end read as a reporting date would be weeks out |
| `growth_estimates` | required | the vendor's own period labels | `(9, 1)` | One column, `stock`; `industry`/`sector`/`index` are **absent, not zero** |
| `insider_transactions` | required | the page's `Period` column, verbatim | `(174, 13)` | **500-row server cap**; a frame at 500 is truncated and says so; a frame below it is **not** called complete |
| `isin` | no flag | the feed's own `ISIN` | `str` or `None` | Refuses a frame whose rows disagree; falls back to **archived** index memberships only |
| `splits` | no flag | NSE **ex-date** | e.g. RELIANCE `1` row, `2024-10-28 → 2.0` | Selected on the published `SplitRatio`, **not** the `Action` kind |
| `actions` | no flag | NSE **ex-date** | one row per declared ex-date | `Dividends` here is **not** `ticker.dividends` (different source, different date basis) |

**`0q/+1q/0y/+1y` is derived from flags, not row order.** `0q`/`0y` are the rows the vendor marks
`is_current`; `+1q`/`+1y` are the **earliest** row of the same period type marked
`is_past == False` whose `period_end` is later than the anchor's. On the recorded page the first
quarter row is `Q3FY26` while `0q` is `Q1FY27`, so a position-based implementation would answer a
different number — the distinction is observable in the fixture.

**`NaN` cells.** `earnings_estimate` and `revenue_estimate` each carry two `NaN`
`yearAgo*` cells on the recorded page, and `attrs["bfinance_estimate_note"]` names them:
`yearAgoEps` for `0q` (`Q1FY27`, period end 2026-06-30) and `+1q` (`Q2FY27`, 2026-09-30); the same
two periods for `yearAgoRevenue`. Nothing is back-filled. The note is part of the contract, not a
warning.

**Flag-off is a raise, not an empty frame.** Every one of the flag-gated members above raises
`NotImplementedError` naming `trendlyne_sections=True` before a socket exists. An empty frame would
be indistinguishable from "the vendor published no rows", which is why the refusal is a raise.

**Cost of the flag-gated members.** They read the **stock page** — the same document the OHLC token
call already fetches — so after any trendlyne call on the Ticker the marginal cost is **zero
requests**; on a cold client the first one pays that one page fetch. `insider_transactions` is the
exception: it is a route of its own, one request on first access, then memoised (a **failure** is
not memoised and the next access retries).

**`info` count, measured 2026-10-23.** `INFO_VALUE_UNITS` declares **80** keys. The returned dict
also carries the **10** raw screener top-ratio labels (`Book Value`, `Current Price`, …) when the
profile publishes them. Four declared keys are **conditional** — `regularMarketChange`,
`lastFiscalYearEnd`, `nextFiscalYearEnd`, `mostRecentQuarter` are omitted, never `None`, when their
source is absent. Measured across five profiles: **76, 80, 89, 90, 90** keys. The range is a
property of the profile, not a bug; the docstring on `Ticker.info` now says so instead of claiming a
fixed count.

### 6.2 `FastInfo` is a mapping

**21 keys** (measured). The four added in this pass are `regular_market_previous_close`,
`ten_day_average_volume`, `three_month_average_volume`, `year_change`. The other 17 predate the
change. `len(fast_info)`, `dict(fast_info)`, `fast_info.get(...)`, `key in fast_info` and
iteration all work; attribute access is unchanged.

### 6.3 `Search`

`bf.Search(query, max_results=8, *, timeout=20.0, max_retries=3, proxy=None)` reads NSE MarketLens
`/api/search` and exposes `.quotes` (list of dicts) and `.all` (`{"quotes": [...]}` only).

- **Measured payload, 2026-10-23:** seven keys —
  `id`, `type`, `name`, `ticker`, `description`, `matchScore`, `marketCap`. Passed through
  verbatim; no `symbol` rename, no `.NS` suffix, no invented `shortname`/`quoteType`/`exchange`.
- `max_results` is applied client-side (`DataFrame.head`); the endpoint has no count parameter.
- The index is **equities-only**: `Search("nifty")` and `Search("niftybees")` returned zero quotes
  (measured 2026-10-22). MarketLens is NSE-only, so a BSE scrip cannot match either.
- A failure raises; it is never converted into an empty list.

### 6.4 `Tickers.download`

Added as a forwarder to `bfinance.download`, with `actions=True` (matching `Tickers.history` and
`yfinance`'s default for this spelling; the module-level `bfinance.download` keeps `actions=False`).

### 6.5 `download()`

| parameter | measured 2026-10-23 |
|---|---|
| `timeout` | **Reaches the wire.** Passed to the per-symbol `history_async(timeout=...)` call and to the one HTTP client the batch builds. Previously accepted and dropped. |
| `session` | Non-`None` **raises** `NotImplementedError` naming `httpx`, before any Ticker exists. |
| `ignore_tz` | `True` is honoured (the index is tz-naive). `False` **raises** `NotImplementedError`: the timezone-preserving frame cannot be produced. `None` is the default and the established tz-naive index. |
| `progress` | `True` emits one `INFO` line per finished symbol on the `bfinance.download` logger (`download: 2/5 TCS`); `False` emits none. Previously both were the same call. |

One cache and one screener client are built per batch and shared through `Ticker(cache=...)` /
`Ticker(screener_client=...)`, instead of one of each per symbol.

---

## 7. `splits`, `actions` and `isin` — the source and the selection rule

All three read NSE's corporate-actions filings, parsed by
`bfinance.nse.corporate_actions.parse_corporate_actions`. `splits` is
`splits_from_actions(frame)`: the factor is the filing's own **`SplitRatio`**, read out of the
published subject when the frame was parsed.

**Selection is on the ratio, not on the event kind.** A composite subject
(`"Interim Dividend - Rs 1.50 Per Share / Face Value Split - From Rs 10 To Rs 5"`) is filed as a
dividend while declaring a real `2.0` factor, and `yfinance` publishes that factor for the same
date. Filtering on `Action` would drop it. A plain dividend row declares no ratio, so it can never
contribute one.

**An empty series is not "no split happened".** It can also mean the feed could not be read. The
two cases are distinguished by `attrs["bfinance_actions_source"]`:
`"nse_corporate_actions"` means the feed answered and published no split/bonus;
`"none"` means the fetch failed, and `bfinance_actions_source_detail` carries the exception while
`ticker.data_warnings` names it. Both `splits` and `actions` leave `bfinance_empty_feed` **absent**
on the failed-feed path: `True` would assert NSE answered with nothing, which was not observed.

Measured on the recorded payloads (2026-10-23):
RELIANCE `{2024-10-28: 2.0}`; ITC `{2010-08-03: 2.0, 2016-07-01: 1.5}`. The index is
`Asia/Kolkata`-aware and named `Date`, matching `dividends`' basis.

**Cost.** The first of `splits`, `actions` and `isin` pays **one** corporate-actions request; the
other two read the memoised events frame. This is not `dividends`' route, so a caller who has already
read `dividends` still pays it.

---

## 8. Concrete breaks for a migrating caller

1. **`Ticker("X", True)` now raises.** The second positional is `session`, so a stray bool is a
   `NotImplementedError`, not `multi_level_index=True`. Write `Ticker("X", multi_level_index=True)`.
2. **`Ticker(..., session=...)` raises.** The transport is `httpx`; a `requests.Session` or
   `httpx.Client` cannot be honoured. Omit the argument.
3. **Every new Trendlyne-backed member requires `trendlyne_sections=True`.** With the flag off,
   `recommendations`, `recommendations_summary`, `earnings_estimate`, `revenue_estimate`,
   `earnings_history`, `growth_estimates` and `insider_transactions` raise
   `NotImplementedError` naming the flag. `analyst_price_targets` stays `None` instead of raising
   (pinned, unchanged behaviour).
4. **`info` has 76–90 keys, not a fixed 76**, and four keys can be absent. `key in info` is the
   availability test.
5. **`splits` is no longer always empty.** Code that asserted `ticker.splits.empty` will now see
   real rows for symbols with a declared split or bonus.
6. **`isin` can be `None`.** `None` means "not in the corporate-actions payload and not in the
   archived index memberships on this machine", **not** "no ISIN exists".
7. **`Tickers(...)`' second positional is still `cache_ttl_hours`**, not `session`. This is a
   residual swap-boundary divergence (§5.2).

---

## 9. What is not measured here

- **Live per-symbol shapes.** The trendlyne-backed shapes in §6.1 are measured from the recorded
  MOTHERSON symbol page (capture 2026-10-21, `retrieved_at` 2026-10-22), not from a live fetch on
  2026-10-23. A different symbol can have more or fewer rows; the column sets and the derivation
  rules do not change.
- **The insider route's observed span.** "2020-02-12 to 2025-06-16" is the span recorded when the
  500-row pull was captured. It was not re-measured on 2026-10-23 (the committed fixture has 174
  rows, below the cap).
- **Performance.** No latency or request-count claim is made here. Any speed claim belongs to a
  measurement on this machine with the method recorded next to it.
- **`yfinance`'s own key counts per symbol.** `docs/INFO_KEY_GAP.md` records a 2026-09-30 live
  measurement of `yf.Ticker("RELIANCE.NS").info` at 163 keys; that is a per-symbol figure and was not
  re-measured today.
