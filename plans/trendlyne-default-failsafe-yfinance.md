# bfinance: Trendlyne default, real failsafe, yfinance-compatible output

Revision 2 — after momus REJECT. Every gap below is an adversarial finding, not a preference.
Repo baseline: `fd89e84`, `-m "not live"` = **2,251 green** (2,391 collected, 140 live).

Operator decisions: adapter indirection; yfinance gaps = statements shape + period
truncation + auto_adjust; failover loud in attrs, quiet in return type.

---

## Corrected findings that change the plan

| # | Correction | Consequence |
|---|---|---|
| 1 | T1's gate `-k "by_module or registration or unwired"` **cannot see** the two tests it breaks. `test_sources_cascade.py:777` asserts `"4 sources"`; `:780-785` asserts the exact 4-name try-order. Both in `TestUnavailability`. The new-file guards live in `TestSubprocessProbes:1022,1041`. | T1 must update those tests; gate becomes the **full offline suite** + `TestSubprocessProbes`. |
| 2 | `to_yfinance()` needs **three** changes, not two: alias de-dup, **column reversal** (`:124-125` preserves caller order, so newest-first cannot hold), and an **explicit TTM decision**. | T2 re-scoped. |
| 3 | TTM: `to_yfinance()` **already drops** TTM (`:118-119`), so xfail 4 *passes today* and is red as a strict xfail. The only reading where all 7 flip: **keep dropping, decide it explicitly, log it in attrs.** | Recorded decision. |
| 4 | Of the 3 "will now pass" live tests, `test_live_yfinance_parity:50` **cannot** flip — it calls `history()` with *no* `auto_adjust` (default `False`) and asserts no `Adj Close`. | Dropped from the claim. |
| 5 | **Four OFFLINE** tests assert `auto_adjust=True` raises, all in the required CI job: `test_nse_bhavcopy_wiring:369,476`, `test_corporate_actions_wiring:412`, `test_parity_history:337`. | These are T4's real targets. |
| 6 | T3 has **no injection seam**. ~130 offline call sites stub `OHLCVEngine(screener, corporate_actions_client=...)`; the cascade's lazy `importlib` bypasses every stub. | Split T3: add the seam + opt-in first, flip the default only once green. |
| 7 | `to_yfinance()` scales **every** row by `1e7`, including `EPS in Rs` -> `Diluted EPS`. yfinance's Diluted EPS is **rupees**, not absolute. No test catches it; wiring makes it the primary path. | T2 must exempt per-share rows. |
| 8 | `test_dropin_financials.py` — named in rev 1 as T2's "no regression" gate — is **the opposite**: entirely `@pytest.mark.live`, asserts the *native* shape throughout. | Moved to an explicit "these change" list. |

Also: region `ticker.py:373-410` holds **four** methods; the 12 delegating properties are at
`:414-472` and are in scope. Extend the region to `373-472`.

---

## Task graph

### T1 — Trendlyne price provider + registration (owns `src/bfinance/sources/`)

New `sources/price_provider.py` — named for the data, not the vendor. Its **own** OHLC reader
over robots-allowed routes. **Must not** import `bfinance.trendlyne`. Registers as a
`price_history` row, **first** in try-order. Binding lands in the same change
(`test_sources_cascade.py:1297` asserts `ADAPTERS` and registry agree bidirectionally).

`UNWIRED_NOTE` must be rewritten **honestly** and must retain four literal strings the claim
tests require: `by_module` (`:65`), `TestIsolation` (`:69`), `DEMERGER` (`:90`), `splic` (`:91`).

Four independent guards confirm the module string is acceptable: `test_sources_cascade.py:1129`,
`test_trendlyne_claims.py:80`, `test_sources_cascade.py:1033` (filename), `:1059` (AST).

**Must also update, or T1 ships red:** `test_sources_cascade.py:777` (4 -> 5 sources) and
`:780-785` (try-order now `["trendlyne-ohlc", "nse-bhavcopy", "marketlens-close", "screener-chart", "vendor-b-adjustments"]`).

Verify:
- `uv run pytest -m "not live" -q` — full suite green, count >= 2251
- `uv run pytest tests/test_sources_cascade.py -k TestSubprocessProbes -q` — new-file guards
- `grep -rn "bfinance\.trendlyne" src/bfinance/sources/` — **zero** hits
- `httpx.MockTransport` test over a **recorded** payload; **negative control** (mutated payload
  must fail) — non-vacuity
- **ES rows must be allowed and AS-TRADED-comparable to bhavcopy, or the default order is a lie.**
  Trendlyne's `PRICE_BASIS` is `as_traded_split_bonus_adjusted` (`client.py:575`) and
  `Basis.comparable()` (`model.py:151`) allows only identical bases. Resolve by measurement
  (side-by-side max |dClose| over >=8 symbols x 2 windows) and record the outcome in the
  registry note. **If the bases are not comparable, Trendlyne may not be first in the default
  as-traded order** — say so and stop rather than shipping a default that splices.

### T2 — Wire `to_yfinance()` into the statement accessors
(owns `src/bfinance/models/statements.py` + `ticker.py:373-472`) — disjoint from T1

Three changes to `to_yfinance()`: (a) alias de-dup, (b) **reverse columns** for newest-first,
(c) **exempt per-share rows** from the `1e7` scale (Diluted EPS is rupees). TTM keeps being
dropped, now deliberately and recorded in attrs.

**Tests T2 must update, all named in rev 2:** `test_stmt_accessor_contract.py:146`
(native labels vs accessor), `:195` (duplicate-label assertion, contradicted by xfail `:212`),
`test_screener_offline.py:594-598` (order-dependent `Total Debt` assertion),
`:600` (TTM dropped).

`to_dataframe()` (native) must survive untouched.

Verify:
- the **7** named strict xfails are converted to real passes, and the self-guard
  `test_every_alarm_is_a_strict_xfail` / `test_the_alarms_still_xfail_...` are **replaced**
  by a guard asserting each of the 7 specific properties
- `uv run pytest -m "not live" -q` green
- `test_dropin_financials.py` is **expected to change** — it is the acceptance evidence, not
  the gate. Run it live and report the delta honestly.

### T3a — Cascade path + injection seam + `sources=` param (opt-in, default unchanged)
(owns `src/bfinance/market/ohlcv.py`, `ticker.py:260-370`, `download.py`, `tickers.py`)

Add the seam **first**: an injectable registry/provider callable on `OHLCVEngine`, mirroring
`cascade.resolve(..., fetch=...)`. Without it, ~130 offline call sites that stub the screener
client are bypassed the moment the cascade is on the path, and "no regression" is unreachable.

Then: `source=` (one) and `sources=` (ordered, failsafe) on `Ticker.history`,
`history_async`, `fetch_history`, `download`, `Tickers.history`. Unknown name refused **before
any request** (`ohlcv.py:698` precedent). Default **unchanged** this task.

`ohlc=` is **kept** as a legacy alias, not replaced. Decision recorded.

New attrs, on populated **and** empty frames:
`bfinance_price_source` (winning **vendor**, not transport),
`bfinance_price_source_attempts` (every source + outcome),
`bfinance_price_source_degraded` (bool),
`bfinance_price_source_detail` (sentence).

**No blending across price bases** — the winner's data is used whole and its real basis
published. `Basis.comparable()` stays strict for same-source joining.

Verify:
- failover test against the **real** registry with a stubbed fetch, asserting
  `fetch.calls == ["trendlyne-ohlc", "nse-bhavcopy"]` — the recon found this class only ever
  runs against a *synthetic* registry
- healthy-first-choice is **not** degraded (`calls == ["trendlyne-ohlc"]`)
- unknown `source=` spends **zero** requests
- `uv run pytest -m "not live" -q` — 2,251 still green

### T3b — Flip the default to Trendlyne-first (serial, after T3a)

Only now change the default try-order. Update `test_corporate_actions_wiring.py:438-450`
(whose docstring calls an unrequested signature change "worse than a no-op") **deliberately**,
and any other test that pins the old default. Ships only if T3a is green first.

### T4 — Two ohlcv honesty defects (serial after T3a, same file)

- **`period="max"` truncation is silent.** Bhavcopy floor `2024-01-01` (`ohlcv.py:31`).
  Publish `bfinance_window_truncated: bool` + `bfinance_window_truncated_detail: str`,
  following the `bfinance_bar_interval_mismatch` precedent (`:814`, `:1059`). No invented bars.
- **`auto_adjust=True` raises.** Implement yfinance semantics: `ratio = Adj Close / Close`,
  scale O/H/L/C, drop `Adj Close`. Default **stays `False`** (pinned by
  `test_adjust_default_drift.py:45`); `True` must now work instead of raising. Refuses when
  `Adj Close` did not come from a real feed, as today.

**Four OFFLINE tests to replace (all in the required CI job):**
`test_nse_bhavcopy_wiring.py:369`, `:476`, `test_corporate_actions_wiring.py:412`,
`test_parity_history.py:337`.

Verify: new offline test — `auto_adjust=True` has no `Adj Close` and `Close` equals the former
`Adj Close`; negative control refusing when actions unavailable. New offline test —
`period="5y"` past the floor sets the truncation attrs. `-m "not live"` green.

### T5 — Live verification of the shipped default (execution only)

The operator's opening question: does the default actually fetch data? Run against the
**shipped** default, no flags. Mixed list (4 equities + 2 ETFs). Per symbol: bar count,
first/last date, `bfinance_price_source`, `price_basis`, `degraded`. Pacing honoured
(>=1.2 s/host, >=2.2 s trendlyne). ETF run acknowledged as robots-disallowed.
**A failing symbol is reported as failing, never averaged into the result.**

---

## Non-goals

No new `Ticker` attribute bfinance cannot fill with real data: `earnings_dates`,
`recommendations`, `sustainability`, `shares_full`, `insider_*`, `sec_filings`,
`analyst_price_targets` stay **absent**. `Ticker.shares`/`get_shares()` are **dead in
yfinance 1.7.0** — do not implement. `download(proxy=)` was **removed** in 1.7.0 — do not add.
No intraday intervals (own phase). "Complete yfinance replacement" is a roadmap; the honest
deliverable is a gap inventory with per-gap evidence, the shape `docs/UPSTREAM_SOURCES.md`
already has.

## Risks

| Risk | Mitigation |
|---|---|
| Trendlyne's split/bonus-adjusted basis is not comparable to bhavcopy as-traded | T1 measures before ranking. If not comparable, Trendlyne does **not** go first in the as-traded default. |
| ~130 offline call sites bypassed by the cascade | T3a adds the seam **first** and keeps the default; T3b flips only after green |
| 7 xfails become 7 green tests that no longer guard anything | Replace with a per-property guard, not a blanket removal |
| Duplicating a Trendlyne reader rots | Tested against a recorded payload; drift is a test failure, not silent staleness |
| No two parallel tasks write one file | T1 (`sources/`) and T2 (`statements.py` + `ticker.py:373-472`) disjoint; T3a/T3b/T4 serial on `ohlcv.py` |
