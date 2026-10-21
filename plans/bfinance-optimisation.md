# Plan: bfinance performance optimisation

Slug: `bfinance-optimisation`. Status: ready for adversarial review. Every number below was measured
on this machine on 2026-10-13 (TCS unless stated), and the four load-bearing claims were re-verified
by the orchestrator rather than taken on trust.

## The problem, measured

`Ticker("TCS").info` takes **184s**. Root cause, one line: **`OHLCVEngine` is the only major component
in the codebase with no cache at all.** `SQLiteCache` is constructed in `Ticker.__init__` and wired to
`ScreenerClient` and `TrendlyneClient`, but `market/ohlcv.py` contains zero cache code, and
`ticker.py:758` calls `history(period="1y")`, which lands in `nse/bhavcopy.py` — which downloads
**262 whole-market archive files, one per calendar weekday**, behind a 350ms pacing floor
(262 × 350ms = 92s of pure floor). A second identical `info()` re-does all 262 (measured 233.81s then
232.89s).

Second-order, CPU: a per-symbol sweep costs **83.6ms**, of which **92% is `ScreenerHTMLParser`**, and
62% of that is 13 whole-document `soup.select()` calls (soupsieve measured 6–12× slower than
`find_all`). ≈42s of CPU per 500-symbol sweep.

## The blocker that shapes the plan (independently re-verified)

`SQLiteCache` **cannot hold a DataFrame**. Measured directly:

```
c.set('ohlcv_TCS_1y', df)   ->  WARNING "Skipping cache set: unserializable payload"
c.get('ohlcv_TCS_1y')        ->  None
```

So "just wire OHLCVEngine to SQLiteCache" is a **silent no-op** — every write fails, the read misses,
and the only symptom is a log line. Three more traps, each measured:

| claim | measurement |
|---|---|
| JSON erases provenance | `pd.read_json(df.to_json()).attrs == {}` |
| **pickle preserves it** | `pickle.loads(pickle.dumps(df)).attrs == df.attrs`, index intact |
| `pd.concat` of differing attrs wipes them | `pd.concat([a,b]).attrs == {}` |
| handing one frame to two callers corrupts both | shared list + shared data |

**Consequence:** the storage for price frames must be new (pickle-backed), and every cache read must
return `.copy()` (verified: `.copy()` isolates both the data and the shared mutable list).

## Task graph

### Wave 1 — independent, parallel

**G0 — Green the tree. (BLOCKING, first.)**
All **9** failures are located (momus, verified by execution). Eight are **stale order assertions**
left by the reorder that put `marketlens-close` ahead of `nse-bhavcopy` — 5 in
`test_ohlcv_price_sources.py`, 3 in `test_sources_requires_columns.py`, each asserting *which vendor
served*. The ninth is the pre-existing `tests/test_exceptions_alias.py::TestTheDocumentedLimitIsTrue::test_a_yfinance_except_clause_does_not_catch_a_bfinance_raise`.

**The live correctness fix that must land with it (SUPERSEDED — see the DONE note below).**
The original instruction here was to wire `ohlcv.py:1151`'s signature default to
`_DEFAULT_PRICE_COLUMNS`. **That was wrong and has been reversed twice over:**
- `requires is not None` is what routes a call into `_narrow_price_frame` (`ohlcv.py:1562`). Flipping
  the default would send **every** `history()` down the narrow path and drop `Adj Close`, `Dividends`
  and `Stock Splits`.
- The real defect was elsewhere: `_fetch_cascade_bars` passed the **raw** `requires` (`None`) to
  `plan()`/`resolve()` while `:2140` independently computed `required = ("Open","High","Low","Close")`.
  With `marketlens-close` first, `resolve()` committed to it and the post-hoc column check then
  **raised `RealOHLCUnavailableError` — a hard refusal with no failover** — while `nse-bhavcopy`,
  which publishes every column, sat unasked behind it.

**DONE — the shipped fix** (`ohlcv.py:2105`), passed to both `plan()` (`:2115`) and `resolve()` (`:2125`):
```python
cascade_requires = tuple(requires) if requires else ("Open", "High", "Low", "Close")
```
Measured before: `RAISED RealOHLCUnavailableError`, `calls: ['marketlens-close']`.
After: no raise, `calls: ['nse-bhavcopy']`, `marketlens-close: skipped_columns`, full OHLCV.
The dead `_DEFAULT_PRICE_COLUMNS` constant and its false docstring were **deleted**.
The signature default remains `requires=None` and must stay that way.

⚠ **An earlier attempt at exactly this wiring produced 115 failures and was reverted without being
diagnosed. That was an orchestrator error. Do not repeat it: wire it, run the full suite, and if
failures appear, DIAGNOSE EACH ONE and report which are legitimate (tests stubbing sources without
`provides` metadata) before touching anything.** `provides=()` is defined as "unfiltered", so a stub
without `provides` should not be skipped — if it is, that is a real bug in `missing_columns`, not a
stale test.
Verify: `uv run pytest tests/ -m "not live" -p no:cacheprovider` → exactly ONE failure, the node id
above (closed-form, not prose).
Rules: rewrite each stale assertion with a NEW measurement; never edit an expected tuple to match new
behaviour without proving the new behaviour is correct; never delete an assertion.

**A1 — Promote `CacheLookup.status` onto every frame.**
Zero latency change; it is the **instrument** every later optimisation needs. Today the status is
discarded at `screener/client.py:342,410,520,609,741` (`cached is not None` and the status is thrown
away). Publish `bfinance_cache_status` on cached frames so a reader can always tell a cached answer
from a live one. `ScreenerChartEngine.parse_chart_json_to_dataframe` (`screener/charts.py:31-57`)
currently sets **no attrs at all** — close that gap or the chart cache must stay banned.
Verify: a frame read from cache carries the status; with the cache disabled the same assertion holds
with `"miss"`; a mutation that drops the attr turns the test red.

### Measured outcomes (orchestrator-verified, not worker-reported)

| task | status | my own verification |
|---|---|---|
| A1 cache-status provenance | **DONE** | 28 passed (`test_cache_status_provenance.py`), 281 passed (`test_screener_offline` + `test_ownership`) |
| A2 parser `select`→`find_all` | **DONE** | mutation-tested; **1.21x measured** (56.59 → 46.68 ms/call), not the projected 62% |
| A2 hardening | **DONE** | my mutation went **1 → 3** failures; found 2 previously unguarded composition paths |
| B1 frame store | **DONE** | 29 passed; round-trip reproduced (list+dict attrs survive); aliasing guarded |
| C1 bhavcopy cache | blocked on G1 | `ohlcv.py:12` imports `bhavcopy_range` as the only bhavcopy entry; narrowing is inside `_fetch_bhavcopy_bars`, so C1 is a writer in G1's file |
| D1 1y memo | blocked on G1 | same file |

**Honest correction to the A2 projection.** The recon estimated ~62% of `parse_full_profile` was
selector cost (implying ~24 s saved per 500 symbols). Measured end-to-end on `company_itc.html`:
**56.59 → 46.68 ms/call = 1.21x = 5.0 s per 500-symbol sweep.** The projection extrapolated from
isolated `select('a')` vs `find_all('a')` microbenchmarks; in the real function soupsieve's compiled
selector cache and the document walk (which `find_all` also pays) absorb most of the gap. Report 1.21x.

**Two honesty gaps found and recorded, NOT fixed:**
1. `market/ohlcv.py` has zero `bfinance_cache_status` references, so an `ohlc="synthetic"` OHLCV frame
   cannot report that the screener chart row behind it may be up to 6 h old.
2. `get_company_profile` returns a pydantic model, which has nowhere to carry an `attrs` dict, so it
   cannot publish a cache status at all. Documented in its own comment; needs an owner.

**A2 — `soup.select` → `find_all` in `screener/parser.py`.**
Pure CPU, no I/O. ~24s per 500-symbol sweep. **Mechanical but NOT a blind sed** — four measured traps:
`.company-profile a` is nested inside `.company-info a`, so the comma-union measures **7** where a naive
sum of `find_all` halves gives **13 (+86%)**, and it feeds bse_code/nse_symbol/website;
`li.select_one(".name")` is scoped to `#top-ratios li`, and document-wide `find_all` returns **9×** the
nodes; `:not()` with class predicates (`:425-431`) has **no `find_all` equivalent** and must be
hand-filtered; `A or B` at `:367` is a short-circuit fallback, not a union.
**Verification trap (momus R4):** `company_itc_full_columns.html` returns **0 matches for ALL 16**
selectors tested, so a refactor validated only against it compares empty to empty and proves nothing.
Only **two** fixtures in the repo match anything — `company_itc.html` and `company_tcs.csv`'s sibling
`company_tcs.html`. Use those two **plus at least one hand-authored fragment for the `:not()` site**
(`parser.py:425-431`), which no existing fixture covers.
**Oracle for "did the parse change?":** `src/bfinance/screener/parser.py` is **unmodified vs HEAD**, so
`.scratch/clean_head/src/bfinance/screener/parser.py` is a valid pre-change reference (confirmed by
momus; `pyproject.toml` pins `testpaths=["tests"]` so the duplicate tree is never collected by the
plan's Verify commands).
**Scope note (momus, non-blocking):** the plan's earlier list named 5 of the select-family sites;
`:166, :287, :288, :293, :407, :434` were unlisted. Sweep all of them, not only the named ones.
Verify: `parse_full_profile` output **field-by-field identical** to the pre-change parse on both
fixtures where selectors fire; each rewritten site asserted on values (not counts) against
`soup.select(original_css)` on a fixture where **both are non-zero**; an empty-match negative control;
each new test red on a named mutation.

### Wave 2 — the storage blocker (serial; everything in Wave 3 depends on it)

**B1 — Price-frame storage that preserves `attrs`.**
New module `bfinance/cache/frame_store.py` (pickle-backed, per verified behaviour), **not** a change
to `SQLiteCache.set`'s handler (metis: making it raise breaks every other caller; changing it to
succeed silently is worse). Also update `cache/__init__.py`'s `__all__`.
⚠ **R5 (momus): it must use its OWN table or its own DB.** `bfinance_cache.payload` is
`TEXT NOT NULL` (`sqlite_cache.py:130`) and `set()` JSON-encodes with `allow_nan=False` (`:218`), so a
pickled `bytes` payload hits the same `TypeError` and is **silently skipped** — re-creating the exact
bug this module exists to fix. Threat model addition: `pickle.loads` on read means the cache file at
`~/.bfinance/cache.db` (`:114`) becomes a deserialisation surface — state that explicitly and keep the
file user-owned.
Key = (symbol, exchange, series, resolved start, resolved end).
Must publish: `bfinance_cache_status`, a `fetched_at`, and an **observed-coverage-end that is distinct
from the requested end**.
**Freshness ownership (R6 — one rule, both layers):** the **store** owns TTL and the **memo** owns
freshness. Neither layer may memoise a window that contains today. Stated once here so the two Wave-3
workers cannot ship incompatible policies.
Verify: pickle round-trip preserves every attr (assert it); a frame stored and read back carries
provenance AND `price_basis` unchanged; a read returns an object whose mutation does not affect the
next read; storing into the shared `SQLiteCache` is proven NOT to happen (assert the frame store uses a
different table/DB).

### Wave 3 — the headline wins (parallel; all depend on B1)

**C1 — Cache the bhavcopy range.** The 184s → ~1s win. Must **not** fabricate a liveness signal: a
cached frame replayed at 14:00 Tuesday must not read as "NSE is up, TCS hasn't traded". Publish the
observed-coverage-end and label missing-dates as observed at fetch time; a request beyond coverage
**must refetch or record the gap**, never return a clean short frame with zero missing dates.
Never cache a 404 (a CDN 404 would look permanently like a holiday) and never cache a failure.
⚠ **R3 (momus): do NOT hardcode 262.** `iter_trading_days` skips weekends only (`bhavcopy.py:74`, no
holiday calendar), so the count drifts every weekday. Assert first call count `== len(iter_trading_days(start, end))`
and **second == 0**. Harness: the `_Fetch` pattern at `tests/test_ohlcv_price_sources.py:473`.
Verify: transport call count `len(iter_trading_days(...))` → 0 on replay; coverage-end attr present and
distinct from requested end; cache-disabled negative control passes with `status == "miss"`; a mutation
that drops the coverage attr turns a test red.

**D1 — Process-level memo for the 1y frame.** Per-instance is **useless for the motivating case**:
`download.py:68` builds N distinct Tickers, so N instance-memos and zero sharing. Must: key on the
**resolved window** — and that requires **adding a clock seam to `_window_start`**, which today calls
`pd.Timestamp.now(tz=IST)` inline (`ohlcv.py:3045`) with no injectable clock, so a memo without a
hoisted window silently serves yesterday's window past IST midnight. Key on every parameter that
changes the frame's **shape** (`requires`, `auto_adjust`, `adjust_corporate_actions`, `sources`,
`ohlc`) so a narrow `info` frame is never served to a later full `history()` (silent column loss);
never memoise a failed/degraded fetch (that latches an empty answer for the life of the object — the
defect the `_profile_degraded` comment exists to prevent); return `.copy()` to every caller (verified
to isolate both the data and the shared mutable list in attrs).
Verify: **second `info()` at least 10× faster than the first** (a ratio, not "materially faster"); two
calls 1ms apart across an **injected clock** return the same window; a full `history()` after a narrow
`info()` returns all six columns; a degraded fetch leaves the memo empty; mutating one returned frame
does not affect another.

**E1 — Cache `resolve_company_id` + fix the chart-cache ordering.** Symbol→id is stable identity, not
a price. The big one is an **ordering bug**: `get_chart_timeseries` resolves the id at `:511` **before**
reading the chart cache at `:520`, so **a chart cache HIT still costs a live HTTP request** — the cache
is effectively defeated. Populate the resolve cache *from* the profile fetch; do **not** route the
profile fetch through it (that would add a network request to the profile path — a refactor that is a
latency regression). Normalise the key so `TCS`/`tcs`/`TCS.NS` share one row. Never reintroduce
`search()` — robots-disallowed, source of a past silent-empty-profile incident.
Verify: a chart-cache HIT costs **zero** network calls (the assertion that goes red if anyone moves
`:511` back); key normalisation gives one row; `get_company_profile` request count is unchanged.

### Wave 4 — close out

**F1 — Full verification + oracle review.** Run the whole offline suite, the hermetic runner, and
compare against the recorded baseline; then an oracle pass over the final diff for architecture and
regression risk.

## Decisions taken (metis's open questions, answered)

1. **500-symbol sweep is in scope** → D1 must be process-level.
2. **BSE (`.BO`) support must keep working** → **the "route `info` through marketlens" idea is
   EXCLUDED.** Two measured reasons: `marketlens` returns zero rows for BSE shapes, so `.BO` would
   silently lose coverage; and `marketlens.py:193` is a **100ms floor = 10 req/s against NSE's
   documented 3 req/s** — routing `info` and a sweep through it would amplify a pre-existing compliance
   deviation. That deviation is raised as separate work, not smuggled in here.
3. **Source order intent is recorded**: trendlyne first where its basis fits; bhavcopy last for speed;
   basis filters what actually gets called.
4. **A new storage mechanism is in scope** — pickle-backed, required by the verified blocker.

## MUST NOT (from metis, verified against source)

- Do not touch any `min_request_interval_ms` — `trendlyne/client.py:774` (1000ms) is a
  `Crawl-delay: 1` compliance number pinned by `tests/test_trendlyne_pacing.py`; `bhavcopy.py:1048`
  (350ms) is under NSE's 3 req/s.
- Do not change a source's registered basis, or the `"as_traded_split_bonus_adjusted"` string.
- Do not change `requires` semantics (`cascade.py:305-320`).
- Do not fabricate O/H/L, resample bars, or back-fill a narrow result from a different basis.
- Do not cache a failure, a null row, or a 404.
- Do not deliver the same cached object to two callers.
- Do not make `SQLiteCache.set` raise.
- Do not delete or adjust an assertion to get green.

## Risks the implementers must handle, not discover

- A cache HIT must never read as a liveness claim about the exchange.
- The narrow `info` request must not poison a later full `history()` call.
- The parser refactor must not be validated on a fixture where nothing matches.
- `pd.concat` in any new path silently empties `attrs` — re-assert after it.
- `.scratch/clean_head/` contains a second copy of the source tree (metis); measurements must be taken
  against `src/`.