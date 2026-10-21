# bfinance: close the yfinance swap, and stop wasting upstream requests

Revision 1 — after momus/metis-style recon. Baseline `58dc802`, tree clean,
offline suite **2367 / 0 failures / 1 xfailed**. Nothing pushed.

Operator goal: "find what are the problems that still exists and find them and fix them also
find optimisation you can do. overall i want complete replacement of yfinance for indian market"

---

## The honest ceiling (metis)

**"Complete replacement" is not reachable and must not be written down anywhere.** Two bounds:

1. yfinance 1.7.0's own surface has rotted. `Ticker.shares`/`get_shares()` raise
   `YFNotImplementedError` unconditionally (`yfinance/scrapers/fundamentals.py:36-39`;
   `self._shares = None` at `:19` is never reassigned). `download(proxy=)` was removed.
   Cloning a dead method is reproducing a defect, not achieving parity.
2. ~40% of the surface has no Indian upstream at all: no free sell-side consensus feed, no
   Form-4-equivalent insider table, no free ESG scorecard.

The reachable target: **indistinguishable from yfinance where Indian exchanges publish
something; loudly typed where they do not.**

## Work sizing — the number that matters

Of the 65 missing `Ticker` members (98 yfinance vs 67 bfinance):

| class | n | action |
|---|---|---|
| pure delegations to a member that ALREADY EXISTS | **24** | ADD — one line each |
| delegations that would LIE | **5** | **DO NOT ADD** — `get_shares`, `get_shares_full`, `get_funds_data`, `get_isin`, `get_sec_filings` |
| no data source exists | **36** | **DO NOT ADD** — analyst estimates, recommendations, insider tables, ESG, earnings_dates, `live` |

So **24 of 36 `get_*` aliases are one-liners; 14 need real sources we do not have.**

---

## Task graph

### Wave A — disjoint, parallel

**A1 — `bfinance.exceptions` as a duck-typed alias module** (new file only)

yfinance is **dev-only** (`pyproject.toml:42-44`), not a runtime dependency (`:31-39`), so
`BFinanceError` CANNOT subclass `YFException` without inverting the premise — a drop-in
replacement that cannot import without the thing it replaces. Only the *aliasing* direction is
achievable, and it must be stated honestly.

- New `src/bfinance/exceptions.py` re-exporting the 6 classes in `utils/exceptions.py` under
  yfinance names. **ZERO new classes** — `test_public_surface.py:934-967` requires exactly one
  direct `Exception` subclass in the whole tree, and `:880-892` filters on
  `value.__module__`, so a re-export is not counted.
- Names to provide: `YFException`, `YFTickerMissingError`, `YFTzMissingError`,
  `YFPricesMissingError`, `YFDataException`, `YFRateLimitError`, `YFInvalidPeriodError`,
  `YFNotImplementedError`. The four with no bfinance counterpart get an **honest stub that
  raises**, never a fake value.
- NOT imported by `__init__.py`. `reach_report` probe must show
  `bfinance.exceptions not in sys.modules` after `import bfinance`.
- The module docstring must state the limit plainly: this makes `from bfinance.exceptions
  import YFRateLimitError` resolve; it does **not** make `except yf.exceptions.X` catch a
  bfinance raise, because that clause binds yfinance's class object.

Verify: `uv run pytest tests/test_public_surface.py tests/test_error_vocabulary.py -q`
plus a subprocess assertion that the module is absent from `sys.modules` after `import
bfinance`, WITH a negative control.

**A2 — Promote `CacheLookup.status` on the screener cache path** (`screener/client.py`,
`cache/sqlite_cache.py`)

`screener/client.py:402, 512, 601, 733` call `.get()` and discard the status that
`sqlite_cache.py:155` already computes. `ownership.py:1699-1700` shows the consumer pattern.
**This is the prerequisite for measuring every other optimisation** — without it nobody can
tell whether the 1h profile cache is hitting. Zero latency change.

Verify: an offline test asserting the status reaches the frame; a mutation that discards it
must redden.

**A3 — Inject one `CorporateActionsClient`; de-`iterrows` the market-wide actions check**
(`market/corporate.py`, `nse/corporate_actions.py`)

`corporate.py:455` constructs a client per `resolve_actions_async` call because
`ohlcv.py:1100` stores the constructor arg and `ticker.py:73` never passes one. Separately,
`corporate_actions.py:1011` uses `iterrows()` + `row.get()` over the market-wide frame — every
corporate action in India when `symbol=None`. Both are pure cleanups with no behavioural change.

Verify: a test asserting the injected client is the one used (identity); mutation → red.

**A4 — Docstring lie at `ticker.py:236`** (`ticker.py` docstring only)

Says *"Unified 76-key metrics dictionary"*; measured live it is **86** (`INFO_VALUE_UNITS`
declares 76, and `build_info_dict` injects 10 raw screener labels at `quotes.py:371-374`).
One word. `test_no_lying_docs.py` cannot catch it — its cross-reference lint is scoped to
`OWNED_MODULES` = `ai/context.py`, `market/derivatives.py` only (`:43-46`, `:277`).
Do NOT widen `OWNED_MODULES` in this pass; its own docstring (`:304-311`) says widening
immediately fails on three pre-existing unresolved refs, and a partially-widened lint is
disabled within a week.

### Wave B — `ticker.py` + `tickers.py`, ONE agent, sequenced internally

Everything here touches the same two files, so it is one bundle rather than four racing agents.

**B1 — Remove the import-time `Ticker.__init__` patch; make `session` real**

`tickers.py:14-41` replaces `Ticker.__init__` at import time (fired by `__init__.py:53`),
so `inspect.signature(bf.Ticker)` reports `(ticker, cache_ttl_hours=24.0, *args, **kwargs)` —
hiding `timeout`, `proxy`, `session` and `raise_errors` from `help()`, IDEs and any
introspection shim.

- Move the bool-detection into the real `__init__` body (`ticker.py:55-63`), add
  **keyword-only** `multi_level_index: bool = True`. Keyword-only is what makes the signature
  honest: it declares that the 2nd positional is not it.
- **Preserve the exact precedence** the shim has: `kwargs.pop("multi_level_index", None)`
  FIRST, so a positional bool only wins when the keyword is absent.
- `tests/test_dropin_history.py:177-192` must stay **untouched and green**. It pins
  behaviour, not location. Do NOT rewrite it.
- **`session` must either reach a client or raise loudly.** It is currently accepted at
  `ticker.py:61` and **never forwarded** (only `cache_ttl_hours`/`proxy`/`cache` reach
  `ScreenerClient` at `:70-72`), and `ScreenerClient` has no `session` parameter while
  yfinance's is `requests`-based. Accept-and-ignore is banned: it converts a loud `TypeError`
  into a plausible-but-wrong object. Either wire it to the httpx transport or raise
  `NotImplementedError` naming the reason.
- Delete `tickers.py:14-41` entirely.

**B2 — `Tickers.download`** (same agent, after B1)

Absent; an `AttributeError` at the swap boundary on a first-line call.
- yfinance's `Tickers.history` is a pure positional forward to `download`. Mirror that:
  `history` becomes `return self.download(...)`.
- `download` names `prepost`, `repair`, `progress`, `timeout` explicitly (they reach
  `download()` only via `**kwargs` today) and uses yfinance's period sentinel
  `'1mo if start & end None'` — the one genuine behavioural divergence from the hard-coded
  `"1mo"` at `tickers.py:76`.
- **Must NOT re-implement `swaplevel`/`sort_index`** — `download.py:222-232` already does it;
  doing it twice double-swaps the MultiIndex.
- Keep `Tickers.history(actions=True)` overriding `download(actions=False)`: yfinance's
  group layer defaults True while the module layer defaults False, and bfinance already mirrors
  that correctly.

**B3 — `.tickers` keying bundle** (same agent, after B2)

Not a one-line fix. Measured: switching the dict to suffixed keys makes `__getitem__` fail in
**both** directions, because `tickers.py:119` normalizes the *incoming* symbol before lookup.
- `tickers.py:68` — key by the uppercased symbol as given
- `tickers.py:119` — drop the normalize on lookup (or add a suffix-stripping fallback)
- `tickers.py:116` — `news()` must key by `self.symbols` (suffixed), matching yfinance
- 4 live tests assert bare keys and must be updated deliberately:
  `test_live_yfinance_parity.py:126`, `test_ticker_yfinance_compat.py:144`,
  `test_exhaustive_screener_and_yfinance.py:396-398`, `test_ticker_yfinance_compat.py:145`

**B4 — the 24 honest delegations** (same agent, last)

Group A only. **Identity-or-nothing rule:** an alias may delegate to an attribute that already
returns a verified-correct yfinance shape, and may **never** synthesize, default, coerce or
reshape. Per-alias test: `type(Ticker.<alias>) is type(Ticker.<canonical>)` AND, on ONE offline
instance, `getattr(t, alias) is getattr(t, canonical)`.

**DO NOT ADD** the 5 liars or the 36 no-source names. Specifically:
- `shares`/`get_shares` — yfinance raises unconditionally; adding a name that cannot work is a
  lie with a working-looking name on it.
- `get_actions` — yfinance returns `['Dividends','Dividends FX','Stock Splits','Capital Gains']`
  filtered to non-zero; bfinance returns `['Dividends','Stock Splits']` with `0.0` fills. A
  delegation would be type-compatible and semantically wrong.
- `get_news` — yfinance takes `count`/`tab` and hits a different endpoint entirely.
- `get_analyst_price_targets` — yfinance returns a dict, bfinance returns `None`.

No `ENDPOINTS`-style table for the aliases: a table with no builder behind it is the
`SNAPSHOT_ONLY_ENDPOINTS` defect (`trendlyne/client.py:212-213`). Use explicit `def`s —
`inspect.signature` then gives the right answer for free.

### Wave C — `download.py` + `market/ohlcv.py`, one agent

**C1 — memoise the whole-market walk across a `download()` batch**

The largest win in the package. Today: a 50-symbol 1y `download()` issues **~13,050** archive
requests where **261** would do. Every symbol in a batch receives identical
`period`/`start`/`interval`, so the key is uniform.

- Hoist the window computation to the batch: `_window_start` (`ohlcv.py:2903`) calls
  `pd.Timestamp.now(tz=IST)` per call, so a batch straddling midnight IST currently produces
  windows one day apart. Hoisting is **required** for a correct memo, not tidying.
- The whole-market design itself is CORRECT (`ohlcv.py:2514-2517` documents why); do not
  redesign it. Only stop re-running it N times.
- Pins that must stay green: `test_nse_bhavcopy_wiring.py:210` (`len(calls) == 1`),
  `test_corporate_actions_wiring.py:385-387` (asserts the request carries
  `start=None, end=None`), `test_bhavcopy_parity_adjust.py:663-664`.

**C2 — one `SQLiteCache` + `ScreenerClient` per batch**

`download.py:68` builds N Tickers; each runs `SQLiteCache._init_db` (3 DDL statements) and a
fresh `ScreenerClient`. 500 symbols = 500 connections + 1500 DDL statements against an
existing database.

### Wave D — `screener/client.py` pooling, after A2

**D1 — replace the per-attempt `AsyncClient` at `screener/client.py:269` with the per-loop
keep-alive already proven at `nse/bhavcopy.py:1061-1143`.**

The client is currently built *inside* `for attempt in range(self.max_retries)` (`:258`), so
every retry pays a TCP+TLS handshake. `bhavcopy.py:1027-1033` records the measured cost of
exactly this: **2.7-5.7 s per file** cold. That figure is on `nsearchives.nseindia.com`,
**not** screener.in — the transfer is an assumption until measured, and must be labelled so.

The randomized UA at `screener/client.py:206` must stay per-*request* (headers are passed to
`.get()`, not pinned on the client) or it stops rotating.

### Wave E — `ticker.py`, after Wave B

**E1 — share one `history(period="1y")` between `info` and `fast_info`.**

`fast_info` calls `history(period="1y")` at `ticker.py:213`; `info` sets
`self._history_for_metadata` at `:256`, which `fast_info` never reads. Touching both — the
normal yfinance pattern — costs **522** archive requests where 261 would do.

The window is NOT the problem: `fast_info` needs ~200 bars and gets ~250. Only 20% over-fetch.
The waste is the duplicated walk.

The memo must honour `_profile_degraded` (`ticker.py:83-85, 144`), which exists precisely so a
cached empty fallback does not latch. Getting this wrong reintroduces the defect that comment
was written to prevent.

---

## Explicit non-goals

- **No `shares`/`get_shares`** — dead upstream, and `quotes.py:190`'s inferred
  `shares_out = int(mcap_inr / cmp)` is derived, not declared, so it must not back it.
- **No `download(proxy=)`** — removed upstream.
- **No intraday intervals, and above all no resampling daily bars into 1h/5m buckets.** That
  would be ~10 lines, would pass every existing test, and would fabricate OHLC. The repo has a
  pinned tripwire for exactly this class: `test_no_lying_docs.py:82-103` exists because a
  seeded-RNG option-chain fabricator shipped and CI asserted the fabrication was correct.
  `ohlcv.py:1428`'s `NotImplementedError` stays.
- **No analyst estimates / earnings_dates / recommendations / insider tables / ESG** — no
  Indian upstream. Any "implementation" would be arithmetic on current price, the exact class
  of fabrication removed in 0.1.3.
- **No bulk-filling the 102 `info` keys.** 28 are structural (no source). Filling them with
  `None` makes `info` look complete while breaking `if key in info` and `screens.py`'s
  `or 0.0` fallbacks. Only add keys carrying a measured value.
- **No `info` key work at all in this pass.** Adding `debtToEquity` would flip
  `screens.py:190-197` from fail-closed to evaluating, changing `debt_free_compounders`
  output. That needs a before/after proof this pass does not have budget for.
- **Do not touch the retry/backoff paths.** `screener/client.py:257-307` has **zero** test
  coverage for 429/backoff. Request *counts* are pinned in five files; backoff is pinned
  nowhere. Treat it as untested code.
- **No `ENDPOINTS`-style alias table**, no benchmark harness, no plugin registry.
- **Do not widen `test_no_lying_docs.py::OWNED_MODULES`** in this pass.

## Risks

| Risk | Mitigation |
|---|---|
| Removing the patch without relocating the bool logic breaks `test_dropin_history.py` | Measured: `cache_ttl_hours=True` would store `default_ttl_hours=True`, failing both the `approx(24.0)` and `multi_level_index` assertions. B1 relocates the logic; that test stays untouched. |
| `session` becomes accept-and-ignore once the patch is gone | Explicitly banned. Wire it or raise `NotImplementedError` naming why. |
| `.tickers` keying breaks `__getitem__` in both directions | Measured and expected. It is a 3-line bundle + 4 deliberate test edits, not a patch. |
| An alias delegates to an attribute with a different shape | Identity-or-nothing rule + per-alias identity test. The 5 liars are named and excluded. |
| `bfinance.exceptions` adds a second `Exception` root | Zero new classes; a re-export is filtered by `test_public_surface.py:880-892`. |
| Batch memoisation changes request counts | Five files pin counts with exact equality; they are the gate. |
| `download`'s 4 live tests in A2/B3 are invisible to `-m "not live"` | Every live test touched must be run explicitly and reported. |
| Screener pooling measured on the wrong host | `bhavcopy.py:1027-1033` is cited as the precedent AND labelled as host-specific. |
