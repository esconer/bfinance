"""
NSE BhavCopy — truthful end-of-day OHLCV, straight from NSE's own archives.

Why this module exists
----------------------
``bfinance.market.ohlcv.OHLCVEngine`` builds bars from a **close-only** feed: it sets
``Open = previous Close`` and wraps ``High``/``Low`` in a fixed +-0.2% envelope. That fabricates
the intraday range, and therefore corrupts ATR, true range, Parkinson and Garman-Klass
volatility, drawdown and every candlestick pattern computed from them. Worse, because the
envelope is a *fixed proportion*, the error is correlated across symbols — a cross-sectional
screen cannot detect it.

Every frame produced here carries::

    df.attrs["bfinance_synthetic_ohlc"] = False

That is the entire point of the module. :mod:`bfinance.nse.marketlens` sets
``bfinance_marketlens_partial_coverage = True`` because it has *no* O/H/L; this sets the flag to
``False`` because it has *real* O/H/L. A consumer can finally tell the two apart.

This module never fabricates. If a field is missing for an instrument it stays ``NaN`` and the
count is recorded in ``attrs["bfinance_missing_ohlcv"]``. Nothing is filled, interpolated or
derived.

Endpoints — verified live 2026-09-27
------------------------------------
Base ``https://nsearchives.nseindia.com``. **No cookie, no API key, no anti-bot to evade** — a
browser ``User-Agent`` is the whole requirement.

UDiFF (2024-01-01 -> present), 34 columns::

    /content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
    /content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip   (37165 rows on 2026-09-25)

Legacy (pre-2024), 14 uppercase columns::

    /content/historical/EQUITIES/{yyyy}/{MMM}/cm{dd}{MMM}{yyyy}bhav.csv.zip

Probed and confirmed: ``20260925`` -> 200, ``20240101`` -> 200, ``20231229`` on the legacy route
-> 200 while the UDiFF route 404s for the same date. So the cutover is a hard boundary and
:data:`UDIFF_CUTOFF` is where it sits. NSE has **retired** the legacy route for recent dates
(``2026/SEP/cm25SEP2026bhav.csv.zip`` -> 404), so a legacy URL for a post-2024 date is a 404, not
a fallback. There is no legacy derivatives route that was verified, so ``exchange="FO"`` is
UDiFF-only and :func:`validate_exchange` rejects a pre-cutover FO date locally.

Upstream traps recorded here, because each one silently corrupts data if ignored
---------------------------------------------------------------------------
1. **Never parse by column position.** The 2024-01-01 UDiFF file has a **35-field header** and
   **34-field data rows** — a malformed CSV with a trailing comma on the header line, which
   pandas surfaces as a junk ``Unnamed: 34`` column. The same file also names its tail columns
   ``Rsvd01``..``Rsvd04`` where the 2026 file uses ``Rsvd1``..``Rsvd4``. Both schemas are handled
   by looking columns up **by name** (:data:`_SCHEMAS`), never by offset.
2. **A bhavcopy is not equities, but ``EQ`` alone is not equities either.** ``SctySrs`` mixes many
   series. The raw 2026-09-25 file holds 3654 rows of which only 2660 are ``EQ``; the rest include
   40 gold bonds (``GB``, e.g. ``SGBJUN28``), 239 ``BE`` and 380 ``SM``. So a filter is
   mandatory. But ``BE`` (trade-to-trade) is an **equity** series and belongs in the default,
   because a new listing debuts in it: ITCHOTELS prints ``SctySrs="BE"`` from 2025-01-29
   (``FinInstrmId`` 29253) and first prints ``"EQ"`` on 2025-02-11 (``FinInstrmId`` 29251), so an
   ``EQ``-only read lost its first 9 sessions and mis-dated the listing by 13 days while returning
   a well-formed, non-empty frame. :data:`EQUITY_SERIES` is therefore ``('EQ', 'BE')``. See
   :data:`EQUITY_SERIES` for the full measurement, including why ``series=None`` is not the answer
   (22.6% of those rows are gold bonds, T-bills, REITs, NCDs and SME).
3. **A 404 is an expected outcome, not an error.** A weekend, an exchange holiday, or a date
   outside the route's coverage all yield 404. That is different from a network failure or a 5xx,
   which are real problems: those raise. The two conditions are never conflated.
4. **Legacy ``SYMBOL`` is not always a ticker.** Verified numeric scrip codes on non-EQ rows
   (``1018GS2026``, ``182D080623``). Do not assume the column is alphabetic.
5. **Dates come back in two different string formats**: UDiFF ``TradDt`` is ``YYYY-MM-DD``,
   legacy ``TIMESTAMP`` is ``dd-MMM-YYYY``.
6. **No holiday calendar is bundled.** :func:`iter_trading_days` skips Saturdays and Sundays
   without issuing a request, which removes ~2/7 of the traffic; exchange holidays still cost one
   cheap 404 each. A real trading calendar would need to be maintained, and a stale one is worse
   than a 404, so this module does not have one.
7. **A scrip keeps its ticker across an ``EQ``/``BE`` migration, and is never in both on one day.**
   Measured over 15 archives (2025-01-27..2025-02-14): 43 distinct symbols changed series, in both
   directions, and same-day ``EQ``-and-``BE`` overlap was **0** on every one of the 15 days. That
   zero is what makes the ``EQ``+``BE`` union safe — it cannot manufacture a duplicate
   ``(Date, Symbol)``, and a migrated listing stitches into one continuous price series. It also
   explains why the migration is not a rare edge case: it is a routine, both-ways, monthly-scale
   event on a ~2,300-symbol board.

A note on the ``TickerNotFoundError`` reused for a missing date
---------------------------------------------------------------
A 404 for a non-trading date raises :class:`~bfinance.utils.exceptions.TickerNotFoundError` under
``raise_errors=True`` / ``on_missing="raise"``. That is a deliberate reuse of an existing
``BFinanceError`` rather than a new type: no exception was added to
``bfinance.utils.exceptions``. The name reads oddly for a date, so the message always names the
date and says plainly that NSE published no file for it. By default (no flags) the same condition
returns an **empty frame** with the columns intact and ``bfinance_synthetic_ohlc`` still
``False`` — never a synthesised row.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import threading
import time
import zipfile
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple, Union

import httpx
import pandas as pd

from bfinance.utils.exceptions import (
    ParsingError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

logger = logging.getLogger("bfinance")

#: Provenance tag recorded on every frame.
PROVENANCE = "nse_bhavcopy"

#: The flag this module exists to set truthfully. Always ``False`` on a frame from here.
SYNTHETIC_OHLC_ATTR = "bfinance_synthetic_ohlc"

#: Attr listing requested dates for which NSE published no file (weekend, holiday, out of range).
MISSING_DATES_ATTR = "bfinance_bhavcopy_missing_dates"

#: Attr recording which series the returned rows *actually* came from, measured from the rows
#: rather than echoed from the request. It differs from ``bfinance_series`` exactly when it is
#: useful: ask for ``("EQ", "BE")`` on a file that has no ``BE`` line today and this reads
#: ``("EQ",)``. A consumer that needs to know what it got reads this one key.
SERIES_INCLUDED_ATTR = "bfinance_series_included"

#: Exact output contract — must match what ``OHLCVEngine`` produces (minus the adjusted/action
#: columns, which this feed does not carry).
COLUMNS: Tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

#: First date served by the UDiFF route. Verified: the UDiFF URL 404s for 2023-12-29 while the
#: legacy URL returns 200 for the same day.
UDIFF_CUTOFF = _date(2024, 1, 1)

#: Accepted ``exchange`` values.
EXCHANGES: Tuple[str, ...] = ("CM", "FO")

#: The series a **default** read covers: cash equities plus the trade-to-trade segment.
#:
#: ``BE`` is here because a newly listed scrip *debuts* in it. Measured on ITCHOTELS over
#: 2025-01-26..2025-02-14 (15 archives, 44,044 rows): the raw files carry ``ITCHOTELS`` from
#: **2025-01-29** as ``SctySrs="BE"`` (``FinInstrmId`` 29253) and first as ``"EQ"``
#: (``FinInstrmId`` 29251) on **2025-02-11**. A ``series="EQ"``-only default therefore lost the
#: first **9** sessions and mis-dated the listing by **13** calendar days — silently, because the
#: frame it returned was non-empty and well-formed. The same set migration runs *backwards* too:
#: 43 distinct symbols changed series inside that one 15-day window, including single-day flips
#: (POWERMECH was ``BE`` on 2025-02-05 and ``EQ`` on every other day), so an ``EQ``-only filter
#: punches single-session holes in the middle of an established history too, not just at the start.
#:
#: Only these two. ``EQ``+``BE`` is a strict superset of the old default and still excludes every
#: non-equity: over the same 15 archives ``series=None`` would have added 9,964 rows (22.6%) of
#: gold bonds (``GB``, e.g. ``SGBDE30III`` at 8223.30), G-sec loans (``GS``), T-bills (``TB``),
#: REITs (``RR``, ``EMBASSY`` at 365.26), SME (``SM``), structured notes (``BZ``/``E1``), NCDs
#: (``N2``) and invit funds (``IV``). Measured same-day ``EQ``/``BE`` symbol overlap: **0**, on all
#: 15 archives, so this union never creates a duplicate ``(Date, Symbol)``.
EQUITY_SERIES: Tuple[str, ...] = ("EQ", "BE")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

#: Map from a logical field to the *column name* upstream uses, per schema. Position is never
#: used — see trap 1 in the module docstring.
_SCHEMAS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "udiff": {
        "symbol": ("TckrSymb",),
        "series": ("SctySrs",),
        "date": ("TradDt",),
        "open": ("OpnPric",),
        "high": ("HghPric",),
        "low": ("LwPric",),
        "close": ("ClsPric",),
        "volume": ("TtlTradgVol",),
    },
    "legacy": {
        "symbol": ("SYMBOL",),
        "series": ("SERIES",),
        "date": ("TIMESTAMP",),
        "open": ("OPEN",),
        "high": ("HIGH",),
        "low": ("LOW",),
        "close": ("CLOSE",),
        "volume": ("TOTTRDQTY",),
    },
}

#: Schema dispatch order. ``udiff`` is checked first; the two are disjoint by column name.
_SCHEMA_ORDER: Tuple[str, ...] = ("udiff", "legacy")

#: Date string format per schema (trap 5).
_DATE_FORMATS: Dict[str, str] = {"udiff": "%Y-%m-%d", "legacy": "%d-%b-%Y"}

# Global pacing state shared across all client instances, ported from
# ``bfinance.screener.client`` so concurrent clients respect one rate limit instead of
# bursting N x. Deliberately not a second, private scheme.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_SERIES_RE = re.compile(r"^[A-Z0-9]{1,4}$")


# ------------------------------------------------------------------ validation helpers


def validate_exchange(exchange: str) -> str:
    """Uppercase and validate the segment before any request."""
    if not isinstance(exchange, str):
        raise ValueError(f"exchange must be a string, got {type(exchange).__name__}")
    clean = exchange.strip().upper()
    if clean not in EXCHANGES:
        raise ValueError(
            f"exchange={exchange!r} is not supported. Must be one of {', '.join(EXCHANGES)}."
        )
    return clean


def validate_series(series: str) -> str:
    """Uppercase and validate **one** series code before any request.

    A bhavcopy mixes many series in one file; an unmatched filter is the same failure mode as a
    bad symbol — a silent zero-row frame — so it is rejected locally instead.

    Kept single-valued on purpose: it is the public one-code validator, and a caller that already
    holds a ``str`` wants a ``str`` back. :func:`resolve_series` is the multi-code entry point and
    calls this.
    """
    if not isinstance(series, str):
        raise ValueError(f"series must be a string, got {type(series).__name__}")
    clean = series.strip().upper()
    if not clean:
        raise ValueError("series must be a non-empty string (e.g. 'EQ', 'BE', 'GB')")
    if not _SERIES_RE.match(clean):
        raise ValueError(
            f"series={series!r} is not a series code. Expected 1-4 letters/digits, e.g. 'EQ'."
        )
    return clean


#: What a caller may pass as ``series``. ``None`` means "no filter at all" (every series the file
#: holds — gold bonds, T-bills, NCDs included, so it is opt-in and never a default).
SeriesSpec = Union[None, str, Iterable[str]]


def resolve_series(series: SeriesSpec) -> Optional[Tuple[str, ...]]:
    """Normalise a ``series`` argument into the tuple of codes to keep, or ``None`` for "no filter".

    Three accepted shapes, all unambiguous:

    * a ``str`` — exactly as before, one code, via :func:`validate_series`. ``"eq"`` -> ``("EQ",)``.
    * an iterable of ``str`` — normalised, de-duplicated, order-preserving, each code validated.
    * ``None`` — **no series filter at all**, every row upstream sent is kept.

    ``None`` is returned rather than an empty tuple on purpose: an empty tuple is what
    ``series=[]`` would naturally mean, and "keep nothing" and "keep everything" are opposite
    answers, so the distinction is carried by ``None`` and ``[]`` is rejected outright.

    An unmatched code is *not* rejected — a series simply absent from one day's file is a valid
    answer (zero rows for that series), and a bhavcopy's series mix changes day to day.
    """
    if series is None:
        return None
    if isinstance(series, str):
        return (validate_series(series),)
    if isinstance(series, (bytes, bytearray)):
        raise ValueError(f"series must be a string or an iterable of strings, got {type(series).__name__}")
    try:
        codes = list(series)
    except TypeError:
        raise ValueError(
            f"series must be None, a string, or an iterable of strings, got "
            f"{type(series).__name__}"
        ) from None
    resolved: List[str] = []
    for item in codes:
        if not isinstance(item, str):
            raise ValueError(
                f"series={series!r} contains a non-string entry {item!r} of type "
                f"{type(item).__name__}"
            )
        clean = validate_series(item)
        if clean not in resolved:
            resolved.append(clean)
    if not resolved:
        raise ValueError(
            "series=[] keeps nothing, which is a silent zero-row frame. Pass series=None for "
            "'every series in the file', or name at least one code."
        )
    return tuple(resolved)


def series_request_attr(series: SeriesSpec) -> Union[None, str, Tuple[str, ...]]:
    """The value recorded in ``attrs['bfinance_series']``: what the *caller asked for*.

    A single string stays a string, because ``attrs['bfinance_series'] == 'GB'`` is an existing
    contract that callers and tests read. A collection stays a normalised tuple and ``None`` stays
    ``None`` ("no filter was applied"). What the rows actually carry is
    :data:`SERIES_INCLUDED_ATTR`, which is measured, not echoed.
    """
    if isinstance(series, str):
        return validate_series(series)
    if series is None:
        return None
    return resolve_series(series)


def to_date(value: Union[str, _date, datetime, pd.Timestamp]) -> _date:
    """Coerce ``str`` / ``date`` / ``datetime`` / ``Timestamp`` to a plain :class:`datetime.date`.

    ``date`` is used rather than ``Timestamp`` throughout: the archive URL is built from calendar
    fields, and a tz-aware input must not shift the day.
    """
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        parsed = pd.to_datetime(value.strip(), errors="coerce")
        if parsed is pd.NaT or pd.isna(parsed):
            raise ValueError(
                f"date={value!r} is not parseable. Expected 'YYYY-MM-DD', 'YYYYMMDD', "
                f"or a date/datetime."
            )
        return parsed.date()
    raise ValueError(
        f"date must be a str, date or datetime, got {type(value).__name__}"
    )


def archive_url(day: Union[str, _date, datetime, pd.Timestamp], exchange: str = "CM") -> str:
    """Return the archive URL for one trading day, choosing the route by date.

    Pure and offline — no request is made. Exposed so a caller can see exactly which of the two
    verified routes a date will hit before spending a request on it.
    """
    clean_exchange = validate_exchange(exchange)
    clean_day = to_date(day)
    if clean_day >= UDIFF_CUTOFF:
        return (
            f"https://nsearchives.nseindia.com/content/{clean_exchange.lower()}"
            f"/BhavCopy_NSE_{clean_exchange}_0_0_0_{clean_day:%Y%m%d}_F_0000.csv.zip"
        )
    if clean_exchange != "CM":
        raise ValueError(
            f"exchange={clean_exchange!r} has no verified legacy archive: derivatives coverage "
            f"starts at the UDiFF cutover ({UDIFF_CUTOFF:%Y-%m-%d}). Requesting "
            f"{clean_day:%Y-%m-%d} would be a guaranteed 404."
        )
    month = _MONTHS[clean_day.month - 1]
    return (
        "https://nsearchives.nseindia.com/content/historical/EQUITIES"
        f"/{clean_day:%Y}/{month}/cm{clean_day:%d}{month}{clean_day:%Y}bhav.csv.zip"
    )


def is_weekday(day: Union[str, _date, datetime, pd.Timestamp]) -> bool:
    """True for Mon-Fri. NSE trades Monday to Friday; Saturday and Sunday never have a file."""
    return to_date(day).weekday() < 5


def iter_trading_days(start, end) -> List[_date]:
    """Every Mon-Fri day in ``[start, end]``, ascending.

    A *candidate* list, not the exchange calendar: NSE holidays are not bundled, so a holiday is
    still attempted and costs one cheap 404. Skipping weekends alone removes ~2/7 of the traffic
    without needing a holiday list that would go stale. Raises ``ValueError`` if ``end < start``.
    """
    first, last = to_date(start), to_date(end)
    if last < first:
        raise ValueError(f"end={last} is before start={first}")
    days: List[_date] = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _run_async(coro):
    """Bridge to sync, matching ``nse.marketlens._run_async`` and ``Ticker._run_async``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


# ------------------------------------------------------------------ parsing


def _detect_schema(columns: List[str]) -> str:
    """Identify the schema from the *presence of column names*, never from their count.

    ``_SCHEMAS[schema]["symbol"][0]`` plus the date column is enough: ``TckrSymb``/``TradDt``
    only exist in UDiFF, ``SYMBOL``/``TIMESTAMP`` only in legacy.
    """
    available = set(columns)
    for schema in _SCHEMA_ORDER:
        names = _SCHEMAS[schema]
        if names["symbol"][0] in available and names["date"][0] in available:
            return schema
    raise ParsingError(
        f"bhavcopy CSV has no recognised schema. Looked for "
        f"{[(_SCHEMAS[s]['symbol'][0], _SCHEMAS[s]['date'][0]) for s in _SCHEMA_ORDER]}; "
        f"got {len(columns)} columns starting {columns[:4]!r}. Upstream schema changed."
    )


def unzip_bhavcopy(payload: bytes, url: str = "") -> str:
    """Decode a bhavcopy ZIP into the text of its single CSV member.

    A bhavcopy ZIP holds exactly one ``.csv``. A multi-member archive is treated as a schema
    change rather than guessed at, because silently reading the wrong member would produce
    plausible, wrong bars.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [n for n in archive.namelist() if not n.endswith("/")]
            if len(members) != 1:
                raise ParsingError(
                    f"bhavcopy archive{(' for ' + url) if url else ''} has {len(members)} "
                    f"members ({members[:5]}); expected exactly one CSV."
                )
            return archive.read(members[0]).decode("utf-8-sig", errors="replace")
    except zipfile.BadZipFile as exc:
        raise ParsingError(
            f"bhavcopy archive{(' for ' + url) if url else ''} is not a readable ZIP. "
            f"An HTML error page served with a 200 is the usual cause."
        ) from exc


def parse_bhavcopy(
    csv_text: str,
    *,
    series: SeriesSpec = EQUITY_SERIES,
    schema: Optional[str] = None,
    url: str = "",
) -> pd.DataFrame:
    """Turn bhavcopy CSV text into an OHLCV frame. Pure — no network, no clock.

    Returns a ``MultiIndex`` ``(Date, Symbol)`` with level 0 a tz-naive ``DatetimeIndex`` named
    ``Date`` (normalised to midnight, matching ``nse.marketlens``' daily convention —
    ``OHLCVEngine`` localises to IST itself). Columns are exactly :data:`COLUMNS`, sorted
    ascending, **one row per instrument**.

    Args:
        series: Which series to keep — a code (``'EQ'``), an iterable of codes, or ``None`` for no
            filter. Defaults to :data:`EQUITY_SERIES` = ``('EQ', 'BE')``, **not** ``('EQ',)``: a
            new listing debuts in ``BE`` and migrates to ``EQ`` weeks later, so an ``EQ``-only
            read silently drops the first sessions of every recent listing and mis-dates it. The
            price/volume are the same whether a scrip prints in ``EQ`` or ``BE``; only the series
            label differs, and dropping ``BE`` drops *data*, not noise. ``None`` is accepted but
            never a default — it pulls in gold bonds, T-bills, REITs, NCDs and SME rows, which
            carry entirely plausible prices near 8,000 and would land in a portfolio as
            instruments.

    .. warning::
       The ticket specified "a ``DatetimeIndex`` named ``Date``", but a bhavcopy is a
       **cross-section**: the 2026-09-25 CM file holds 2660 ``EQ`` rows. A flat ``DatetimeIndex``
       can hold only one row per timestamp, so returning one would silently discard 2659 of 2660
       instruments — the same class of quiet data loss this module exists to end. The symbol is
       therefore carried as index level 1 rather than as a column, which keeps the column list
       *exactly* :data:`COLUMNS` while losing nothing. A single instrument comes back with
       ``frame.xs("RELIANCE", level="Symbol")``, which is a plain ``DatetimeIndex`` named ``Date``.

    Every column is looked up **by name**. Numeric coercion is ``errors="coerce"``: a blank or
    malformed field becomes ``NaN`` and is counted in ``attrs["bfinance_missing_ohlcv"]``. It is
    never filled.
    """
    clean_series = series_request_attr(series)
    wanted = resolve_series(series)
    frame = pd.read_csv(
        io.StringIO(csv_text), dtype=str, keep_default_na=False, na_values=[""]
    )
    # Junk columns from a malformed header (the 35-field UDiFF header yields 'Unnamed: 34').
    frame = frame.drop(columns=[c for c in frame.columns if str(c).startswith("Unnamed:")])

    resolved = schema or _detect_schema(list(frame.columns))
    if resolved not in _SCHEMAS:
        raise ParsingError(f"schema={resolved!r} is not one of {', '.join(_SCHEMAS)}")
    names = _SCHEMAS[resolved]

    missing_names = {
        field: candidates[0]
        for field, candidates in names.items()
        if not any(c in frame.columns for c in candidates)
    }
    if missing_names:
        raise ParsingError(
            f"{resolved} bhavcopy is missing column(s): "
            f"{ {f: n for f, n in missing_names.items()} }. Upstream schema changed."
        )

    # Bind each logical field to the column name actually present, once. Every read below goes
    # through this map, so a column can never be fetched by offset.
    bound: Dict[str, str] = {}
    for field, candidates in names.items():
        bound[field] = next(c for c in candidates if c in frame.columns)

    # Series filter FIRST (trap 2): a bhavcopy is not equities, and its GB rows carry real-looking
    # gold-bond prices that would otherwise pass straight into a portfolio. Filtering before any
    # other read is what keeps the row counts aligned. The column is normalised ONCE and reused for
    # both the mask and the "what did I actually get" attr — a second .str.upper() over ~3,600 rows
    # per day would be waste for a value the frame must report anyway.
    series_col = frame[bound["series"]].astype(str).str.strip().str.upper()
    # isin, not ==, so the default can be a set. wanted is None only for an explicit series=None.
    kept = frame if wanted is None else frame[series_col.isin(wanted)]
    dropped_by_series = int(len(frame) - len(kept))
    # Measured from the rows, not echoed from the request: these are the series actually present.
    series_included = tuple(
        sorted(kept[bound["series"]].astype(str).str.strip().str.upper().unique())
    )

    symbols = kept[bound["symbol"]].astype(str).str.strip().to_numpy()
    raw_dates = kept[bound["date"]].astype(str).str.strip()
    index = pd.to_datetime(raw_dates, format=_DATE_FORMATS[resolved], errors="coerce")
    if index.isna().any():
        # Tolerate an upstream format change rather than silently producing NaT rows.
        index = pd.to_datetime(raw_dates, errors="coerce", dayfirst=True)

    index = pd.DatetimeIndex(index.to_numpy()).normalize()
    out = pd.DataFrame(index=pd.MultiIndex.from_arrays([index, symbols], names=["Date", "Symbol"]))
    for field, label in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close")):
        out[label] = pd.to_numeric(kept[bound[field]], errors="coerce").to_numpy()
    out["Volume"] = pd.to_numeric(kept[bound["volume"]], errors="coerce").to_numpy()

    # An instrument whose date did not parse has no location in time. It is dropped, counted, and
    # never given an invented timestamp. An instrument with no symbol is likewise unidentifiable.
    unparsed_dates = int(out.index.get_level_values("Date").isna().sum())
    if unparsed_dates:
        out = out[out.index.get_level_values("Date").notna()]
    nameless = int(pd.isna(out.index.get_level_values("Symbol")).sum())
    if nameless:
        out = out[out.index.get_level_values("Symbol").notna()]

    missing_ohlcv = {
        label: int(out[label].isna().sum())
        for label in COLUMNS
        if int(out[label].isna().sum()) > 0
    }

    # Volume is int64 when it is complete, float64 when a value is genuinely absent. Casting
    # blind would turn a missing volume into a bogus integer.
    if out["Volume"].isna().any():
        out["Volume"] = out["Volume"].astype("float64")
    else:
        out["Volume"] = out["Volume"].astype("int64")

    # A bhavcopy carries one row per instrument per day, so a duplicated (Date, Symbol) means the
    # upstream file itself repeated a row. Keep the last, but record it rather than hide it.
    duplicates = int(out.index.duplicated(keep="last").sum())
    if duplicates:
        out = out[~out.index.duplicated(keep="last")]

    out = out.sort_index(kind="stable")
    out = out[list(COLUMNS)]

    out.attrs[SYNTHETIC_OHLC_ATTR] = False
    out.attrs["bfinance_provenance"] = PROVENANCE
    out.attrs["bfinance_schema"] = resolved
    out.attrs["bfinance_series"] = clean_series
    out.attrs[SERIES_INCLUDED_ATTR] = series_included
    out.attrs["bfinance_source_row_count"] = int(len(frame))
    out.attrs["bfinance_rows_dropped_by_series_filter"] = dropped_by_series
    out.attrs["bfinance_missing_ohlcv"] = missing_ohlcv
    out.attrs["bfinance_unparsed_date_rows"] = unparsed_dates
    out.attrs["bfinance_nameless_rows"] = nameless
    out.attrs["bfinance_duplicate_date_rows"] = duplicates
    out.attrs["bfinance_symbol_count"] = int(out.index.get_level_values("Symbol").nunique())
    return out


def empty_bhavcopy_frame(attrs: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """An empty OHLCV frame with the right columns and index — the honest shape of 'no data'.

    This is what a 404, a weekend or an exchange holiday yields. It is *never* a synthesised row,
    and it still reports ``bfinance_synthetic_ohlc = False`` so a consumer can tell "NSE had
    nothing for that date" from "this feed fabricates".
    """
    out = pd.DataFrame(
        {label: pd.Series(dtype="int64" if label == "Volume" else "float64") for label in COLUMNS},
        index=pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex([], name="Date"), pd.Index([], dtype="object", name="Symbol")],
            names=["Date", "Symbol"],
        ),
    )
    if attrs:
        out.attrs.update(attrs)
    # Set last, so a caller-supplied attrs dict can never suppress the flag. The whole point of
    # this module is that this key is trustworthy, so it is not overridable.
    out.attrs[SYNTHETIC_OHLC_ATTR] = False
    return out


# ------------------------------------------------------------------ client


class BhavcopyClient:
    """Client for NSE's published end-of-day bhavcopy archives.

    Stateless per call and safe to reuse: the archive route has no session, no cookie and no
    cache-staleness problem. A long-lived instance is fine, and sharing one is *preferred* because
    request pacing is coordinated globally.
    """

    BASE_URL = "https://nsearchives.nseindia.com"

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 250.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0

    # ------------------------------------------------------------ internals

    def _headers(self) -> Dict[str, str]:
        """Browser headers only. No cookie priming, no landing-page fetch — not required here."""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/zip, application/octet-stream, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _throttle_pacing(self) -> None:
        """Pace requests against a process-global clock.

        Ported from ``bfinance.screener.client._throttle_pacing`` on purpose: a 250 ms floor plus
        jitter keeps a multi-year ``bhavcopy_range`` from tripping NSE's edge. The ``range`` walk
        issues one request per candidate trading day, so this is the difference between a few
        thousand files over hours and a block.
        """
        global _GLOBAL_LAST_REQUEST_TIME
        with _GLOBAL_PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms < self.min_request_interval_ms:
                wait_sec = (self.min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(
                    0.01, 0.05
                )
                _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
            else:
                wait_sec = 0.0
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait_sec > 0.0:
            await asyncio.sleep(wait_sec)
        self._last_request_time = time.monotonic()
        with _GLOBAL_PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(_GLOBAL_LAST_REQUEST_TIME, self._last_request_time)

    async def _get_archive(self, url: str) -> Optional[bytes]:
        """Fetch one archive.

        Returns:
            The ZIP bytes, or ``None`` when NSE answered **404** — a weekend, a holiday, or a
            date outside the route's coverage. That is expected, not a failure, and it is kept
            distinct from every other outcome.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, proxy=self.proxy, follow_redirects=True
                ) as client:
                    response = await client.get(url, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code == 404:
                return None

            if response.status_code == 429:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt))
                    continue
                raise RateLimitExceededError(
                    f"NSE bhavcopy archive rate limited (HTTP 429) for {url}. Slow down, or lower "
                    f"the date span."
                )

            if response.status_code >= 500:
                last_error = UpstreamServiceError(
                    f"NSE bhavcopy archive returned HTTP {response.status_code} for {url}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code != 200:
                raise UpstreamServiceError(
                    f"NSE bhavcopy archive returned unexpected HTTP {response.status_code} "
                    f"for {url}"
                )

            return response.content

        raise UpstreamServiceError(
            f"NSE bhavcopy archive failed after {self.max_retries} attempts: {url}"
        ) from last_error

    # -------------------------------------------------------------- public

    async def bhavcopy_async(
        self,
        day: Union[str, _date, datetime, pd.Timestamp],
        *,
        series: SeriesSpec = EQUITY_SERIES,
        exchange: Literal["CM", "FO"] = "CM",
        raise_errors: bool = False,
    ) -> pd.DataFrame:
        """Truthful EOD bars for one trading day, for the whole segment.

        Args:
            day: ``'YYYY-MM-DD'``, ``'YYYYMMDD'``, ``date`` or ``datetime``. The route is chosen
                by date, not by probing: >= :data:`UDIFF_CUTOFF` uses UDiFF, earlier uses legacy.
            series: Series to keep — a code, an iterable of codes, or ``None`` for no filter.
                Defaults to :data:`EQUITY_SERIES` = ``('EQ', 'BE')``. A bhavcopy holds many
                series, so *some* filter is mandatory — see trap 2 in the module docstring — but
                it must include ``BE``, or a scrip's first sessions vanish (see
                :data:`EQUITY_SERIES` for the measured ITC Hotels case).
            exchange: ``'CM'`` (cash market) or ``'FO'`` (F&O). ``'FO'`` is UDiFF-only; a
                pre-cutover FO date raises :class:`ValueError` before any request.
            raise_errors: When ``True``, a day with no archive raises
                :class:`~bfinance.utils.exceptions.TickerNotFoundError` instead of returning an
                empty frame. When ``False`` (default) it returns an empty frame.

        Returns:
            A ``DataFrame`` indexed by ``(Date, Symbol)`` — level 0 a tz-naive ``DatetimeIndex``
            named ``Date`` — with columns exactly :data:`COLUMNS`, ascending, **one row per
            instrument**, and ``attrs['bfinance_synthetic_ohlc'] = False``. Narrow to one symbol
            with ``frame.xs("RELIANCE", level="Symbol")`` to get a plain ``DatetimeIndex``.

            Cross-source check: on 2026-09-24 RELIANCE returned ``Close=1219.2`` and
            ``Volume=13923795``, matching the recorded MarketLens fixture to the rupee and the
            share. A wrong column mapping (``ClsPric`` against ``LastPric``, say) would still look
            plausible, so that agreement is what proves the mapping rather than mere plausibility.

        Raises:
            ValueError: bad ``exchange``, ``series`` or ``day``.
            TickerNotFoundError: no archive for that day, and ``raise_errors=True``.
            UpstreamServiceError: transport failure or a 5xx after retries.
            RateLimitExceededError: HTTP 429 after retries.
            ParsingError: the body was not a single-CSV ZIP, or the columns are unrecognised.
        """
        clean_day = to_date(day)
        clean_series = series_request_attr(series)
        clean_exchange = validate_exchange(exchange)
        url = archive_url(clean_day, clean_exchange)

        # Request-level provenance only. Anything the parser discovers (the resolved schema, the
        # missing-field counts, the series actually present) is left alone — a blind update() here
        # would overwrite the parser's real schema with a placeholder and hide which branch ran.
        base_attrs: Dict[str, Any] = {
            "bfinance_provenance": PROVENANCE,
            "bfinance_series": clean_series,
            "bfinance_exchange": clean_exchange,
            "bfinance_date": clean_day.isoformat(),
            "bfinance_source_url": url,
        }

        payload = await self._get_archive(url)
        if payload is None:
            message = (
                f"NSE published no bhavcopy for {clean_day:%Y-%m-%d} "
                f"({clean_exchange}, series={clean_series}). Expected: a weekend, an exchange "
                f"holiday, or a date outside this route's coverage — not a data error."
            )
            if raise_errors:
                raise TickerNotFoundError(message)
            logger.debug("%s", message)
            return empty_bhavcopy_frame(
                {
                    **base_attrs,
                    MISSING_DATES_ATTR: [clean_day.isoformat()],
                    # No file, so no series were included. Present on every return so a consumer
                    # can read one key without branching.
                    SERIES_INCLUDED_ATTR: (),
                }
            )

        frame = parse_bhavcopy(
            unzip_bhavcopy(payload, url=url), series=clean_series, url=url
        )
        # update(), not assignment: base_attrs must not clobber what the parser established.
        frame.attrs.update(base_attrs)
        # Present on every return so a consumer can read one key without branching. An empty list
        # means "an archive existed"; a populated one means "NSE published nothing for that date".
        frame.attrs[MISSING_DATES_ATTR] = []
        if frame.empty:
            # The file existed but held nothing for this series. Recorded, never back-filled.
            frame.attrs["bfinance_no_rows_for_series"] = True
        return frame

    def bhavcopy(
        self,
        day: Union[str, _date, datetime, pd.Timestamp],
        *,
        series: SeriesSpec = EQUITY_SERIES,
        exchange: Literal["CM", "FO"] = "CM",
        raise_errors: bool = False,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`bhavcopy_async`."""
        return _run_async(
            self.bhavcopy_async(
                day, series=series, exchange=exchange, raise_errors=raise_errors
            )
        )

    async def bhavcopy_range_async(
        self,
        start: Union[str, _date, datetime, pd.Timestamp],
        end: Union[str, _date, datetime, pd.Timestamp],
        *,
        series: SeriesSpec = EQUITY_SERIES,
        exchange: Literal["CM", "FO"] = "CM",
        on_missing: Literal["skip", "raise"] = "skip",
        progress: bool = False,
    ) -> pd.DataFrame:
        """Walk ``[start, end]`` day by day and stack every trading day that has an archive.

        Saturdays and Sundays are skipped *without a request* (trap 6). Exchange holidays are not
        bundled in a calendar, so each costs one cheap 404. Requests are strictly sequential and
        globally paced — this is a bulk download, not a fan-out, because NSE blocks aggressive
        clients.

        Args:
            series: Series to keep, as in :meth:`bhavcopy_async`. Defaults to
                :data:`EQUITY_SERIES` = ``('EQ', 'BE')``, so a scrip that debuts in ``BE`` and
                migrates to ``EQ`` weeks later comes back as one continuous history instead of a
                truncated one.
            on_missing: ``'skip'`` (default) records the date in
                ``attrs['bfinance_bhavcopy_missing_dates']`` and moves on. ``'raise'`` aborts on
                the first date with no archive — use it when a gap is a bug, not a holiday.
            progress: Log each day. Off by default so a library call stays quiet.
        """
        if on_missing not in ("skip", "raise"):
            raise ValueError(
                f"on_missing={on_missing!r} is not supported. Must be 'skip' or 'raise'."
            )
        clean_series = series_request_attr(series)
        clean_exchange = validate_exchange(exchange)
        candidates = iter_trading_days(start, end)

        frames: List[pd.DataFrame] = []
        missing: List[str] = []
        observed_series: set = set()
        for candidate in candidates:
            url = archive_url(candidate, clean_exchange)
            if progress:
                logger.info("bhavcopy: %s", url)
            payload = await self._get_archive(url)
            if payload is None:
                missing.append(candidate.isoformat())
                if on_missing == "raise":
                    raise TickerNotFoundError(
                        f"NSE published no bhavcopy for {candidate:%Y-%m-%d} ({clean_exchange}). "
                        f"Use on_missing='skip' to tolerate weekends and exchange holidays."
                    )
                continue
            day_frame = parse_bhavcopy(
                unzip_bhavcopy(payload, url=url), series=series, url=url
            )
            if day_frame.empty:
                continue
            # Accumulated before the attrs are dropped: the range answer to "which series did I
            # actually get" is the union over the days that returned rows, which is a measurement
            # no single day can make.
            observed_series.update(day_frame.attrs.get(SERIES_INCLUDED_ATTR, ()))
            # Drop per-day attrs: after a merge they would be a lie (they'd describe one day, not
            # the whole span). Provenance is re-asserted on the combined frame below.
            day_frame.attrs.clear()
            frames.append(day_frame)

        merged_attrs: Dict[str, Any] = {
            "bfinance_provenance": PROVENANCE,
            "bfinance_series": clean_series,
            SERIES_INCLUDED_ATTR: tuple(sorted(observed_series)),
            "bfinance_exchange": clean_exchange,
            "bfinance_requested_start": to_date(start).isoformat(),
            "bfinance_requested_end": to_date(end).isoformat(),
            "bfinance_candidate_trading_days": len(candidates),
            MISSING_DATES_ATTR: missing,
        }

        if not frames:
            return empty_bhavcopy_frame(merged_attrs)

        combined = pd.concat(frames)
        # Two different days cannot share a (Date, Symbol) pair, so a duplicate here means an
        # upstream file repeated a row. Keep the last, record it.
        if combined.index.duplicated().any():
            merged_attrs["bfinance_duplicate_date_rows"] = int(
                combined.index.duplicated(keep="last").sum()
            )
            combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index(kind="stable")
        combined = combined[list(COLUMNS)]
        combined.attrs.update(merged_attrs)
        combined.attrs[SYNTHETIC_OHLC_ATTR] = False
        combined.attrs["bfinance_symbol_count"] = int(
            combined.index.get_level_values("Symbol").nunique()
        )
        return combined

    def bhavcopy_range(
        self,
        start: Union[str, _date, datetime, pd.Timestamp],
        end: Union[str, _date, datetime, pd.Timestamp],
        *,
        series: SeriesSpec = EQUITY_SERIES,
        exchange: Literal["CM", "FO"] = "CM",
        on_missing: Literal["skip", "raise"] = "skip",
        progress: bool = False,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`bhavcopy_range_async`."""
        return _run_async(
            self.bhavcopy_range_async(
                start,
                end,
                series=series,
                exchange=exchange,
                on_missing=on_missing,
                progress=progress,
            )
        )


_DEFAULT_CLIENT: Optional[BhavcopyClient] = None


def _default_client() -> BhavcopyClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = BhavcopyClient()
    return _DEFAULT_CLIENT


# ------------------------------------------------------------------ module shortcuts


def bhavcopy(
    date: Union[str, _date, datetime, pd.Timestamp],
    *,
    series: SeriesSpec = EQUITY_SERIES,
    exchange: Literal["CM", "FO"] = "CM",
    raise_errors: bool = False,
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`BhavcopyClient.bhavcopy`.

    Truthful EOD bars for the whole segment on one day. Sets
    ``attrs['bfinance_synthetic_ohlc'] = False`` — the fix for the fabricated
    ``Open=prev_close`` / ``High=+0.2%`` bars in :mod:`bfinance.market.ohlcv`.

    ``series`` defaults to :data:`EQUITY_SERIES` = ``('EQ', 'BE')``, so a scrip that debuts in the
    trade-to-trade segment is returned from its first session. Pass ``series='EQ'`` for the old
    behaviour, or ``series=None`` for no filter at all.

    Import from the module, not the package: the package does not re-export this name, because
    ``bfinance.nse.bhavcopy`` is the module.

    >>> from bfinance.nse.bhavcopy import bhavcopy              # doctest: +SKIP
    >>> day = bhavcopy("2026-09-25")                            # doctest: +SKIP
    >>> day.xs("RELIANCE", level="Symbol")["Open"]              # doctest: +SKIP
    1210.5
    """
    return _default_client().bhavcopy(
        date, series=series, exchange=exchange, raise_errors=raise_errors
    )


def bhavcopy_range(
    start: Union[str, _date, datetime, pd.Timestamp],
    end: Union[str, _date, datetime, pd.Timestamp],
    *,
    series: SeriesSpec = EQUITY_SERIES,
    exchange: Literal["CM", "FO"] = "CM",
    on_missing: Literal["skip", "raise"] = "skip",
    progress: bool = False,
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`BhavcopyClient.bhavcopy_range`.

    Walks the NSE trading calendar over ``[start, end]`` and stacks every day that has an
    archive, skipping weekends without a request. Defaults to :data:`EQUITY_SERIES`
    (``'EQ'`` and ``'BE'``), so a listing's first session is not silently dropped.

    >>> from bfinance.nse import bhavcopy_range                # doctest: +SKIP
    >>> ytd = bhavcopy_range("2026-01-01", "2026-09-25")        # doctest: +SKIP
    """
    return _default_client().bhavcopy_range(
        start,
        end,
        series=series,
        exchange=exchange,
        on_missing=on_missing,
        progress=progress,
    )
