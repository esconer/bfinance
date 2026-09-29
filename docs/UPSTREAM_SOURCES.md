# Upstream source facts — measured, not assumed

Every fact here was **executed against the live upstream on 2026-09-27** unless marked otherwise.
Where an earlier belief turned out to be wrong, the correction is recorded with the evidence, because
the wrong belief is the dangerous part.

Environment used for all measurements: Python 3.13.15, pandas 3.0.5, httpx 0.28.1, pydantic v2.

---

## 1. NSE CM bhavcopy — the authoritative OHLCV source

### Routes (cookie-free, no API key, browser `User-Agent` only)

```
UDiFF  (2024-01-01 → present)
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip

Legacy (pre-2024)
  https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{MMM}/cm{dd}{MMM}{yyyy}bhav.csv.zip
     e.g. /content/historical/EQUITIES/2023/MAY/cm15MAY2023bhav.csv.zip

F&O    /content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip        (verified 200, 37,165 rows)
Full   /products/content/sec_bhavdata_full_{dd}{mm}{yyyy}.csv
Bulk   /content/equities/bulk.csv                                    (verified 200, 237 rows)
```

Derived from `github.com/jugaad-py/jugaad-data` (public domain — no code copied, routes re-verified live).

### Depth — measured by probing, not assumed

| date | result |
|---|---|
| 20260925 | 200, 3,654 rows, **34 cols** |
| 20250101 | 200, 2,911 rows |
| 20240101 | 200, 2,683 rows, **35 cols** ← different width |
| 20230102 / 20210101 / 20180101 / 20150101 / 20100101 / 20070101 / 20050101 / 20000103 | **all 404** |
| legacy 2026/SEP | **404** — NSE retired it for recent dates |
| legacy 2023/MAY | 200, 2,427 rows |

**So: UDiFF ≈ 2024-01-01 → now, legacy for anything older, and the two 404 in opposite directions.**
Also: one 2024-01-01 file had 35 header fields against 34 data fields and named its tail columns
`Rsvd01..Rsvd04` where the 2026 file uses `Rsvd1..Rsvd4`. **Parse strictly by column name, never by
position.** There is no evidence this is a deliberate sentinel; do not "fix" it.

### Columns

- UDiFF (34): `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,
  FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,
  PrvsClsgPric,UndrlygPric,…` — OHLCV is `OpnPric`/`HghPric`/`LwPric`/`ClsPric`/`TtlTradgVol`
- Legacy (14, uppercase): `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,
  TIMESTAMP,TOTALTRADES,ISIN` — OHLCV is `OPEN`/`HIGH`/`LOW`/`CLOSE`/`TOTTRDQTY`
- **`SctySrs` must be filtered to `EQ`.** Raw files mix equities with `GB` (gold bonds, e.g. `SGBJUN28`)
  and blanks. Legacy `SYMBOL` can be a numeric BSE scrip code (`1018GS2026`).
- ⚠ **The `EQ` filter silently dropped sessions in BOTH directions. Now fixed.** A new listing debuts
  in series **`BE`** (trade-to-trade) and migrates to `EQ` weeks later. `ITCHOTELS` first appears
  2025-01-29 as `BE` (`FinInstrmId` 29253) and only reaches `EQ` on 2025-02-11 (29251), so the old
  `series="EQ"` default **lost the first 9 sessions and mis-dated the listing by 13 days**.
  Worse — the migration is **bidirectional and routine**: 43 distinct symbols changed series in a
  15-day window, and `AGSTRA`, `AHL`, `EVERESTIND`, `SADBHAV`, `VERTOZ` go **EQ→BE**. So an
  `EQ`-only read also **punches single-session holes in the middle of established histories**, which
  no listing-date sanity check would ever catch. `POWERMECH` was `BE` on one day and `EQ` on the
  other 14.
  The default is now `("EQ", "BE")`. That is safe because **same-day `EQ`∩`BE` overlap is 0** across
  24 archives, so the union is strictly additive.
  ⚠ **Do not default to "all series".** Measured cost: **9,964 extra rows = 22.6% of a file**, none
  of them equities — gold bonds (`SGBDE30III` @ 8,223.30), G-sec loans, T-bills, NCD codes,
  structured notes, and **`EMBASSY` the REIT @ 365.26** and `IRBINVIT` the invit. Every one carries a
  plausible price and would land in a portfolio as an instrument. The archives hold **100+ distinct
  `SctySrs` values**. `series=None` exists for callers who want everything and is never a default;
  `series=[]` raises rather than silently meaning "everything".
  ⚠ **Still excluded: `SM` (SME) and `ST`.** Both are equity companies. Including SME changes the risk
  profile of a large-cap screen, so that is a product decision, not a bug fix.
  ⚠ **Unresolved, pre-existing:** upstream prints **the same ticker twice in one file under two
  series** — **65 instances across 24 archives** (`RADIOCITY` as `EQ`+`P1`). `parse_bhavcopy` keys on
  `(Date, Symbol)` and dedups, so one bar is silently dropped. Outside `EQ`∪`BE` so the fix neither
  causes nor worsens it, but the union makes the general hazard newly reachable. Fixing it means
  keying on `(Date, Symbol, Series)`, which changes the public output contract.

### Performance — one file covers the WHOLE market

**10 symbols cost exactly the same as 1.** The 2026-09-25 file held 2,740 EQ instruments; all 10
tested symbols came from that single fetch (230 bars total). Cost is driven by *trading days*, not symbols.

| Window | Trading days | Wall time |
|---|---|---|
| 1 month | 23 | **73–94s** (two runs; real variance) |
| 3 months | 65 | 211s |
| 6 months | 125 | ~8.5 min |
| 1 year | 250 | ~17 min |

Pacing is 250 ms but each request actually takes **3.2–4.1 s** — latency dominates, the rate limiter
is idle ~93% of the time. Two consequences:
- **Bounded concurrency is the win, not a shorter delay.** 250 ms already permits 4 req/s.
- **Cache by trading date, not by symbol.** `bhavcopy_range` did not cache during this work, so
  re-fetching a month cost another ~94 s every time.

### The price-basis fact that governs everything

**bhavcopy is AS-TRADED. yfinance's `auto_adjust=False` OHLC are ALREADY split-adjusted.**

Measured bhavcopy ÷ yfinance, 2024-09-01..2024-11-15:

| symbol | bars matching | implied factor | what it is |
|---|---|---|---|
| RELIANCE | 14/53 → **53/53** after fix | 2.000000 | 1:1 bonus 2024-10-28 |
| HDFCBANK | 0/53 → **53/53** after fix | 2.000000 | 1:1 bonus **2025-08-26 (outside the window)** |
| TCS | 53/53 | 1.000000 | Rs 238 dividends after the window |
| ITC | 0/53 | 1.038513 | demerger 2025-01-06 — see §6 |

Volume scaling rule, and **why the obvious test is the wrong test.** Volume is scaled by the
**inverse** factor so `price × volume` is invariant. Do **not** verify that by asserting volume
roughly doubles across a 1:1 bonus. Measured on RELIANCE, 2024-09-01..2024-11-16, 39 bars pre /
14 post:

```
median Close   2924.90 -> 1305.47   ratio 0.446331
median Volume  8132084 -> 11996392  ratio 1.475193
mean   Volume  8388982 -> 12883285  ratio 1.535739
```

Neither halves nor doubles. **The confirming evidence is direction, not magnitude:** if volume were
already denominated in post-bonus shares it would have *fallen* ~2.2× along with the price. It rose,
so the column carries two share-count bases. Assert the **invariant** — `price_adjusted ×
volume_adjusted` identical on every bar (21,476,020,636 on both sides) — not the ratio. And do not
scale `Dividends` (rupees) or `Stock Splits` (a ratio): `Volume` is the only share-count column in
the history schema.

Consequences, all learned the hard way:
- **TWO factors, not one.** `share-count` (bonus/split/bonus-issue) → O/H/L/C. `dividend` → `Adj Close`
  only. Applying the dividend factor to O/H/L/C puts TCS **7.6%** off yfinance.
- **The factor must be the WHOLE-HISTORY product, not the window product.** HDFCBANK's bonus is
  2025-08-26 yet yfinance halves all 53 bars of a Sep–Nov 2024 window. A window-only factor gives
  1.0 where yfinance gives 0.5.
- Verified after the fix: RELIANCE 212/212 exact, HDFCBANK 212/212, MOTHERSON 212/212 and 132/132
  across a bonus-spanning window. Max relative difference **6e-8** (yfinance's own float noise).
- MOTHERSON proof the mechanism is right, not coincidental: as-traded shows **−34.4%** on the
  2025-07-18 1:2 bonus; bfinance's adjusted series shows **−1.6%** (the real move), and yfinance
  also shows −1.6%. Implied factor before the ex-date is **1.4993 ≈ 1.5**, so NSE's `1:2` means
  *one share per two held* (count × 1.5, price ÷ 1.5) — not 1-for-2.

### `PrvsClsgPric` is NOT a corporate-action signal

The hypothesis that NSE's official previous close would reveal actions by differing from
`ClsPric(t-1)` is **refuted**. It is byte-equal on event days too:

- RELIANCE 19-Aug-2024 (₹10 div): `ClsPric(08-16)=2956.4`, `PrvsClsgPric(08-19)=2956.4` — equal
- SHRIRAMFIN 10-Jan-2025 (5:1): equal
- RELIANCE 28-Oct-2024 (bonus): equal

NSE publishes the previous close unadjusted, like every other vendor. Zero action information.

---

## 2. NSE corporate actions — the official events feed

```
https://www.nseindia.com/api/corporates-corporateActions?index=equities
```

**camelCase.** The kebab-case spellings everyone guesses (`corporates-corporate-actions`,
`corporate-actions`) return **404**, and that 404 is a retired route, not a block — a deliberately
nonsense route returns the identical 272-byte body. Recovered from NSE's own bundle
`/dist/js/sections/corporate-filings.js`. **No cookie needed** (4/4 HTTP 200 with zero cookies primed).

- **43,789 rows / 3,930 symbols / 1995→2026, in one request.**
- Flat 14 keys: `symbol, comp, series, subject, faceVal, exDate, recDate, bc{Start,End}Date,
  nd{Start,End}Date, ind, isin, caBroadcastDate`
- `index=debt` returns a **dict** `{"data":[],"msg":…}`, not an array. Handle both shapes.

### Where NSE beats yfinance

RSSOFTWARE 21-Jan-2015: NSE records `Interim Div Rs 1.50 / FV Split 10→5`. yfinance records the
split 2.0 correctly but the dividend as 0.375 = 1.50/**4** — inconsistent with its own split.
Legacy bhavcopy agrees: 600.6 → 290.65 on the day, 2.07× of which 2.0 is the split.

### Bonuses are equity or not, and the parser must tell them apart

Only 9 of 252 merger-ish subjects contain an `a:b` or `%` token. Eight are `Bonus Ncrps` /
`Bonus Debentures` — non-equity, correctly rejected. The ninth, SREINFRA
`Bonus 4:5 (Pursuant To Scheme Of Amalgamation)`, is a genuine equity bonus and yields 1.8.

### Demerger — see §6. The short version: no ratio exists.

---

## 3. NSE MarketLens — close + volume only, 30 years

```
https://marketlens.nseindia.com/api/stocks/{SYMBOL}/price-history?period={PERIOD}
```

30Y = 8,069 rows from 1996-09-30. No auth. **Returns no O/H/L** — keys are `date`, `price`, `volume`.

**It is NOT an adjusted close.** Measured against yfinance over 244 days × 3 symbols:

| Symbol | ML ÷ yfinance **raw** | ML ÷ yfinance **adj** |
|---|---|---|
| RELIANCE | **1.000000** (1 distinct) | 1.000000 – 1.004624 |
| TCS | **1.000000** (1 distinct) | 1.000000 – 1.041513 |
| HDFCBANK | **1.000000** (1 distinct) | 1.000000 – 1.016539 |

`price` is the raw traded close — the same quantity as bhavcopy's `ClsPric`. The field is *named*
`price`, which reads like a cleaned/derived price. It is not. **This is the third time in this
project a field named like a derived quantity was actually raw** (the others: `dividendYield`,
and the native-vs-yfinance statement shape). Check the name against the value.

Traps: periods are **UPPERCASE only** (all-lowercase returns `success:true, data:[], error:null`);
a bad symbol is also a silent empty; `.NS` must be stripped; `L&T` fails because NSE's symbol is
`LT`; pagination/`from`/`to` params are ignored; EOD lag ~1 trading day; ~100 of 372 intraday bars
carry **negative volume** (−1 to −4), clamped and counted in `attrs["bfinance_negative_volume_bars"]`.

**Role: an authority source, not a cascade tier.** A tier answers "give me data"; an authority
answers "is this data right". It verifies close and volume; it must never be coerced into an OHLCV
frame, because inventing O/H/L to satisfy a frame contract recreates the exact bug issue 03 exists
to fix.

---

## 4. Trendlyne — third party, unofficial, yfinance-basis

Legal posture: `User-agent: *` has **Crawl-delay 1** (not 10 — see the correction below) and 28 Disallow
rules, **none of which cover the paths used** (`/equity/`, `/mapp/`, `/member/api/`, `/latest-news/`,
`/fundamentals/`, `/research-reports/`, `/equity-sitemap*`). The disallowed paths are
member/authenticated areas (`*/accounts/`, `*/like/`, `*/edithistory/`, `*/get-document/`). Only
`AhrefsBot` gets `Disallow: /`.

⚠ **CORRECTION — I misread this earlier and recommended the wrong default.** The 10-second
`Crawl-delay` is scoped to **GPTBot and ClaudeBot only**; the `*` group is **1 second**. So the
module's 750 ms default is marginally *under* the applicable 1 s, not 13× over a 10 s preference.
The correct fix is **1 s**, and a 10 s default would have made bulk scraping ~10× slower for no
compliance reason. Reading a `Crawl-delay` out of the wrong `User-agent` group is an easy and
consequential mistake.

**Two of the disallowed rules are load-bearing for planned sections:**
`Disallow: */equity/technical-analysis-*` and `*/alerts/addStockAlert/`. So **Technicals and Alerts
are off-limits on Trendlyne by policy**, not merely unavailable. Do not attempt either.

**Renaming the module does not reduce trademark exposure** — the domain is in every URL either way.
The real levers are: don't republish their editorial content, match their crawl-delay, keep honest
provenance.

### Four distinct silent-failure modes

| Symptom | Cause | Correction |
|---|---|---|
| **HTTP 444**, empty body | **missing `Referer`** on `/mapp/` | send `Referer: <the symbol's page_url>` |
| **HTTP 200**, 92 bytes, `head.status=="2"` | wrong token or wrong stock id | re-scrape the token once |
| **HTTP 200**, non-JSON body | **missing `X-Requested-With: XMLHttpRequest`** on `/member/api/ac_snames/` | send the header |
| HTTP 200 + `head.status=="0"` | success | — |

None of these are error statuses except the 444. A client that only checks status codes will
silently return empty frames.

### Symbol resolution — two routes, both needed

```
Sitemap  https://trendlyne.com/equity-sitemap-stocks.xml   →  5,922 entries, 831 KB
         path shape /equity/{id}/{SYMBOL}/{slug}/
         ⚠ XML-ESCAPED: "&" symbols are stored as "M&amp;M". Must html-unescape BEFORE
           percent-decoding. 16 entries affected, incl. M&M (807) and M&MFIN (808).
         828 of the entries are BSE scrip codes; indices sit in the same namespace.
         NOT paginated (?p=2 → 404).

Search   https://trendlyne.com/member/api/ac_snames/?term={SYMBOL}   (needs X-Requested-With)
         returns [{id, label, value, k, slugname, country, defaultExchange, category}]
         `k` is the stock id. THE ONLY ROUTE TO ETFs — NIFTYBEES (1588), BANKBEES (1581),
         GOLDBEES (1582), JUNIORBEES (1584), SETFNIF50 (1753) are absent from the sitemap.
         Miss answers [{"headline": "No matches found"}] — a result, not an error.
```

Use sitemap first (one bulk request, 5,922 instruments), search as fallback. **Neither covers
mutual funds** — those are a separate namespace.

### Per-symbol token

`https://trendlyne.com/equity/{id}/{SYMBOL}/{slug}/` contains a URL matching
`/mapp/v1/stock/web/ohlc/\d+/([A-Za-z0-9=]+)/`; capture group is the token. **Differs per symbol.**
So 2 requests minimum: HTML page for the token, then the JSON call.

### OHLC payload and quality

`body` keys: `liveHeaders, liveData, eodHeaders, eodData, eodDataLastWeek, stockHeaders, stockData,
exchange, suggestions`

- **History is a 2006-01-02 floor, truncated at listing.** Old names 5,107 bars; IREDA (2023 IPO)
  696; PAYTM (delisted) 1,199. It is *not* a fixed window.
- **`eodData` is newest-first and the freshest 5 sessions live in `eodDataLastWeek`.** Reading
  `eodData` alone silently loses a week — it stops at 2026-09-18 while the vendor's latest is
  2026-09-25. Merge both.
- `stockHeaders`/`stockData` (12 fields) include `NSE Symbol`, `BSEcode`, `Stock ID`, `isin` —
  directly useful for the `resolve_company_id` problem.
- **Price basis: as-traded, back-adjusted for splits and bonuses ONLY. Demergers are not applied
  at all.** Measured over 2024-12-16..2025-03-13 against bhavcopy, per symbol:
  `ITC 62/62 exact · RELIANCE 62/62 · TCS 62/62 · INFY 62/62 · SBIN 62/62 · AXISBANK 62/62` but
  `HDFCBANK 0/62 (max diff 932.60) · MOTHERSON 0/62 (max diff 56.27)`.
  The two failures are exactly the two names with a **split/bonus after the window** (HDFCBANK
  1:1 on 2025-08-26, MOTHERSON 1:2 on 2025-07-18) — Trendlyne back-adjusts those, matching
  yfinance's convention, and leaves everything else at the exchange print. So it diverges from
  bhavcopy exactly where a share-count event happened, and agrees everywhere else.
- **It therefore does NOT carry yfinance's demerger rewrite.** ITC 2025-01-03: Trendlyne 481.60,
  bhavcopy 481.60, yfinance 463.74. Across 2017 / 2018 / 2020 / 2022 / 2024 the Trendlyne ÷ yfinance
  ratio is **1.038513 to six decimals every time** — the exact reciprocal of Yahoo's 0.962915
  constant. So on a demerger **Trendlyne is the exchange and yfinance is the one that rewrote history.**
- ✅ **`attrs["bfinance_trendlyne_price_basis"]` no longer lies — fixed.** It was set unconditionally
  to `"yfinance_back_adjusted"`, which was wrong for the whole demerger class. It is now
  `as_traded_split_bonus_adjusted`, a **constant rather than a per-symbol derivation** — re-measured
  across 8 symbols × 2 windows, with a second window past each event confirming the same two names
  then match byte-identically. A separate machine-readable
  `attrs["bfinance_trendlyne_demerger_applied"] = False` lets a consumer branch on the demerger
  fact without parsing a label string.
  The guard asserts the value is **neither** `yfinance_back_adjusted` **nor** bare `as_traded` — the
  latter is also false, because splits and bonuses *are* applied. Basis keys are stamped by
  `parse_ohlc` only, **not** by `build_attrs`: a deals list or a holdings grid has no price basis,
  and stamping one on it is the same class of false claim.
  It is set unconditionally and it is wrong for the entire demerger class. The honest value names
  what is actually applied: `as_traded_split_bonus_adjusted`, demergers explicitly not applied.
- Consequence: Trendlyne **can** be blended with bhavcopy for demerger work — they agree exactly
  there — but never for split/bonus work. And do not trust `attrs` to tell you which case you are
  in; measure it, because the flag does not.
- OHLC invariant held **51,070 / 51,070 rows across 10 symbols** — zero violations.
- `close_price ≠ last_price` on ~4,995 of 5,112 rows. They are different fields.
- Verified matching yfinance on 7 of 8 symbols (MARUTI 99.7% … ); the outlier was ITC, which is the
  demerger, not a data defect. My earlier "77% aggregate, RELIANCE 14%" figure came from a
  1-year window vs a 20-year window — over 1 year the agreement is ~99.8%.

### Server-rendered tables (no API, no token) — classify by HEADER SIGNATURE

**Never select by heading text.** The `h2` "Check all corporate actions such as stock splits,
bonuses, dividends…" matches all three keywords and shadows the real `h3`s; doing so produced a
"bonus ratio" of `140.000` and "95 splits" for TCS. Classify on the header row:
`Bonus Ratio` → bonus, `Old FV`/`New FV` → split, `Dividend Amount` → dividend.

| Page | Rows | Depth |
|---|---|---|
| `/equity/bulk-block-deals/{SYM}/{id}/{slug}/` | 82 | 2008–2024 (~16y) |
| `/equity/insider-trading-sast/all/{SYM}/{id}/{slug}/` | **500 exactly** | **2020-02-12 → 2025-06-16** ⚠ |
| `/equity/share-holding/{id}/{SYM}/latest/{slug}/` | 7 tables, **incl. Pledged** | **6 quarters, latest only** |
| `/equity/monthly-mutual-fund-share-holding/{id}/{SYM}/latest/{slug}/` | **497 funds × 10 cols** | **3-month grid, ragged** ⚠ |
| `/research-reports/stock/{id}/{SYM}/{slug}/` | 22 | analyst target, LTP, price-at-reco |
| `/equity/board-meeting/{SYM}/{id}/{slug}-board_meeting/` | 101 | ex-date + purpose — the only **forward-dated** dataset here |
| `/fundamentals/financials/{id}/{SYM}/{slug}/` | 118 | quarterly + CAGR 3Y/5Y/TTM |
| `/tools/buy-sell-zone/{id}/{SYM}/{slug}/` | **2 tables × 9 = 18** | PE band, days traded in band |

⚠ **Three row counts from an earlier pass did not reproduce, and the corrections matter more than
the numbers:**

- **Insider trading is 500 rows, 2020-02-12 → 2025-06-16** — not 247 / 2013-2026. The newest
  disclosure is **15 months stale**, and 500 is exactly round, so a **server-side row cap is
  plausible and cannot be ruled out from one symbol**. A truncated frame must never wear a full
  provenance note; a row-cap guard is required.
- **MF holdings is 497 funds, not 355**, and the shape is a **3-month grid with ragged columns** —
  AUM (Cr) and AUM % exist **only for the most recent month**, earlier months carry just
  `Shares Held` + `Month Change %`. Requires `header=[0,1]`; the ragged-columns trap is real, not
  theoretical.
- **Buy Sell Zone is 18 rows, not 22, and the module's own classification model cannot handle it.**
  The two tables have **byte-identical headers**, differing only in PE-band geometry
  (current PE 17.0 standalone vs 17.8 consolidated), separated in markup by a
  `Standalone Data | Consolidated Data` tab bar. A header signature matches both. This page needs a
  **positional** path, not a signature. Also `header=[0,1]` drops a real row — the `Total` row
  (495 trading days) is data, not chrome.

Also: the insider-trading header carries `Security Type` and `Client Category`, with **no
`Deal Type`**. The registered `deals` signature must **not** be reused for it.

### None of the 8 are actually implemented

The table above describes what the **site** serves. It is not what the **module** does. An AST + test
audit found:

- **0 of 8 pages have a fetch method.** The tests exercise the generic `tables.find_table` against
  fixtures, not a route.
- **0 of 8 have a fixture matching their own route.** All 9 fixtures are MOTHERSON, and the 6
  registered signatures were validated against just **2** of them.
- The `board_meeting` signature's **only ever validation is a corporate-actions table.** So
  `find_table(corporate_actions_page, "board_meeting")` returns a 10×3 corporate-actions *purpose*
  table, and `/equity/board-meeting/` — the route the name exists to serve — has never been checked.
  Two honest options: rename the signature to what it was actually measured on
  (`corporate_action_purpose`), or re-measure it on its own route. **Undecided.**
  Separately, `tables.py:24` annotates the corporate-actions page's 4th table as having "no
  signature" when that table **is** the `board_meeting` signature — the docstring is simply wrong,
  and `test_trendlyne_core.py` already asserts the truth.
- ✅ **The 14 dead `ENDPOINTS` templates are deleted.** `ENDPOINTS` now means exactly *routes this
  package has a fetch method for* — `stock_page`, `ohlc`, `chart_metric`, and nothing else.
  The reason deletion beat wiring-later: a template with no method behind it still lets `build_path`
  mint a plausible URL for a route nobody asked for, and **5 of the 14 had never returned HTTP 200 at
  all** (`rights`, `news`, `news_only`, `news_analyst_calls`, `news_bse_announcements`) — pure
  invention. `TestNoDeadScaffolding` now enforces declared == referenced by AST walk **and** by
  driving the real methods against `httpx.MockTransport`, plus a guard that no docstring names a
  module absent from disk. All guards proven non-vacuous by negative control.
- ✅ **The forward references to `ownership`, `deals` and `corporate` are removed.** The package
  manifest now says explicitly that no such modules exist.

### Still true, worth re-reading
- **0 of 8 pages have a fetch method.** The tests exercise the generic `tables.find_table` against
  fixtures, not a route. Deleting the templates did not add them.
- **0 of 8 have a fixture matching their own route.** All 9 fixtures are MOTHERSON, and the 6
  registered signatures were validated against just **2** of them.

So the honest description of `bfinance.trendlyne` is: **a working OHLC client and symbol resolver,
plus scaffolding for 8 section tables that are not yet wired.** Treat the row counts as a scraping
feasibility study, not as delivered features.

`share-holding` is **snapshot-only** — `Jun-2024` and `2024` in the path both return **HTTP 500**.

**Not scrapeable — confirmed empty client-rendered shells** (after stripping scripts: 2,000–2,500
chars, only `nav-item`/`dropdown-item` classes): `FORECASTER`, `Technicals`, `Consensus`, `About`,
`Charts & Report`, `F&O`, and all ~110 `/equity/etf-dashboard/…` pages. Use the NSE FO bhavcopy instead
of reverse-engineering the `smartoptions` SPA.

### Mutual funds

```
/mf-sitemap.xml → 14,033 schemes | 9,432 dividend pages | 84 AMCs | 51 categories
/mutual-fund/get-mf-analysis-data/?pk={id}   → 200 JSON, no auth, generalises across the universe
```
Six sections, every metric `{val, insight, color, categoryAvg, category}`: `returnRatios`
(Sharpe, Jensen's alpha, Treynor, information), `riskRatios` (StdDeviation, beta, Sortino, R²),
`valuationRatios` (PE/PB/PS TTM, prospective div yield), `efficiencyAndOperational`
(**ROE, ROA, netExpenseRatio**), plus header metadata. Validated on 5 randomly sampled schemes;
1 returned nulls (a closed scheme). **NAV time series not found.** `getMFInfo` and `getUserNavBar`
404 at every base tried.

---

## 5. Pocketful - RETIRED: the host forbids all automated access

**`https://dev-trade.pocketful.in/robots.txt` returns, in full:**

```
User-Agent: *
Disallow: /
```

A **full-host prohibition.** Every route under that host is off-limits to an automated client, so
this section records a source that must not be used, and a measurement that **should not have been
taken**.

### The measurement below was performed in violation

The reliability study that follows - 495 sessions across four symbols - was run against
`/api/cardinal/v1/chart-data`, a path this host's own `robots.txt` forbids outright. It is retained
because deleting it would delete the finding, and the finding stands on its own: the O/H/L are
structurally unusable, independent of the compliance question. But it must not be re-run, and
nothing in this package may depend on it.

No compliant substitute is needed, because **NSE bhavcopy (`nse/bhavcopy.py`) and MarketLens
(`nse/marketlens.py`) already cover close and volume from official, key-free, robots-permitted
sources - and bhavcopy carries real O/H/L.** The cross-check this source was kept for is better
served by those two than by the source itself.

### What the (unauthorised) study found, for the record


```
https://dev-trade.pocketful.in/api/cardinal/v1/chart-data?slug={full-name-slug}&exchange=NSE
  &candle_type=3&start_time={unix}&end_time={unix}&data_duration=1
```
No auth. `data.candles` = `[timestamp, O, H, L, C, V]`, ISO timestamps with `+0530`.
`candle_type`: `3` = daily (20y), `2` = hourly (~2y), `1` = minute (times out on a 10y request),
`4+` = empty. **`data_duration` is ignored.** Slug must be the exact full-name slug —
`reliance-industries-ltd` works, `tata-consultancy-services-limited` does not, and there is no
resolver. Bare symbols 404.

**O/H/L are unusable.** *(study run in violation of the host's robots.txt - retained as
evidence, not as permission)* Across RELIANCE / INFY / SBIN / MOTHERSON over 495 sessions:
Open matched 346–416/495, High 388–435, Low 377–436, max relative error **1.08–3.98%**.
**68 rows violate the basic OHLC invariant** (close outside the high–low band, up to 8.16%),
clustered 2022(28) / 2023(39) / 2024(1) — the fingerprint of O/H/L adjusted for corporate actions
while close was not. Close is 96–99% and volume ~99% good. ~~**Use for close/volume cross-check only.**~~ **RETIRED - do not use.** The O/H/L finding
stands as evidence the vendor is unusable; the recommendation to cross-check against it does
not, because cross-checking requires access this host refuses.

---

## 6. Demerger — a factor IS derivable; bfinance applying 1.0 was wrong

**This section reverses an earlier conclusion in this file.** The previous text argued that no
numeric ratio exists, that bfinance was "on the correct side", and recommended not converting
(Option C). That was wrong, and it was wrong because it reasoned from the *absence* of a published
ratio instead of *measuring* what the market did. Both are now measured.

### The ground truth (bhavcopy, as-traded, no dividend confound)

| | |
|---|---|
| `P_pre` — ITC Close 2025-01-03 | **481.60** |
| `P_ex` — ITC Close 2025-01-06 | **442.65** |
| price drop | **−8.0876%** |
| `F_price = P_pre / P_ex` | **1.087992771** |

**Confound cleared:** the official feed has exactly 2 ITC events in 2024-11-01..2025-04-30 — the
`Demerger` (2025-01-06) and a ₹6.50 dividend (2025-02-12). The prior dividend was ₹7.50 on
2024-06-04, seven months earlier. So the 8.0876% move has one candidate cause. The dividend-adjusted
and raw `F_price` are identical because no adjustment is applicable, not because one was skipped.

### The entitlement is real, and it is measurable

The child is **`ITCHOTELS`** (ITC Hotels Limited, ISIN INE379A01028) — not `ITCHL`. Scheme: 1 ITC
share → 1 ITC share + **0.1 ITCHOTELS**. First print 2025-01-29, close **171.85**.

```
V_dist   = 0.1 x 171.85                             = 17.185
F_true   = P_pre / (P_ex + V_dist)
         = 481.60 / (442.65 + 17.185)              = 1.047332195
```

**The mark choice barely matters.** Across five defensible marks (listing close, next close, last BE
close, first EQ close, mean of 11 closes) `F_true` = **1.047 ± 0.002**. The measurement is not
knife-edge.

### The residual is real value destruction, and must stay visible

NIFTY 50 fell **−1.6193%** on the ex-date session (SENSEX −1.5881%; RELIANCE −2.65%, HDFCBANK
−2.21%). Decomposing ITC's −8.0876%:

| component | % |
|---|---|
| mechanical entitlement, 0.1 × ITCHOTELS@171.85 | −3.5683% |
| market (NIFTY 50) | −1.6193% |
| **ITC-specific residual** | **−2.9000%** |

The residual is corroborated: ex-date volume 22,009,958 vs next-20 mean 14,071,201 (**1.56×**), 5th
highest of 62 sessions. So the market genuinely repriced the demerger below the scheme's stated cost
allocation. **A correct back-adjustment must strip only the 3.5683% mechanical part and leave the
2.9000% visible** — absorbing the residual would falsify history.

### What yfinance actually does — measured, not inferred

NSE quotes on a ₹0.05 tick grid, so a correct factor makes every as-traded print snap back onto the
grid. Grid-searched the divisor:

```
pre-event  2017-01-01..2025-01-03   1978/1978 bars snap at f = 0.962915244729931   (100.00%)
                                                                 0% at f = 1.0
post-event 2025-01-06..2025-08-29     0/162  snap at f                              (0.00%)
per-year solve, 2005..2024, each year independently: f = 0.96291524… ± 3e-9
```

So it is a **whole-history rewrite by one undocumented constant**, applied to `Open`/`High`/`Low`/
`Close`/`Adj Close`, **not** to `Volume`. It ends at 2025-01-03; 2025-01-06 onward is untouched —
yfinance's own ex-date bar, 442.6499938965, is byte-equal to bhavcopy's as-traded 442.65.

Two traps in diagnosing it:
- **`Adj Close / Close` is structurally blind to it.** The ratio is flat at 0.9216973 across the
  boundary (max step 8.0e-8, float32 noise) — because the rewrite scales both columns by the same
  constant. Flatness here does **not** exonerate the raw series. Do not use it as the test.
- A median price-level ratio (e.g. `median(Close[d]/Close[2025-01-06])`) **cannot** test it — it
  just tracks ITC's price trend and drifts (0.9382 over 2022-06 onward, 0.9618 over 2023-24). The
  tick-grid snap is the test that discriminates.

`Ticker("ITC.NS").actions` has **40 rows, none in January 2025**, and splits only in 2005/2010/2016.
NSE-wise, yfinance does no demerger handling at all; it is not "ad hoc per name" — it is one global
constant that happens to be numerically close to right for this event.

### A second vendor restates the same event — differently

bfinance's own `screener` client hits **screener.in**, which publishes an independent price history
(`/api/company/{id}/chart/?q=Price-DMA50-DMA200-Volume&days=3652`, comp id 1552 for ITC). It returns
**weekly** bars only — 522 rows from 2016-09-30 — so it cannot isolate a single ex-date, but a
pre-event weekly close discriminates the basis just as well. It is close-only, no OHLC, and publishes
no `Adj Close` analogue.

Aligned on 514 common dates against yfinance, `screener / yfinance`:

| | median | spread |
|---|---|---|
| post-event (89 bars) | **1.000000000** | 1.0 ± 4e-8 |
| pre-event (425 bars) | **0.973496378** | stdev 1.1e-5 |

Pre-event per-year medians, 2016 → 2025: `0.973500 0.973500 0.973497 0.973494 0.973492 0.973495
0.973497 0.973496 0.973497 0.973498`. **One constant, whole history, no year dependence.** And it is
**not** a dividend ladder: all 12 dividend ex-dates inside the window produce steps of ±1e-5 to ±3e-5
with no consistent sign — noise, not a cumulative adjustment.

So screener.in also transforms ITC's pre-demerger history. **Whether that transform is a dividend
adjustment or a demerger restatement is NOT established — but the evidence now points at dividends.**

Its **quote is exactly as-traded**: `current_price` 269.0 vs the 2026-09-25 `ClsPric` 269.0, absolute
difference **0.0**; `volume` 6,240,148 vs `TtlTradgVol` **6,240,148**; the 50-DMA 273.055 vs the mean
of the last 50 as-traded closes, abs diff **0.0000000000**. **A current quote cannot be back-adjusted**,
so the chart series and the quote are on different bases. That is the classic signature of a vendor
that keeps a raw quote for display and a dividend-adjusted series for its technicals — which is also
what `Price-DMA50-DMA200-Volume` is *for*. So screener.in most likely **never heard of the demerger
at all**, and its chart is simply dividend-adjusted.

Not proven: the dividend hypothesis was tested and neither confirmed nor refuted. The chained product
over the 4 post-2025-01-03 dividends (₹6.50, ₹7.85, ₹6.50, ₹8.00 = ₹28.85) gives 0.919840,
reproducing neither 451.45 nor 454.60 — but the same formula also misses yfinance's own `Adj Close`
(0.919840 vs the published 0.921697), so **the formula is wrong, not the hypothesis**. Fixing the
chaining convention would settle it. **Unresolved. Do not assert a cause.**

The published closes for the last session before the ex-date, all five sources:

| source | 2025-01-03 close | implied factor vs as-traded | error vs `F_true` | what it is |
|---|---|---|---|---|
| **NSE bhavcopy** (exchange) | **481.60** | 1.000000 | −4.52% | the actual print |
| **Trendlyne** | **481.60** | 1.000000 | −4.52% | byte-identical to bhavcopy |
| yfinance | 463.74 | 1.038513 | **−0.84%** | undocumented constant |
| **mechanically correct** | **459.835** | **1.047332** | — | derived from exchange data |
| TradingView | 454.60 | 1.059393 | +1.15% | **unexplained** |
| screener.in | 451.45 | 1.066785 | +1.86% | **unexplained** |

⚠ **454.60 and 451.45 are not any actual NSE print for ITC anywhere in 2024-12-15..2025-03-15**
(checked against Open/High/Low/Close). They carry the 2-decimal tick-grid *appearance* of a real
print, which is what makes them dangerous — a derived number that looks like an exchange price.

**The two third-party vendors disagree with each other by 2.73% on the same instrument, same event,
same date — and the correct answer sits between them.** Neither publishes the constant. Neither can
derive it from any feed: NSE publishes no ratio, no stub price, and no adjustment field.

### Verdict

**There is no such thing as an authoritative *adjusted* price.** Only the as-traded print is a fact,
and two independent sources reproduce it exactly. Everyone else applies a transformation they do not
publish, and the three third-party factors span 1.038513 → 1.066785, a **2.72% disagreement** with each
other on the same bar.

| applied factor | value | error vs `F_true` |
|---|---|---|
| **mechanically correct** | **1.047332195** | — |
| yfinance (undocumented constant) | 1.038513 | **−0.84%** |
| screener.in (undocumented, cause unknown) | 1.066785 | **+1.86%** |
| TradingView (undocumented, cause unknown) | 1.059393 | **+1.15%** |
| **bfinance (Option C)** | **1.000000** | **−4.52%** |

**Option C is dead, and the "principled" framing was self-justification.** Applying 1.0 is not
declining to guess — it is a specific claim that the ex-date moved the security's value by nothing,
and that claim is wrong by 4.52%. Of that, 3.57 points is a mechanical entitlement any correct
library must strip, and 0.95 points is the market's own move, which no library should strip.

The earlier claim that "matching Yahoo on demergers is not a coherent target" is **right, for a
reason I did not give and could not have given**. It is not that the ratio is unknowable — it is
derivable to ±0.2%. It is that **yfinance is not a reference implementation for this event class at
all**: it rewrote history by an undisclosed constant, and the only source that still matches the
exchange on a demerger is Trendlyne. A parity test against yfinance would have been a test against a
bug.

Consequence for the parity suite: the demerger cases must be excluded from yfinance-parity
assertions and asserted against `F_true` instead.

### The buildable design (Option D)

1. **A curated structural-event table** — symbol, ex-date, child symbol, child shares per parent
   share. ~123 demerger rows. This is the irreducible manual input; NSE will never publish it.
2. **Mark the child from bhavcopy** at its first available close. **Requires the `BE`-series fix in
   §1 — without it the child mark is unmeasurable for exactly the securities this feature exists for.**
3. `F = P_pre / (P_ex + r x P_child_mark)`, applied to O/H/L/C, volume × `F`.
4. **Label it fully** — `ratio`, `child`, `child_mark`, `child_mark_date`, `factor`, and the
   unexplained `residual`. The residual is the honest part: it keeps the −2.9% repricing visible.
5. Rows with no curated ratio stay labelled non-applied. **Option C is right as the fallback and
   wrong as the policy** — declining to invent a number is correct only when there is no number to
   be had.

The `other` bucket is **16,797 rows, 81.9% AGM notices** (13,803 rows), with 124 demerger rows
(0.74%) lumped in. Structural keywords measured over a fresh 43,788-row pull: `demerger`/`de-merger`
**124** (123 unhyphenated + exactly 1 `De-Merger`, disjoint), `scheme` **142**, `amalgam` **6**,
`composit` **5** — union **255** rows over 202 symbols, 1996-05-29 → 2026-09-08. `composit` is a false
friend: one of its three distinct subjects is a *company* (`Gujarat Composite Ltd`), so `composite`
only counts when it qualifies a scheme or amalgamation.

**Counts corrected from an earlier pass:** 43,788 rows, not 43,789; the `other` bucket is 81.9% AGM,
not 69.5%; `demerger` is 124 rows, not 123; and the largest committed corporate-actions fixture was
only 223 curated rows, so the figures above come from a live 43,788-row re-pull.

---

## 7. BSE — the largest remaining official-source gap

bfinance's `resolve_exchange` has a BSE branch with **zero test coverage**; BSE's
`api.bseindia.com` returns **403 Akamai**; the only BSE data currently reachable is 828 third-party
Trendlyne scrip codes.

A GPL-3.0 project (`github.com/pparesh25/NSE_BSE_Downloader` — **learn from, never copy**, as with
jugaad) documents a three-era BSE map that has not been verified here:

> BSE Equity: ISIN legacy reports before 17 Aug 2022, second-generation through 7 Jul 2024,
> UDiFF from 8 Jul 2024.

It also reports NSE SME filenames switching from two-digit to four-digit year on 13 Oct 2025, and
NSE equity's first trading day as 1994-11-03. **Unverified — probe before coding to any of it.**
A wrong era date produces a silent 404 run, not a loud failure.

Also from that project, and independently corroborated by the research in §8: **adjust volume
inversely.** `O,H,L,C ÷ factor` and `VOLUME, DELIVERY_QTY, QTY_PER_TRADE × factor`, so price ×
volume is unchanged. Do **not** scale `TOTAL_TRADES` (counts transactions, not shares) or
`DELIVERY_PERCENT` (a ratio of two columns that both scale).

---

## 8. Mergers and demergers — the correct method

From an external research note, cross-checked against what was built:

**A demerger cannot be handled by one split-like factor.** The parent adjustment is

```
F = (P_parent,ex  +  r × P_child,ex  +  c) / P_cum
```

adjusted onto O/H/L/C, with volume scaled inversely. But since the child is a *separately listed*
security, **the child must not receive the parent's history** — it did not trade before listing, so
copying history there would fabricate a security. The research's own conclusion is that
**portfolio accounting (keep quantities, add the received shares separately) is the safest method**,
because a single adjusted series hides the fact that the investor received another security. That
is a portfolio engine, i.e. finengine — not a price library.

It also recommends keeping two datasets (raw, never overwritten; adjusted analytical series) and
using **ISIN plus exchange security code** rather than symbols, *"because symbols can remain
unchanged, change temporarily, or be reused."* That lands directly on the `resolve_company_id` work.

Era-dependent unit trap to watch: **NSE published F&O turnover in lakhs before 2024-07-08 and index
turnover in crores.** Same class of bug as the `dividendYield` fraction/percent one.

## 9. Routes found via a third-party client — verified, not inherited

`github.com/BennyThadikaran/NseIndiaApi` (GPL-3.0, 161 stars, last pushed 2026-08-31)
exposes ~39 NSE routes, several of which bfinance does not read. Like
`pparesh25/NSE_BSE_Downloader` above: **learn from, never copy.** Nothing was copied; these
are public HTTP endpoints and every fact below was **re-verified live on 2026-09-29**
rather than taken from the other project's source. A route in someone else's code is a
hypothesis, and bfinance's rule is that an unverified route is not a route.

Verified on 2026-09-29, `www.nseindia.com/api` with a primed cookie, >= 2s pacing:

| route | verdict | what came back |
|---|---|---|
| `/holiday-master?type=trading` | **200, 34,022 B** | 12 segment keys, CM = 20 rows, **all holidays** |
| `/historicalOR/indicesHistory` | **200, 19,021 B** | 70 rows x 9 columns of index OHLC for NIFTY 50 |
| `/option-chain-v3` **with `symbol` + `expiry`** | **200, 941 B** | `expiryDates` and `strikePrices` fully populated |
| `/public-past-issues` | **200, `[]`** | empty for a 2014-2015 window |
| `/historicalOR/indicesHistory?indexType=NIFTY 50 - TRI` | **200, 0 rows** | 11 bytes |
| `nsearchives.../content/equities/sec_list_{YYYYMMDD}.csv` | **404** | an HTML error page, not CSV, for both a 2026 and a 2015 date |

### What this changes

**The exchange holiday list exists, and bfinance was reading it backwards.** `/holiday-master?type=trading`
returns 12 segment keys (`CBM`, `CD`, `CM`, `CMOT`, `COM`, `EGR`, `FO`, `IRD`, `MF`, `NDM`,
`NTRP`, `SLBS`). The CM segment is **20 rows and every one is a holiday** — "Republic Day",
"Holi", "Mahashivratri", "25-Dec-2026". It is a **holiday list, not a trading calendar**,
and the correction matters: **every one of those rows carries `morning_session: null` and
`evening_session: null`**, so the session columns are not an "was it open" signal here.

`docs/BAVCOPY_MIRROR.md` section 11a records what that cost: a first implementation read
null-both as "therefore a trading day" and would have made the mirror demand ~20 files a
year that are never published. The route is still what the mirror needs, with the polarity
reversed, and the payload covers **one calendar year** (15-Jan-2026 to 25-Dec-2026), so a
date outside it is *unknown* rather than assumed either way.

**Index OHLC history is available and bfinance does not read it.**
`/historicalOR/indicesHistory` returns, per session: `EOD_INDEX_NAME`, `EOD_OPEN_INDEX_VAL`,
`EOD_HIGH_INDEX_VAL`, `EOD_LOW_INDEX_VAL`, `EOD_CLOSE_INDEX_VAL`, `HIT_TURN_OVER`,
`HIT_TRADED_QTY`, `EOD_TIMESTAMP`, `HI_TIMESTAMP`. That is genuine index OHLC, which
bfinance currently has no source for.

**CORRECTION: the option-chain route is not dead.** Recorded elsewhere as
"`/api/option-chain-v3` returns 200 with a 2-byte `{}`". That is a **call artefact, not an
upstream fact.** Called with no parameters it returns `{}`; called with `symbol` and
`expiry` it returns the full `expiryDates` and `strikePrices` grids. `records.data` was
empty in this test, consistent with being outside market hours, so the open question is
narrower than "the route is dead": it is whether `records.data` populates in session. The
route itself works.

**TRI is still absent, now re-confirmed twice.** Section 7 records no TRI route; this is an
independent second attempt via a different route and a different `indexType`, and
`NIFTY 50 - TRI` returns zero rows where `NIFTY 50` returns 70. The refusal in
`sources/registry.py` stands, with better evidence.

**Two negative results worth keeping, so nobody re-derives them.**

* `/public-past-issues` is empty for historical windows, so it cannot supply listing dates.
  The Trendlyne client's undeclared 2,222 pre-listing rows for SBIN therefore have **no
  official fix available from this route**; the honest options remain a listing-date gate
  from another source, or publishing the row count as an attribute.
* The per-day securities list 404s on the archive host. bfinance's `universe` module is a
  forward-only **recorder** for exactly this reason, and that design is now re-confirmed
  from a second direction: there is no per-day membership file to backfill from.

### Corroborations, not new facts

Three things this project states independently were confirmed by a maintainer who has run
against NSE for years, which raises confidence in all three:

* **"All requests through NSE are rate limited or throttled to 3 requests per second."**
  bfinance's `min_request_interval_ms = 250` allows 4/s, so the shipped default is
  slightly **more aggressive** than the rate this project considers real.
* That project recommends "an extra 0.5 - 1 sec sleep between requests", i.e. 1-2/s, well
  below bfinance's 4/s floor.
* It recommends "save the file and reuse them instead of re-downloading", which is the same
  conclusion `docs/BAVCOPY_MIRROR.md` reaches from measurement.

Its transport is factored into its own module with a persistent session and a
`download_folder` holding cookies and downloaded files, which independently matches the
measured 3.03x keep-alive win in that document.

The one place this project is *more* conservative: it never re-verifies a route from
another implementation without fetching it, and it records the negative results alongside
the positive ones.
