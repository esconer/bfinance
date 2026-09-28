"""
NSE index close + valuation — India VIX and per-index P/E, P/B, dividend yield, dated.

One route carries both capabilities
-----------------------------------
``https://{host}/content/indices/ind_close_all_{DDMMYYYY}.csv``

:data:`HOSTS` names the two hosts that serve it —
``nsearchives.nseindia.com`` and ``archives.nseindia.com`` — and
``tests/fixtures/nse/ROBOTS_EVIDENCE.json`` records that the second was verified to mirror
the 2026-09-25 file *byte for byte* (17,285 B, identical hash). One dataset, two hosts, so
every frame records which one answered in ``attrs['bfinance_source_host']``.

This is a **dated time series, not a snapshot**. The 2026-09-25 file carries 166 indices and
the 2020-01-01 file 77; a caller walks dates and gets a genuine per-index history. Observed
coverage starts at **2015-01-01**; dates on or before 2010-01-01 return 404. :data:`COVERAGE_START`
records that boundary, and :func:`in_coverage` reports it, but no date is rejected locally: the
boundary was *observed*, not structurally guaranteed, so a local hard-fail would claim more
than the evidence supports.

Verbatim schema — 13 fields, in this order
------------------------------------------
``Index Name, Index Date, Open Index Value, High Index Value, Low Index Value, Closing Index
Value, Points Change, Change(%), Volume, Turnover (Rs. Cr.), P/E, P/B, Div Yield``

:data:`RAW_FIELDS` holds that list and every field is looked up **by name** through
:data:`FIELD_MAP`, never by offset. The output uses the shorter canonical names in
:data:`COLUMNS`, matching the ``bhavcopy`` module's convention of renaming on read.

The ``-`` sentinel is a third state, not a zero and not a blank
---------------------------------------------------------------
India VIX prints ``-`` in Volume, Turnover, P/E, P/B and Div Yield. That means **"not
applicable to this index"** — a volatility index has no traded constituent volume and no
constituent earnings, so a P/E is undefined rather than small. A parser that coerces ``-`` to
``0.0`` publishes a fabricated India VIX volume of zero and a P/E of zero, and both survive
every downstream aggregate silently.

This module therefore keeps three distinguishable states:

==========================  ==========================================  ==========================
state                       on the row                                 how it is reported
==========================  ==========================================  ==========================
**not applicable**          ``pd.NA`` in a nullable ``Int64``/``Float64`` column, never ``0.0``  ``NotApplicable`` column names the fields; counted in ``attrs['bfinance_macro_not_applicable']``
**missing** (blank/unparseable)  ``pd.NA`` as well                       ``Unparsed`` column names the fields; counted in ``attrs['bfinance_macro_unparsed']``
**a real number** — *including a genuine* ``0.0``                         neither column, neither counter
==========================  ==========================================  ==========================

``pd.NA`` and ``0.0`` are trivially separable with ``isna()``; the two ``pd.NA`` reasons are
separated by which counter and which audit column they land in. The nullable dtypes are load
bearing, not cosmetic: a downstream ``.fillna(0)`` on ``Int64`` is a visible mistake, while on
``float64`` it is indistinguishable from an upstream zero.

Units — one is labelled, three are not
--------------------------------------
Only ``Turnover (Rs. Cr.)`` and ``Change(%)`` carry a unit **in the header itself**. ``P/E``,
``P/B`` and ``Div Yield`` carry none, and no recorded NSE schema or factsheet document in
``tests/fixtures/nse/macro/`` states one. So :data:`UNITS` reports ``unit: None`` for all
three and nothing is rescaled. ``Div Yield`` is *presumed* a percent — its values run
0.15–1.24 across both recordings and a fraction would print ``0.01`` — but that is an inference
from magnitude, and :data:`UNITS` marks it ``confirmed: False`` rather than asserting a unit
no artifact in this repository establishes.

What does **not** exist, and is deliberately absent from the public surface
--------------------------------------------------------------------------
**Index TRI — no route exists.** Four candidate routes were probed and all four returned 404.
There is no total-return-index series to read, and this module does not approximate one from
the price index. There is deliberately no ``tri``/``total_return`` accessor; a test asserts
that.

**RBI reference rate — not compliantly reachable.** ``www.rbi.org.in`` answers **HTTP 418** on
``robots.txt`` *and* on every POST, so a client cannot even establish what the site permits.
The data would additionally need an ASP.NET ``__doPostBack`` round trip carrying
``__VIEWSTATE``; the form shell GETs 200 but yields nothing. A prior agent stopped rather than
evading the WAF, and so does this one. :func:`rbi_reference_rate` exists only to raise
:class:`SourceUnavailableError` naming the 418 — it is a clearly labelled unavailable state,
not a parser for a payload nobody has seen. See :data:`UNAVAILABLE_SOURCES`.

The VIX-specific routes are dead; this CSV is the answer
--------------------------------------------------------
``/historical/indicesHistory?indexType=INDIA%20VIX`` returns **503** even with a primed
session, and ``ind_history_vix_30d.csv`` returns **404**. India VIX is simply a **row** in the
index close file, and :meth:`MacroClient.india_vix` is a first-class accessor for it rather
than something a caller has to know to ask for by name.

Caching: none, deliberately
---------------------------
There is no TTL here and there is no ``SQLiteCache``. Both sibling archive modules
(``nse.bhavcopy``, ``nse.marketlens``) are cache-free for the same reason, and it is the
strongest one available: ``ind_close_all_{DDMMYYYY}.csv`` for a **past** date is immutable
upstream, so a cache entry for it can never go stale and a TTL would only be a way to
re-download a byte-identical file. Today is the one exception — a file published this morning
is still settling upstream — which is handled by an explicit ``cache_today=False`` argument on
the public reads rather than by a blanket expiry.

Pacing is **not** shared with :mod:`bfinance.nse.bhavcopy`. Each module keeps its own
module-global clock, so a process that drives both holds two independent rate limits. Both are
opt-in clients that default to off, and the overlap is rare enough that sharing one clock
across module privates was judged the worse trade; change it if that stops being true.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import os
import random
import re
import threading
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx
import numpy as np
import pandas as pd

from bfinance.nse.bhavcopy import iter_trading_days, to_date
from bfinance.utils.exceptions import (
    BFinanceError,
    ParsingError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

logger = logging.getLogger("bfinance")

#: Every public name in this module, and nothing else. Without it, ``from bfinance.nse.macro
#: import *`` binds this module's own ``pd``, ``httpx``, ``np``, ``asyncio`` and the
#: ``iter_trading_days`` / ``to_date`` it borrows from :mod:`bfinance.nse.bhavcopy` into the
#: caller's namespace, which is a way to shadow a caller's ``date`` handling with a sibling
#: module's. Only names **defined here** are listed; every imported name is excluded, so a star
#: import carries the API and not the dependencies. :class:`IndexNotFoundError` and
#: :class:`SourceUnavailableError` **are** listed, because they are defined here even though
#: their ancestors are imported. Matches the convention :mod:`bfinance.flows` and
#: :mod:`bfinance.costs` use.
__all__ = [
    # ---- the one route this module reads ----
    "PROVENANCE",
    "HOSTS",
    "DEFAULT_HOST",
    "INDIA_VIX",
    "SENTINEL",
    "DATE_FORMAT",
    "COVERAGE_START",
    "validate_host",
    "index_url",
    "in_coverage",
    "iter_candidate_days",
    "reconstruct_change_pct",
    # ---- the upstream schema, transcribed ----
    "RAW_FIELDS",
    "FIELD_MAP",
    "COLUMNS",
    "NOT_APPLICABLE_COLUMN",
    "UNPARSED_COLUMN",
    "INTEGER_FIELDS",
    "VALUATION_FIELDS",
    "CANONICAL_BINDING",
    "UNITS",
    # ---- provenance attrs, published not inferred ----
    "HOST_ATTR",
    "URLS_ATTR",
    "URL_ATTR",
    "RETRIEVED_ATTR",
    "RETRIEVED_ALL_ATTR",
    "NOT_APPLICABLE_ATTR",
    "UNPARSED_ATTR",
    "COVERAGE_ATTR",
    "MISSING_DATES_ATTR",
    "RANGE_VIOLATIONS_ATTR",
    "NONPOSITIVE_CLOSE_ATTR",
    "NON_INTEGER_VOLUME_ATTR",
    "VIX_HAS_VOLUME_ATTR",
    "DATE_ATTR",
    "START_ATTR",
    "END_ATTR",
    "CANDIDATE_DAYS_ATTR",
    "INDEX_COUNT_ATTR",
    "FILE_ROW_COUNT_ATTR",
    "FILE_INDEX_COUNT_ATTR",
    # ---- documented absences, as data a caller can branch on ----
    "UNAVAILABLE_SOURCES",
    "RBI_REFERENCE_RATE_REASON",
    "INDEX_TRI_REASON",
    "unavailable_sources",
    # ---- errors ----
    "IndexNotFoundError",
    "SourceUnavailableError",
    # ---- parsing ----
    "parse_index_close_csv",
    "select_index",
    "empty_index_frame",
    # ---- transport seams, so a test can be offline ----
    "HttpSource",
    "RecordingSource",
    # ---- the client ----
    "MacroClient",
    "all_indices",
    "index_daily",
    "india_vix",
    "valuations",
    "index_range",
    "valuation_series",
    "rbi_reference_rate",
]

#: Provenance tag recorded on every frame.
PROVENANCE = "nse_macro"

#: The two hosts that serve ``/content/indices/ind_close_all_{DDMMYYYY}.csv`` with identical
#: bytes. ``archives`` is the mirror; ``nsearchives`` is the one the recordings were taken from.
HOSTS: Tuple[str, ...] = ("nsearchives.nseindia.com", "archives.nseindia.com")

#: Host used when a caller names none.
DEFAULT_HOST = HOSTS[0]

#: The index whose row carries the ``-`` sentinel across every non-price field.
INDIA_VIX = "India VIX"

#: The upstream token that means "not applicable to this index".
SENTINEL = "-"

#: Upstream date format. **``DD-MM-YYYY``** — positional, not ISO. There is no ``dayfirst``
#: fallback, deliberately: a wrong guess here moves a row by eight months (25-09-2026 read as
#: 2026-03-09 under ``MM-DD``) and does it silently. A row whose date does not parse strictly
#: is dropped and counted in ``attrs['bfinance_macro_unparsed_date_rows']`` instead.
DATE_FORMAT = "%d-%m-%Y"

#: First date the route serves. Observed: 2010-01-01 and earlier 404. *Observed*, not
#: guaranteed — see the module docstring for why no date is rejected locally.
COVERAGE_START = _date(2015, 1, 1)

#: The 13 upstream field names, verbatim and in file order.
RAW_FIELDS: Tuple[str, ...] = (
    "Index Name",
    "Index Date",
    "Open Index Value",
    "High Index Value",
    "Low Index Value",
    "Closing Index Value",
    "Points Change",
    "Change(%)",
    "Volume",
    "Turnover (Rs. Cr.)",
    "P/E",
    "P/B",
    "Div Yield",
)

#: Logical field -> the verbatim upstream column that carries it. The only way this module ever
#: reaches a column. A renamed or reordered upstream header therefore changes the *error*,
#: never the values.
FIELD_MAP: Dict[str, str] = {
    "name": "Index Name",
    "date": "Index Date",
    "open": "Open Index Value",
    "high": "High Index Value",
    "low": "Low Index Value",
    "close": "Closing Index Value",
    "points_change": "Points Change",
    "change_pct": "Change(%)",
    "volume": "Volume",
    "turnover": "Turnover (Rs. Cr.)",
    "pe": "P/E",
    "pb": "P/B",
    "div_yield": "Div Yield",
}

#: Output contract. The eleven data columns, plus two audit columns that make the
#: not-applicable / missing distinction legible row by row.
COLUMNS: Tuple[str, ...] = (
    "Open",
    "High",
    "Low",
    "Close",
    "PointsChange",
    "ChangePct",
    "Volume",
    "Turnover",
    "PE",
    "PB",
    "DivYield",
)

#: Audit columns appended after :data:`COLUMNS`. Each is a comma-joined list of the *canonical*
#: field names in that state for that row, or ``""``. They are strings on purpose: a caller
#: asking "was India VIX's P/E real?" gets the answer from the row, not from a second lookup.
NOT_APPLICABLE_COLUMN = "NotApplicable"
UNPARSED_COLUMN = "Unparsed"

#: Fields parsed as whole numbers. A fractional value in any of them is recorded, not rounded.
INTEGER_FIELDS: Tuple[str, ...] = ("Volume",)

#: The three valuation fields, in the order :func:`MacroClient.valuations` reports them.
VALUATION_FIELDS: Tuple[str, ...] = ("PE", "PB", "DivYield")

#: Fields bracketed by ``Low <= min(Open, Close) <= max(Open, Close) <= High``.
_RANGE_FIELDS: Tuple[str, ...] = ("Low", "Open", "Close", "High")

# --------------------------------------------------------------------------- attrs keys

#: ``str`` — the host that answered. One client is pinned to one host, so this is scalar even
#: on a multi-date range.
HOST_ATTR = "bfinance_source_host"

#: ``list[str]`` — every file URL read, in read order. Always present, so a range answer to
#: "how many requests did this cost" needs no branch.
URLS_ATTR = "bfinance_source_urls"

#: ``str`` — the single file URL. Present only when exactly one file was read, so it can never
#: be a lie about a multi-date read.
URL_ATTR = "bfinance_source_url"

#: ``list[str]`` — the retrieval timestamp of each URL in :data:`URLS_ATTR`, ISO-8601 UTC, same
#: order. For a replayed recording this is the *recording's* retrieval time, not the replay
#: time, because that is when those bytes were actually fetched from NSE.
RETRIEVED_ATTR = "bfinance_retrieved_utc"

#: ``list[str]`` — the per-URL form of :data:`RETRIEVED_ATTR`, aligned with :data:`URLS_ATTR`.
RETRIEVED_ALL_ATTR = "bfinance_source_retrieved_utc"

#: ``dict[str, int]`` — count of ``-`` sentinels per field. *"Not applicable"*, the state that
#: must never become ``0.0``.
NOT_APPLICABLE_ATTR = "bfinance_macro_not_applicable"

#: ``dict[str, int]`` — count of fields that were neither ``-`` nor a parseable number.
#: *"Missing"*, the third state, counted separately so it is never confused with the above.
UNPARSED_ATTR = "bfinance_macro_unparsed"

#: ``dict[str, dict]`` — per valuation field: ``present`` / ``not_applicable`` / ``unparsed`` /
#: ``total`` / ``coverage_pct``. Reported on every frame precisely so a caller does **not** have
#: to assume all 166 indices carry a P/E.
COVERAGE_ATTR = "bfinance_macro_valuation_coverage"

#: Dates for which NSE published no file. Present on every return; an empty list means "an
#: archive existed for every candidate day".
MISSING_DATES_ATTR = "bfinance_macro_missing_dates"

#: Rows whose ``Low``/``Open``/``Close``/``High`` ordering is impossible. Should be 0 always.
RANGE_VIOLATIONS_ATTR = "bfinance_macro_range_violations"

#: Rows with a non-positive close. Should be 0 always.
NONPOSITIVE_CLOSE_ATTR = "bfinance_macro_nonpositive_close_rows"

#: Volume values that were not whole numbers, and so kept the frame on ``Float64``.
NON_INTEGER_VOLUME_ATTR = "bfinance_macro_non_integer_volume_rows"

#: ``True`` when an India VIX row arrived carrying a non-``-`` Volume. That would mean upstream
#: changed the sentinel convention, so it is logged rather than absorbed.
VIX_HAS_VOLUME_ATTR = "bfinance_macro_india_vix_has_volume"

#: The date a single-date read covers.
DATE_ATTR = "bfinance_macro_date"

#: Requested bounds of a range read, echoed so a merged frame describes the whole span.
START_ATTR = "bfinance_macro_requested_start"
END_ATTR = "bfinance_macro_requested_end"

#: Candidate weekdays a range read walked — the request count before any 404 filtering.
CANDIDATE_DAYS_ATTR = "bfinance_macro_candidate_days"

#: How many distinct index names the frame holds.
INDEX_COUNT_ATTR = "bfinance_macro_index_count"

#: How many rows / index names the **file** held before any narrowing. On a one-index frame this
#: is the only honest way to learn that a date's file carried 166 indices, of which one is yours.
FILE_ROW_COUNT_ATTR = "bfinance_macro_file_row_count"
FILE_INDEX_COUNT_ATTR = "bfinance_macro_file_index_count"

# --------------------------------------------------------------------------- units

#: What each output column is measured in, and **whether this repository can prove it**.
#:
#: ``labelled_in_header`` is a mechanical fact: the unit appears in the upstream column name.
#: Only two columns have one. ``confirmed`` says whether a unit can be asserted at all from
#: the recorded artifacts; for ``P/E``, ``P/B`` and ``Div Yield`` it cannot, so ``unit`` is
#: ``None`` and nothing is rescaled.
UNITS: Dict[str, Dict[str, Any]] = {
    "Open": {"unit": "index points", "labelled_in_header": False, "confirmed": False,
             "note": "A level in the index's own points, the index's base value."},
    "High": {"unit": "index points", "labelled_in_header": False, "confirmed": False,
             "note": "A level in the index's own points, the index's base value."},
    "Low": {"unit": "index points", "labelled_in_header": False, "confirmed": False,
            "note": "A level in the index's own points, the index's base value."},
    "Close": {"unit": "index points", "labelled_in_header": False, "confirmed": False,
              "note": "A level in the index's own points, the index's base value."},
    "PointsChange": {"unit": "index points", "labelled_in_header": False, "confirmed": False,
                     "note": "Absolute day-over-day change in index points."},
    "ChangePct": {"unit": "percent", "labelled_in_header": True, "confirmed": True,
                  "note": "The header literally reads 'Change(%)'. Printed without a leading "
                          "zero ('.34'), so read it as a number, never as a fraction."},
    "Volume": {"unit": None, "labelled_in_header": False, "confirmed": False,
               "note": "Aggregate traded volume of the index's constituents, in shares. "
                       "'Not applicable' for computed indices such as India VIX."},
    "Turnover": {"unit": "Rs. Cr. (crore rupees)", "labelled_in_header": True,
                 "confirmed": True,
                 "note": "The header literally reads 'Turnover (Rs. Cr.)'. 1 crore = 10,000,000."},
    "PE": {"unit": None, "labelled_in_header": False, "confirmed": False,
           "note": "A dimensionless ratio, and the header carries no unit. The earnings window "
                   "(trailing vs forward) is not stated by any recorded artifact, so it is not "
                   "asserted either."},
    "PB": {"unit": None, "labelled_in_header": False, "confirmed": False,
           "note": "A dimensionless ratio. The header carries no unit."},
    "DivYield": {"unit": None, "labelled_in_header": False, "confirmed": False,
                 "presumed": "percent",
                 "presumption_evidence": "Values run 0.15-1.24 across both recordings and move "
                                         "with the index level; a fraction would print 0.01. "
                                         "That is an inference from magnitude only.",
                 "confirmation_requires": "An NSE schema or factsheet document stating the "
                                          "unit. None exists in tests/fixtures/nse/macro/."},
}

# --------------------------------------------------------------------------- absences

#: Sources a caller might expect and that are **documented as unavailable**, with the reason.
#: Kept as data rather than as prose so a consumer can branch on it without parsing a docstring,
#: and so a test can assert the reasons survive.
UNAVAILABLE_SOURCES: Dict[str, Dict[str, Any]] = {
    "index_tri": {
        "available": False,
        "why": "No route exists. Four candidate routes were probed and every one returned "
               "HTTP 404.",
        "probe_count": 4,
        "consequence": "This module exposes no total-return-index accessor and does not "
                       "approximate one from the price index. A TRI read would be a "
                       "fabrication: total return includes dividend reinvestment, which the "
                       "price index does not carry.",
    },
    "rbi_reference_rate": {
        "available": False,
        "host": "www.rbi.org.in",
        "http_status": 418,
        "why": "www.rbi.org.in returns HTTP 418 on robots.txt AND on every POST, so a client "
               "cannot even establish what the site permits. The body is a WAF refusal: "
               "'You are not authorized to view this page.' The data would additionally need an "
               "ASP.NET __doPostBack round trip carrying __VIEWSTATE; the form shell GETs 200 "
               "but yields no data.",
        "not_circumvented": True,
        "consequence": "Excluded. A prior agent stopped rather than evading the WAF, and no "
                       "attempt was made to build a parser for a payload nobody has seen.",
    },
}

#: The exact reason string used by the :func:`rbi_reference_rate` unavailable state.
RBI_REFERENCE_RATE_REASON = UNAVAILABLE_SOURCES["rbi_reference_rate"]["why"]

#: The exact reason string for the total-return-index absence.
INDEX_TRI_REASON = UNAVAILABLE_SOURCES["index_tri"]["why"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Module-global pacing, ported from nse.bhavcopy so concurrent clients respect one floor.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


# --------------------------------------------------------------------------- errors


class IndexNotFoundError(TickerNotFoundError):
    """An index name that is not present in the file that was read.

    Deliberately a *subclass* of the existing
    :class:`~bfinance.utils.exceptions.TickerNotFoundError` rather than a new root, so the
    established ``except TickerNotFoundError`` / ``except BFinanceError`` handlers keep
    catching it while a caller who cares can catch the specific type. The point is that an
    unknown index raises — never an empty frame, which is indistinguishable from a holiday.
    """


class SourceUnavailableError(BFinanceError):
    """A source that is documented as not reachable, or as not existing at all.

    Not a subclass of :class:`~bfinance.utils.exceptions.UpstreamServiceError` on purpose: a
    418 from a WAF is not a transient outage and retrying it is pointless. It is not a
    subclass of ``TickerNotFoundError`` either, because nothing is missing — the route does
    not exist, or the host refuses every compliant request.
    """


# --------------------------------------------------------------------------- validation


def validate_host(host: str) -> str:
    """Lowercase and validate a host before any request.

    The allow-list is closed. An unvalidated host would let a caller point this module at any
    URL on the internet with an ``ind_close_all_`` filename, which is not what "NSE index
    data" should mean.
    """
    if not isinstance(host, str):
        raise ValueError(f"host must be a string, got {type(host).__name__}")
    clean = host.strip().lower()
    if clean.startswith("https://"):
        clean = clean[len("https://") :]
    clean = clean.rstrip("/")
    if clean not in HOSTS:
        raise ValueError(
            f"host={host!r} is not an NSE index-archive host. Must be one of "
            f"{', '.join(HOSTS)}."
        )
    return clean


def index_url(
    day: Union[str, _date, datetime, pd.Timestamp], host: str = DEFAULT_HOST
) -> str:
    """Return the archive URL for one date. Pure and offline — no request is made.

    The filename is ``ind_close_all_{DDMMYYYY}.csv`` and the path is
    ``/content/indices/``. There is exactly one such route; :func:`MacroClient` builds no
    other index URL, and a test asserts that no public accessor can produce one.
    """
    clean_host = validate_host(host)
    clean_day = to_date(day)
    return f"https://{clean_host}/content/indices/ind_close_all_{clean_day:%d%m%Y}.csv"


def in_coverage(day: Union[str, _date, datetime, pd.Timestamp]) -> bool:
    """True when ``day`` is on or after :data:`COVERAGE_START`.

    Reported, never enforced. See the module docstring: the boundary was observed, so a local
    rejection would claim more than the evidence supports.
    """
    return to_date(day) >= COVERAGE_START


def iter_candidate_days(start, end, skip_weekends: bool = True) -> List[_date]:
    """Every candidate date in ``[start, end]``, ascending.

    Reuses :func:`bfinance.nse.bhavcopy.iter_trading_days` rather than reimplementing the walk,
    and therefore inherits its explicit ``ValueError`` for an inverted range — that is the
    reversed-range contract, and it is the same message a bhavcopy caller already knows.

    ``skip_weekends`` drops Saturdays and Sundays *without a request*. That is safe: a weekend
    has no archive. It is **not** a trading calendar, and an exchange holiday is still
    requested — 2020-01-01 was a Wednesday, a market holiday, and its file exists (HTTP 200,
    recorded). So the filter removes traffic, it does not define coverage.
    """
    if not skip_weekends:
        first, last = to_date(start), to_date(end)
        if last < first:
            raise ValueError(f"end={last} is before start={first}")
        days: List[_date] = []
        cursor = first
        while cursor <= last:
            days.append(cursor)
            cursor += timedelta(days=1)
        return days
    return iter_trading_days(start, end)


# --------------------------------------------------------------------------- helpers


def reconstruct_change_pct(close: Any, points_change: Any) -> Any:
    """The ``Change(%)`` implied by a close and a points change, in percent.

    ``Points Change`` is ``Close - previous Close``, so the previous close is recoverable as
    ``Close - Points Change`` and the percent follows. Used by the tests as an internal
    consistency check on the CSV, and exposed because a consumer computing a return series from
    the same two columns should be able to do the same. Accepts scalars or arrays and returns
    the matching shape.

    The reconstruction is **approximate**: the file publishes close and points change rounded
    to two decimals, so the implied percent inherits that rounding. The worst case in the two
    recordings is India VIX on 2026-09-25 — close 12.16, points -0.53 — which reconstructs to
    -4.1765 against a reported -4.16, a 0.017 pp gap. A test therefore allows 0.05 pp, which
    is tight enough that treating the percent as a fraction, or taking the change against the
    open instead of the previous close, fails by orders of magnitude.

    A previous close of zero yields ``NaN`` rather than an infinity, because that is a data
    state to report and not an answer.
    """
    closes = np.asarray(close, dtype="float64")
    points = np.asarray(points_change, dtype="float64")
    previous = closes - points
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(previous == 0.0, np.nan, points / previous * 100.0)
    if np.ndim(close) == 0 and np.ndim(points_change) == 0:
        return float(result)
    return result


def unavailable_sources() -> Dict[str, Dict[str, Any]]:
    """A copy of :data:`UNAVAILABLE_SOURCES`.

    A copy, so a caller annotating the result cannot corrupt the module-level record that the
    tests read.
    """
    return {name: dict(detail) for name, detail in UNAVAILABLE_SOURCES.items()}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_attrs(
    host: str, urls: Sequence[str], retrieved: Sequence[str]
) -> Dict[str, Any]:
    """The five provenance keys, on every return.

    ``bfinance_source_url`` and ``bfinance_retrieved_utc`` are set **only** when exactly one
    file was read, so a single-date read gets the simple scalar key a bhavcopy caller already
    knows, and a range read cannot have a single-URL key that silently describes one of many.
    """
    attrs: Dict[str, Any] = {
        HOST_ATTR: host,
        URLS_ATTR: list(urls),
        RETRIEVED_ALL_ATTR: list(retrieved),
        "bfinance_provenance": PROVENANCE,
    }
    if len(urls) == 1:
        attrs[URL_ATTR] = urls[0]
        attrs[RETRIEVED_ATTR] = retrieved[0]
    return attrs


def _state_counts(frame: pd.DataFrame, column: str) -> Dict[str, int]:
    """Count, per field, how many rows list that field in one of the audit columns.

    An exact token match on a comma-delimited list, not a substring test: ``"PB"`` must not be
    counted because the row says ``"PE"``, and ``"Open"`` must not be counted because the row
    says ``"NotApplicable"``. Only non-zero entries appear, so a caller reading the dict gets
    the gaps rather than eleven zeroes.
    """
    counts: Dict[str, int] = {}
    if column not in frame.columns or frame.empty:
        return counts
    for label in COLUMNS:
        # Non-capturing groups: a capturing group here makes pandas warn on every call.
        pattern = rf"(?:^|,){re.escape(label)}(?:,|$)"
        count = int(frame[column].astype(str).str.contains(pattern, regex=True).sum())
        if count:
            counts[label] = count
    return counts


def _coverage(frame: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """Per valuation field: how many rows carry a value, and how many do not, and why not."""
    total = int(len(frame))
    not_applicable = _state_counts(frame, NOT_APPLICABLE_COLUMN)
    unparsed = _state_counts(frame, UNPARSED_COLUMN)
    report: Dict[str, Dict[str, Any]] = {}
    for field in VALUATION_FIELDS:
        present = int(frame[field].notna().sum()) if total else 0
        report[field] = {
            "present": present,
            "not_applicable": not_applicable.get(field, 0),
            "unparsed": unparsed.get(field, 0),
            "total": total,
            "coverage_pct": round(100.0 * present / total, 2) if total else None,
        }
    return report


def _field_states(raw: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Classify one raw column into (numeric, not-applicable, missing).

    Three states, decided here and nowhere else:

    * the raw token is exactly :data:`SENTINEL` -> **not applicable**;
    * the token is blank, or is present but will not coerce to a number -> **missing**;
    * anything else is a real number, *including a real* ``0.0``.

    Reading the raw text before any numeric coercion is the whole point: ``pd.to_numeric`` maps
    ``-`` to ``NaN`` on its own, which is indistinguishable from a blank and from a genuine
    zero until it is too late. Returns the coerced numbers alongside the masks so the caller
    cannot re-derive them and drift out of step.

    The two ``pd.NA`` reasons are *both* returned as ``NaN`` in the numeric column, which is
    why they are separated by mask rather than by dtype. Folding "blank" into the missing mask
    rather than leaving it as a bare ``NaN`` is what keeps the missing counter honest: an
    excluded blank would make the frame look complete while ``isna()`` said otherwise.
    """
    cleaned = raw.astype(str).str.strip()
    not_applicable = cleaned == SENTINEL
    blank = cleaned.eq("")
    numeric = pd.to_numeric(cleaned.where(~not_applicable), errors="coerce")
    missing = (blank | numeric.isna()) & ~not_applicable
    return numeric, not_applicable, missing


def _join_names(pieces: List[Any]) -> List[str]:
    """Join one label per field per row into a single comma-joined string.

    Empty labels are skipped, so a row with only a not-applicable P/E reads ``"PE"`` and a row
    with none reads ``""`` — never ``",,,"``, which would make a truthiness check on the audit
    column lie.
    """
    if not pieces:
        return []
    count = len(pieces[0])
    return [
        ",".join(str(piece[i]) for piece in pieces if piece[i]) for i in range(count)
    ]


#: Logical field -> canonical output label, in :data:`COLUMNS` order. The parse loop's spine.
CANONICAL_BINDING: Tuple[Tuple[str, str], ...] = (
    ("open", "Open"),
    ("high", "High"),
    ("low", "Low"),
    ("close", "Close"),
    ("points_change", "PointsChange"),
    ("change_pct", "ChangePct"),
    ("volume", "Volume"),
    ("turnover", "Turnover"),
    ("pe", "PE"),
    ("pb", "PB"),
    ("div_yield", "DivYield"),
)


# --------------------------------------------------------------------------- parsing


def parse_index_close_csv(
    csv_text: str,
    *,
    index: Optional[str] = None,
    host: str = DEFAULT_HOST,
    url: str = "",
    retrieved_utc: str = "",
) -> pd.DataFrame:
    """Turn an ``ind_close_all`` CSV into a typed frame. Pure — no network, no clock.

    A ``MultiIndex`` ``(Date, IndexName)`` — level 0 a tz-naive ``DatetimeIndex`` named
    ``Date``, matching :mod:`bfinance.nse.bhavcopy`. A single date's file is a **cross-section**
    (166 indices on 2026-09-25), so a flat ``DatetimeIndex`` would hold only one row per
    timestamp and silently discard 165 of them. One index comes back with
    ``frame.xs("India VIX", level="IndexName")``.

    Columns are :data:`COLUMNS` + the two audit columns, ascending. ``Volume`` is nullable
    ``Int64`` and every other numeric column is nullable ``Float64``; see the module docstring
    for why the nullable dtypes are load bearing.

    Args:
        csv_text: the raw file body.
        index: keep only this index name (case-insensitive, whitespace-normalised). A name that
            is not in the file raises :class:`IndexNotFoundError` — never an empty frame.
        host: the host this text came from; recorded in attrs. Not validated, so a recording
            taken from either host can be replayed honestly.
        url: the source URL, recorded in attrs.
        retrieved_utc: ISO-8601 UTC retrieval timestamp of these bytes, recorded in attrs.

    Raises:
        ParsingError: the header is missing, or is missing a required field.
        IndexNotFoundError: ``index`` is not among the names in this file.
    """
    frame = pd.read_csv(
        io.StringIO(csv_text), dtype=str, keep_default_na=False, na_values=[]
    )
    frame = frame.drop(columns=[c for c in frame.columns if str(c).startswith("Unnamed:")])

    missing = [raw for raw in RAW_FIELDS if raw not in frame.columns]
    if missing:
        raise ParsingError(
            f"ind_close_all CSV is missing field(s): {missing}. Upstream schema changed; this "
            f"module resolves every column by name and will not guess at a rename."
        )

    names = frame[FIELD_MAP["name"]].astype(str).str.strip()
    raw_dates = frame[FIELD_MAP["date"]].astype(str).str.strip()
    index_values = pd.DatetimeIndex(
        pd.to_datetime(raw_dates, format=DATE_FORMAT, errors="coerce").to_numpy()
    ).normalize()

    out = pd.DataFrame(index=pd.MultiIndex.from_arrays(
        [index_values, names.to_numpy()], names=["Date", "IndexName"]
    ))

    not_applicable_names: List[Any] = []
    unparsed_names: List[Any] = []
    not_applicable_counts: Dict[str, int] = {}
    unparsed_counts: Dict[str, int] = {}

    for field, label in CANONICAL_BINDING:
        numeric, not_applicable, missing = _field_states(frame[FIELD_MAP[field]])
        count_na = int(not_applicable.sum())
        count_missing = int(missing.sum())
        if count_na:
            not_applicable_counts[label] = count_na
        if count_missing:
            unparsed_counts[label] = count_missing
        out[label] = pd.array(
            np.asarray(numeric.to_numpy(), dtype="float64"), dtype="Float64"
        )
        not_applicable_names.append(not_applicable.map({True: label, False: ""}).to_numpy())
        unparsed_names.append(missing.map({True: label, False: ""}).to_numpy())

    # A non-integral volume means the frame cannot honestly be Int64. Keep Float64 and record
    # how many, rather than rounding a share count into a lie.
    non_integer_volume = 0
    volume = out["Volume"].dropna()
    if len(volume) and not bool((volume % 1 == 0).all()):
        non_integer_volume = int((volume % 1 != 0).sum())
    else:
        out["Volume"] = out["Volume"].astype("Int64")

    out[NOT_APPLICABLE_COLUMN] = _join_names(not_applicable_names)
    out[UNPARSED_COLUMN] = _join_names(unparsed_names)

    # A row whose date did not parse has no location in time, and a row with no name is
    # unidentifiable. Both are dropped and counted, never given an invented value.
    unparsed_dates = int(out.index.get_level_values("Date").isna().sum())
    if unparsed_dates:
        out = out[out.index.get_level_values("Date").notna()]
    nameless = int(
        (out.index.get_level_values("IndexName") == "").sum()
    )
    if nameless:
        out = out[out.index.get_level_values("IndexName") != ""]

    duplicates = int(out.index.duplicated(keep="last").sum())
    if duplicates:
        out = out[~out.index.duplicated(keep="last")]

    out = out.sort_index(kind="stable")
    out = out[list(COLUMNS) + [NOT_APPLICABLE_COLUMN, UNPARSED_COLUMN]]

    # Index-level sanity, measured and recorded rather than trusted.
    range_violations = 0
    nonpositive_close = 0
    if len(out):
        bounded = out[list(_RANGE_FIELDS)].astype("Float64")
        lower = bounded["Low"]
        upper = bounded["High"]
        inside = (lower <= bounded[["Open", "Close"]].astype("Float64").min(axis=1)) & (
            upper >= bounded[["Open", "Close"]].astype("Float64").max(axis=1)
        )
        range_violations = int((~inside.fillna(False)).sum())
        nonpositive_close = int((out["Close"].astype("Float64") <= 0).sum())

    attrs: Dict[str, Any] = {
        "bfinance_raw_fields": list(RAW_FIELDS),
        "bfinance_field_map": dict(FIELD_MAP),
        NOT_APPLICABLE_ATTR: not_applicable_counts,
        UNPARSED_ATTR: unparsed_counts,
        COVERAGE_ATTR: _coverage(out),
        RANGE_VIOLATIONS_ATTR: range_violations,
        NONPOSITIVE_CLOSE_ATTR: nonpositive_close,
        NON_INTEGER_VOLUME_ATTR: non_integer_volume,
        "bfinance_macro_unparsed_date_rows": unparsed_dates,
        "bfinance_macro_nameless_rows": nameless,
        "bfinance_macro_duplicate_rows": duplicates,
        "bfinance_macro_sentinel": SENTINEL,
        INDEX_COUNT_ATTR: int(out.index.get_level_values("IndexName").nunique()),
        FILE_ROW_COUNT_ATTR: int(len(out)),
        FILE_INDEX_COUNT_ATTR: int(out.index.get_level_values("IndexName").nunique()),
    }
    attrs.update(_source_attrs(
        host, [url] if url else [], [retrieved_utc] if retrieved_utc else []
    ))
    if range_violations:
        logger.warning(
            "nse_macro: %d row(s) have Low > min(Open, Close) or High < max(Open, Close). "
            "The data is upstream, not derived; reported in attrs[%r].",
            range_violations, RANGE_VIOLATIONS_ATTR,
        )
    out.attrs.update(attrs)

    if index is not None:
        out = select_index(out, index)
    return out


def select_index(frame: pd.DataFrame, index: str) -> pd.DataFrame:
    """Narrow a parsed frame to one index name, or raise :class:`IndexNotFoundError`.

    Matching is case-insensitive and whitespace-normalised, because upstream is not internally
    consistent about casing — ``Nifty 50``, ``NIFTY Midcap 100`` and ``India VIX`` in the same
    file. The name **as upstream wrote it** is what comes back.

    An unknown name raises. It does not return an empty frame, which would be indistinguishable
    from "NSE published no file that day" — the exact confusion this module's siblings were
    written to end. When the name is merely a typo the message names the closest matches.
    """
    if not isinstance(index, str) or not index.strip():
        raise ValueError(f"index must be a non-empty string, got {index!r}")

    available = list(dict.fromkeys(frame.index.get_level_values("IndexName")))
    folded = [re.sub(r"\s+", " ", name).strip().casefold() for name in available]
    wanted = re.sub(r"\s+", " ", index).strip().casefold()
    for name, key in zip(available, folded):
        if key == wanted:
            single = frame.xs(name, level="IndexName")
            single.attrs.update(frame.attrs)
            # File-level counters keep describing the *file* — a range violation on some other
            # index is a real fact about the source and hiding it would be the opposite error.
            # The three coverage dicts, though, are about the rows a caller is holding, and
            # carrying the file's numbers onto a one-row slice would tell them India VIX has a
            # P/E because 4 of the file's 5 indices do. Recomputed over the slice.
            single.attrs[FILE_ROW_COUNT_ATTR] = int(len(frame))
            single.attrs[FILE_INDEX_COUNT_ATTR] = len(available)
            single.attrs[NOT_APPLICABLE_ATTR] = _state_counts(single, NOT_APPLICABLE_COLUMN)
            single.attrs[UNPARSED_ATTR] = _state_counts(single, UNPARSED_COLUMN)
            single.attrs[COVERAGE_ATTR] = _coverage(single)
            single.attrs[INDEX_COUNT_ATTR] = 1
            single.attrs["bfinance_macro_index"] = name
            return single

    close_keys = difflib.get_close_matches(wanted, folded, n=3)
    suggestion = (
        f" Did you mean: {', '.join(available[folded.index(key)] for key in close_keys)}?"
        if close_keys
        else ""
    )
    raise IndexNotFoundError(
        f"index={index!r} is not in the file. {len(available)} index name(s) are present."
        f"{suggestion} The list is 'every NSE index on that date' and changes over time, so "
        f"read all_indices(day) to see it. This raises rather than returning an empty frame, "
        f"which would be indistinguishable from NSE publishing no file for that date."
    )


def empty_index_frame(attrs: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """An empty frame with the right index and dtypes — the honest shape of 'no file'.

    What a 404, a weekend or an exchange holiday yields. Never a synthesised row. Nullable
    dtypes are preserved so an empty frame and a populated one cannot be told apart by dtype.
    """
    index = pd.MultiIndex.from_arrays(
        [
            pd.DatetimeIndex([], name="Date"),
            pd.Index([], dtype="object", name="IndexName"),
        ],
        names=["Date", "IndexName"],
    )
    data: Dict[str, Any] = {}
    for label in COLUMNS:
        data[label] = pd.array([], dtype="Int64" if label in INTEGER_FIELDS else "Float64")
    data[NOT_APPLICABLE_COLUMN] = pd.Series([], dtype="object")
    data[UNPARSED_COLUMN] = pd.Series([], dtype="object")
    out = pd.DataFrame(data, index=index)
    if attrs:
        out.attrs.update(attrs)
    out.attrs.update({
        "bfinance_raw_fields": list(RAW_FIELDS),
        "bfinance_field_map": dict(FIELD_MAP),
        NOT_APPLICABLE_ATTR: {},
        UNPARSED_ATTR: {},
        COVERAGE_ATTR: {
            field: {
                "present": 0, "not_applicable": 0, "unparsed": 0, "total": 0,
                "coverage_pct": None,
            }
            for field in VALUATION_FIELDS
        },
        RANGE_VIOLATIONS_ATTR: 0,
        NONPOSITIVE_CLOSE_ATTR: 0,
        INDEX_COUNT_ATTR: 0,
    })
    return out


# --------------------------------------------------------------------------- sources


class HttpSource:
    """Fetches a file URL over HTTPS. The default source for :class:`MacroClient`.

    Contract, identical to :meth:`bfinance.nse.bhavcopy.BhavcopyClient._get_archive`:

    * **404 -> ``None``.** A weekend, an exchange holiday, or a date outside the route's
      coverage. Expected, not a failure, and kept distinct from every other outcome.
    * **429 -> :class:`RateLimitExceededError`**, after retries.
    * **5xx / transport failure -> retried**, then :class:`UpstreamServiceError`.
    * **any other non-200 -> :class:`UpstreamServiceError`** immediately.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 250.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0

    @staticmethod
    def _headers() -> Dict[str, str]:
        """Browser headers only. No cookie priming and no landing-page fetch — the index
        archive is a static file and needs neither, exactly like the bhavcopy archive."""
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/csv,application/csv,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _throttle_pacing(self) -> None:
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
            _GLOBAL_LAST_REQUEST_TIME = max(
                _GLOBAL_LAST_REQUEST_TIME, self._last_request_time
            )

    async def get(self, url: str) -> Optional[bytes]:
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
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RateLimitExceededError(
                    f"NSE index archive rate limited (HTTP 429) for {url}"
                )
            if response.status_code >= 500:
                last_error = UpstreamServiceError(
                    f"NSE index archive returned HTTP {response.status_code} for {url}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.status_code != 200:
                raise UpstreamServiceError(
                    f"NSE index archive returned unexpected HTTP {response.status_code} "
                    f"for {url}"
                )
            return response.content

        raise UpstreamServiceError(
            f"NSE index archive failed after {self.max_retries} attempts: {url}"
        ) from last_error

    def retrieved_utc(self, url: str) -> str:
        """When these bytes were fetched. A live fetch, so that is now."""
        return _now_utc()


class RecordingSource:
    """Serve a directory of recorded ``ind_close_all_*.csv`` files. **Never** touches a network.

    The offline seam. Given a directory, it resolves a URL's basename to
    ``<directory>/<basename>`` and, when a ``<basename minus .csv>.META.json`` sits beside it,
    reads that file's ``retrieved_utc`` so the reported retrieval date is the date the bytes
    were *actually* fetched from NSE rather than the date of the replay.

    A miss raises :class:`TickerNotFoundError` under the default ``on_missing="raise"``, so an
    unrecorded date is loud and there is no code path on this object that could reach a host.
    ``on_missing="return_none"`` maps a miss to ``None``, which is how a real 404 looks, and is
    what a range walk needs.

    Args:
        directory: a :class:`pathlib.Path` (or a string) holding the recordings.
        on_missing: ``'raise'`` (default) or ``'return_none'``.
    """

    def __init__(self, directory: Union[str, "Path"], *, on_missing: str = "raise") -> None:
        if on_missing not in ("raise", "return_none"):
            raise ValueError(
                f"on_missing={on_missing!r} is not supported. Must be 'raise' or 'return_none'."
            )
        self.directory = Path(directory)
        self.on_missing = on_missing
        #: Every basename this source was asked for, in order. The request count a range walk
        #: actually cost, measured rather than inferred.
        self.requested: List[str] = []

    def _resolve(self, url: str) -> Optional[Path]:
        basename = url.rsplit("/", 1)[-1]
        self.requested.append(basename)
        path = self.directory / basename
        if path.is_file():
            return path
        if self.on_missing == "raise":
            raise TickerNotFoundError(
                f"no recording of {basename} in {self.directory}. This source is offline by "
                f"construction and will not fetch it. Record the file, or pass "
                f"on_missing='return_none' to treat the miss as a 404."
            )
        return None

    async def get(self, url: str) -> Optional[bytes]:
        path = self._resolve(url)
        if path is None:
            return None
        return path.read_bytes()

    def retrieved_utc(self, url: str) -> str:
        """The recording's own retrieval timestamp, when its META.json records one."""
        basename = url.rsplit("/", 1)[-1]
        meta_path = self.directory / (basename[: -len(".csv")] + ".META.json")
        if not meta_path.is_file():
            # An un-meta'd recording still has a real retrieval date: the replay. Say so.
            return _now_utc()
        try:
            recorded = json.loads(meta_path.read_text(encoding="utf-8")).get("retrieved_utc")
        except (ValueError, OSError):
            recorded = None
        return recorded or _now_utc()


# --------------------------------------------------------------------------- client


class MacroClient:
    """Client for NSE's published daily index close + valuation archives.

    Stateless per call and safe to reuse: the route is a static file with no session, no cookie
    and no staleness problem. A long-lived instance is fine, and sharing one is *preferred*
    because request pacing is coordinated globally.

    One client is pinned to one :data:`HOSTS` entry, so ``attrs['bfinance_source_host']`` on any
    result is unambiguous even for a multi-date range.
    """

    def __init__(
        self,
        source: Optional[Any] = None,
        *,
        host: str = DEFAULT_HOST,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 250.0,
    ) -> None:
        self.host = validate_host(host)
        if source is None:
            source = HttpSource(
                timeout=timeout,
                max_retries=max_retries,
                proxy=proxy,
                min_request_interval_ms=min_request_interval_ms,
            )
        #: Anything with ``async get(url) -> bytes | None`` and ``retrieved_utc(url) -> str``.
        #: Swap in a :class:`RecordingSource` to replay an archive with no network at all.
        self.source = source

    # ------------------------------------------------------------ internals

    def url_for(self, day: Union[str, _date, datetime, pd.Timestamp]) -> str:
        """The URL this client would read for ``day``. Pure."""
        return index_url(day, self.host)

    async def _read_day(
        self, day: _date
    ) -> Tuple[Optional[pd.DataFrame], str, str]:
        """Read and parse one date. Returns ``(frame_or_None, url, retrieved_utc)``."""
        url = self.url_for(day)
        payload = await self.source.get(url)
        retrieved = self.source.retrieved_utc(url)
        if payload is None:
            return None, url, retrieved
        return parse_index_close_csv(
            payload.decode("utf-8-sig", errors="replace"),
            host=self.host,
            url=url,
            retrieved_utc=retrieved,
        ), url, retrieved

    # -------------------------------------------------------------- public

    async def all_indices_async(self, day, *, raise_errors: bool = False) -> pd.DataFrame:
        """Every index in the file for one date.

        Returns a ``MultiIndex`` ``(Date, IndexName)`` frame with :data:`COLUMNS` plus the two
        audit columns, and ``attrs['bfinance_macro_valuation_coverage']`` reporting exactly how
        many of those indices carry a P/E, a P/B and a dividend yield — so a caller never has to
        assume all of them do.

        Args:
            day: ``'YYYY-MM-DD'``, ``'DDMMYYYY'``, ``date`` or ``datetime``.
            raise_errors: raise :class:`TickerNotFoundError` instead of returning an empty frame
                when NSE published no file for the date.

        Raises:
            ValueError: ``day`` is not a date, or the host is not a known NSE archive host.
            TickerNotFoundError: no file for that date and ``raise_errors=True``.
            UpstreamServiceError: transport failure, or a status other than 200/404.
            RateLimitExceededError: HTTP 429 after retries.
            ParsingError: the header is missing or a field was renamed.
        """
        return await self._read_single(day, raise_errors=raise_errors)

    def all_indices(self, day) -> pd.DataFrame:
        """Sync variant of :meth:`all_indices_async`."""
        return _run_async(self.all_indices_async(day))

    async def _read_single(
        self, day, *, index: Optional[str] = None, raise_errors: bool = False
    ) -> pd.DataFrame:
        clean_day = to_date(day)
        frame, url, retrieved = await self._read_day(clean_day)
        if frame is None:
            message = (
                f"NSE published no index close file for {clean_day:%Y-%m-%d} at {self.host}. "
                f"Expected: a weekend, an exchange holiday, or a date outside the route's "
                f"coverage (observed from {COVERAGE_START:%Y-%m-%d}) — not a data error."
            )
            if raise_errors:
                raise TickerNotFoundError(message)
            logger.debug("%s", message)
            attrs = _source_attrs(self.host, [url], [retrieved])
            attrs[DATE_ATTR] = clean_day.isoformat()
            attrs[MISSING_DATES_ATTR] = [clean_day.isoformat()]
            attrs[CANDIDATE_DAYS_ATTR] = 1
            return empty_index_frame(attrs)

        frame.attrs[DATE_ATTR] = clean_day.isoformat()
        frame.attrs[MISSING_DATES_ATTR] = []
        frame.attrs[CANDIDATE_DAYS_ATTR] = 1
        if index is not None:
            frame = select_index(frame, index)
        return frame

    async def index_daily_async(self, index: str, day) -> pd.DataFrame:
        """One index on one date, as a plain ``DatetimeIndex`` frame.

        The cross-section narrows to the shape a time-series caller wants, exactly as
        ``bhavcopy``'s does.

        Raises:
            IndexNotFoundError: the name is not in that date's file. A typed error, never an
                empty frame — see :func:`select_index`.
        """
        return await self._read_single(day, index=index, raise_errors=False)

    def index_daily(self, index: str, day) -> pd.DataFrame:
        """Sync variant of :meth:`index_daily_async`."""
        return _run_async(self.index_daily_async(index, day))

    async def india_vix_async(self, day) -> pd.DataFrame:
        """``India VIX`` on one date — a first-class accessor, not a row you must know to ask for.

        The VIX-specific routes are **dead**: ``/historical/indicesHistory?indexType=INDIA%20VIX``
        returns **503** even with a primed session, and ``ind_history_vix_30d.csv`` returns
        **404**. India VIX is a row in the index close file and this file is the answer.

        The returned row carries :data:`SENTINEL` (``pd.NA``, never ``0.0``) in Volume,
        Turnover, PE, PB and DivYield: a volatility index has no traded constituent volume and
        no constituent earnings, so those are *not applicable* rather than zero. If a future
        file ever ships a real VIX volume, that is an upstream convention change and it is
        logged and recorded in ``attrs['bfinance_macro_india_vix_has_volume']`` rather than
        absorbed.
        """
        frame = await self._read_single(day, index=INDIA_VIX, raise_errors=False)
        if frame.empty:
            return frame
        has_volume = bool(frame["Volume"].notna().iloc[0])
        frame.attrs[VIX_HAS_VOLUME_ATTR] = has_volume
        if has_volume:
            logger.warning(
                "nse_macro: India VIX arrived with a real Volume on %s. Upstream may have "
                "changed the '-' sentinel convention; recorded in attrs[%r].",
                frame.index[0], VIX_HAS_VOLUME_ATTR,
            )
        return frame

    def india_vix(self, day) -> pd.DataFrame:
        """Sync variant of :meth:`india_vix_async`."""
        return _run_async(self.india_vix_async(day))

    async def valuations_async(self, day) -> pd.DataFrame:
        """P/E, P/B and dividend yield for every index on one date, indexed by name.

        The coverage gap is reported, not implied: ``attrs['bfinance_macro_valuation_coverage']``
        carries ``present`` / ``not_applicable`` / ``unparsed`` / ``total`` / ``coverage_pct``
        per field, and every cell that is ``pd.NA`` is a field the file declined to state.

        On the 2026-09-25 recording the file carried 166 indices of which 143 had a real P/E,
        144 a P/B and 144 a dividend yield. That 143/166 figure comes from the live capture and
        **cannot be recomputed from the trimmed fixture** on disk, which retains five rows; the
        number this function reports for a replayed recording is measured from the rows it
        actually read, and is never assumed to be 166.
        """
        frame = await self._read_single(day, raise_errors=False)
        if frame.empty:
            empty = empty_index_frame(frame.attrs)
            return empty
        out = frame[list(VALUATION_FIELDS)]
        out.index = pd.Index(out.index.get_level_values("IndexName"), name="IndexName")
        out = out.sort_index()
        out.attrs.update(frame.attrs)
        out.attrs[INDEX_COUNT_ATTR] = int(out.index.nunique())
        out.attrs["bfinance_macro_columns"] = list(VALUATION_FIELDS)
        return out

    def valuations(self, day) -> pd.DataFrame:
        """Sync variant of :meth:`valuations_async`."""
        return _run_async(self.valuations_async(day))

    async def index_range_async(
        self,
        index: str,
        start,
        end,
        *,
        on_missing: str = "skip",
        skip_weekends: bool = True,
        raise_errors: bool = False,
        progress: bool = False,
    ) -> pd.DataFrame:
        """Walk ``[start, end]`` and stack every day that has a file, for one index.

        Requests are strictly sequential and globally paced. Saturdays and Sundays are skipped
        **without a request** when ``skip_weekends`` is on; exchange holidays are not bundled in
        a calendar, so each still costs one 404 — and that is correct, because a holiday *can*
        have a file: 2020-01-01 was a Wednesday, a market holiday, and its file exists.

        Args:
            index: the index name, matched case-insensitively and whitespace-normalised.
            start / end: inclusive bounds. An inverted range raises :class:`ValueError` from
                :func:`iter_candidate_days` before any request.
            on_missing: ``'skip'`` (default) records the date in
                ``attrs['bfinance_macro_missing_dates']`` and moves on; ``'raise'`` aborts on the
                first date with no file.
            raise_errors: when a file exists but the *index* is absent from it, raise
                :class:`IndexNotFoundError` instead of recording the date in
                ``attrs['bfinance_macro_index_absent_on_dates']`` and moving on. Off by default
                because an index can legitimately launch partway through a span.

        Returns:
            A ``DatetimeIndex`` frame named ``Date`` for ``index``, ascending, one row per date
            NSE published it on. Empty — with the requested bounds in attrs — when NSE published
            no file for any date in the span.

        Raises:
            ValueError: an inverted range, or an unknown ``on_missing``.
            TickerNotFoundError: no file for a date, and ``on_missing='raise'``.
            IndexNotFoundError: NSE published files in the span and the index was in **none** of
                them. That is a naming error, not a calendar one, and the two are never merged
                into the same empty frame.
        """
        if on_missing not in ("skip", "raise"):
            raise ValueError(
                f"on_missing={on_missing!r} is not supported. Must be 'skip' or 'raise'."
            )
        candidates = iter_candidate_days(start, end, skip_weekends=skip_weekends)

        frames: List[pd.DataFrame] = []
        urls: List[str] = []
        retrieved: List[str] = []
        missing: List[str] = []
        dates_with_file: List[str] = []
        index_absent_on: List[str] = []
        found_any = False

        for candidate in candidates:
            url = self.url_for(candidate)
            if progress:
                logger.info("nse_macro: %s", url)
            payload = await self.source.get(url)
            stamp = self.source.retrieved_utc(url)
            urls.append(url)
            retrieved.append(stamp)
            if payload is None:
                missing.append(candidate.isoformat())
                if on_missing == "raise":
                    raise TickerNotFoundError(
                        f"NSE published no index close file for {candidate:%Y-%m-%d}. Use "
                        f"on_missing='skip' to tolerate weekends and exchange holidays."
                    )
                continue
            day_frame = parse_index_close_csv(
                payload.decode("utf-8-sig", errors="replace"),
                host=self.host,
                url=url,
                retrieved_utc=stamp,
            )
            dates_with_file.append(candidate.isoformat())
            if day_frame.empty:
                continue
            try:
                single = select_index(day_frame, index)
            except IndexNotFoundError:
                # An index can legitimately launch partway through a span, so a day that simply
                # does not carry it is not an error by default. Recorded, not dropped silently.
                index_absent_on.append(candidate.isoformat())
                if raise_errors:
                    raise
                continue
            found_any = True
            # Per-day attrs describe one day; after a merge they would be a lie. Dropped, and
            # re-asserted on the combined frame below.
            single.attrs.clear()
            frames.append(single)

        merged: Dict[str, Any] = _source_attrs(self.host, urls, retrieved)
        merged["bfinance_macro_index"] = index
        merged[START_ATTR] = to_date(start).isoformat()
        merged[END_ATTR] = to_date(end).isoformat()
        merged[CANDIDATE_DAYS_ATTR] = len(candidates)
        merged[MISSING_DATES_ATTR] = missing
        merged["bfinance_macro_skip_weekends"] = bool(skip_weekends)
        merged["bfinance_macro_dates_with_file"] = dates_with_file
        merged["bfinance_macro_index_absent_on_dates"] = index_absent_on

        if not frames:
            # Two different situations, kept apart on purpose. "NSE published nothing for any
            # day in this span" is a calendar answer and returns an empty frame. "NSE published
            # files and the index was in none of them" is a naming error, and an empty frame
            # would read like the first one.
            if dates_with_file and not found_any:
                raise IndexNotFoundError(
                    f"index={index!r} is absent from every one of the {len(dates_with_file)} "
                    f"file(s) NSE published between {to_date(start):%Y-%m-%d} and "
                    f"{to_date(end):%Y-%m-%d}. It may have launched later, been renamed, or "
                    f"been discontinued. This raises rather than returning an empty frame, "
                    f"which would be indistinguishable from NSE publishing no file at all."
                )
            return empty_index_frame(merged)

        combined = pd.concat(frames)
        duplicates = int(combined.index.duplicated(keep="last").sum())
        if duplicates:
            combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index(kind="stable")
        combined = combined[list(COLUMNS) + [NOT_APPLICABLE_COLUMN, UNPARSED_COLUMN]]
        combined.attrs.update(merged)
        combined.attrs["bfinance_macro_duplicate_rows"] = duplicates
        combined.attrs[COVERAGE_ATTR] = _coverage(combined)
        combined.attrs[INDEX_COUNT_ATTR] = 1
        # Recomputed over the merged rows, so each count measures the series actually returned
        # rather than summing a per-day file-level count that would include every other index
        # on that day.
        combined.attrs[NOT_APPLICABLE_ATTR] = _state_counts(combined, NOT_APPLICABLE_COLUMN)
        combined.attrs[UNPARSED_ATTR] = _state_counts(combined, UNPARSED_COLUMN)
        return combined

    def index_range(self, index: str, start, end, **kwargs) -> pd.DataFrame:
        """Sync variant of :meth:`index_range_async`."""
        return _run_async(self.index_range_async(index, start, end, **kwargs))

    async def valuation_series_async(
        self, index: str, start, end, *, on_missing: str = "skip", skip_weekends: bool = True
    ) -> pd.DataFrame:
        """P/E, P/B and dividend yield for one index over a date span.

        A thin projection of :meth:`index_range_async`, so the coverage gap is reported on the
        series as well as on the cross-section.

        Raises:
            IndexNotFoundError: the index is not present on **any** date in the span. Distinguishable
                from "no file on any date in the span", which returns an empty frame with the
                requested bounds in attrs.
        """
        full = await self.index_range_async(
            index, start, end, on_missing=on_missing, skip_weekends=skip_weekends
        )
        if full.empty:
            return full
        out = full[list(VALUATION_FIELDS)]
        out.attrs.update(full.attrs)
        return out

    def valuation_series(self, index: str, start, end, **kwargs) -> pd.DataFrame:
        """Sync variant of :meth:`valuation_series_async`."""
        return _run_async(self.valuation_series_async(index, start, end, **kwargs))


def _run_async(coro):
    """Bridge to sync, matching ``nse.bhavcopy._run_async`` and ``nse.marketlens._run_async``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


# --------------------------------------------------------------------------- shortcuts

_DEFAULT_CLIENT: Optional[MacroClient] = None


def _default_client() -> MacroClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = MacroClient()
    return _DEFAULT_CLIENT


def all_indices(day, *, host: str = DEFAULT_HOST) -> pd.DataFrame:
    """Module-level shortcut: every index NSE published for one date.

    Import from the module, not the package — ``bfinance.nse.__init__`` deliberately does not
    re-export this, and ``bfinance.nse.macro`` is not pulled in by ``import bfinance``.

    >>> from bfinance.nse.macro import all_indices                # doctest: +SKIP
    >>> all_indices("2026-09-25").attrs["bfinance_macro_index_count"]   # doctest: +SKIP
    166
    """
    return _run_async(MacroClient(host=host).all_indices_async(day))


def index_daily(index: str, day, *, host: str = DEFAULT_HOST) -> pd.DataFrame:
    """Module-level shortcut: one index on one date.

    Raises :class:`IndexNotFoundError` for a name the file does not carry.
    """
    return _run_async(MacroClient(host=host).index_daily_async(index, day))


def india_vix(day, *, host: str = DEFAULT_HOST) -> pd.DataFrame:
    """Module-level shortcut: ``India VIX`` on one date.

    >>> from bfinance.nse.macro import india_vix                   # doctest: +SKIP
    >>> india_vix("2026-09-25")[["Close", "ChangePct"]]            # doctest: +SKIP
    Close       12.16
    ChangePct   -4.16
    """
    return _run_async(MacroClient(host=host).india_vix_async(day))


def valuations(day, *, host: str = DEFAULT_HOST) -> pd.DataFrame:
    """Module-level shortcut: P/E, P/B and dividend yield for every index on one date."""
    return _run_async(MacroClient(host=host).valuations_async(day))


def index_range(index: str, start, end, **kwargs) -> pd.DataFrame:
    """Module-level shortcut: one index over a date span."""
    kwargs.setdefault("host", DEFAULT_HOST)
    return _run_async(MacroClient(host=kwargs.pop("host")).index_range_async(
        index, start, end, **kwargs
    ))


def valuation_series(index: str, start, end, **kwargs) -> pd.DataFrame:
    """Module-level shortcut: P/E, P/B and dividend yield for one index over a date span."""
    kwargs.setdefault("host", DEFAULT_HOST)
    return _run_async(MacroClient(host=kwargs.pop("host")).valuation_series_async(
        index, start, end, **kwargs
    ))


def rbi_reference_rate(*args, **kwargs) -> pd.DataFrame:
    """**NOT AVAILABLE. This always raises :class:`SourceUnavailableError`.**

    ``www.rbi.org.in`` returns **HTTP 418** on ``robots.txt`` *and* on every POST — a WAF
    refusal, so a client cannot even establish what the site permits — and the data would
    additionally need an ASP.NET ``__doPostBack`` round trip carrying ``__VIEWSTATE``. The form
    shell GETs 200 but yields no data.

    A prior agent stopped rather than evading the WAF, and no attempt was made here either.
    This function exists so the absence is a *typed, self-describing* state rather than an
    ``AttributeError`` on a name that was never written: it names the host, the status and the
    missing payload. There is deliberately no parser behind it, because nobody has seen the
    payload.

    ``*args, **kwargs`` is accepted and ignored so that *any* call shape lands on the same
    honest answer instead of a ``TypeError`` that reads like a bug.

    Raises:
        SourceUnavailableError: always.
    """
    raise SourceUnavailableError(
        f"RBI reference rate is not available from a compliant client. "
        f"{RBI_REFERENCE_RATE_REASON} No request is attempted and no payload is parsed. "
        f"See macro.UNAVAILABLE_SOURCES['rbi_reference_rate']."
    )
