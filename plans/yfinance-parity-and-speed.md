# Plan: yfinance parity and remaining speed

Baseline: `0fc4123` (`main`). Date: 2026-10-22. yfinance comparison baseline: **1.7.0**, read from
`.venv/Lib/site-packages/yfinance/`.

Evidence used to write this plan (all measured, none inferred):

| source | what it established |
|---|---|
| `docs/INFO_KEY_GAP.md` | the `Ticker.info` **key** gap: 102 missing keys → 37 reachable, 1 reachable-only-as-`None`, 34 upstream limit, 28 structural, 2 not-Indian |
| `explore` surface inventory | bfinance has 74 public `Ticker` members; 65 of yfinance's are absent |
| `librarian` yfinance 1.7.0 read | the exact member list, signatures, return shapes, and which are dead upstream |
| `explore` source map | which missing members have a real Indian source, and at what marginal cost |
| `explore` hot-path audit | measured local costs: 387 ms row-select, 43–49 ms profile parse, 75 ms `fast_info` refetch, 11–15 ms double store write |
| `metis` | swap-boundary breaks, fabrication temptations, and the three-tier scope discipline |
| `momus` pass 1 | corrected the plan: `GROWTH` is a field, `marketlens.search` is a method, no cache seam exists |

---

## 1. Verdict this plan is allowed to reach

`bfinance` is **not yet** a drop-in Indian yfinance, and the honest ceiling is lower than a `dir()` diff suggests:

- **65 `Ticker` members missing**, but only ~14 are honest one-line delegations, ~20 have a real but
  differently-shaped Indian source, and ~30 are unreachable or encode US-only concepts.
- **Four measured swap-boundary breaks** (not missing features — *wrong behaviour* in present ones):
  1. `bfinance/__init__` monkey-patches `Ticker.__init__`, so `inspect.signature` reports
     `(ticker, cache_ttl_hours=24.0, *args, **kwargs)`.
  2. `Ticker("RELIANCE", requests.Session())` raises `TypeError` — yfinance's second positional is `session`.
  3. `session=` is accepted and forwarded nowhere.
  4. `dict(t.fast_info)` fails — `FastInfo` has no `keys`/`items`/`values`/`get`.
- **Several members exist but are structurally empty**: `splits` (always empty although NSE carries splits),
  `capital_gains` (correctly N/A for India, but undocumented as such), `institutional_holders`/`mutualfund_holders`
  (`(0,0)`).

The plan closes the swap boundary, adds the members whose data is genuinely available, and **records the refusals**
rather than padding the surface.

### Reconciliation of the recon disagreement

`metis` refused `earnings_estimate` / `revenue_estimate` / `earnings_history` / `growth_estimates` as having
"no Indian consensus feed". That is true of the standalone consensus route (a measured 404) and of the default
path. It is **not** true of the Trendlyne **stock page** added in `0fc4123`:

- `consensus` section: `(116, 15)`, top-level keys are `QUARTER` and `ANNUAL` statement groups for `EPS` and
  `REVENUE`, each row carrying `AVG`, `HIGH`, `LOW`, `NUMBER_OF_ANALYSTS`, `growth_pct`, `is_past`, `is_current`,
  `periodtype`, `actual`.
- `recommendations` section: `(6, 12)` — six monthly snapshots of `strongBuy`/`buy`/`hold`/`sell`/`strongSell`
  plus `reco_value`/`reco_text`, which is yfinance's semantics exactly.

Both ride in the same page the OHLC path already downloads, so marginal cost is **zero**. Note the correction from
`momus`: **`GROWTH` is not a statement** — `growth_pct` is a *field* on the `EPS`/`REVENUE` rows
(`trendlyne/client.py:2720-2754, 2793`). `growth_estimates` is therefore built from that field, not from a
`statement == "GROWTH"` filter.

Still refused: `eps_trend`, `eps_revisions` — the vendor publishes one snapshot and a prose hit-rate.

---

## 2. Decisions (binding on every task)

| # | decision | reason |
|---|---|---|
| D1 | Remove the `Ticker.__init__` monkey-patch. Real signature: `Ticker(ticker, session=None, *, cache_ttl_hours=24.0, ...)`. `session` non-`None` raises `NotImplementedError` naming httpx. | yfinance's second positional is `session`; the current patch makes it a `cache_ttl_hours` collision and a `TypeError`. Silently ignoring a passed session is a lie. |
| D2 | New yfinance-named members behind `trendlyne_sections=True` **raise `NotImplementedError` naming the flag when off**. | An empty frame is how "the vendor published no rows" reads (`ticker.py:634-646`). A raise is explicit; `None` breaks `.empty`. `analyst_price_targets`' existing `None` is pinned and stays untouched. |
| D3 | `earnings_history` indexes on **fiscal period end**, with `df.attrs["bfinance_date_basis"] = "fiscal_period_end"`. | The consensus frame carries no announcement date. A different, disclosed basis is acceptable; an undisclosed one is not. |
| D4 | `insider_transactions` publishes the vendor's columns verbatim. **No roster.** | A roster is an aggregation of transactions into "current holding" — a derivation, not a published fact. Route is 500-row capped and 2020→present. |
| D5 | New DataFrame members attach `bfinance_provenance`, `bfinance_symbol`, `bfinance_trendlyne_snapshot_only`, `bfinance_trendlyne_source`, `bfinance_trendlyne_retrieved_at`, and one measured count. | House rule; an unlabelled empty frame is the one most likely to be misread. |
| D6 | `splits` becomes real (NSE `Stock Splits`), and `actions` reflects it. `capital_gains` stays empty **and its docstring says India has no capital-gains distributions for equities**. | Real data exists and is currently dropped; an always-empty Series with no explanation is a silent lie by omission. |
| D7 | `isin` is `Optional[str]`, sourced per-symbol from the corporate-actions payload, with archived-index fallback; coverage disclosed. | yfinance returns `None` on failure; partial coverage must be stated, not implied. |
| D8 | "Fast" is measured as **local CPU and request count**, not cold wall-clock. Cold `info` is floor-bound at 261 archive files × 350 ms; the plan does not touch pacing. | `metis` A5. Claiming cold-latency wins against a pacing floor would be dishonest. |
| D9 | F&O is **do not touch**. `options`, `option_chain`, `OptionChain`, `SmartOptionsClient`, `market/derivatives.py` are already honestly empty and out of scope. | User's explicit exclusion. |
| D10 | The six `info` keys that need a **new** corporate-actions request on the default `info` path (`dividendRate`, `exDividendDate`, `lastDividendValue`, `lastDividendDate`, `lastSplitDate`, `lastSplitFactor`) are **not added to `info`**. | `info` currently short-circuits corporate actions (`requires=("Close","Volume")`); adding them would put +1 request on the most-called, batch-used entry point. They stay documented as reachable-but-not-paid. |

---

## 3. Refusals (documented, not implemented)

Each lands in `docs/YFINANCE_PARITY.md` with this reason, so the gap is a decision rather than an oversight.

| refused | reason |
|---|---|
| `eps_trend`, `eps_revisions` | Vendor publishes one current snapshot and a prose 3-year hit-rate. Emitting 7/30/60/90-day columns would be invention. |
| `sustainability`, `get_sustainability` | No ESG/BRSR source anywhere in `src/`. A score would be fabricated. |
| `funds_data`, `get_funds_data` | The AMFI client is category-flow only; no per-fund portfolio exists. |
| `get_shares_full`, `shares`, `get_shares` | No NSE route publishes share-count history. yfinance's own `shares` **raises** unconditionally in 1.7.0. |
| `insider_roster_holders`, `insider_purchases` | Derivation from a 500-capped transactions table; no position/title field upstream. |
| `upgrades_downgrades` | Vendor table has `Author`/`Date`/`Type`/`Target`; no `FromGrade`, no explicit `Action`. A same-named frame that cannot express "upgrade vs downgrade" is worse than absent. Raw data stays reachable via `trendlyne_reports`. |
| `earnings_dates` | No join exists between a board-meeting announcement date and an EPS row; the recorded fixture has no future-dated row. |
| `sec_filings` | US SEC only. |
| `earnings`, `quarterly_earnings`, `get_earnings` | Dead upstream in 1.7.0 (`None` + `DeprecationWarning`). |
| `Sector`, `Industry` at package root | The names are burned: `__init__.py:7-12` records the previous implementation was hardcoded fabrication with a test enforcing it. |
| `screen`/`EquityQuery`, `config`/`set_config`, `WebSocket` | An abstraction with one implementation and no caller; `bfinance` already has `Screen`/`screens` with a different vocabulary. |
| 28 structural `info` keys (`region`, `marketState`, `fullday*`, …) | Yahoo plumbing and one geo bug. Copying them copies a defect. |
| 6 corporate-action `info` keys | D10. |

---

## 4. Task graph

Waves are ordered by **file ownership**: no two tasks in a wave write the same file.

| wave | task | agent | owns | depends on |
|---|---|---|---|---|
| 1 | **F1 swap surface** | hephaestus | `ticker.py`, `tickers.py`, `test_public_surface.py`, `test_dropin_history.py`, `test_ticker_trendlyne_statement_fallback.py` | — |
| 1 | **F4 actions accessors** | sisyphus-junior | `nse/corporate_actions.py`, `universe.py` | — |
| 1 | **F6 `Search`** | sisyphus-junior | `search.py` (new), `__init__.py` | — |
| 1 | **P1 row-select fast path** | hephaestus | `market/ohlcv.py` | — |
| 2 | **F2 FastInfo + free `info` keys** | sisyphus-junior | `market/fast_info.py`, `market/quotes.py`, `test_ratio_units.py` | F1 |
| 2 | **F3 Ticker data members** | hephaestus | `ticker.py`, new test files | F1, F4 |
| 2 | **P2 single store fetch** | sisyphus-junior | `market/ohlcv.py` | P1 |
| 2 | **P3 download honesty + shared client** | sisyphus-junior | `download.py` | F1 |
| 3 | **D1 parity report + doc truth** | sisyphus-junior | `docs/*`, `README.md` | all |

---

## 5. Tasks

### F1 — Swap surface

**Scope.** `src/bfinance/ticker.py`, `src/bfinance/tickers.py`,
`tests/test_public_surface.py`, `tests/test_dropin_history.py`,
`tests/test_ticker_trendlyne_statement_fallback.py`.

1. Delete `_apply_ticker_bool_shim` (`tickers.py:14-41`) and its call (`:41`). Give `Ticker.__init__` the real
   signature `(ticker, session=None, *, cache_ttl_hours=24.0, timeout=15.0, proxy=None, raise_errors=False,
   statement_fallback=False, trendlyne_sections=False, multi_level_index=True, cache=None,
   screener_client=None)`.
   `session` non-`None` → `NotImplementedError` naming `httpx` and saying the parameter cannot be honoured.
   `cache` / `screener_client` are the seam P3 uses: **when non-`None` they must be used instead of the
   unconditional construction at `ticker.py:330-333`**; both default to `None` and preserve today's behaviour.
2. **Update the two tests that currently assert the shim** — `tests/test_dropin_history.py:184-186` (which loops
   `(True, False)`) and `tests/test_ticker_trendlyne_statement_fallback.py:604-608` assert
   `Ticker("X", True).multi_level_index is True`. After the change, **every** non-`None` second positional is a
   session, so `Ticker("X", True)` and `Ticker("X", False)` both raise `NotImplementedError`; only
   `Ticker("X", multi_level_index=True)` sets it. Rewrite both expectations and say why in the docstring.
3. Add the identity-delegation layer, each an exact forwarder (no new logic):
   `get_info`, `get_fast_info`, `get_calendar`, `get_news(count=10)`, `get_dividends(period="max")`,
   `get_splits(period="max")`, `get_actions(period="max")`, `get_capital_gains(period="max")`,
   `get_major_holders`, `get_institutional_holders`, `get_mutualfund_holders`, `get_analyst_price_targets`,
   `get_valuation_measures(freq="quarterly", periods=5)`, plus the statement spellings `incomestmt`, `balancesheet`,
   `quarterly_incomestmt`, `quarterly_balancesheet`. Honour `as_dict` where yfinance has it.
   `ttm_financials` / `ttm_incomestmt` are **not** added: they forward to `ttm_income_stmt`, which raises today
   (`ticker.py:1111`). An alias to a raiser only moves the failure.
4. Add `Tickers.download(...)` as a forwarder to `bfinance.download.download`, defaulting `actions=True` (matching
   `Tickers.history`).
5. Make `fast_info` reuse the 1y frame `info` already memoised instead of refetching with actions resolution.
   Record the request delta in the docstring.

**Constraints.** No new network path. `analyst_price_targets`' default `None` untouched. `history()` signature
untouched. `multi_level_index` keyword-addressable.

**Verify.**
```
uv run python -c "import inspect, bfinance as bf; s=inspect.signature(bf.Ticker); assert list(s.parameters)[:2]==['ticker','session'], s; print(s)"
uv run python -c "import bfinance as bf; t=bf.Ticker('TCS'); assert hasattr(t,'get_info') and hasattr(t,'incomestmt') and hasattr(t,'quarterly_balancesheet'); print('aliases ok')"
uv run python -c "import bfinance as bf
try:
    bf.Ticker('TCS', True)
except NotImplementedError:
    print('session refused')
else:
    raise SystemExit('expected NotImplementedError')"
uv run pytest tests/test_public_surface.py tests/test_dropin_history.py tests/test_ticker_trendlyne_statement_fallback.py tests/test_ticker_yfinance_compat.py -m "not live" -q
```

### F4 — Actions accessors (pure, frame-in)

**Scope.** `src/bfinance/nse/corporate_actions.py`, `src/bfinance/universe.py`, new
`tests/test_corporate_actions_identity.py`.

1. Add `isin_from_actions(frame) -> Optional[str]` and `splits_from_actions(frame) -> pd.Series` as **pure
   functions over an already-parsed actions frame**. No I/O, no cache, no request. `splits_from_actions` is indexed
   by ex-date with the split factor (e.g. `2.0` for 2:1) and is empty (with a DatetimeIndex) when the frame has no
   split rows.
2. Confirm from the parser and the recorded fixture that the ISIN field is actually present in the payload. If it
   is not, **stop and report** — do not derive it from a name match or a different page.
3. Add a read-only archived-index fallback helper in `universe.py` that takes a symbol and returns `Optional[str]`
   from data already on disk. It must not create a network call and must state its coverage (archived indices only).
4. Do not change any existing signature or add a TTL.

**Verify.**
```
uv run python -c "from bfinance.nse import corporate_actions as ca; import inspect; print(inspect.signature(ca.isin_from_actions), inspect.signature(ca.splits_from_actions))"
uv run pytest tests/test_corporate_actions_identity.py tests/test_nse_corporate_actions.py -q
```

### F6 — `Search`

**Scope.** `src/bfinance/search.py` (new), `src/bfinance/__init__.py`, new `tests/test_search.py`.

1. `Search(query, max_results=8, ...)` wraps **`MarketLensClient().search(query, limit=max_results)`**
   (`nse/marketlens.py:443` — `search` is a *method*, not a module-level function). Expose `.quotes` and `.all`.
2. Document that the index is equities-only: index/ETF queries return empty (measured).
3. Export `Search` in `__all__`. No new transport.

**Verify.**
```
uv run python -c "import bfinance as bf; s=bf.Search('reliance'); print(type(s).__name__, len(s.quotes))"
uv run pytest tests/test_search.py tests/test_public_surface.py -q
```

### P1 — Row-select fast path

**Scope.** `src/bfinance/market/ohlcv.py` (`_select_symbol_rows`), new
`tests/test_select_symbol_rows_fastpath.py`.

Measured baseline: **387 ms** per 939,861-row frame, of which `.str.upper()` is 182.6 ms and `.str.strip()` ~150 ms;
a direct `.eq()` probe is 37–48 ms.

1. Probe the raw column with cheap equality (both as-is and upper-cased) and return immediately on a hit.
2. Fall through to the existing normalisation only when the probe finds nothing. **Never return empty on a probe
   miss.**
3. Preserve the existing empty-frame contract and index exactly.
4. Record the measured before/after in the test docstring, and keep the messy-input cases (lowercase, whitespace,
   `.BO` suffix) as correctness tests.

**Verify.**
```
uv run pytest tests/test_select_symbol_rows_fastpath.py -m "not live" -q
uv run pytest tests/test_nse_bhavcopy_wiring.py tests/test_bhavcopy_parity_adjust.py tests/test_ohlcv_honesty.py -m "not live" -q
```

### F2 — FastInfo mapping + free `info` keys

**Scope.** `src/bfinance/market/fast_info.py`, `src/bfinance/market/quotes.py`, `tests/test_ratio_units.py`,
new `tests/test_info_exact_keys.py`.

1. `FastInfo` gains `keys()`, `items()`, `values()`, `get(key, default=None)`, `__contains__`, `__iter__`,
   `__len__`, so `dict(t.fast_info)` works.
2. Add the four yfinance `fast_info` keys that are pure arithmetic on data already fetched:
   `regular_market_previous_close`, `ten_day_average_volume`, `three_month_average_volume`, `year_change`.
3. Add only the `info` keys derivable from **what `build_info_dict` already receives**
   (`profile`, `latest_price`, `history`): `regularMarketChange`, `lastFiscalYearEnd`, `nextFiscalYearEnd`,
   `mostRecentQuarter`. Verify each against the profile's own statement headers; if a source is absent, **omit the
   key** and say so — do not emit `None`.
4. Declare each added key in `INFO_VALUE_UNITS` with a unit from the closed vocabulary, and add it to the
   hand-written expectation dict in `tests/test_ratio_units.py`. That module's stale-key test builds
   `info_from(TopRatios())` with no statement headers and `history=None`, so **give that helper statement headers
   plus a 2-bar history** or the declared keys read as stale. `tests/test_ratio_units.py:311-316` has no epoch
   unit — extend the allowed vocabulary rather than smuggling a number into an existing unit.
5. Do **not** add the six corporate-action-backed keys (D10).

**Verify.**
```
uv run python -c "import bfinance as bf; f=bf.Ticker('TCS').fast_info; d=dict(f); assert set(['regular_market_previous_close','ten_day_average_volume','three_month_average_volume','year_change']).issubset(d), sorted(d); print('fast_info keys ok')"
uv run python -c "import bfinance as bf; i=bf.Ticker('TCS').info; ks=['regularMarketChange','lastFiscalYearEnd','nextFiscalYearEnd','mostRecentQuarter']; print({k:i.get(k) for k in ks})"
uv run pytest tests/test_ratio_units.py tests/test_info_exact_keys.py tests/test_dropin_info.py -m "not live" -q
```

### F3 — Ticker data members

**Scope.** `src/bfinance/ticker.py`; new `tests/test_ticker_consensus_members.py`,
`tests/test_ticker_isin_and_splits.py`, `tests/test_ticker_insider.py`; update `tests/test_dropin_actions.py`
if `splits`/`actions` expectations change.

All Trendlyne-backed members require `trendlyne_sections=True`; when off, `NotImplementedError` naming the flag (D2).

1. `recommendations` → DataFrame, columns `period, strongBuy, buy, hold, sell, strongSell`, from the six-row
   `recommendations` section. `recommendations_summary` → the last four rows of the **same frame** (no second fetch).
2. `earnings_estimate` → index `0q,+1q,0y,+1y`; columns `numberOfAnalysts, avg, low, high, yearAgoEps, growth`,
   from the `QUARTER`/`ANNUAL` `EPS` rows. Derive the index from `is_current`/`is_past` and the
   `periodtype` label — **not** from the bare period string. `revenue_estimate` → same index and shape from the
   `REVENUE` rows.
3. `yearAgoEps` / `yearAgoRevenue`: the fixture's quarter rows do not all have a same-period-last-year sibling.
   Where the value cannot be produced from a published row, publish `NaN` and set
   `attrs["bfinance_estimate_note"]` naming which cells are absent. Do not derive it from `growth_pct` and call it
   a published figure.
4. `earnings_history` → columns `epsEstimate, epsActual, epsDifference, surprisePercent`, index = fiscal period end,
   `attrs["bfinance_date_basis"] = "fiscal_period_end"` (D3). Do not re-derive `surprisePercent`; the vendor
   publishes it.
5. `growth_estimates` → built from the **`growth_pct` field** on the `EPS`/`REVENUE` rows, index by period, column
   `stock` only. Docstring states `industry`/`sector`/`index` are not published and are absent, not zero.
6. `insider_transactions` → verbatim vendor frame from `insider_trading_async`, with the 500-row cap and
   2020→present range stated in the docstring (D4). No roster, no purchases member.
7. `isin` → `Optional[str]` via F4's `isin_from_actions` + the archived-index fallback (D7). `splits` → real Series
   via F4's `splits_from_actions`; `actions` reflects it. `capital_gains` stays empty **with the reason in its
   docstring** (D6). Both need an actions *frame*: use the route `Ticker.dividends` already uses
   (`CorporateActionsEngine.resolve_actions_async`), so a caller who has not called `dividends` pays **one**
   corporate-actions request — say so in the docstring. The D5 `bfinance_trendlyne_*` attrs do **not** apply to
   these frames; give them `bfinance_provenance` and `bfinance_source` from the actions engine instead.
8. Every frame carries the D5 attrs. Do **not** assert on `bfinance_trendlyne_requests` for an OHLC frame — that
   attr is set on the snapshot frame only. Prove zero marginal requests by counting transport calls with the
   existing recorder before and after the member access.

**Verify.**
```
uv run pytest tests/test_ticker_consensus_members.py tests/test_ticker_isin_and_splits.py tests/test_ticker_insider.py -m "not live" -q
uv run pytest tests/test_ticker_trendlyne_sections.py tests/test_trendlyne_stock_page.py tests/test_dropin_actions.py -m "not live" -q
```

### P2 — Single store fetch

**Scope.** `src/bfinance/market/ohlcv.py`, new `tests/test_price_path_single_fetch.py`.

1. Cold `info` performs two `FrameStore.read` + two `FrameStore.write`; the second `cache.get_or_fetch`
   (`ohlcv.py:3375-3383`) is the **prefix read** and must stay. The removable re-wrap that only republishes the
   five `bfinance_cache_*` attrs is `ohlcv.py:3433-3441`. Build those attrs from the first pass instead of the
   re-wrap.
2. **Every attr must still be present and truthful.** `requested_end` / `uncovered_days` describe the caller's
   request, not the prefix window — compute them from the request.
3. Do not touch `_may_serve` (`cache/frame_store.py:558`), the liveness rule, or the write policy.

**Verify.**
```
uv run pytest tests/test_price_path_single_fetch.py tests/test_ohlcv_cached_window.py tests/test_cache_status_provenance.py -q
```

### P3 — `download()` honesty and shared client

**Scope.** `src/bfinance/download.py`, new `tests/test_download_param_honesty.py`.

1. `ignore_tz`, `progress`, `session`, `timeout` are currently accepted and never read. `timeout` is wired to the
   per-Ticker history call. `session` non-`None` raises `NotImplementedError` naming httpx. `ignore_tz` and
   `progress` either work or raise — no silent acceptance.
2. Build **one** cache/client per batch and pass them through the `cache=` / `screener_client=` seam added in F1,
   instead of one per symbol.
3. Per-symbol failure isolation and the all-`NaN` column contract must not change.

**Verify.**
```
uv run python -c "import inspect, bfinance as bf; print(inspect.signature(bf.download))"
uv run pytest tests/test_download_param_honesty.py tests/test_dropin_history.py -q
```

### D1 — Parity report and doc truth

**Scope.** `docs/YFINANCE_PARITY.md` (new), `docs/FUNCTIONALITY.md`, `docs/API_REFERENCE.md`,
`docs/YFINANCE_MIGRATION_GUIDE.md`, `README.md`, `src/bfinance/ticker.py` (docstring only).

1. Write `docs/YFINANCE_PARITY.md`: the measured member table (present / present-but-empty / absent), the three
   tiers, the refusal table from §3, and the swap-boundary fixes — every number dated.
2. Correct the README's "100% Drop-in Equivalent" claim to say what is actually true.
3. Add the new members to `FUNCTIONALITY.md` §3.1 and the migration guide's mapping table, with divergences stated
   in the row (flag required, date basis, 500-row cap, NaN cells).
4. Fix `ticker.py`'s `info` docstring key count to match a live measurement.

**Verify.**
```
uv run pytest tests/test_no_lying_docs.py tests/test_public_surface.py -q
uv run python -c "import pathlib; t=pathlib.Path('docs/YFINANCE_PARITY.md').read_text(encoding='utf-8'); assert 'eps_trend' in t and 'sustainability' in t; print(len(t), 'bytes')"
```

---

## 6. Verification protocol

1. Each worker runs its own `Verify:` block and reports the **actual output**, not a summary.
2. The orchestrator re-runs every `Verify:` line on the settled tree. A worker's report is a claim.
3. Final gate, twice, from a fresh `BFINANCE_HOME`:
   `uv run pytest tests/ -m "not live" -p no:cacheprovider -o addopts= --asyncio-mode=auto -q`
   Only acceptable failure: `tests/test_exceptions_alias.py::TestTheDocumentedLimitIsTrue`.
4. Hermetic check: `uv run python tests/hermetic_offline_suite.py` on a cold home.
5. Any perf claim must be **measured on this machine and recorded**, not projected.

## 7. Explicitly out of scope

- F&O / options / derivatives (D9).
- Any pacing-floor change, failure caching, cache narrowing, `_may_serve` loosening, or Ticker-level memo without a
  clock seam.
- Parallelising the bhavcopy / trendlyne / screener walks.
- Lazy imports for cold-start (needs `test_public_surface.py` read first; separate change).
- `download()` whole-market batching across symbols (biggest remaining win, but it changes failure isolation).
- The six corporate-action `info` keys (D10).
- Fixing `debtToEquity` / `heldPercentInstitutions` basis: a naming decision, not a patch.
