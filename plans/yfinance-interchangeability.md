# bfinance: make the yfinance swap SAFE (not wide)

Revision 1. Operator decisions: adopt yfinance's TTM labelling and publish the convention in
attrs; this pass makes the swap safe, widening deferred.
Baseline: `0910257`, tree clean, offline suite 2334 / 0 failures / 1 skipped.

---

## What the evidence says (measured, not assumed)

yfinance 1.7.0 works from this venv (3.2 s fetch, 163 `info` keys, 160 populated). The 32-of-48
failing parity tests are real incompatibilities, not a network artifact.

| finding | number | source |
|---|---|---|
| yfinance `Ticker` public members | 98 | `dir(yf.Ticker)` live |
| bfinance `Ticker` public members | 67 | `dir(bf.Ticker)` live |
| **missing** | **65**, incl. all 26 `get_*` aliases | live diff |
| `info` populated keys | 160 yfinance vs 76 bfinance | live |
| `FastInfo` keys | 20 yfinance vs 16 bfinance | live |
| parity tests failing | 32 of 48 | live run, 5 files |

`README.md:26` claims "100% Drop-in Equivalent" and `:55` says "simply replace
`import yfinance as yf` with `import bfinance as yf`". Both are currently false.

---

## The reason this pass is "safe", not "wide"

Three of the divergences do not degrade — they **crash**, and one of them silently returns a
wrong number:

1. `t.info` works after the swap; `t.get_info()` raises `AttributeError`. 26 aliases.
2. `Ticker("RELIANCE", requests.Session())` raises `TypeError` at `ticker.py:69`
   (`if cache_ttl_hours > 0`). Measured. Worse, `session=` is accepted at **4** sites
   (`ticker.py:61`, `tickers.py:55`, `tickers.py:69`, `download.py:189`) and forwarded to
   **none** — silently ignored.
3. **The dangerous one.** `ohlcv.py:1512` sets `applied = False`, emits a warning, and
   **continues**; `ohlcv.py:1533` then scales O/H/L/C by the *partial* factor and
   `ohlcv.py:1550` drops `Adj Close`. Measured on ITC's real 2025-01-06 demerger: as-traded
   fell 8.0876%, correct factor 1.047332, applied 1.000000. A backtest across that date gets a
   **-8% drawdown that never happened**, with no `Adj Close` left to detect it and the only
   evidence in `df.attrs`, which ordinary code never reads.

Widening `info` from 76 to 160 keys helps a `.get()` caller who is already fine. Fixing these
three stops a crash and a wrong number. Hence: safe first.

---

## Task graph

### S1 — the `auto_adjust` flip, atomic, with the fifth refusal case

MUST land as one commit. The suite is currently **guaranteed red** if the flip is split:
`tests/test_live_yfinance_parity.py:59-61` asserts `Adj Close` is ABSENT at the default while
`tests/test_ticker_yfinance_compat.py:68-69` asserts it is PRESENT. Exactly one can hold.

- default `True` on all five layers (`Ticker.history`, `history_async`, `fetch_history`,
  `Tickers.history`, `download`)
- `tests/test_adjust_default_drift.py:41` `EXPECTED_DEFAULT` → `True`; keep the reflection
  guard. Its docstring at `:6-8` is already false (says `True` raises because bhavcopy carries
  no corporate-actions series) — commit `0910257` made that untrue.
- **Add the fifth refusal case**: `auto_adjust=True` with a non-`None`
  `_incomplete_adjustment(...)` must RAISE `AdjustmentNotAvailableError`, not scale by a
  partial factor. Without this, the flip makes NTPC-class symbols return a wrong price level
  by default. The four existing cases are `ohlc="synthetic"`, `actions=False`,
  `actions_source in {None,"none"}`, `actions_source == "screener"` — none of them is
  "known-incomplete".
- `ohlcv.py:1306-1323`: empty bars + `auto_adjust=True` must return an **empty frame**, not
  raise, or `Ticker("NONEXISTENT_TICKER_9999").history()` breaks. `yfinance` returns empty;
  `tests/test_ticker_yfinance_compat.py:221` asserts `.empty`.
- Reconcile the two contradictory column assertions in the same commit.

Verify: `uv run pytest -m "not live" -q` green and count >= 2334; a live
`Ticker("NTPC").history(period="1mo")` either returns bars with a real `Adj Close`-derived
scale or **raises** — never silently partial; `Ticker("NONEXISTENT_TICKER_9999")` returns
empty; the two contradictory tests are reconciled and both pass.
Non-vacuity: make the fifth case `if False:` and show the NTPC-shaped test goes red.

### S2 — the 26 `get_*` aliases

Pure one-line delegations to the properties that already exist. Plus the alternate spellings
`incomestmt`, `balancesheet`, `quarterly_balancesheet`, `ttm_cashflow`, `ttm_incomestmt`.
Add an offline reflection test pinning `set(dir(yf.Ticker)) - set(dir(bf.Ticker))` so this
cannot regress; it needs no network.

Verify: the reflection test is green; every name in the pinned set is either present on
`bf.Ticker` or is on an explicit, justified allow-list (e.g. `shares`, which is dead upstream).

### S3 — the `session` collision, and delete the monkey-patch

- `Ticker("X", <requests.Session>)` must not `TypeError`. Detect a non-numeric
  `cache_ttl_hours` and route it to `session`.
- `session=` must either reach a client or raise a clear `NotImplementedError`.
  **Accept-and-ignore is banned** — it is the current behaviour and it is the worst option.
- `_apply_ticker_bool_shim()` monkey-patches `Ticker.__init__` at import
  (`tickers.py:14-41`, runs on `import bfinance` via `__init__.py:46`), so
  `inspect.signature(bf.Ticker)` returns `*args, **kwargs` and the real parameters are
  invisible. **Delete the patch and fix the real signature.**
- Note for whoever implements: `ScreenerClient` is httpx-based and has no `session`
  parameter, while yfinance's is `requests`/`curl_cffi`. Scope it honestly.

Verify: `inspect.signature(bf.Ticker)` lists real named parameters; `Ticker("X", object())`
does not raise `TypeError`; a reflection test pins it.

### S4 — un-pin the green tests that enforce the divergence

These pass today, so they will never surface as failures, and each one locks in a wrong claim:

- `tests/test_ticker_yfinance_compat.py:116-118` — `assert targets is None`, justified "no
  analyst feed upstream". **yfinance serves real targets for that symbol** (measured:
  current 1187.0, high 1890.0, low 1350.0, mean 1676.85). The test enforces a lie.
- `tests/test_live_yfinance_parity.py:79-107` — six `pytest.raises(NotImplementedError)`
  locking in hard failures where yfinance returns DataFrames.
- `tests/test_live_yfinance_parity.py:18-22` — claims to verify all yfinance `FastInfo`
  attributes, lists **15 of 20**. Unfalsifiable for the 4 missing keys, in a repo with a
  doc-truthfulness gate.
- `tests/test_parity_info.py:244-246` — loose `v is None or approx` loop, completely subsumed
  by the strict re-asserts at 247-249. Dead assertions.
- `README.md:26` / `:55` — the "100% Drop-in Equivalent" claim. Either make it true or stop
  making it. Recommend rewording to what is actually true.

Verify: `uv run pytest tests/test_no_lying_docs.py -q` green; no test in the suite asserts
`is None` for a feature yfinance populates; the README claim is either true or removed.

### S5 — TTM labelling, per the operator decision

yfinance's `ttm_income_stmt` single column is `Timestamp('2026-06-30')` — it invents exactly
the date `be218a7` refused to invent. Adopt the convention and **publish it**: set
`attrs["bfinance_ttm_convention"]` naming the trailing quarter-end and flagging the column as
TTM rather than a fiscal period. Serve `ttm_income_stmt`, `ttm_cash_flow`,
`ttm_balance_sheet` where the data exists.

Two of the four raising accessors are **not** philosophical and should just be fixed:
`quarterly_cash_flow` and `ttm_cash_flow` return `(0,0)` in yfinance for NSE, so an honest
empty frame is both truthful and yfinance-identical. `quarterly_balance_sheet` returns
`(77,3)` and needs a parser change, not a debate.

Verify: a TTM frame carries the trailing quarter-end AND the attrs convention key; a
fiscal frame does NOT carry that key; the six pinning assertions are gone.

### S6 — two self-violations of the repo's own rules

- `market/corporate.py:311` — `"Earnings Date": ["N/A"]` is the "plausible empty" the culture
  forbids, and yfinance fills the key with real dates. Use `None`.
- `market/corporate.py:280` — `institutionsCount` is `float("nan")`. `ticker.py:903-907` argues
  at length that a NaN "is a *number*: it propagates silently through arithmetic, prints as a
  plausible cell", and used that to force `dtype=object` in `valuation_measures`. Use `None`
  with `dtype=object`.
- Add the 4 missing `FastInfo` keys: `regular_market_previous_close`,
  `ten_day_average_volume`, `three_month_average_volume`, `year_change`.

Verify: a frame test on synthetic input; no `NaN` remains in `major_holders`; the 4
`FastInfo` keys return real values or `None`, never `NaN`.

---

## Explicit non-goals

- **Do not** unify `download`'s tz-naivety with `history`'s tz-awareness. `download.py:142-148`
  reproduces yfinance's inconsistency deliberately and correctly. "Cleaning it up" breaks
  parity.
- **Do not** remove yfinance's "retired" `info` keys. The retirement is **not implemented** in
  1.7.0; they are still present and populated for NSE. Removing them would be strictly wrong.
- **Do not** widen `info` to 160 keys this pass. ~40 are pure arithmetic and worth doing next,
  but a caller using `.get()` is not crashing today.
- **Do not** build a benchmark harness, a surface-adapter abstraction, or a plugin registry.
  65 one-line delegations beat one indirection.
- **Do not** claim any performance comparison against yfinance. Different upstreams, different
  geographies, different throttles — any such number is not hard to produce and means nothing.
- **Do not** add `download(proxy=)` handling as new work; note it only as a yfinance-1.7 removal
  we are more permissive about.
- No intraday intervals (a real data limit). But DO make `download.py:52` treat
  `NotImplementedError` per-symbol rather than aborting the whole batch — 3 lines, most of the
  user-visible benefit.

## Risks

| Risk | Mitigation |
|---|---|
| The flip ships without the fifth refusal case, and NTPC-class symbols return wrong prices silently | S1 requires it in the SAME commit; the non-vacuity mutation is the control |
| Splitting the flip leaves the suite red (the two contradictory assertions) | S1 is explicitly atomic; momus checks this |
| Adding 26 aliases as thin wrappers that return wrong shapes | Each delegates to a property that already exists; the reflection test pins presence, not behaviour — say so |
| `session` is "fixed" by accepting-and-ignoring | Explicitly banned; the alternative is a loud `NotImplementedError` |
| Un-pinning the green tests is read as deleting coverage | Each replacement must assert the TRUE current behaviour and say what changed and why |
| TTM date invention reintroduces a fabrication | Mitigated by publishing the convention in `attrs`; a TTM column is distinguishable from a fiscal one |
