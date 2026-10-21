# bfinance — Current Functionality

A reference of what `bfinance` does today, what it returns, and — just as importantly — what it
refuses to do. Every shape below is taken from the source; where a value is honest-empty by design,
that is stated rather than hidden.

Live-verified 2026-10-11 against screener.in, trendlyne.com and the NSE.

---

## 1. What it is

`bfinance` is a Python SDK for **Indian equities (NSE/BSE)** built as a **drop-in replacement for
`yfinance`**. `import bfinance as yf` gets you the yfinance-shaped API; the difference is that the data
is Indian, the fiscal years are real, and the vendor gaps are named instead of faked.

```python
import bfinance

t = bfinance.Ticker("TCS")
t.info                 # 76 keys, yfinance-shaped
t.history(period="1y") # DataFrame, Asia/Kolkata tz
t.get_income_stmt()    # 14 periods x 12 rows
```

The package is honest by construction. Where a number cannot be measured it is `None` or an empty
frame, never a guess. `docs/UPSTREAM_SOURCES.md` carries the measured evidence behind each source.

---

## 2. Quick start

| Object | Import | What it does |
|---|---|---|
| `Ticker` | `from bfinance import Ticker` | One instrument. The main entry point. |
| `Tickers` | `from bfinance import Tickers` | A collection; forwards its flags to every member. |
| `Screen` / `screens` | `from bfinance import Screen, screens` | Screener.in screen queries. |
| `download` | `from bfinance import download` | Multi-symbol OHLCV download. |
| `ratios` | `from bfinance import ratios` | `ScreenerRatioSearch` — custom ratio search. |
| `FastInfo` | `from bfinance import FastInfo` | Lightweight scalar container. |
| `ScreenerClient` | `from bfinance import ScreenerClient` | Raw screener.in access. |
| `marketlens` | `from bfinance import marketlens` | NSE MarketLens intraday/history. |

---

## 3. The `Ticker` surface

### 3.1 yfinance-shaped members

These match yfinance's names and return shapes. (`src/bfinance/ticker.py`)

| Member | Returns | Notes |
|---|---|---|
| `info` | `dict` (76 keys) | Screener-derived company profile. |
| `fast_info` | `FastInfo` | 16 scalar properties + `to_dict()`. |
| `history(period, interval, start, end, auto_adjust, …)` | `DataFrame`, tz `Asia/Kolkata` | `history_async()` also available. |
| `get_income_stmt(as_dict, pretty, freq)` | `DataFrame` | 10 fiscal years, Rs Cr. |
| `get_balance_sheet(...)` | `DataFrame` | |
| `get_cash_flow(...)` / `get_cashflow(...)` | `DataFrame` | |
| `financials`, `income_stmt`, `quarterly_financials`, `quarterly_income_stmt`, `balance_sheet`, `quarterly_balance_sheet`, `cashflow`, `cash_flow`, `quarterly_cashflow`, `quarterly_cash_flow` | `DataFrame` | Property aliases. |
| `ttm_income_stmt`, `ttm_cash_flow` | `DataFrame` | Raise `NotImplementedError`. |
| `dividends` | `Series` | From the real ex-dated NSE feed. |
| `splits` | `Series` | **Always empty** — see §8. |
| `actions` | `DataFrame` | Columns `Dividends`, `Stock Splits`. |
| `capital_gains` | `Series` | Empty by design. |
| `calendar` | `dict` (8 keys) | Earnings + ex-dividend dates. |
| `analyst_price_targets` | `dict` or `None` | See §3.3. |
| `options` | `tuple` | F&O expiries, gated on real membership. |
| `option_chain(date)` | `OptionChain` | Empty chain when there is no verified F&O membership. |
| `major_holders` | `DataFrame` | |
| `institutional_holders`, `mutualfund_holders` | `DataFrame` | Empty `(0,0)` for Indian equities (yfinance parity). |
| `news` | `list[dict]` | Screener announcements, yfinance `{id, content}` shape. |
| `history_metadata` / `get_history_metadata()` | `dict` (22 keys) | |
| `valuation_measures` | `DataFrame` | `Value` column is `object` dtype on purpose (`None` must not become `NaN`). |

### 3.2 Indian-superpower members

Data yfinance does not carry for US equities.

| Member | Returns |
|---|---|
| `sector`, `industry_group`, `industry`, `sub_industry` | 4-level taxonomy (`str`) |
| `indices` | `list[str]` — every index the stock is in |
| `concalls` | `list[Concall]` — transcript/audio/PPT PDFs |
| `annual_reports` | `list[dict]` |
| `credit_ratings` | `list[dict]` |
| `shareholding`, `shareholding_yearly` | `DataFrame` — quarterly / annual promoter-FII-DII-MF-public |
| `ratios_history` | `DataFrame` |
| `custom_ratios` | `dict` |
| `piotroski_score` | `int` |
| `graham_number` | `float` or `None` |
| `enterprise_value`, `ev_to_ebitda`, `interest_coverage` | `float` or `None` |
| `cagrs` | `dict` of `dict[str, str]` |
| `pros_cons` | `dict` with `pros` / `cons` |
| `peers` | `DataFrame` |
| `valuation_history(metric, days)` | `DataFrame` (+ `_async`) |

### 3.3 The opt-in Trendlyne surface

`Ticker(..., trendlyne_sections=True)` — **default `False`**, costs **zero** requests when off, and
loads nothing (`bfinance.trendlyne` stays out of `import bfinance`).

| Member | Returns | Measured live (TCS) |
|---|---|---|
| `trendlyne_news(page=1)` | yfinance-shaped `list[dict]` | 10 items |
| `trendlyne_reports(page=1)` | `DataFrame` | 21×12 |
| `trendlyne_shareholding()` | `DataFrame` | 12×13 |
| `trendlyne_deals()` | `DataFrame` | 181×8 |
| `trendlyne_buy_sell_zone()` | `dict` keyed `standalone`/`consolidated` | 2 frames |
| `trendlyne_technicals()` | `DataFrame` | 55 signals |
| `trendlyne_swot()` | `dict` of 5 fact tables | 5 tables |

It also upgrades **`analyst_price_targets`** from a hardcoded `None` to a real measurement derived
from the research-reports `Target` column:

```python
{'current': None, 'low': 1860.0, 'high': 3600.0, 'mean': 2690.05, 'median': 2525.0,
 'n_reports': 20, 'bfinance_analyst_source': 'trendlyne_research_reports'}
```

- `current` is **always `None`** — the page's `LTP` column is the price *at each report's
  publication*, not the current price, so publishing it under that key would be a relabelling.
- The vendor's own **consensus aggregate row is excluded** from every statistic (including it would
  fold the consensus into its own mean).
- Returns `None` when the flag is off or the frame has no usable target.

### 3.4 Export / AI helpers

`to_excel`, `download_concall_audio`, `download_concall_transcript`, `download_annual_report`,
`to_ai_context`, `to_investment_memo_prompt`, `to_forensic_audit_prompt`,
`to_concall_analyst_prompt`.

---

## 4. The opt-in modules

None of these are imported by `import bfinance`; each is reached by naming it.

| Module | Purpose | Entry point |
|---|---|---|
| `bfinance.statements` | Financial-statement vendor cascade + parsers | `resolve_from_payloads`, `parse_fundamental_results`, `PROVIDERS`, `TABLES`, `UNAVAILABLE` |
| `bfinance.sources` | The capability registry/cascade every source is described by | `Registry`, `DEFAULT`, `resolve()` |
| `bfinance.costs` | Brokerage/stamp-duty/TDS cost schedules | `CostSchedule*`, `TransactionShape` |
| `bfinance.flows` | FII/DII participant flows | `participant_url`, `ParticipantFlowsError` |
| `bfinance.universe` | Index constituent archives | `resolve_index`, `IndexSpec` |
| `bfinance.ownership` | NSE shareholding, deals, pledge, FPI | `OwnershipClient`, `shareholding_url` |
| `bfinance.nse` | Bhavcopy archive, MarketLens, corporate actions | `BhavcopyClient`, `MarketLensClient`, `price_history`, `intraday` |
| `bfinance.nse.macro` | Macro series | — |
| `bfinance.trendlyne` | Third-party analytics client (see §5) | `TrendlyneClient`, `SmartOptionsClient` |
| `bfinance.screener` | screener.in client, parser, charts | `ScreenerClient`, `ScreenerHTMLParser`, `ScreenerChartEngine` |
| `bfinance.market` | Engines | `QuoteEngine`, `FastInfo`, `DerivativesEngine`, `CorporateActionsEngine`, `TechnicalIndicators` |
| `bfinance.models` | Pydantic models | `CompanyProfile`, `FinancialStatement`, `OptionChain` |
| `bfinance.ai` | AI context + prompt builders | `AIContextBuilder`, `AIPromptFactory`, `BFinanceAITools` |
| `bfinance.cache` | SQLite cache | `SQLiteCache` |

---

## 5. The Trendlyne layer

`from bfinance.trendlyne import TrendlyneClient`

- **Resolution** — the equity sitemap first (one polite GET, 5,922 instruments), then the member
  search route (which robots.txt disallows under `*/member/`; open by default, `allow_member_search=False`
  closes it). ETFs are absent from the sitemap and reachable only via that fallback.
- **Prices** — `/mapp/v1/stock/web/ohlc/{id}/{token}/`, price basis **as-traded, back-adjusted for
  splits and bonuses only, demergers NOT applied**. 1 request/second floor (robots `Crawl-delay: 1`).
- **19 ENDPOINTS**: corporate actions, bulk/block deals, shareholding, board meetings, research reports,
  latest news, stock alerts, insider/SAST, monthly MF holdings, buy/sell zone, SWOT, plus three JSON
  routes (advanced technicals, overview-second-part, price-performance).
- **Guards** — WAF (405 or `x-amzn-waf-action` → terminal `TrendlyneWafChallengeError`), shareholding
  snapshot-only (non-`latest` periods 500), insider row cap (500).
- **`SmartOptionsClient`** — separate host, opt-in `allow_smart_options` (default `False`); full option
  chain with greeks, maxPain, PCR.
- **Not implemented, and why** — `/equity/holding-overtime/` is login-gated (302 → `/accounts/login/`);
  `/equity/stock-mf-holding-table/` returns HTTP 500 to every public request shape.

---

## 6. Data sources, provenance, and the no-fabrication contract

Sources: **screener.in** (profile, statements, peers), **NSE** (bhavcopy prices, corporate actions,
delivery, intraday), **trendlyne** (sections, technicals, reports), **smartoptions** (option chains).

- Provenance travels on `df.attrs` (`bfinance_provenance`, `bfinance_price_source`,
  `bfinance_actions_source`, `bfinance_trendlyne_*`, …), so a frame always says where it came from.
- Missing data is `None` or an empty frame; a failed section is recorded in `ticker.data_warnings`
  rather than silently dropped.
- `-1` on a published count means "**not measured**"; `0` means "**measured, none found**". They are
  never collapsed.
- Run the suite offline with `uv run pytest -m "not live"`. Live tests are `-m live`; the
  robots-gated ones also carry `-m etf`.

---

## 7. Known limits (by design)

- `splits` is always empty; `ttm_*` raise; `institutional_holders`/`mutualfund_holders` are `(0,0)`.
- `analyst_price_targets['current']` is always `None`.
- Mutual funds are a separate namespace with no OHLC route.
- `historical` for Trendlyne is symbol-dependent; it is neither "truncated at listing" nor a fixed window.

---

## 8. Live verification (2026-10-11)

Run across 3 equities (TCS, RELIANCE, INFY) and 3 ETFs (NIFTYBEES, BANKBEES, GOLDBEES).

A real bug was found and fixed by this run: `bfinance.screener` advertised
`Accept-Encoding: …, br`; httpx decodes gzip/deflate natively but brotli only with an optional package
that is not a dependency, so screener.in's brotli response reached the parser as raw bytes and **every
screener-backed member silently returned empty at HTTP 200**. Fixed by not advertising `br`
(`src/bfinance/screener/client.py`), regression-tested in `tests/test_screener_accept_encoding.py`.

Post-fix, live:

| | TCS | RELIANCE | INFY | ETFs |
|---|---|---|---|---|
| profile | 0–3.7s, `id=3365` | 1.5s, `id=2726` | 2.8s, `id=1489` | `id` resolves; no P&L (correct) |
| sector / industry | Information Technology / IT - Software | Energy / Petroleum Products | Information Technology / IT - Software | `None` (ETF has no sector) |
| dividends / actions | (12,) / (12,2) | (12,) / (12,2) | (12,) / (12,2) | empty (correct) |
| news | 5 | 5 | 5 | 0 (correct) |
| shareholding | (6,12) | (6,12) | (7,12) | empty (correct) |
| peers | (0,0)\* | (9,12) | (9,12) | (0,0) |
| income / balance / cash | (14,12)/(10,13)/(6,12) | (14,12)/(10,12)/(6,12) | (14,12)/(10,12)/(6,12) | (0,0) (correct) |
| valuation_history | (247,3) | (248,3) | (247,3) | (0,3) |
| trendlyne sections | all 7 members OK | all OK | all OK | members degrade cleanly with recorded reasons |

\* TCS peers empty is a screener page with no peer rows; RELIANCE/INFY prove the parser works.
ETF statements being empty is correct — ETFs have no income statement.

**Offline suite after the fix:** `3182 passed, 158 deselected, 1 xfailed`, with one pre-existing
failure in `tests/test_exceptions_alias.py` that belongs to unrelated in-flight work, not to this change.