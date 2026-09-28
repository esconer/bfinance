# Proposal: a local mirror of the NSE bhavcopy tape in SQLite

**Status: PROPOSED, not implemented.** Written 2026-09-29 so the design can be picked up
without re-deriving it. Every number here was measured against live NSE on that date; the
scripts that produced them are in `.scratch/benchmark-2026-09-28/`, which is untracked.

Environment: Python 3.13.15, pandas 3.0.5, httpx 0.28.1, SQLite 3.53.1, Windows 11.

---

## 1. The problem this solves

A single bhavcopy file carries the **entire market** — about 2,900 symbols. One file per
trading day. The package currently downloads it **once per symbol**, so a twenty-symbol
request downloads the same 200 KB twenty times over.

Measured, same three sessions for the same data:

| path | cost | what it covers |
|---|---|---|
| `bhavcopy_range("2026-09-22", "2026-09-24")` | **9.034 s** | 2,906 symbols x 3 days |
| `Ticker("TCS").history(...)`, 3 days | 16.92 s cold, 12.68 s second | 1 symbol |
| 20 symbols x 80 sessions, projected | ~5,040 s | 20 symbols |

The engine already does the right thing internally — `market/ohlcv.py` documents that the
whole-market range "is fetched ONCE and sliced to `clean_symbol` here, because a single
daily file already covers every NSE scrip: one request per symbol would multiply traffic
for no extra data." **The optimisation exists; the public API cannot reach it across
symbols.** `Ticker.history()` is per-symbol by construction, and `download()` fans those
out with `asyncio.gather` — maximally parallel over exactly the work that should be
maximally shared.

There is also **no cache at all** on either hot path: `nse/bhavcopy.py` imports
`SQLiteCache` zero times, `market/ohlcv.py` mentions `cache` zero times, and a repeated
identical fetch measures **-35% recovery** (4.93 s then 6.66 s — the second is *slower*).

## 2. Where the time actually goes

One file, `BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv.zip`, 203,837 bytes compressed:

| phase | cost | share |
|---|---|---|
| HTTP GET, fresh client per request | 3.99 s | — |
| HTTP GET, one client, keep-alive | **1.32 s** | — |
| unzip | 0.008 s | 0.3% |
| `pd.read_csv` (3,637 rows x 34 cols) | 0.036 s | 1.2% |
| **slice one symbol out of the market frame** | **0.0019 s** | **0.14% of a keep-alive fetch** |

**Parsing is 1% and slicing is 0.4%.** Everything else is waiting for bytes. 2.67 s of every
3.99 s request was TLS handshake, which is why keep-alive is a 3.03x win for free.

## 3. Measured feasibility of the mirror

A real mirror was built from **20 live files (57,703 rows)** and both paths timed.

**Write** — `DataFrame.to_sql`, on the schema in section 5:

| | |
|---|---|
| 20.2 ms per day, 7.0 us per row | |
| 119 bytes per row on disk | 6.9 MB for 57,703 rows |

**Read** — the query that dominates real use, "one symbol, one month", 200 runs:

| | |
|---|---|
| median **0.039 ms**, p99 0.063 ms, max 0.342 ms | from SQLite |
| **5,920 ms** | the same read from the network |

**~152,000x faster on the read path.** Writing the entire 30-year history costs about two
minutes; only fetching costs anything.

**Extrapolated from the measured bytes/row and rows/day:**

| history | days | rows | on disk | insert time |
|---|---|---|---|---|
| 2.7y (the UDiFF era) | 663 | 1,912,854 | **0.23 GB** | 0.2 min |
| 10y | 2,420 | 6,982,063 | **0.83 GB** | 0.8 min |
| 25y (full) | 6,050 | 17,455,158 | **2.08 GB** | 2.0 min |

Wire volume for the full 25 years is **1.23 GB** (6,050 days x 203,837 B, using today's
file size; older files are smaller, so the real total is lower).

## 4. How far back the data goes

Every floor probe returned HTTP 200. Older files are *cheaper*, because fewer symbols were
listed.

| date | era | bytes | verdict |
|---|---|---|---|
| 2024-01-01 | UDiFF | 151,548 | first UDiFF day, matches `UDIFF_CUTOFF` |
| 2023-12-29 | legacy | 99,885 | last legacy day |
| 2019-01-01 | legacy | 65,605 | 200 |
| 2010-01-04 | legacy | 39,269 | 200 |
| 2005-01-03 | legacy | 24,455 | 200 |
| 2001-01-02 | legacy | 23,719 | 200 |
| **1997-01-02** | legacy | 27,718 | **200 — pre-history probe also answers** |

So a full mirror is the whole exchange tape, roughly 30 years, and the legacy era is the
cheap part of it.

## 5. Proposed schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=OFF;

CREATE TABLE ohlcv (
    day      TEXT    NOT NULL,
    symbol   TEXT    NOT NULL,
    series   TEXT    NOT NULL,
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   INTEGER NOT NULL,
    turnover REAL,
    era      TEXT    NOT NULL,
    PRIMARY KEY (day, symbol, series)
) WITHOUT ROWID;

CREATE INDEX ix_symbol_day ON ohlcv (symbol, day);

CREATE TABLE mirror_days (        -- the coverage watermark; see section 6
    day        TEXT PRIMARY KEY,
    era        TEXT NOT NULL,
    n_rows     INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
```

Three decisions in that schema are load-bearing:

- **`PRIMARY KEY (day, symbol, series)`**, not `(symbol, day)`. One row per symbol-day is
  what makes the O(symbols x days) request problem impossible to reintroduce in the storage
  layer. `ix_symbol_day` then serves the read query directly. The key order suits
  backfill (inserts are day-ordered) and the index suits reads.
- **Store the normalised shape, not the 34 raw columns.** `TtlTrfVal`/`TOTTRDVAL` and
  `SYMBOL`/`SERIES` differ between eras; `_SCHEMAS` and `_detect_schema` already resolve
  that, and duplicating it in the DB layer is how the two would drift apart.
- **`WITHOUT ROWID`.** The table *is* the index, so the composite key is stored once
  rather than twice.

`sha256` makes the mirror tamper-evident, the same property the recorded fixtures already
carry via `META.json`.

## 6. The requirement that decides whether this is safe

This is the part that must not be skipped, and it is a correctness requirement rather than
a performance one. Measured on the real mirror:

```
'RELIANCE on a day never downloaded'                  -> 0 rows in 0.124 ms
'RELIANCE on a day before it listed'                   -> also 0 rows
'RELIANCE on a day that exists but has not been fetched' -> ALSO 0 rows
```

**A mirror without `mirror_days` returns 0 for all three.** A caller cannot distinguish a
delisted security from an unfetched day — which is precisely the silent-empty-frame defect
class this package exists to eliminate, relocated from the network into the database.

The read path must therefore answer three questions, not one:

1. is the requested day in `mirror_days`? if not, **go to the network or raise** — never
   return an empty frame;
2. if it is, is the row absent because the security was not listed or not traded that day?
   Absence is then a real answer;
3. today's file is volatile and is republished intraday, so it must be **replaced**, never
   appended, and must not be served as final before the close.

## 7. How the mirror should be used

`bfinance.mirror` with `sync()`, `read()`, `coverage()`, wired as an **optional** `ohlc`
source so that `history()` prefers the mirror and falls back to the network for uncovered
days, keeping the refusal-on-gap behaviour rather than silently returning partial data.

It should subsume the rest of the performance work rather than sit beside it. Keep-alive
and concurrency stop being read-path optimisations and become **backfill and daily-update
optimisations only**, which is where they belong.

## 8. Backfill cost

| approach | 25 years | UDiFF era only |
|---|---|---|
| serial, as the client does it now (4.00 s/day) | 403 min | 44 min |
| keep-alive (1.32 s/day) | 133 min | 15 min |
| keep-alive + 3 in flight (0.64 s/file effective) | **65 min** | **7 min** |

The last row is derived from the *measured* 2.05x concurrency speedup, not from dividing
by the worker count — 3 workers deliver 2.05x, not 3x, because the host does not scale
linearly. An earlier draft of this document claimed 44 min for that row by dividing 133 by
3; that was an assumed speedup and it was wrong.

One-time. After that the daily update is **one file**.

Concurrency was measured, not assumed: 1 in flight 1.32 s/file, 2 → 1.23 s, **3 → 0.64 s
(2.05x)**, 5 → 0.73 s (worse). Three is the peak.

**The rate limiter must be a token bucket shared across the pool and keyed on host**, not a
per-client sleep, or N workers will collectively exceed the floor. At 3 in flight the
observed rate is ~0.94 req/s, inside the client's own 4 req/s floor
(`min_request_interval_ms = 250`), so a shared limiter at the existing floor does not bind.

## 9. Dead ends, measured so they are not re-derived

**Conditional GET does not work on this host.** The server sends
`ETag: W/"203837-1790247782238"` and `Last-Modified`, but `If-None-Match` returns **200, not
304** — a repeat transfers the full body. `HEAD` works (0.397 s), so existence probes are
cheap, but revalidation buys nothing. Do not build a revalidating cache here.

**There is no aggregate file.** No single-request-many-days route exists:

```
200  .../content/cm/BhavCopy_NSE_CM_0_0_0_20260924_F_0000.csv.zip
503  .../content/historical/EQUITIES/2026/09/cm24092026bhav.csv.zip
503  https://archives.nseindia.com/content/historical/EQUITIES/...
404  .../archives/equities/mkt/2026/cm24092026bhav.csv.zip
```

One request per day is the floor. The 503s also mean a client must try both eras rather
than assume one.

**Compression will not help.** `Content-Encoding: none`, and the payload is already a zip
that decompresses in 8 ms.

## 10. Honest costs

- **First run is ~65 minutes** for 30 years, ~7 minutes for the UDiFF era alone. It must be
  an explicit command, never a side effect of `import`.
- **2.08 GB on disk** for the full history; 0.23 GB if only the UDiFF era is mirrored.
- **A full backfill is a burst**: 6,050 files from one IP at ~2.3 req/s. Inside the floor,
  but it should be paced, interruptible, and resumable — and `mirror_days` is what makes a
  partial backfill honest about being partial.
- **Today's file is volatile** and must be replaced, not appended.
- **Two eras, different schemas.** The normalisation stays in `nse/bhavcopy.py`; the mirror
  stores its output.

## 11. Open questions, to answer during implementation

1. **Is 68 KB/s this host or this network path?** 203 KB in 1.32 s is slow for a CDN. If
   NSE is simply far away, a mirror is even more valuable; if it is a local network fault,
   fix that first and re-measure. This changes the priority ordering, not the design.
2. **Is the 3-in-flight peak NSE's or this path's?** Re-measure before hard-coding a worker
   count.
3. **How should the mirror interact with `universe.UniverseArchive`?** Both accumulate
   forward-only state. A mirror that already holds 30 years of membership-by-absence would
   largely solve the survivorship problem that recorder exists to mitigate — but
   `UPSTREAM_SOURCES.md` section 9 records that the per-day securities list
   (`sec_list_{YYYYMMDD}.csv`) returns **404**, so there is no per-day membership file to
   backfill from. The recorder stays necessary. Still worth checking whether `mirror_days`
   plus the `ohlc` table can answer "was this symbol listed on this day" without a second
   store, for symbols that traded.
4. **Does the mirror need the F&O segment too?** `/content/fo/` is verified 200 with 37,165
   rows, and `TtlTrfVal / TtlTradgVol` is structurally unresolvable there. Same schema,
   different unit — probably a separate table rather than a `segment` column, but undecided.

## 11a. Resolved: the coverage watermark has an official source

Open question 3 in `UPSTREAM_SOURCES.md` section 9 settles part of section 6 above.
`https://www.nseindia.com/api/holiday-master?type=trading` returns **HTTP 200, 34,022
bytes, 12 segments**, each row carrying `tradingDate`, `weekDay`, `description`,
`morning_session` and `evening_session`.

So the three-way distinction in section 6 does not need a guessed holiday list:

| question | answered by |
|---|---|
| is this date a trading day at all? | `/holiday-master` |
| have I downloaded it? | `mirror_days` |
| did the security not exist / not trade? | absence from `ohlc` on a day that *is* in `mirror_days` |

**The watermark should be seeded from that route**, which also means a caller can be told
"2026-01-26 was a holiday" rather than merely "no data".

## 12. Why it is worth doing beyond speed

Thirty years of daily OHLCV for every listed NSE security, offline, with no rate-limit
exposure and no network dependency. It enables survivorship-aware backtesting, which
`bfinance.universe` can currently only begin accumulating from today. That is a capability
change, not a performance tweak — which is the argument for doing it properly rather than
bolting on a request cache.
