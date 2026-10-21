# Plan: trendlyne.com fundamentals through bfinance's yfinance-compatible statement API

Slug: `trendlyne-fundamentals-yfinance`. Status: **executed 2026-10-10**. All measurements below
were taken live on 2026-10-09 against `/fundamentals/financials/959/NTPC/ntpc-ltd/` unless
another date is given.

> **Two corrections made during execution**, both recorded here rather than rewritten in place:
>
> 1. The public keyword this plan names `trendlyne_fallback` was shipped as **`statement_fallback`**
>    (see "Phase 2 remediation graph"). A vendor name does not belong in a provider-agnostic
>    constructor signature; the third vendor would make it a lie.
> 2. The row partition this plan's T1/T2 assumed does **not** work. The fix that shipped partitions
>    on `parameterMetadata[id]["cat1"]`, because `body.level_first_dict_map` is a collapsed level-1
>    UI list — **223 of 260 `_A` ids appear in no group** — and using it as membership would have
>    silently deleted 174 real rows while returning a plausible, correctly-scaled frame.

## The gap

`bfinance.statements` already **parses** two trendlyne fundamentals routes into
`StatementResult`s, but **nothing in the repo fetches either**. The cascade's transport is
caller-supplied (`statements/cascade.py:96`, `Fetch = Callable[[ProviderSpec], Any]`), and no
caller passes a real one. So `Ticker.financials` / `income_stmt` / `quarterly_income_stmt` /
`balance_sheet` / `cashflow` (`ticker.py:506-548` → `get_*_stmt` → `profile.*.to_yfinance()`) are
served **only** by screener.in, and an independent vendor with 11 fiscal years and 13 quarters
sits unused. `resolve_from_payloads(cascade.py:297)` is the offline entry point — it takes the
payloads as strings and opens no socket, which is exactly the seam a fetcher should fill.

## Route discovery, measured live (this supersedes an assumption in an earlier draft)

Three routes were fetched and their answers recorded. All three are **allowed** by the live
`robots.txt` (`User-agent: *`, `Crawl-delay: 1`, 28 `Disallow` rules; the only `/fundamentals/`
rules are `*/fundamentals/chart/v2/` and `*/fundamentals/back-test/runhistory/`).

| # | route | status | bytes | what it carries |
|---|---|---|---|---|
| 1 | `GET /fundamental-sitemap-quarter-result.xml` | 200 | 970,942 | 6,201 `<loc>` of `/fundamentals/financialals/…` — the symbol resolver (NTPC present: `/fundamentals/financials/959/NTPC/ntpc-ltd/`) |
| 2 | `GET /fundamentals/balance-sheet/{id}/{SYM}/{slug}/` (any tab shell) | 200 | 83,150 | **zero** `<table>`, but the start tag of `<main id="fundamental_tables">` carries `data-tablesurl="https://trendlyne.com/fundamentals/get-fundamental_results-v2/959/4GWEMQNUALMSYB5FM2VX37IUYE======/"` — the per-symbol JSON token |
| 3 | `GET /fundamentals/get-fundamental_results-v2/{id}/{token}/` | 200 | 841,461 | `head.status == "0"`; `annualDataDump.{consolidated,standalone}` + `quarterlyDataDump.{…}`; `parameterMetadata` (347 keys) |
| 4 | `GET /fundamentals/financials/{id}/{SYM}/{slug}/` | 200 | 538,028 | 5 SSR `<table>`s, **standalone only**, visible labels |

Two corrections to the earlier draft of this plan, both measured:

1. **The JSON route is reachable.** Its token appears on no *financials* page (the page was
   re-rendered as a React island; `id="fundamental_tables"` is now a `<div>` with no
   `data-tablesurl`), but it is on the **tab shell** (route 2), which is robots-allowed and answers
   200. This matters because route 3 is the only one that serves the **consolidated** basis.
2. **The SSR page is not needed for this work.** Route 3's `parameterMetadata[id]['sstr']` equals
   the label the SSR page renders, verified byte-for-byte on four ids (`SR_A` → `Total Rev. Ann.`
   vs the page's `Total Rev. Ann. ⌄`; `BVSH_A`, `TA_A`, `CFO_A` likewise). Fetching it would be a
   third request for labels the JSON already carries.

Route 3 already parses through the **shipped** parser on the **live** bytes and on the recorded
fixture, on both bases — no fixture-invented shortcut:

```
consolidated  income/annual 11 periods x 189 rows (2,073 non-null)   standalone 182 rows (2,002)
consolidated  income/quarterly 13 periods x 63 rows                  standalone 60 rows
```

## The two things that decide yfinance parity, both measured

**Labels.** `parameterMetadata[id]` carries `sstr` (short, = what the site renders), `lstr`,
`name`, `cat1` (`Income Statement` 130 / `Balance Sheet` 83 / `Financial Ratios` 72 /
`Cash Flow` 8 rows) and `detail` prose. The shipped JSON parser ignores all of it and sets
`labels[row_id] = row_id` (`fundamentals_provider.py:805`), so a trendlyne-json statement is
indexed by machine ids (`SR_A`, `OEXPNS_A`). For a yfinance drop-in that is the wrong index.

**Units — and the trap inside them.** The vendor states a unit per row, and it is not what an
id-suffix heuristic would guess. Census over all 347 keys of the NTPC payload:

| `unit` | rows | examples | scale to absolute ₹? |
|---|---|---|---|
| `Cr` | 201 | `SR_A` 189,798.56, `TA_A` 558,643.56 | **yes, ×1e7** |
| `%` | 55 | `OPMPCT_A` 29.13, `ROE_A` 13.31 | no |
| `Rs` | 25 | `BVSH_A` 209.53, `EPS_adj_A` 27.9 | no — per share |
| `abs` | 15 | `CRATIO_A`, `DEBT_CE_A`, `EYield_A` | no — ratio/count |
| `None` | 6 | **`PBV_A` 1.49** + 5 document rows | no |

`PBV_A` is a **ratio** with `unit=None`, so any rule of the form "scale unless the unit says
otherwise" multiplies a price-to-book multiple by 10,000,000. The rule must be the positive one:
**scale only what the vendor labels `Cr`.** This is the same class of defect
`models/statements.py:99-138` records for screener.in (`OPM %` came back as `350000000.0`),
arriving through the second vendor.

## Constraints inherited from the repo

1. `bfinance.trendlyne` is a **leaf**: `tests/test_trendlyne_core.py:109` AST-scans every module
   under `src/` outside that package and fails on any import naming it **or any filename
   containing the substring `trendlyne`**. New modules go elsewhere and duplicate rather than
   import. (Precedents: `statements/fundamentals_provider.py`, `sources/price_provider.py`.)
2. `bfinance.statements` is opt-in; `bfinance/__init__.py` must not import it, and importing it
   must open no socket.
3. Pacing floor 1000 ms is a **robots compliance** number (`trendlyne/client.py:649`), shared
   globally, not per-client.
4. **Basis is never relabelled or blended** (`statements/model.py:98-106`,
   `registry.py:378-416`). The SSR page prints standalone while its own config says
   `is_consolidated: true` — re-confirmed live: `TotalShareHoldersFunds_A` Mar 2026 is
   **203,175.67** consolidated and **174,865.25** standalone on the same payload.
5. No invented label map. `statements/model.py:16-20` refuses a cross-vendor rename that was
   never measured; this plan uses the vendor's own `sstr`, which *is* the measurement.

## Tasks

### T1 — fetcher: `src/bfinance/statements/fundamentals_fetch.py` (new)

```python
FUNDAMENTALS_SITEMAP_PATH = "/fundamental-sitemap-quarter-result.xml"
MIN_REQUEST_INTERVAL_MS = 1000.0
@dataclass(frozen=True) class FundamentalsPage: symbol, stock_id, slug, token, url
async def fundamentals_index(*, client=None, ttl_hours=24) -> Mapping[str, FundamentalsPage]
async def fetch_fundamentals_json(symbol, *, client=None) -> Any     # decoded payload
def  fetch_fundamentals_json_sync(symbol, **kw) -> Any               # asyncio.run wrapper
class FundamentalsFetchError(BFinanceError)      # resolution/transport, typed
class FundamentalsTokenError(FundamentalsFetchError)               # no data-tablesurl
```

- Resolve the symbol on the **exact** symbol segment of the sitemap's `<loc>` list, TTL-cached in
  memory (6,201 entries; duplicate `<loc>`s exist — first wins, count recorded).
- Take the token from the tab shell's `data-tablesurl`, then GET route 3 with that URL verbatim.
  **Re-extract once on a rejected token** (`head.status != "0"`) — the token rotates, which the
  leaf client already documents.
- Transport: browser UA pool, pacing on the global slot at 1000 ms, exponential backoff honouring
  `Retry-After` (cap 30 s), retry on 429/5xx/transport only, every 4xx terminal, and **never**
  call `.json()` on a zero-length 444.
- A module-level `ALLOWED_ROUTES` frozenset of exactly the three path shapes above, asserted by
  test so a future route cannot be added silently.
- No `bfinance.trendlyne` import, no `trendlyne` substring in the filename.

**Verify:** `uv run pytest tests/test_statements_fundamentals_fetch.py -q` (offline, no socket:
sitemap from the recorded excerpt; transport monkeypatched at the httpx boundary).

### T2 — labels, units, and the yfinance bridge

`src/bfinance/statements/model.py`: add `row_units: Mapping[str, str] = field(default_factory=dict)`
beside the existing `row_ids` / `row_labels` — frozen dataclass, defaulted, so every existing
construction site and test still passes.

`src/bfinance/statements/fundamentals_provider.py` (`parse_fundamental_results` only): read
`body.parameterMetadata` and set `row_labels[id] = sstr or lstr or id` and
`row_units[id] = unit or ""`. Backward compatible — the HTML parser already supplies its own
labels; nothing else calls this function with different expectations. Record in the units note
that the unit is the vendor's own string, verbatim.

`src/bfinance/statements/yfinance_bridge.py` (new): the single conversion point.

```python
def result_to_financial_statement(result: StatementResult) -> FinancialStatement
def result_to_yfinance_frame(result: StatementResult) -> pd.DataFrame
```

- `headers = result.periods` chronological (`FinancialStatement.to_yfinance` reverses to
  newest-first itself); `rows = {result.row_labels.get(m, m): values}`.
- **Scale rule:** a row is scaled ×`CR_TO_RS` **iff** `result.row_units[row] == "Cr"`. Nothing
  else, including `unit=None`. Rows whose unit is unknown or absent are left unscaled **and**
  named in `attrs["bfinance_statements_unscaled_rows"]` so the decision is auditable rather than
  silent in either direction.
- **No rename.** `INDIAN_TO_YFINANCE_LABELS` is a measured screener.in→yfinance map and there is
  no measured trendlyne→yfinance one. The index carries the vendor's own `sstr`
  (`"Total Rev. Ann."`), and `attrs["bfinance_statements_provider"]` says whose vocabulary it is.
- The frame carries the result's basis attrs (`bfinance_statements_basis`,
  `…_basis_requested`, `…_basis_mismatch`, `…_basis_note`) merged with `to_yfinance`'s own
  (`bfinance_dropped_period_columns`, `bfinance_merged_row_labels`).

**Verify:**
```
uv run pytest tests/test_statements_yfinance_bridge.py -q
# one case per unit class, all from the recorded NTPC fixture:
#   SR_A   Mar 2026 consolidated == 1897985600000.0   (Cr scaled)
#   OPMPCT_A == 29.13                                  (% untouched)
#   BVSH_A == 209.53, EPS_adj_A == 27.9                (Rs untouched)
#   CRATIO_A / DEBT_CE_A untouched                     (abs)
#   PBV_A == 1.49, named in bfinance_statements_unscaled_rows   (unit=None -> NOT scaled)
#   index[0] label == "Total Rev. Ann."  (vendor sstr, no rename)
#   columns[0] is the newest period; "TTM" absent from columns and listed in
#     bfinance_dropped_period_columns; basis attrs present, mismatch True when requested != served
```

### T3 — Ticker wiring: `src/bfinance/ticker.py`

`Ticker` gains one keyword, **default off**: `trendlyne_fallback: bool = False`. When set, and
only when the screener path yields **no** statement at all (the `_read_statement` empty/error
path at `ticker.py:152-177`, already recorded through `_degrade`), the accessor falls back to
`fetch_fundamentals_json_sync` → `resolve(...)` → `result_to_yfinance_frame`.

- **One vendor per frame.** Either screener's statement whole, or trendlyne's whole. No row-level
  merge, no back-fill of a missing period from the other vendor.
- The frame's `attrs` name the provider and the basis, including a mismatch against the
  consolidated basis `Ticker` normally serves — the caller sees which vendor answered and on what
  basis rather than having to infer it.
- No `asyncio.run` inside an awaited path: the async accessor awaits, the sync accessor wraps once.
- `Ticker(symbol)` with no flag must not open a second-vendor socket — asserted by test, not by
  comment.

**Verify:**
```
uv run pytest tests/test_ticker_trendlyne_statement_fallback.py -q
uv run python -c "<Ticker('NTPC', trendlyne_fallback=True, raise_errors=True).financials against the
  recorded fixture via monkeypatched fetch: equals the T2 frame; Ticker('NTPC') issues no
  second-vendor request>"
uv run pytest -q -m "not live"     # no new failures
```

### T4 — fixtures and tests

- `tests/fixtures/trendlyne/fundamental_results_ntpc.json` — **already recorded** in this plan's
  preparation: live route-3 capture for NTPC, 841,461 B → trimmed to 508,734 B by dropping the 14
  body keys with zero numeric leaves and no label/unit/order content
  (`parameterInsights`, `quarter_params`, `annual_params`, `indicator_info_dict_map`,
  `stockInsight`, `ROW_BOLDS`, `ROW_BGCOLOR`, `isMissingAnnualReportParas`, and 6 UI booleans).
  Verified to parse identically to the live bytes through the shipped parser (189 rows
  consolidated / 182 standalone, 11 and 13 periods). `stored_sha256
  f8575a4800dbed90c2cf15420860d89a32387ebfcda9f896d09816784514fef9`, LF endings.
- `tests/fixtures/trendlyne/META.json` — add the entry above in the existing format.
- `tests/test_statements_fundamentals_fetch.py`, `tests/test_statements_yfinance_bridge.py`,
  `tests/test_ticker_trendlyne_statement_fallback.py` — offline, fixture-backed, no network.
- Re-run `uv run pytest tests/test_trendlyne_core.py -q` (isolation + no import-time I/O).

## Pre-existing failure — not mine, do not chase

`tests/test_exceptions_alias.py::TestTheDocumentedLimitIsTrue::test_a_yfinance_except_clause_does_not_catch_a_bfinance_raise`
fails on a clean `-m "not live"` run from **untracked, uncommitted** work
(`tests/test_exceptions_alias.py`, `src/bfinance/exceptions.py`, and in-flight edits to
`market/corporate.py`, `nse/corporate_actions.py`, `tests/test_corporate_actions_wiring.py`).

## Task graph

```
T1 (fetcher)  ─┐
               ├─► T3 (Ticker wiring) ─► T4 (integration test)
T2 (labels/units/bridge) ─┘
```
Wave 1 = T1 and T2 in parallel (no shared file). Wave 2 = T3, which needs `result_to_yfinance_frame`
from T2 and `fetch_fundamentals_json` from T1. T4's fixture is already in place; its two unit tests
land with their owning task and the integration test with T3.

## Phase 2 remediation graph (added 2026-10-10 after adversarial recon)

Recon against the shipped tree found the change is **shape-compatible but not yet correct**. The
blocking item is one the original plan missed entirely.

### R1 — BLOCKING: the three statements return the same rows

`parse_fundamental_results` builds its row set from every `_A` id in the dump, then appends
everything the section did not order. Measured on the committed fixture, 2026-10-10:

```
income         rows=189  SR_A=True TA_A=True CFO_A=True ROE_A=True
balance_sheet  rows=189  SR_A=True TA_A=True CFO_A=True ROE_A=True
cash_flow      rows=189  SR_A=True TA_A=True CFO_A=True ROE_A=True
all three row-id sets identical: True
```

So `Ticker.balance_sheet` returns revenue and ROE rows. This was dormant while the route was
unfetched; the Ticker wiring made it user-visible. Fix: restrict each statement to
`body.level_first_dict_map[section]` intersected with ids present in the dump; raise a typed error
rather than falling back to "all ids" if the section is missing. Keep trap 3 intact (row set from
the dump's own ids, never from `order`).

**Verify:** `uv run pytest tests/test_statements_row_partition.py -q` — asserts the three row-id
sets are disjoint, and that balance sheet carries no `SR_A`/`CFO_A`/`ROE_A`.

### R2 — BLOCKING: a label collision silently loses a row

`result_to_financial_statement` keys rows by display label, so two ids sharing an `sstr` overwrite.
Measured on the fixture: `OPM Ann. %` <- [OPM_A, OPMPCT_A], `Cash Accruals Ann.` <-
[ACCRUALS_A, DepreciationOnLeasedAssets_A]. Both partners are all-null there today, so no value is
lost *yet*; `StatementResult.duplicate_rows` cannot catch it because it counts ids upstream. Also
the money-scale decision is currently keyed on the **display label**, so a collision straddling
`Cr` and `%` would scale both or neither. Fix: key the scale on the row **id**; record colliding
labels on attrs.

**Verify:** a test with two ids forced to share an `sstr`, asserting the loss is recorded rather
than silent.

### R3 — BLOCKING: a healthy fallback is reported as a degradation

`ticker.py` calls `_degrade(...)` on the success path, producing
`"NTPC: income statement at annual frequency is unavailable (served by the fallback vendor ...)"`
for a complete 189-row frame. `_degrade`'s own docstring says "what is **unavailable**". The file's
established convention for a multi-vendor failover is `attrs`, not `data_warnings` — see
`bfinance_price_source*` in `history()`. Fix: drop the success-path `_degrade`; provenance stays on
the frame attrs.

### R4 — `pretty` is ignored, and that is a real parity gap

Against yfinance 1.7.0 (`3d9d2f0`): `Ticker.income_stmt` returns **Title Case** ("Total Revenue")
while `Ticker.get_income_stmt()` with its default `pretty=False` returns **CamelCase**
("TotalRevenue"). bfinance's bridge returns the vendor's `sstr` for both. Most callers use
`get_*()`, so this is the incompatibility a drop-in swap actually hits.

Also confirmed against yfinance 1.7.0, and **correct as shipped**: there is no TTM column in
`income_stmt`/`balance_sheet`/`cashflow` (trailing is a separate one-column `freq="trailing"` frame),
values are raw currency units with no scaling (bfinance's ×1e7 Cr->Rs matches that convention), and
missing data yields a `(0,0)` frame rather than an exception.

### R5 — request volume: 4 accessors = 8 live fetches

Only the sitemap is cached. Each statement accessor calls the fetcher, so one `Ticker` with four
accessors issues 4 shell + 4 JSON fetches of ~841 KB each. Fix: a per-`Ticker` payload cache.

### R6 — test gaps

`sitemap TTL hit/expiry`, `force_refresh`, `max_retries` exhaustion, the 429 branch,
`_retry_after_seconds` in all four branches, transport-exception retry, and a direct
`fetch_fundamentals_json_sync` call (the one function `ticker.py` actually calls).

### R7 — docs

`docs/API_REFERENCE.md` constructor signature omits `trendlyne_fallback` and misorders `session`;
the statement-accessor section promises generic yfinance labels, which the fallback path does not
deliver. `README.md` quickstart does not mention the flag.

### R8 — open questions, decided 2026-10-13

| Question | Decision | Why |
|---|---|---|
| `pretty=True` raises rather than returning a guess | **keep the raise** | yfinance's Title Case comes from its own label vocabulary. Producing it from trendlyne needs a measured label map; the repository forbids inventing one. A typed error beats a confidently wrong label. |
| Ratios rows (`ROE_A`, `BVSH_A`, `CRATIO_A`, `PBV_A`, 64 measured) unreachable on this route | **do not add a ratios `StatementKind` yet** | yfinance 1.7.0 has no ratios statement, so parity does not require it. It also would not close the hole: `market/ratios.py` reads the screener `CompanyProfile`, not `bfinance.statements`, so a fallback frame never fed ratios even before the partition. Adding a fourth kind would touch the whole capability matrix for no automatic benefit. Revisit only alongside a `market/ratios.py` bridge. |
| Index labels are the vendor's own, not yfinance's | **keep** | `.loc["Total Revenue"]` works or raises depending on which vendor served. A cross-vendor map would be a rename with nothing measured behind it — the exact fabrication `statements/model.py` refuses. Documented in `docs/API_REFERENCE.md` §2.3.1. |
| `fundamentals_index()` returned the process-wide cache by reference | **store a `MappingProxyType`** | The declared return type was `Mapping` while the object handed out was a mutable `dict`; one caller could corrupt the cache every other caller in the process reads. |
| A dead `elif quarterly: raise` in `get_income_stmt` | **deleted** | Unreachable, and behaviourally identical — the statement after it raises the same error. |

## Definition of done

1. `bf.Ticker("NTPC", trendlyne_fallback=True).financials` returns a yfinance-shaped frame whose
   index carries trendlyne's own labels, whose money rows are absolute rupees and whose
   percentage / per-share / ratio rows are untouched — with provider and basis attrs naming both.
2. `Ticker("NTPC")` without the flag opens no second-vendor socket.
3. The fetcher addresses exactly the three robots-allowed routes and paces at ≥1 s.
4. `uv run pytest -q -m "not live"` shows no new failure beyond the pre-existing one above.
