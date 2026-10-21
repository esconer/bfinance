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

## 4. Trendlyne - third party, unofficial, and the price basis is NOT yfinance's

Legal posture: `User-agent: *` has **Crawl-delay 1** (not 10 — see the correction below) and 28 Disallow
rules, ~~**none of which cover the paths used**~~ **CORRECTION, 2026-09-30: that was wrong in the
permissive direction, and one of the rules does cover a path this package used to call.** Re-read from
the live `https://www.trendlyne.com/robots.txt` (HTTP 200, 2,536 bytes), the `User-agent: *` group
carries 28 `Disallow` rules and **one of them is `Disallow: */member/`**, which covers
**`/member/api/ac_snames/`** (trendlyne's own autocomplete, and the only route to every ETF). It is
therefore *not* among the clean paths; `/equity/`, `/mapp/`, `/latest-news/`, `/fundamentals/`,
`/research-reports/` and `/equity-sitemap*` are the clean ones. The narrower rule
`Disallow: */member/api/ac_snames/stock/` covers a *sibling* endpoint, not the `?term=` search.
The 28 are **not** all member/authenticated areas, which is the other half the old sentence got
wrong: besides `*/member/` they include `*/onwards/`, `*/trendit/`, `*/visitor/`, `*/wholikes/`,
`*/relationships/`, `*/posts/comment/`, `*/getUserNavBar/`, `*/polls/getMemberVotes/`,
`*/rapid-results/all/`, `*/alerts/modal/stockAlert/`, `*/alerts/addStockAlert/`,
`*/create-basket/from/`, `*/fundamentals/back-test/runhistory/`, `*/fundamentals/chart/v2/`,
`*/equity/technical-analysis-*`, `*/stock-screeners/by-type/?searchparam=`, and seven
`*/portfolio|equity/.../custom/?query=%E|%F` rules. **Every route this package can build is
clean except the one member route** -- `/equity/sector/{id}/{slug}/` and
`/equity/industry/{id}/{slug}/` included, since `*/equity/technical-analysis-*` is the only
equity rule and a stock or group page never carries that literal prefix.

The code agrees with the robots file and gates on it, which is why the docstring, not the prose, was
right: `ROBOTS_DISALLOWED_ROUTES` is exactly `{"/member/api/ac_snames/": "Disallow: */member/"}`,
`TrendlyneClient._require_member_search` raises `TrendlyneRobotsDisallowedError` **before a socket
opens**, and `allow_member_search` now defaults to `True` -- see the three-tier table below. The stated consequence is that **ETFs are
unreachable through this package by default**: `resolve_any` skips the fallback explicitly and records
it rather than reporting the instrument as unknown. A legal-posture claim that errs toward "permitted"
is the dangerous direction to be wrong in, so this is recorded as a correction rather than an edit.
Only `AhrefsBot` gets `Disallow: /`.

⚠ **CORRECTION — I misread this earlier and recommended the wrong default.** The 10-second
`Crawl-delay` is scoped to **GPTBot and ClaudeBot only**; the `*` group is **1 second**. So the
module's 750 ms default was *under* the applicable 1 s -- 1.33 requests per second
against a published floor of one, a 250 ms per-request violation, not 13x over a 10 s
preference. not 13× over a 10 s preference.
The correct fix was **1 s**, and a 10 s default would have made bulk scraping ~10× slower for no
compliance reason. Reading a `Crawl-delay` out of the wrong `User-agent` group is an easy and
consequential mistake.

**DONE, 2026-09-30.** `DEFAULT_MIN_REQUEST_INTERVAL_MS` is now `1000.0`, and
`TrendlyneClient().min_request_interval_ms` is `1000.0` -- exactly one request per second.
Re-verified against the live `robots.txt` on the day of the change. The old page-weight
justification for 750 ms was not deleted silently: a 292 KB page per symbol is a real
observation, but a heavy page does not lower a stated `Crawl-delay`, so it was never a
robots-derived argument. Pinned by `tests/test_trendlyne_pacing.py`, which fails if the number
is tidied back toward 750 -- including if 750 reappears as a live literal anywhere in the
module, which that file checks by AST rather than by line grep.

### OPERATOR DECISION, 2026-09-30: the member route is now fetched by default

This package **requests a path trendlyne's robots.txt disallows**, on any lookup for a
symbol the equity sitemap omits, unless a caller passes `allow_member_search=False`.
That is a standing compliance choice and it is the operator's, not a default arrived at
by accident, so it is stated here rather than inferred from a resolution order.

The reason is coverage, and the tension is real. The sitemap carries **no ETF at all**,
so with the route closed every ETF is unreachable. Measured on a mixed list of six real
symbols -- `ntpc`, `cipla`, `wipro`, `indgn`, `mafang`, `hngsngbees` -- the strict
default resolved four and refused two, and **both refusals were ETFs that resolve fine**
once the route is open (`MAFANG` 1,334 bars, `HNGSNGBEES` 2,602 bars, both categorised
`etf` by the vendor's own field). A default that fails a third of a realistic portfolio
cannot be shipped, so the route is open and the strict setting remains one argument away.

| configuration | behaviour |
|---|---|
| the default | equities and ETFs both resolve |
| `allow_full_search=True` | adds the categorised 22-field discovery route |
| `allow_member_search=False` | strict: allowed paths only, every ETF refused |

The other constraint is unchanged either way: `Crawl-delay: 1` applies, the pacing floor
is 1000 ms, and the `GPTBot`/`ClaudeBot` `Crawl-delay: 10` is scoped to those crawlers.

### ⚠ CORRECTION, 2026-10-10: "Technicals and Alerts are off-limits by policy" was half wrong

The old sentence read, in full: *"Two of the disallowed rules are load-bearing for planned sections:
`Disallow: */equity/technical-analysis-*` and `*/alerts/addStockAlert/`. So **Technicals and Alerts
are off-limits on Trendlyne by policy**, not merely unavailable. Do not attempt either."*

**The conclusion was drawn one step further than the measurement supports, and it was wrong about
both nouns.** Both rules are still real and both page/write routes are still unwired — nothing below
retracts that. What changed is that "the rule is `*/equity/technical-analysis-*`" is not the same
claim as "technical analysis is unreachable", and the second sentence generalised from a page route
to a *capability*.

**CORRECTION, 2026-10-10 (the rule is a literal substring, and the sibling route does not match it).**
`Disallow: */equity/technical-analysis-*` is a leading-wildcard pattern with no trailing `$`, so it
matches any path **containing** `/equity/technical-analysis-`. The JSON sibling
`/equity/api/stock/adv-technical-analysis/{id}/24/?format=json` contains `/equity/api/stock/` in
front of that substring and therefore **matches none of the 28 rules** — verified by reading the
`User-agent: *` group of the live `robots.txt` that day (28 `Disallow` rules, `Crawl-delay: 1`;
`Disallow: */equity/technical-analysis-*` is the only equity rule). The **page** route
`/equity/technical-analysis-*` stays off-limits and is still unwired; the sibling is not.

**OPERATOR DECISION, 2026-10-10: technicals ship twice, because the local path has no robots
question at all.**

| what ships | why | where |
|---|---|---|
| a **local** computation over `Ticker.history()` — no network, so no robots rule can reach it | a pure-pandas engine over a frame the caller already has | `bfinance.market.technical.TechnicalIndicators` (`sma`, `ema`, `rsi(14)`, `macd(12,26,9)`, `adx(14)`, `atr(14)`, `bollinger(20,2)`, `pivots`, `snapshot`) |
| the **vendor's own** signals, over the robots-clean JSON sibling | same parameter set the site computes, including the insight text and colour the vendor publishes | `TrendlyneClient.adv_technical_analysis` (raw `body["parameters"]`, 56 top-level keys on the recorded TCS and NTPC captures) and `TrendlyneClient.technical_signals` (tidy `name/value/insight/color/parameter` frame, 55 rows on `adv_technical_analysis_tcs.json`) |

Neither is a claim about the page route. `Ticker.trendlyne_technicals()` reaches the JSON sibling
behind `Ticker(..., trendlyne_sections=True)`; the page route remains unwired.

**CORRECTION, 2026-10-10: the Alerts half was wrong on which path the rule covers.** The two
disallowed rules in that namespace are `*/alerts/addStockAlert/` and `*/alerts/modal/stockAlert/`.
Both are **write** routes — creating an alert and the modal that does it. **Reading**
`/alerts/stock-alerts/{symbol}/{stock_id}/{slug}/` matches neither rule, needs no member account
(the list is public; only *creating* an alert is login-walled), and is now wired as
`stock_alerts_page` → `TrendlyneClient.stock_alerts` — measured 200 with the alert list on TCS
2026-10-10, recorded as `tests/fixtures/trendlyne/stock_alerts_tcs.html` (10 parsed rows). **There
is deliberately no `Ticker.trendlyne_alerts()` member** — the seven opt-in `Ticker` members are
news, reports, shareholding, deals, buy-sell-zone, technicals and SWOT — so the alert list is
reached on the client. That is an omission, not a decision against it; the count is stated so it is
not mistaken for one.

⚠ **THE WARNING STANDS AND IS NOT RETRACTED: *adding* an alert is still off-limits.** Do not build
`/alerts/addStockAlert/`, do not build `/alerts/modal/stockAlert/`, and do not post a form to either.
`ENDPOINTS` contains neither name, and that is the mechanical form of the rule — a path absent from
`ENDPOINTS` is a path this package cannot build (`tests/test_trendlyne_core.py::
TestNoDeadScaffolding::test_the_addressed_routes_are_the_ones_with_fetch_methods` asserts the set).
What changed is the *scope of the prohibition*, not its existence.

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

### The richer search — a SECOND route, 22 fields, and a category vocabulary

```
Full     https://trendlyne.com/member/api/ac_snames/all/?term={q}&all-results=true
         (needs X-Requested-With, same as the plain route)
```

Same robots rule as the plain search — `Disallow: */member/` covers `/member/api/ac_snames/all/`
too, so it is not a cleaner path. It is a second path, not a permitted one, and the package gates it
behind a **second** opt-in (`allow_full_search`), which stays `False` even though the plain route beneath it is now open by default.

It returns **22 fields** where the plain route returns 4. One real record, verbatim:

```json
{"id": "NIFTYBEES", "label": "Nippon India ETF Nifty 50 BeES(NIFTYBEES)", "value": "NIFTYBEES",
 "k": 1588, "slugname": "nippon-india-etf-nifty-50-bees", "country": "IND",
 "defaultExchange": "NSE", "stock_code": "NIFTYBEES", "UIcode": "NIFTYBEES", "category": "ETF",
 "pageType": 10, "NSEcode": "NIFTYBEES", "BSEcode": "590103", "isCfd": false,
 "currentPrice": 259.47, "day_change": -1.0, "day_changeP": -0.38, "day_high": 260.49,
 "day_low": 257.9, "sectorName": "Others", "industry": {},
 "nexturl": "https://trendlyne.com/equity/1588/NIFTYBEES/...",
 "urls": [["Overview", "https://trendlyne.com/equity/1588/NIFTYBEES/..."]]}
```

**The top level is a bare JSON list**, not an object with a `data` key. Handled both ways, as the
plain route is.

**The category vocabulary, measured across 136 distinct (category, symbol) pairs from 8 queries on
2026-09-30. Six strings, and two of them are traps:**

| category | n | pageType | can this package fetch OHLC for it? |
|---|---|---|---|
| `Equity` | 73 | 2 | yes |
| `Index` | 26 | 24 | yes |
| `ETF` | 12 | 10 | yes (and absent from the equity sitemap entirely) |
| `Mutual Funds` | 11 | 12 | **NO** — separate namespace, NAV not OHLC |
| `Not Traded` | 9 | 2 | **NO** — delisted or suspended |
| `Superstars` | 5 | *(absent)* | unknown — a vendor grouping, not an instrument class |

This is the part that is **not decoration**. A `Mutual Funds` hit has no OHLC route on this package
at all — the package's own not-found message already says mutual funds are a separate namespace at
`/mutual-fund/nav/{id}/` — and a `Not Traded` hit is delisted. Resolving to either and returning a
price history would be a **wrong** answer rather than a thin one. So the category is *carried on the
result* (`SymbolCategory`, `parse_symbol_category`, `SymbolCategory.has_ohlc()`) and **never used to
filter silently**: a filtered list would drop 9-11 of ~20 ranked rows and still look exhaustive,
and only the caller knows whether they asked for a fund. An unrecognised string maps to `other` and
is reported as unknown, never guessed into `equity`.

**`label` is the display name with the ticker appended in parentheses**, and a company name may
legitimately contain its own — so only a **trailing** group that *equals* the ticker is stripped. A
blind `rsplit("(", 1)` truncates `"Tata Elxsi (India) Ltd"` to `"Tata Elxsi "`. Absent `label`
falls back to the slug, then the symbol.

**Not a resolver.** trendlyne's search is fuzzy and ranked (`"nifty bees"` → BANKBEES, …, NIFTYBEES
fifth), and taking its first hit is what made `ohlc("nifty bees")` return a different security's
bars with nothing to notice it by. The plain route already resolves a ticker correctly, so this one
is for *discovery* and is deliberately not wired into `resolve_any`.

### Per-symbol token

`https://trendlyne.com/equity/{id}/{SYMBOL}/{slug}/` contains a URL matching
`/mapp/v1/stock/web/ohlc/\d+/([A-Za-z0-9=]+)/`; capture group is the token. **Differs per symbol.**
So 2 requests minimum: HTML page for the token, then the JSON call.

### OHLC payload and quality

`body` keys: `liveHeaders, liveData, eodHeaders, eodData, eodDataLastWeek, stockHeaders, stockData,
exchange, suggestions`

- ~~**History is a 2006-01-02 floor, truncated at listing.** Old names 5,107 bars; IREDA (2023 IPO)
  696; PAYTM (delisted) 1,199. It is *not* a fixed window.~~ **CORRECTION: the "truncated at
  listing" half is false, and was removed from the code in this release.** It had been generalised
  from symbols that happened to agree with it. Neither "truncated at listing" nor "a fixed vendor
  window" holds universally. Measured live 2026-09-29, full frame including the last-week merge:

  | symbol | first bar | NSE listing | bars | bars dated before that listing |
  |---|---|---|---|---|
  | RELIANCE (id 1127) | 2006-01-02 | long before | 5,114 | 0 |
  | SBIN (id 1193) | 2006-01-02 | 2015-01-15 | 5,114 | **2,222** (45,561,075,413 shares) |
  | PAYTM (id 712312) | 2021-11-18 | 2021-11-18 | 1,206 | 0 |
  | IREDA (id 1791996) | 2023-11-29 | 2023-10-13 | 703 | 0 |

  What survives is only that the bar count is **symbol-dependent** (a recent listing reaches far
  fewer bars than a mature large cap). Three of the four above start at or after the listing date,
  which is what made the rule look right. **SBIN falsifies it**, and section 9 below records the same
  counter-example independently: 2,222 of its 5,114 bars carry pre-listing volume. Its own payload
  holds a `BSEcode` of 500112 beside the NSE symbol, so those bars are most likely the BSE history
  of the same company filed under the NSE ticker (plausible, not verified).
  There is **no listing-date source here to gate on** (the OHLC payload has no listing date, and
  NSE's `/public-past-issues` answers empty for historical windows), so the package neither clips
  nor pretends to. `parse_ohlc` takes an optional `listing_date` and publishes
  `attrs["bfinance_trendlyne_rows_before_listing"]` and
  `attrs["bfinance_trendlyne_volume_before_listing"]`: **-1 means "not measured"** and must never be
  read as "none", 0 means measured and none found. The rows are never dropped, because the series is
  split/bonus adjusted and clipping it would break the price basis. A frame whose `exchange` attr
  reads `"NSE"` while holding pre-NSE-listing bars is exactly why the decision belongs to the caller.
  (The 5,107 / 696 / 1,199 figures above are also stale by about one trading day; the counts in the
  table are the re-measured ones.)
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

⚠ **And two of the wired routes are not in this table because they have no server-rendered table
at all**: `/latest-news/` and `/alerts/stock-alerts/` render card lists, not `<table>` elements
(0 SSR tables each, verified on the recorded fixtures). "Classify by header signature" is the rule
for the pages above and **does not extend to those two**, which are parsed over their card markup
by `parse_latest_news` / `parse_stock_alerts`. Counting SSR tables is a way to find what a page has,
not a precondition for reaching it.

#### ⚠ CORRECTION, 2026-10-10: row counts are per-symbol, and five of them were re-measured

**Every count in the table above is a count of one symbol's page, not a property of the route.**
That was already true and is now stated, because two of the numbers had been quoted as if it were
not — "500 rows" as if every symbol returns 500, "101 rows" as if board meetings are a fixed count.
The TCS captures recorded that day (`tests/fixtures/trendlyne/`, every entry cross-checkable in
`META.json`) are a second symbol on the same routes, and they do **not** agree:

| page | the number above | re-measured on TCS, 2026-10-10 | where it is recorded |
|---|---|---|---|
| insider-trading-sast | **500 exactly** ⚠ | **174 data rows** (175 `<tr>` = 1 header + 174) | `insider_trading_tcs.html`, `META.json` `notes` |
| board-meeting | 101 | **95 data rows** (96 `<tr>`) | `board_meeting_tcs.html`, `META.json` `notes` |
| research-reports | 22 | **21 data rows** (22 `<tr>`) | `research_reports_tcs.html`, `META.json` `notes` |
| monthly MF holdings | 497 funds | **495 `<tr>`** = 2 `<thead>` + 492 `<tbody>` + 1 `<tfoot>` | `monthly_mf_holding_tcs.html`, `META.json` `notes` |
| bulk block deals | 82 | **182 rows** (fixture keeps the first 39) | `bulk_deals_motherson.html` + `client.bulk_block_deals_async` docstring |

**So `500` is not a per-symbol row count — and that is exactly why the row cap is kept as a number
rather than as a conclusion.** `bfinance.trendlyne.client.INSIDER_ROW_CAP = 500` records the
observed server-side cap; TCS returns 174 today and MOTHERSON returned 500 on an earlier pass. The
guard publishes `attrs["bfinance_trendlyne_insider_rows"]` on every frame and adds a truncation note
**only when the count equals the cap**. The converse is never asserted: a frame below the cap is
never described as complete, because nothing measured says it is. That is the whole of the rule,
and it is why the cap number is kept rather than the sentence "the vendor serves 500 rows".

**CORRECTION, 2026-10-10: the MF grid's shape is confirmed and its period behaviour is the
opposite of share-holding's.** Two routes carry a `{period}` path segment and they do **not** behave
alike, which is the finding worth keeping:

| route | non-`latest` period | guard shipped | measurement |
|---|---|---|---|
| `/equity/share-holding/{id}/{SYM}/{period}/{slug}/` | **HTTP 500** | **yes** — refused locally, **zero requests spent** | `Jun-2026` on TCS → 500, re-verified 2026-10-10; `Jun-2024`, `2024`, `2023` on MOTHERSON → 500 (2026-09-27) |
| `/equity/monthly-mutual-fund-share-holding/{id}/{SYM}/{period}/{slug}/` | **HTTP 200, and it filters** | **no** — the period is forwarded into the template | `Jun-2026` on TCS → 200, 1,577,098 bytes; `2026` → 200, 1,402,262 bytes (2026-10-10). On MOTHERSON: `2024` → 200 (313 rows, top period Sep-2024), `Jun-2024` → 200 (260 rows), `2023` → 200 (179 rows) |

A blanket snapshot guard across both routes would have broken the one route where the segment means
something. `TrendlyneClient.share_holding_async` carries the guard;
`TrendlyneClient.monthly_mf_holdings_async` validates the segment with `validate_period` and
forwards it. The grid itself is **ragged** and stays that way: a `-` is a month in which that fund
held nothing, not a missing measurement, so no month column is coerced to numeric.

#### ⚠ CORRECTION, 2026-10-10: buy-sell-zone — the discriminator is positional, and the stats live in the same pane

The positional claim above is confirmed and made mechanical. The two tables carry
**byte-identical header rows** —
`['PE range', 'Days traded in range', '% Days traded in range', 'Days traded within & below range',
'% Days traded within & below range']`, 10 data rows each on the recorded TCS page — so **no header
signature can separate them, and no rename of one column would**. The only discriminator is the
enclosing **`div.tab-pane#standalone` vs `div.tab-pane#consolidated`**, and their numbers genuinely
differ, so a swap would be invisible in the output:

| basis | current PE | average PE | median PE | strong buy zone |
|---|---|---|---|---|
| `standalone` | 14.9 | 23.6 | 23.8 | 1.0% |
| `consolidated` | 15.1 | 23.2 | 23.1 | 1.8% |

*(measured by parsing `buy_sell_zone_tcs.html` with `tables.buy_sell_zone_tables`; the `Total` row —
495 trading days — is kept as data, since dropping it was the defect the old note recorded.)*

**The summary statistics are published inside the same pane, not on a separate block**, which is why
the extractor reads them there: `bfinance_trendlyne_buy_sell_basis`,
`bfinance_trendlyne_buy_sell_tiles` (the raw tile text, including the forward 1y price / PE / EPS
trio), `bfinance_trendlyne_strong_buy_zone_pct`, `bfinance_trendlyne_current_pe`,
`bfinance_trendlyne_average_pe`, `bfinance_trendlyne_median_pe` and
`bfinance_trendlyne_days_below_current_pe_pct` all ride on the frame's own `attrs`. The last one
reads **`None` on the recorded capture** — the page showed no such tile — and that is the measured
answer, not a gap.

#### ⚠ CORRECTION, 2026-10-10: the board_meeting signature was re-measured on its own route — the collision is real and inherent

The old text recorded this as **"Undecided."** It is decided, and the answer was neither of the two
options offered, because the finding changed what the options were:

> *Two honest options: rename the signature to what it was actually measured on
> (`corporate_action_purpose`), or re-measure it on its own route. **Undecided.***

**Re-measured on 2026-10-10 on its own route**, using `board_meeting_tcs.html` (recorded from
`https://trendlyne.com/equity/board-meeting/TCS/1372/tata-consultancy-services-ltd-board_meeting/`,
HTTP 200): the header row is exactly `Ex-date | Purpose | Notes` over **95 data rows**, leading
forward-dated at `08 Oct 2026 Quarterly Results & Interim Dividend`. **So the signature does serve
the route it was named for, and `corporate_action_purpose` would have been a rename that made things
worse** — it would have given a board-meeting table a corporate-actions name to paper over a
collision that a name cannot fix. No rename shipped.

**The collision is CONFIRMED AND INHERENT.** `/equity/board-meeting/` and the corporate-actions
page's purpose/notes table present the same three header cells, so no header signature can separate
them — this is not a defect in either signature, it is a property of the two pages. The consequence
is stated rather than engineered away: **the caller-side discriminator is the table index**, and
every frame publishes `attrs["bfinance_trendlyne_table_index"]` so a caller holding a page knows
which of them it got. Pinned by
`tests/test_trendlyne_tables_sections.py::TestBoardMeetingIsValidatedOnItsOwnRoute`, which asserts
both halves — `page_signatures(board_meeting_page) == {0: "board_meeting"}` and
`page_signatures(corporate_actions_page)[3] == "board_meeting"` — plus a negative control that
renaming `Notes` unclassifies the board-meeting page.

**The related claim that the corporate-actions page's 4th table has "no signature" is false, and
`tables.py` has been corrected.** The `tables.py` module docstring now reads
`['Ex-Date', 'Purpose', 'Notes'] -> board_meeting` in the header-signature table and carries the
collision note above, rather than annotating that table as unclassified. Measured on
`corporate_actions_motherson.html`: `page_signatures(...)[3] == "board_meeting"` and the frame it
returns is `(10, 3)`.

#### ⚠ CORRECTION, 2026-10-10: four parsing traps, all of which cost rows silently

These are the ones that produce a **smaller, plausible-looking frame** rather than an exception —
the failure mode a review cannot see. Each is measured on a fixture in
`tests/fixtures/trendlyne/`, and each is enforced by a test rather than by a comment.

1. **`display:none` is data, not chrome — and this one loses 8 of 13 rows.** The share-holding
   summary grid (`#shareSummaryTable`) carries 13 `<tbody>` rows and **8 are collapsed accordion
   sub-rows** with `style="display:none"`: Promoter's `Holding` / **`Pledged`** / `Locked`, DII's
   **`Mutual Funds`** / `Banks` / `Insurance` / `Others`, plus one empty spacer `<tr>`. So 5 of 13
   rows are visible and **`pandas.read_html`'s default `displayed_only=True` returns exactly those
   5** — verified directly on `shareholding_tcs.html` with no code of this package involved.
   **`Pledged` is a genuinely different holding figure from Promoter's own `Holding`, so the default
   is data loss and not cosmetics.** `tables.share_holding_tables` passes `displayed_only=False` for
   this one table (wrapping the fragment in `io.StringIO` — a bare HTML string is read as a
   filename) and publishes `frame["Row Type"]` — `"category"` or `"subgroup"` — because with the
   collapsed rows restored the index is deliberately **not unique**: `Others` is both a DII sub-row
   and a top-level category. `displayed_only=False` is **not** a module-wide default: every other
   page in this package uses `display:none` for layout scaffolding no caller wants.
   The parsed TCS frame is `(12, 13)` — 12 rows because the empty spacer row carries no cells, and
   13 columns because `Row Type` is added to the vendor's `Summary` + 12 quarter columns.
2. **Two tables, one header row, byte for byte.** Buy-sell-zone, above. Positional only.
3. **`board_meeting` collides across two pages.** Above. Inherent; the table index is the answer.
4. **Column labels rot.** Share-holding and MF headers carry live month names (`Holding Jun 2026`,
   `Sep-2026`) that rewrite themselves every quarter, so a signature keyed on either expires with
   the data. The three positional extractors select on structure and stable tokens instead —
   `No of holders` / `History` for the holder tables, the fund column's own `MF` label for the MF
   grid, and the enclosing `div.tab-pane` for buy-sell-zone. **The quarter stays a column label.**

### ⚠ CORRECTION, 2026-10-10: "None of the 8 are actually implemented" is false — all of them are

The section below this one described what the **site** serves, and then said, in two places, that
the **module** does not fetch any of it:

> ### None of the 8 are actually implemented
> - **0 of 8 pages have a fetch method.** …
> - **0 of 8 have a fixture matching their own route.** All 9 fixtures are MOTHERSON …

> ### Still true, worth re-reading
> - **0 of 8 pages have a fetch method.** … Deleting the templates did not add them.

**Both statements were true when written and are false now.** All 8 of those pages have a fetch
method and a fixture recorded from their own route, and the reason is a 2026-10-10 reconnaissance
pass that fetched every candidate route live, re-read `robots.txt`, and then wired what answered.
What follows replaces both sections rather than annotating them.

#### The wire status, measured

`bfinance.trendlyne.client.ENDPOINTS` holds **19 keys**, read from the module — not from this table,
which is what the next sentence means by *auditable*. Read it directly:

```
stock_page  ohlc  chart_metric  sector_page  industry_page          (5 base)
corporate_actions_page  bulk_block_deals_page  share_holding_page
board_meeting_page  research_reports_page  latest_news_page
stock_alerts_page  insider_trading_page  monthly_mf_holding_page
buy_sell_zone_page  swot_page                                      (11 section pages)
adv_technical_analysis  overview_second_part  price_performance_analysis  (3 JSON siblings)
```

**Every key has a `build_path` site and a public fetch method behind it, and that pairing is
enforced in both directions** by
`tests/test_trendlyne_core.py::TestNoDeadScaffolding::test_no_endpoint_template_is_a_dead_string`
(declared == referenced, by AST walk) and `::test_the_addressed_routes_are_the_ones_with_fetch_methods`
(hardcodes the 19-name set above, so a key added without a method fails there). That is the whole
argument for "wired": not this table's say-so, but a test that fails when the two disagree.

| the 8 studied pages | `ENDPOINTS` key | fetch method | fixture recorded from **its own route** | rows parsed from that fixture |
|---|---|---|---|---|
| `/equity/bulk-block-deals/` | `bulk_block_deals_page` | `bulk_block_deals` | `bulk_deals_motherson.html` | 39 data rows (the stored window keeps the first 40 `<tr>` of the 82 the page served) |
| `/equity/insider-trading-sast/all/` | `insider_trading_page` | `insider_trading` | `insider_trading_tcs.html` | 174 |
| `/equity/share-holding/` | `share_holding_page` | `share_holding` + `share_holding_holders` | `shareholding_tcs.html` | summary `(12, 13)` + 3 holder tables `(6,6) (10,5) (13,5)` |
| `/equity/monthly-mutual-fund-share-holding/` | `monthly_mf_holding_page` | `monthly_mf_holdings` | `monthly_mf_holding_tcs.html` | `(31, 10)` from the retained window |
| `/research-reports/stock/` | `research_reports_page` | `research_reports` | `research_reports_tcs.html` | `(21, 12)` |
| `/equity/board-meeting/` | `board_meeting_page` | `board_meetings` | `board_meeting_tcs.html` | `(95, 3)` |
| `/equity/corporate-actions/` | `corporate_actions_page` | `corporate_actions` | `corporate_actions_motherson.html` | `bonus (10,4) · dividend (26,6) · other (13,10)` |
| `/tools/buy-sell-zone/` | `buy_sell_zone_page` | `buy_sell_zone` | `buy_sell_zone_tcs.html` | `standalone (10,5)` + `consolidated (10,5)` |
| `/fundamentals/financials/` | *(not here)* | — | `financials_motherson.html` | a **second client**: `bfinance.statements.fundamentals_fetch`, deliberately its own copy because the isolation rule forbids this one importing anything named `*trendlyne*` |

**Two wired routes have zero server-rendered `<table>` elements, and `tables.find_table` would
return nothing for them** — so they are not read that way, and the count is recorded here rather
than left to be discovered:

| route | what the page actually renders | how it is parsed | rows parsed from the fixture |
|---|---|---|---|
| `/latest-news/{id}/{SYM}/{slug}/` | **0 SSR tables** — the news list is a card list (`div.panel-post`, keyed `div#post-{id}`, each carrying the `/posts/{id}/{slug}` href, date, kind badge and publisher) | `parse_latest_news`, over the card markup | `(10, 5)` — `date, kind, title, url, source` |
| `/alerts/stock-alerts/{SYM}/{id}/{slug}/` | **0 SSR tables** — the alert list is cards too (`div.tr-alert-body`; kinds seen: `RESEARCH REPORT`, `SMA 30 Crossover`, `SMA 100 Crossover`) | `parse_stock_alerts`, over the card markup | `(10, 5)` — `date, kind, headline, detail, values` |

⚠ **Both fixtures deliberately keep two alert families, because a parser reading only one would
silently miss the other**: the `aside#allposts` card list *and* the sidebar
`div#stockaccordion` / `div#researchreportcollapse` research-report block. `RESEARCH REPORT` and
`SMA 30 ABOVE Crossover` are both present in the stored file. `?page=2` on the news route was
measured answering 200, which is what makes `page=` a real argument rather than a decoration.

**"0 of 8 have a fixture matching their own route / All 9 fixtures are MOTHERSON" is the claim that
died hardest, and it is now false in a specific way worth naming.** The fixtures are no longer
MOTHERSON-only. `tests/fixtures/trendlyne/` holds **29 recorded responses** (plus `META.json`, which
carries one entry per file), and **12 of them are TCS** (id 1372) rather than MOTHERSON:
`board_meeting_tcs.html`, `buy_sell_zone_tcs.html`, `insider_trading_tcs.html`, `latest_news_tcs.html`,
`monthly_mf_holding_tcs.html`, `research_reports_tcs.html`, `shareholding_tcs.html`,
`stock_alerts_tcs.html`, `swot_tcs.html`, `adv_technical_analysis_tcs.json`,
`overview_second_part_tcs.json` and `price_performance_tcs.json` — plus `sitemap_excerpt_tcs.xml`,
which exists precisely so the offline tests resolve `TCS` rather than `MOTHERSON` (the reason is in
that fixture's own `META.json` entry), and `option_chain_tcs.json`, which is a **different host**
(`smartoptions.trendlyne.com`). Every fixture carries a full `META.json` record (`url`,
`http_status`, `captured_at_utc`, `original_bytes`, `original_sha256_16`, `stored_bytes`, a mandatory
`trim_rule`, `notes`, `stored_sha256`, `stored_line_endings`), and
`tests/test_trendlyne_fixtures.py` fails if a fixture is missing, if its `stored_sha256` does not
match, or if it does not parse.

**The counts in the "rows parsed" column above are what the *shipped parsers* return from the
*stored fixture windows*, which are trimmed, not what the live page served** — where the two differ
the `META.json` `trim_rule` says by how much and why. That is the reason the trimmed number is the
one quoted: it is the number a caller actually gets from this package on the bytes that ship.

**Registered signatures went from 6 to 10**: `board_meeting`, `bonus`, `deals`, `dividend`,
`insider_trade`, `research_report`, `rights`, `sector_constituents`, `sector_industry_aggregate`,
`split`. `research_report` (`Summary` + `Target` + `Upside`) and `insider_trade` (`Client Name` +
`Client Category` + `Post Transaction Holding`) are new, and **`insider_trade` is deliberately not
`deals`** — the insider page has no `Deal Type` column, so reusing the deals header would classify
an insider frame as a bulk-deal frame with no error at all.

**Three section routes are answered by three routes that had no HTML page in the study at all** —
`adv_technical_analysis`, `overview_second_part` and `price_performance_analysis`, each a
`?format=json` sibling recorded as a fixture (`adv_technical_analysis_tcs.json` **and**
`adv_technical_analysis_ntpc.json`, `overview_second_part_tcs.json`, `price_performance_tcs.json`).
They count as wired because they have an `ENDPOINTS` key, a method and a fixture, not because they
were in the 8-page study.

**One section call costs exactly one request**, and that is a contract rather than a hope: the
symbol resolves through the cached sitemap index and the section route is fetched once — no
stock-page fetch, no token fetch, because a section route needs only the id, the symbol and the
slug the index already holds. The per-method request count is pinned in
`tests/test_trendlyne_sections.py` with `len(calls) == N` assertions.

#### What is *still* not implemented — stated precisely, because "everything" would be the same defect

* **`/fundamentals/financials/`** is served by `bfinance.statements.fundamentals_fetch`, a second
  client with its own JSON route (`get-fundamental_results-v2/{id}/{token}/`, token embedded in the
  financials page). It is **deliberately not** in this package's `ENDPOINTS`: the isolation rule
  forbids any module outside `bfinance/trendlyne/` importing anything named `*trendlyne*`, and the
  statements layer is outside it.
* **`/equity/technical-analysis-*` (the page route)** stays unwired and off-limits. Its JSON sibling
  is what ships.
* **`/alerts/addStockAlert/`** and **`/alerts/modal/stockAlert/`** stay unwired and disallowed.
  Reading the alert *list* is a different route and does ship.
* **`/equity/consensus-estimates/json-data/{id}/`** is a **404** — measured, not assumed. Consensus
  targets come from the research-reports table instead.
* **`/equity/delivery-analysis/`**, **`/equity/holding-overtime/{holder_id}/`** and
  **`/equity/stock-mf-holding-table/{tlid}/`** were seen but not measured this pass, and a follow-up
  is the honest description. `stock-mf-holding-table` in particular was found in the share-holding
  page markup as an empty `div#mftabledata[data-fetchurl]` the page lazy-loads; **no request was
  made against it**, so nothing may be claimed about what it returns.
* The `/member/api/ac_snames/*` routes remain gated behind their own opt-ins, unchanged.

#### Still true, worth re-reading

* **`ENDPOINTS` means exactly *routes this package has a fetch method for*.** It is an allow-list
  of what was requested, not a deny-list of what exists — which is the discipline that makes the
  `build_path` rejection a real error rather than a formality. One route is deliberately **not** in
  it: `/member/api/ac_snames/?term=...` is a query-string template and `ENDPOINTS` is validated by
  `build_path` as path templates.
* **The reason deletion beat wiring-later, and the finding that justified it, both stand.** 5 of the
  14 dead templates (`rights`, `news`, `news_only`, `news_analyst_calls`,
  `news_bse_announcements`) had **never returned HTTP 200 at all**. That is what "a template with no
  method behind it is a promise about unwritten code" means, and it is why the 14 came back as
  *measured routes* rather than as 14 re-added strings.
* **The forward references to `ownership`, `deals` and `corporate` remain removed.**
  `tests/test_trendlyne_core.py::TestNoDeadScaffolding::test_the_package_docstring_lists_exactly_the_modules_on_disk`
  asserts the package manifest matches the modules on disk in both directions.

So the honest description of `bfinance.trendlyne` is now: **a working OHLC client and symbol
resolver, a sector/industry layer, and a wired section-page layer over 14 new routes** — 11
server-rendered pages and 3 JSON siblings — **plus a second, separately-gated F&O client.** The row
counts above are still a per-symbol measurement rather than a route property, and the gaps listed
immediately above are the honest remainder. Treat none of this as covering the whole site.

**CORRECTION, 2026-09-30 (still in force, for the sector layer):** the summary previously stopped at
"a working OHLC client and symbol resolver, plus scaffolding for 8 section tables", which predates
`sectors.py` entirely. The sector and industry capability is real, wired and reachable, all opt-in
via an explicit import (nothing in `bfinance` imports this package and `import bfinance` does not
pull it in):

| capability | entry point |
|---|---|
| classification of a symbol, **zero extra request** (it shares the page fetch the OHLC token already needs) | `TrendlyneClient.classification(symbol)` |
| the complete sector + industry vocabulary, **one request** | `TrendlyneClient.taxonomy(group)` |
| every constituent of a group, weighted by market cap, **one request** | `TrendlyneClient.constituents(group)` |
| the per-industry aggregate block beside a sector's constituent list, **no second request** | `TrendlyneClient.industry_aggregate(sector)` |
| the same classification on every OHLC frame, as `{id, slug, name}` mappings or `None` | `attrs["bfinance_trendlyne_sector"]`, `attrs["bfinance_trendlyne_industry"]` |

Two limits worth stating, because the earlier omission is what hid them. **`TrendlyneGroup` is not a
rename of the deleted `bfinance.sector` names**: it requires `kind`, `group_id`, `slug` and `name`,
rejects a `kind` other than `sector`/`industry` and a `group_id <= 0`, and **no id in the module is
hardcoded or guessed**, so there is no constructor path from a bare name (you resolve the name
through a symbol or an id first). And the market-cap **unit is not asserted**:
`attrs["bfinance_trendlyne_market_cap_unit"] == "unverified"`, so use the `Weight %` column, which
is exact and unit-free. `docs/RELEASE_0.2.0.md` section 1 is the release note for the removal, and
`docs/YFINANCE_MIGRATION_GUIDE.md` section 3.D carries the migration shape
for a user coming from `yfinance`'s `Sector`/`Industry`.

**`share-holding` is snapshot-only, and that is a property of the route, not of the parameter.**
`Jun-2024` and `2024` in the path both return **HTTP 500** — measured on MOTHERSON 2026-09-27 and
re-verified on TCS 2026-10-10 with `Jun-2026`, which is also 500. Not 404, not an empty table: a
deterministic server error. So `share_holding_async` refuses a non-`latest` period **before** the
request and costs zero requests; a blanket guard across the MF route would have been the wrong fix
for it, and is the counter-example recorded above. One `latest` request is still worth having —
that single page embeds every quarter it holds (12 quarter columns on the TCS capture, 44 distinct
quarter labels on MOTHERSON).

### ⚠ CORRECTION, 2026-10-10: the "empty client-rendered shells" list splits in two, and three of the six are reachable

The old claim was flat and, as a list, false:

> **Not scrapeable — confirmed empty client-rendered shells** (after stripping scripts: 2,000–2,500
> chars, only `nav-item`/`dropdown-item` classes): `FORECASTER`, `Technicals`, `Consensus`, `About`,
> `Charts & Report`, `F&O`, and all ~110 `/equity/etf-dashboard/…` pages. Use the NSE FO bhavcopy
> instead of reverse-engineering the `smartoptions` SPA.

**The per-page observation was true and is not retracted: those HTML pages really do render as
empty shells** (re-verified 2026-10-10 — `/equity/about/` is 97 KB with **zero** server-rendered
tables). **The conclusion drawn from it was false**, because "this page has no tables" and "this
data is unreachable" are different claims, and for three of the six the vendor serves the content
on a *different* route that the study never looked for. Measured 2026-10-10:

| page | the page itself | what is actually reachable | how |
|---|---|---|---|
| **`About`** | 97 KB, 0 SSR tables — shell | **reachable** | `/equity/overview-second-part/{id}/?format=json` → `companyProfileData`: `directorsTableData`, `topExecutivesTableData`, `companyDescription`, `websiteUrl`; plus `eventsData` (board meeting / dividend / bonus / split / rights). Parsed from `overview_second_part_tcs.json` into a `(44, 12)` directors-and-executives frame + 5 event frames (board_meeting `(5,3)`, dividend `(5,5)`, bonus `(3,3)`, split `(0,4)`, rights `(0,5)` — the two zeroes are TCS having neither, not a parse failure). The `ceo-salary-history/{ID}/{name}/` links on the page are **login-walled** |
| **`Technicals`** | 2 small SSR tables (volume, pivots) — not even a shell | **reachable** | `/equity/api/stock/adv-technical-analysis/{id}/24/?format=json`, **robots-clean** (see the robots-pattern analysis above). `parameters` holds 56 top-level keys on both recorded captures (TCS and NTPC) |
| **`F&O`** | 73 KB SPA, 0 SSR tables | **reachable, on a different host** | `smartoptions.trendlyne.com/phoenix/api/fno/option/chain/?stockCode=TCS&expDate=2026-10-27` → 276 KB, `body.tableData` + 35 `tableHeaders` keyed by `unique_name`, `maxPain=2140.0`, `atTheMoney`, `MWPL`, `stockLevelData` (PCR), `expiryLevelPCRValues`, `tableDataV2`. Shipped as `bfinance.trendlyne.smartoptions.SmartOptionsClient`, opt-in `allow_smart_options` (default `False`). ⚠ **`expDate` must be ISO `YYYY-MM-DD` — `27-Oct-2026` returns HTTP 500**; and `body.IV` is `null` **by design**, never an error |
| **`Consensus`** | client-rendered | **truly unreachable — no clean JSON sibling** | `/equity/consensus-estimates/json-data/1372/` returns **404** (measured). Consensus targets are **derived** from the research-reports `Target` column instead, excluding the vendor's own aggregate row. No endpoint was invented to fill this |
| **`FORECASTER`** | shell | **not addressed** | no route built, and none claimed |
| **`Charts & Report`** | shell | **partly reachable** | `/share-price/price-performance-analysis/{id}/?format=json` → `body.{lastModified, returnsComparison, returnsDeepDive, returnsPatternData, dailyData}` (14 KB on the recorded capture). Note this route's `head.statusDescription` reads `"success"` in **lower case** where every other trendlyne route sends `"Success"` — which is the concrete reason success is decided on `head.status == "0"` and never on the description |
| **~110 `/equity/etf-dashboard/…`** | shells | **not addressed** | unchanged, and no ETF-dashboard route is claimed |

**The two-list form is the honest one**, and it is worth restating why: a client-rendered page is
evidence about that *page*, not about the site. Trendlyne serves several of these datasets as
first-class JSON behind the very pages that render nothing — finding them required reading the
markup for a `data-fetchurl` on the empty containers, which is how `overview-second-part` and
`adv-technical-analysis` were found.

**What is still genuinely out**, stated as a list rather than a hedge: the **consensus page** (the
one measured 404, with no sibling found), **`/equity/delivery-analysis/`** — 98 KB,
client-rendered, **no clean JSON found on 2026-10-10** — `FORECASTER`, `Charts & Report`'s remaining
sections, and the ETF dashboards. No smartoptions-SPA reverse-engineering is needed for F&O any more;
that route is read directly.

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
