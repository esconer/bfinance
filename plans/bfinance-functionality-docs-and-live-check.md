# Plan: bfinance functionality documentation + live verification

Slug: `bfinance-functionality-docs-and-live-check`. Status: in progress.

## Goal
1. A single detailed, accurate document of bfinance's current public functionality in `docs/`.
2. A live fetch across different ETFs and equities proving the surface works against the real sites.

## What is already known (survey complete)
- Top-level package exports: Ticker, Tickers, Screen, screens, ratios, download, FastInfo,
  ScreenerClient, CompanyProfile/Concall/PeerStock/TopRatios, OptionChain/OptionContract,
  FinancialStatement, FinancialModelExcelExporter, DocumentDownloader, AIContextBuilder,
  AIPromptFactory, BFinanceAITools, ScreenerRatioSearch, CustomRatiosCalculator, NotAnNseSymbolError,
  marketlens, BFinanceError, TickerNotFoundError, UpstreamServiceError, RateLimitExceededError.
- Opt-in modules (not in `import bfinance`): statements, sources, costs, flows, universe, ownership,
  nse.macro, trendlyne, nse (bhavcopy/marketlens/corporate_actions), ai, screener internals, models.
- Ticker: 60+ members incl. yfinance-shaped (info, history, get_income_stmt/balance_sheet/cash_flow,
  dividends, splits, actions, calendar, analyst_price_targets, options, option_chain, major_holders,
  institutional_holders, mutualfund_holders, news, history_metadata, valuation_measures) and Indian
  superpowers (sector/industry/sub_industry/indices, concalls, annual_reports, credit_ratings,
  shareholding/shareholding_yearly, ratios_history, custom_ratios, piotroski_score, graham_number,
  enterprise_value, ev_to_ebitda, interest_coverage, cagrs, pros_cons, peers, valuation_history, fast_info).
- New opt-in flag `trendlyne_sections` (default False) with 7 members: trendlyne_news, trendlyne_reports,
  trendlyne_shareholding, trendlyne_deals, trendlyne_buy_sell_zone, trendlyne_technicals, trendlyne_swot.
- `market/technical.py` TechnicalIndicators; `trendlyne/smartoptions.py` SmartOptionsClient (opt-in flag).
- trendlyne client: 19 ENDPOINTS, sitemap resolver, member search (open), OHLC, chart metrics, sectors,
  section pages, JSON routes, WAF/snapshot/row-cap guards, 1s pacing.

## Tasks

### D1 — Detailed functionality document
Scope: write `docs/FUNCTIONALITY.md`. Cover: what bfinance is; the drop-in yfinance surface (with the
exact member list and shapes); the Indian-superpower surface; the opt-in modules (what each does and how
to reach it); the trendlyne layer (incl. `trendlyne_sections` flag + 7 members, SmartOptions flag, the
guards); data sources & provenance model; the honesty/no-fabrication contract; price basis; how to run the
test suite (offline vs live markers); known limits (analyst_price_targets current=None, splits empty,
ETF/mutual-fund handling, the two unimplementable extra routes). Every claim must be checkable against
source. Follow the existing docs voice. Depends on: survey (done).
Verify: `uv run pytest tests/test_trendlyne_claims.py tests/test_no_lying_docs.py -m "not live"`; read back
every section against the code it describes.

### L1 — Live fetch verification
Scope: write and run a script (in `.scratch/`) that constructs bfinance objects and calls a broad,
representative set of members across 3 equities (TCS, RELIANCE, INFY) and 3 ETFs (NIFTYBEES, BANKBEES,
GOLDBEES), serially (trendlyne 1s pacing), recording per-member status/shape/timing. Members: info,
fast_info, history, get_income_stmt, get_balance_sheet, get_cash_flow, dividends, splits, actions,
calendar, analyst_price_targets, options, option_chain, major_holders, institutional_holders,
mutualfund_holders, news, trendlyne_news, trendlyne_reports, trendlyne_shareholding, trendlyne_deals,
trendlyne_buy_sell_zone, trendlyne_technicals, trendlyne_swot, sector/industry/indices, shareholding,
valuation_history, and a full-section sweep on TCS. ETF members that legitimately return empty
(dividends/options/financials) are reported as empty, not failed. Depends on: nothing (I run it myself).
Verify: script exits 0; every member either returns a sane shape or reports a clean error; no exception
escapes; report pasted.

### L2 — Report + reconcile
Scope: paste the live results; reconcile any failures against known-limits; if a live failure reveals a
real defect, file it. Depends on L1.
