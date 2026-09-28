"""
NSE ownership data — shareholding patterns, bulk/short/block deals, and two honest refusals.

Three capability groups live here and they are deliberately separable, because a caller who
wants yesterday's block deals should not have to know that shareholding patterns are in the
same file:

* :func:`parse_shareholding` / :meth:`OwnershipClient.shareholding_patterns` — quarterly
  promoter/public split per symbol, with NSE's own revision fields carried through.
* :func:`parse_deals` / :meth:`OwnershipClient.deals` — whole-market bulk, short and block
  deals **with the client named**, which is the one field no other free source in this package
  carries.
* :func:`probe_pledge` — a labelled probe of a route that answers 200 and is permanently
  empty, plus :func:`fpi_ownership_status`, a typed "unavailable, blocked upstream" for the one
  capability this package cannot reach at all.

Every number in this module docstring was measured from the fixtures in
``tests/fixtures/nse/ownership/`` on 2026-09-28, not inferred from a header. Where something is
**not** verified, it says so in the same paragraph as the claim next to it.

Group 1 — shareholding patterns (``/api/corporate-share-holdings-master``)
--------------------------------------------------------------------
``GET /api/corporate-share-holdings-master?index=equities&symbol=RELIANCE`` -> **HTTP 200,
40,154 bytes**, a **bare JSON array**. Not a dict, and the exact failure differs by which dict habit
you reach for: ``resp.json()["data"]`` raises ``TypeError: list indices must be integers or
slices, not str``, and ``resp.json().get("data")`` raises ``AttributeError: 'list' object has no
attribute 'get'``. Both are worth naming because the sibling ``nse.corporate_actions`` feed
*does* answer with a dict for some segments, so the habit formed there is wrong here.
:func:`shareholding_rows` accepts the measured array shape and raises
:class:`~bfinance.utils.exceptions.ParsingError` on anything else.

* **22 rows, 33 verbatim keys, one key-set.** Every row carries all 33; the count is asserted in
  ``tests/test_ownership.py`` so a schema change is loud rather than a silently narrower frame.
* **History starts at 30-SEP-2021 and runs to 30-JUN-2026** — about five years, and it is
  *history only*: the newest row is the last filed quarter, never a forecast.
* **Twenty quarter-ends and two off-cycle filings.** ``29-OCT-2024`` and ``08-DEC-2021`` are
  not quarter ends; both sit in the series among quarter-ends. Any code that assumes "one row
  per calendar quarter" drops them.
* **Restatement is modelled and unproven.** NSE ships first-class revision fields —
  ``revisedData``, ``revisedDate``, ``revisedStatus``, ``revisionDate``, ``revisionRemark``,
  ``revisedRemark`` — and this module carries every one of them into the frame. But across all
  22 rows ``revisedData == 'N'`` on every one, ``revisedStatus == '-'`` on every one, and
  ``revisedDate``/``revisionDate``/``revisionRemark`` are null on every one. So: the fields
  exist, they are exposed, and **no restatement has ever been observed in this data**. Nothing
  here claims otherwise, and ``attrs`` carries the observed ``revisedData`` histogram so a
  caller can see the same fact without re-reading this paragraph.

Three date spellings, one normaliser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Upstream uses three different representations across this module's payloads, and they are not
interchangeable as *strings*:

============================  =================================  ==============================
spelling                      fields                             example
============================  =================================  ==============================
``DD-MMM-YYYY`` upper         ``date``, ``submissionDate``       ``30-JUN-2026``
``DD-MMM-YYYY HH:MM:SS``      ``broadcastDate``, ``systemDate``  ``16-JUL-2026 19:24:44``
``DD-Mmm-YYYY`` title         deals ``as_on_date``, row ``date`` ``25-Sep-2026``
============================  =================================  ==============================

:func:`normalise_upstream_date` accepts all three, is case-tolerant on the month token, and
returns a **tz-naive** ``pd.Timestamp`` — NSE publishes IST wall-clock with no offset, and this
module does not invent a zone it was not given (same rule as the naive ex-date index in
``nse.corporate_actions``). A fourth value, ``timeDifference``, is a *duration* not a date, so
it is parsed to seconds by :func:`parse_broadcast_lag` beside the raw text. The ``"-"`` / ``""``
/ ``"::"`` / ``null`` sentinels all mean "absent" and are normalised to ``None`` in **both**
columns: the raw text survives for the values upstream does send, and the ``"::"`` sentinel does
not, because a column reading ``"::"`` invites a reader to parse it a third time.

``(symbol, date)`` is not the key, and neither is ``broadcastDate`` on its own
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The off-cycle filing ``08-DEC-2021`` was submitted ``18-DEC-2021`` but **broadcast
07-JAN-2022 03:25:01** — and the ``30-SEP-2021`` quarter-end was broadcast **07-JAN-2022
02:55:01**, 78 days after its own submission. Measured consequence:

* ``(Symbol, AsOf)`` — all 22 distinct **in this capture**, because the feed happens not to file
  twice for one period. Asserted, so if NSE ever does, the test goes red.
* ``(Symbol, broadcast *day*)`` — **not** distinct: 2 filings share ``07-JAN-2022``. Keying on
  the day collapses them.
* ``(Symbol, AsOf, BroadcastAt)`` — distinct, and is what :func:`shareholding_identity` returns.

So the composite is the only key that is provably unique, and ``broadcastDate`` is the field
that earns its place: it is the only value in the object that separates those two filings.
Truncating it to a day — which every "group by date" instinct does — destroys the
distinction, and a test pins that.

Group 2 — bulk / short / block deals (``/api/snapshot-capital-market-largedeal``)
------------------------------------------------------------------------------
``GET /api/snapshot-capital-market-largedeal`` -> **HTTP 200, 54,901 bytes**, and it carries
**all three deal kinds in one request**. The sibling route
``/api/snapshot-capital-market-blockdeal`` is a **404** (route gone); it is not needed, because
this one already carries ``BLOCK_DEALS_DATA``. Do not "fix" the block deals by switching routes.

Seven top-level keys, and three of them are **strings**:

* ``as_on_date`` = ``"25-Sep-2026"``
* ``BULK_DEALS`` = ``"237"``, ``SHORT_DEALS`` = ``"75"``, ``BLOCK_DEALS`` = ``"6"`` — all
  ``str``, despite reading as counts. :func:`deal_counts` parses them to ``int`` and keeps the
  verbatim strings beside them; the counters are the declared row counts and each **equals the
  length of its ``_DATA`` array** on all three kinds (measured), which is what makes them a
  completeness check rather than decoration.
* ``BULK_DEALS_DATA`` 237 rows, ``SHORT_DEALS_DATA`` 75, ``BLOCK_DEALS_DATA`` 6.

Row keys are exactly 8: ``buySell``, ``clientName``, ``date``, ``name``, ``qty``, ``remarks``,
``symbol``, ``watp``.

**``clientName`` is the reason this module exists.** It is the counterparty on a whole-market
bulk/short/block deal, and no other free source this package touches names it. It is
load-bearing, not decorative: all 318 rows are distinct on the full set of output columns, and
317 are distinct once ``ClientName`` is removed — two genuine ``AXIOMGAS`` ``SELL`` 186,000
@ 54.75 bulk rows dated 25-Sep-2026, one to ``BINDU R BAFNA`` and one to ``VBCUBE VENTURES
FUND``, become one indistinguishable row. The test asserts that 318-to-317, so the column cannot
be quietly dropped later.

Trap 1 — ``as_on_date`` does not cover the short deals
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
One payload, **two** dates. ``as_on_date`` is ``25-Sep-2026``; all 237 bulk and all 6 block rows
are ``25-Sep-2026``; and **all 75 short-deal rows are ``24-Sep-2026``**. A caller that filters
on ``as_on_date`` therefore loses **75 of 318 rows — 100% of the short deals — with no error and
no warning**. :func:`deals_for_date` filters on the per-row ``date``, and the count lands in
``attrs["bfinance_deals_off_as_on_date_rows"]`` so the gap is visible without filtering.

Trap 2 — the short-deal rows are largely null
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Measured null density per kind (null cells / all cells):

==========  =====  ==========  =================================================
kind        rows   null share  columns that are null in *every* row
==========  =====  ==========  =================================================
``bulk``      237   0.000      (none)
``short``      75   0.500      ``buySell``, ``clientName``, ``remarks``, ``watp``
``block``      6   0.125      ``remarks``
==========  =====  ==========  =================================================

A short deal arrives with a symbol, a name, a quantity and a date — and no side, no client, no
price. That is a mostly-null frame, and presenting it as data is how "NSE reported 75 short
deals" gets written down when 75 rows with a quantity and nothing else were returned. So
:func:`parse_deals` records the density, the per-column null counts and the all-null column
names in ``attrs``, exposes :func:`is_populated`, and **logs a warning** naming the kind. A
caller can then tell *empty* (0 rows) from *unpopulated* (rows that carry almost nothing), which
are different facts and neither is inferable from ``len(frame)`` alone.

This route is a **snapshot**: one day, no history parameter, no ``date`` argument. A sibling
route was measured to **ignore a ``date=`` argument and return today's data with a 200**, so
this module does not pretend to honour one. :func:`parse_deals` and
:meth:`OwnershipClient.deals` accept ``expected_date`` purely as an **assertion**: it is checked
against the payload's own ``as_on_date`` and raises :class:`DealsSnapshotMismatch` when they
disagree, which turns a silent wrong-day answer into a loud one.

Units — determined by measurement, not by a header
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
No payload here carries a unit string, so every entry in :data:`UNITS` is either
measured, or labelled ``UNDECLARED``/``DERIVED`` because it cannot be:

* ``PromoterPct`` / ``PublicPct`` (``pr_and_prgrp`` / ``public_val``) are **percent of total
  shares**: they sum to exactly ``100.00`` on all 22 rows (observed range 50.00-50.66 and
  49.34-50.00). That arithmetic *is* the evidence; the spelling ``"50"`` means 50.00.
* ``Qty`` is a **share count**: every value is a digit string, range 20,000-14,550,924 (bulk),
  357,000-8,600,000 (block), 1-157,502 (short). The floor of 20,000 bulk shares is the SEBI
  bulk-deal screen, which is an independent check on the reading.
* ``WATP`` is **INR per share** (weighted average traded price): 8.97-2,905.0. Multiplying by
  ``Qty`` lands at 0.03-2,498.30 INR crore, the right order for these deal sizes, which is the
  second check.
* ``NotionalCr`` is **DERIVED** (``Qty * WATP / 1e7``) and marked as such in :data:`UNITS` and in
  ``attrs["bfinance_deals_notional_basis"]``. NSE publishes no notional on this route, so a
  caller who needs a *published* notional does not have one here.
* ``EmployeeTrusts`` is **undeclared**: the string ``"0"`` on all 22 rows, and a zero is
  consistent with a count and with a percent. It is carried verbatim as a string and **not**
  coerced to a number.
* ``XBRLFileSize`` is a formatted string (``"510.29 KB"``, 5 of 22 rows non-null). Not parsed to
  bytes, because NSE does not say whether ``KB`` is 1,000 or 1,024.
* ``BroadcastLag`` is an **elapsed** ``HH:MM:SS`` and is *not* a guess: it equals
  ``systemDate - broadcastDate`` to the second on all 15 rows where both exist
  (``31-MAR-2024``: broadcast ``20-APR-2024 00:20:05``, system ``20-APR-2024 08:05:03``, lag
  ``07:44:58``). Range 3 s to 27,898 s, so it exceeds an hour and cannot be a time of day.
  ``"::"`` is the sentinel on **exactly** the 7 rows whose ``systemDate`` is null and on none of
  the 15 where it is populated.

Group 3a — pledge: reachable, empty, schema unverified
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``GET /api/corporate-pledgedata?index=equities&symbol=YESBANK&from_date=01-01-2018&to_date=27-09-2026``
-> **HTTP 200, 28 bytes, ``{"comNameList":[],"data":[]}``**. Re-measured 2026-09-28:
byte-identical (sha256 ``c2fc7dc8...``). The same request **market-wide** (no ``symbol``) is
also empty.

The route is **not** 404 — it is live and answering. But **zero rows have ever been observed**,
so **row-level field names are UNVERIFIED**. Therefore this module ships a **probe**, not a
parser: :func:`probe_pledge_payload` returns a :class:`PledgeProbe` that reports reachability,
the observed top-level keys, the row count and ``row_schema_verified = False``, with a verdict
string that says all of it; :func:`probe_pledge` and
:meth:`OwnershipClient.probe_pledge` are the request-backed wrappers. Neither returns a
``DataFrame`` and neither names a pledge field, because naming a field nobody has seen is how a
parser for a payload that does not exist gets written. The empty response is stored as a
**deliberate negative fixture** beside the positive ones.

Group 3b — FPI / foreign-ownership limit and utilisation: blocked, and not attempted
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
That data lives in ``/api/quote-equity``, which returns **403 Access Denied (Akamai
edgesuite)** from this host, primed or not — the known blocked route already recorded in
``nse/marketlens.py``. It was **not** requested while building this module, because
:func:`fpi_ownership` raises :class:`FpiOwnershipUnavailableError` before any transport exists,
and a parser for a payload nobody has seen would be fiction. :func:`fpi_ownership_status`
returns the typed, zero-I/O reason. The block is **selective per route** —
``/api/quote-equity`` 403s while these three ``/api/*`` routes returned 200 on the same
session — so "one route worked" is never evidence about another.

Not built, and why
------------------
Both refusals above are capability, not omission. One route returns nothing and the other is
403; in both cases the honest output is a typed statement of *what was observed*, which is
exactly what :class:`PledgeProbe` and :class:`FpiOwnershipStatus` are.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.nse.marketlens import validate_nse_symbol
from bfinance.utils.exceptions import (
    ParsingError,
    RateLimitExceededError,
    UpstreamServiceError,
)

logger = logging.getLogger("bfinance")

#: Every public name in this module, and nothing else. Without it, ``from bfinance.ownership
#: import *`` binds this module's own ``pd``, ``httpx``, ``json``, ``asyncio`` and the
#: ``SQLiteCache`` it borrows from :mod:`bfinance.cache` into the caller's namespace, which is a
#: way to shadow a caller's ``json`` with NSE's parser. Only names **defined here** are listed;
#: every imported name is excluded, so a star import carries the API and not the dependencies.
#: ``SQLiteCache`` and ``validate_nse_symbol`` are borrowed and deliberately not re-exported —
#: they are the callers' to import from their own modules. Matches the convention
#: :mod:`bfinance.flows` and :mod:`bfinance.costs` use.
__all__ = [
    # ---- transport and provenance ----
    "IST",
    "BASE_URL",
    "PROVENANCE_SHAREHOLDING",
    "PROVENANCE_DEALS",
    "PROVENANCE_PLEDGE",
    "SHAREHOLDING_API_PATH",
    "DEALS_API_PATH",
    "PLEDGE_API_PATH",
    "FPI_API_PATH",
    "LANDING_URL",
    # ---- the upstream date formats, read not guessed ----
    "UPSTREAM_DATE",
    "UPSTREAM_DATETIME",
    "UPSTREAM_DEAL_DATE",
    "UPSTREAM_ELAPSED",
    "ABSENT_SENTINELS",
    "normalise_upstream_date",
    "parse_broadcast_lag",
    # ---- shareholding ----
    "SHAREHOLDING_COLUMNS",
    "SHAREHOLDING_KEY_MAP",
    "SHAREHOLDING_DATE_COLUMNS",
    "SHAREHOLDING_CLOCK_COLUMNS",
    "UNITS",
    "SHAREHOLDING_FIRST_OBSERVED_DATE",
    "SHAREHOLDING_LAST_OBSERVED_DATE",
    "SHAREHOLDING_OFF_CYCLE_DATES",
    "validate_index",
    "shareholding_url",
    "shareholding_rows",
    "parse_shareholding",
    "shareholding_identity",
    # ---- bulk / block deals ----
    "DealsSnapshotMismatch",
    "DEAL_KINDS",
    "DEAL_ARRAY_KEYS",
    "DEAL_COUNTER_KEYS",
    "DEAL_COLUMNS",
    "DEAL_NULL_DENSITY_UNPOPULATED",
    "DEALS_AS_ON_DATE_KEY",
    "DEAL_NULLABLE_SOURCE_COLUMNS",
    "deals_url",
    "parse_deals",
    "deal_counts",
    "is_populated",
    "deals_for_date",
    # ---- pledge: the route is reachable and publishes nothing ----
    "PledgeProbe",
    "pledge_url",
    "probe_pledge_payload",
    # ---- FPI: refused, and says so ----
    "FpiOwnershipUnavailableError",
    "FpiOwnershipStatus",
    "FPI_BLOCKED_STATUS",
    "fpi_ownership_status",
    "fpi_ownership",
    # ---- the client ----
    "USE_DEFAULT_CACHE",
    "OwnershipClient",
    "shareholding_patterns",
    "shareholding_patterns_async",
    "deals",
    "deals_async",
    "probe_pledge",
    "probe_pledge_async",
]

IST = ZoneInfo("Asia/Kolkata")

BASE_URL = "https://www.nseindia.com"

#: Provenance tags. One per capability group, so a frame's origin is unambiguous even though
#: three groups share this module.
PROVENANCE_SHAREHOLDING = "nse_ownership_shareholding"
PROVENANCE_DEALS = "nse_ownership_deals"
PROVENANCE_PLEDGE = "nse_ownership_pledge"

SHAREHOLDING_API_PATH = "/api/corporate-share-holdings-master"
DEALS_API_PATH = "/api/snapshot-capital-market-largedeal"
PLEDGE_API_PATH = "/api/corporate-pledgedata"

#: Named only, so the reason FPI is unavailable points at a route rather than a shrug. **This
#: module never requests it** — see :data:`FPI_BLOCKED_STATUS`.
FPI_API_PATH = "/api/quote-equity"

#: Landing page whose cookie primes the retry path, reused from ``nse/corporate_actions.py``.
LANDING_URL = "https://www.nseindia.com/companies-listing/corporate-shareholding-pattern"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Process-global pacing, the same scheme as ``nse.corporate_actions`` and ``nse.marketlens``:
# several agents and several clients share one process against one edge.
_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


# --------------------------------------------------------------------- date handling

#: The three upstream date spellings this module normalises. They stay separate constants
#: because they are separate *facts* about two different payloads, and a reader comparing them
#: should be able to see that they differ.
UPSTREAM_DATE = "DD-MMM-YYYY"                 # "30-JUN-2026"  upper-case month
UPSTREAM_DATETIME = "DD-MMM-YYYY HH:MM:SS"   # "16-JUL-2026 19:24:44"
UPSTREAM_DEAL_DATE = "DD-Mmm-YYYY"            # "25-Sep-2026"  title-case month
UPSTREAM_ELAPSED = "HH:MM:SS"                 # "07:44:58", sentinel "::"

#: Every "absent" spelling seen across these payloads, normalised to ``None``. ``"-"`` is an
#: NSE text sentinel (not null), ``""`` is an empty text field, ``"::"`` is the placeholder in
#: ``timeDifference`` and ``None`` is JSON null. Measured: ``"-"`` on 22/22 rows for
#: ``revisedStatus``/``index``/``industry``, ``"::"`` on 7/22 for ``timeDifference``.
#:
#: ``"N"`` is deliberately **absent** from this set. It is upstream's "no notes" for
#: ``remarksWeb`` (14 of 22 rows) *and* it is ``revisedData``'s "not revised" — two meanings that
#: must not collapse into one ``None``, or a not-revised filing would be indistinguishable from a
#: row that said nothing about revision.
ABSENT_SENTINELS = frozenset({"", "-", "--", "::", "N/A", "NA"})

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: One regex for all three spellings, clock optional -- because ``DD-MMM-YYYY HH:MM:SS`` is the
#: same date as ``DD-MMM-YYYY`` plus a clock, and splitting them into two patterns is how a
#: parser ends up silently dropping the time-of-day it was handed.
_UPSTREAM_TS_RE = re.compile(
    r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:[ T](\d{1,2}):(\d{2}):(\d{2}))?$"
)
_UPSTREAM_ELAPSED_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")


def normalise_upstream_date(value: Any, *, with_clock: bool = False) -> Optional[pd.Timestamp]:
    """Parse any of :data:`UPSTREAM_DATE`, :data:`UPSTREAM_DATETIME`, :data:`UPSTREAM_DEAL_DATE`.

    One function for all three spellings, on purpose: the payloads disagree about case and about
    whether a clock is attached, and a caller should not have to know which route a value came
    from in order to sort it. The month token is matched case-insensitively, so ``30-JUN-2026``
    and ``25-Sep-2026`` both parse — but they are different *strings*, and comparing them as
    strings is a bug (see the module docstring on the two deal dates inside one payload).

    Args:
        value: the raw upstream value, or an already-parsed date-like.
        with_clock: accept and keep a trailing ``HH:MM:SS``. When ``False`` a value that carries
            a clock is truncated to midnight, so a caller who asked for a date never silently
            receives a time-of-day they did not ask for.

    Returns:
        A **tz-naive** ``pd.Timestamp``, or ``None`` for a sentinel, a blank, an unrecognised
        shape, or an impossible date such as ``31-Feb-2026``. NSE publishes IST wall-clock with
        no offset, so no zone is attached to an upstream string: inventing one would be a claim
        upstream never made. A *caller-supplied* aware datetime is different — it carries a
        zone, so it is converted to IST and then made naive, which keeps it comparable with
        everything else this module returns.

    The ``None`` cases are counted rather than dropped — see
    ``attrs["bfinance_shareholding_unparsed_dates"]`` and
    ``attrs["bfinance_deals_unparsed_dates"]``.
    """
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            return stamp.tz_convert(IST).tz_localize(None)
        return stamp
    if isinstance(value, datetime):
        return normalise_upstream_date(pd.Timestamp(value), with_clock=with_clock)
    if isinstance(value, date):
        return pd.Timestamp(value)

    text = str(value).strip()
    if text in ABSENT_SENTINELS:
        return None
    match = _UPSTREAM_TS_RE.match(text)
    if match is None:
        return None
    day, month_token, year, hours, minutes, seconds = match.groups()
    month = _MONTHS.get(month_token.upper())
    if month is None:
        return None
    try:
        stamp = pd.Timestamp(year=int(year), month=month, day=int(day))
    except ValueError:
        return None
    if not with_clock or hours is None:
        return stamp
    return stamp + pd.Timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds))


def _parse_request_date(value: Any) -> Optional[pd.Timestamp]:
    """Parse a **caller-supplied** date argument. Upstream spellings first, ISO as a fallback.

    Kept separate from :func:`normalise_upstream_date` on purpose. That function's contract is
    "these are the three spellings NSE sends", and widening it to accept ISO would quietly make
    the docstring's count a lie while solving nothing. This helper is for the arguments a *caller*
    writes — ``expected_date=``, ``deals_for_date(..., date=)``, ``pledge_url(from_date=)`` — and
    a caller who types ``"2026-09-25"`` must not be told the snapshot is the wrong day because
    this package prefers a different calendar spelling.
    """
    stamp = normalise_upstream_date(value)
    if stamp is not None:
        return stamp
    try:
        parsed = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if parsed is pd.NaT or pd.isna(parsed):
        return None
    if parsed.tzinfo is not None:
        return parsed.tz_convert(IST).tz_localize(None)
    return parsed


def parse_broadcast_lag(value: Any) -> Optional[float]:
    """``timeDifference`` as elapsed **seconds**, or ``None``.

    ``timeDifference`` is a duration, not a timestamp, and that is measured rather than assumed:
    it equals ``systemDate - broadcastDate`` to the second on all 15 rows where both are present
    (``31-MAR-2024`` broadcast ``20-APR-2024 00:20:05``, system ``20-APR-2024 08:05:03``, lag
    ``07:44:58``). Observed range 3 s to 27,898 s, so it exceeds an hour and cannot be mistaken
    for a time of day.

    ``"::"`` (7 of 22 rows) is the absent sentinel and returns ``None`` rather than zero — a lag
    of zero would read as "broadcast and system stamp identical", which is a different claim from
    "not reported". It is the sentinel on **exactly** the 7 rows whose ``systemDate`` is null and
    on none of the 15 where it is populated, which is what identifies it as a derived field with
    a missing input rather than a value in its own right.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text in ABSENT_SENTINELS:
        return None
    match = _UPSTREAM_ELAPSED_RE.match(text)
    if match is None:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def _clean_text(value: Any) -> Optional[str]:
    """``None`` for JSON null and for every sentinel in :data:`ABSENT_SENTINELS`; else stripped text."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ABSENT_SENTINELS else text


def _to_float(value: Any) -> Optional[float]:
    """Best-effort float. Never raises: an unparseable amount is ``None`` and is counted."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ABSENT_SENTINELS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    """Best-effort int for a share count. ``None`` on anything that is not a whole number."""
    number = _to_float(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else None


# ================================================================== group 1: shareholding

#: Output columns of :func:`parse_shareholding`, in order. The as-of date is the index.
SHAREHOLDING_COLUMNS: Tuple[str, ...] = (
    "Symbol", "Company", "PromoterPct", "PublicPct", "EmployeeTrusts",
    "Revised", "RevisedStatus", "RevisedDate", "RevisionDate",
    "RevisedRemark", "RevisionRemark", "SubmissionDate", "BroadcastAt",
    "BroadcastLag", "BroadcastLagSeconds", "SystemAt", "TypeOfSubmission",
    "RecordId", "XBRLUrl", "XBRLFileSize", "ShareholdingNotes",
    "PromoterDematNotes", "PublicNotes", "Remarks",
)

#: Which upstream key feeds which column. A ``None`` value is a key this module **saw and chose
#: not to map**, which is a decision on the record rather than a gap — and a key absent from this
#: dict entirely is an upstream key this module has never seen, which is the loud signal.
SHAREHOLDING_KEY_MAP: Dict[str, Optional[str]] = {
    "symbol": "Symbol",
    "name": "Company",
    "pr_and_prgrp": "PromoterPct",
    "public_val": "PublicPct",
    "employeeTrusts": "EmployeeTrusts",
    "revisedData": "Revised",
    "revisedStatus": "RevisedStatus",
    "revisedDate": "RevisedDate",
    "revisionDate": "RevisionDate",
    "revisedRemark": "RevisedRemark",
    "revisionRemark": "RevisionRemark",
    "submissionDate": "SubmissionDate",
    "broadcastDate": "BroadcastAt",
    "timeDifference": "BroadcastLag",
    "systemDate": "SystemAt",
    "typeOfSubmission": "TypeOfSubmission",
    "recordId": "RecordId",
    "xbrl": "XBRLUrl",
    "xbrlFileSize": "XBRLFileSize",
    "shareholdingPatternNotes": "ShareholdingNotes",
    "promoterDematNotes": "PromoterDematNotes",
    "publicShareholdingNotes": "PublicNotes",
    "remarksWeb": "Remarks",
    "date": "AsOf",
    "desc": None,            # "NEW_1" on all 22 captured rows -- no meaning recoverable
    "newColumn": None,       # True on 5 of 22; NSE's own schema-version flag, opaque
    "countString": None,     # null on all 22
    "format": None,          # null on all 22
    "index": None,           # "-" on all 22
    "industry": None,        # "-" on all 22
    "isin": None,            # null on all 22, so the symbol and not the ISIN is the usable key
    "cgTimeStamp": None,     # null on all 22
    "underlyingDrs": None,   # null on all 22 (RELIANCE's DR holdings are in the prose notes)
}

#: Columns carrying a date parsed with :data:`UPSTREAM_DATE` / :data:`UPSTREAM_DATETIME`.
SHAREHOLDING_DATE_COLUMNS: Tuple[str, ...] = (
    "AsOf", "SubmissionDate", "RevisedDate", "RevisionDate",
)
SHAREHOLDING_CLOCK_COLUMNS: Tuple[str, ...] = ("BroadcastAt", "SystemAt")

#: Declared units. Each is measured, or labelled ``UNDECLARED``/``DERIVED`` because it cannot be.
#: See the module docstring for the measurement behind every line.
UNITS: Dict[str, Dict[str, str]] = {
    "shareholding": {
        "PromoterPct": (
            "percent_of_total_shares; measured: PromoterPct + PublicPct == 100.00 on all 22 "
            "captured rows (50.00-50.66)"
        ),
        "PublicPct": (
            "percent_of_total_shares; measured: sums to 100.00 with PromoterPct on all 22 rows "
            "(49.34-50.00)"
        ),
        "EmployeeTrusts": (
            "UNDECLARED; the string '0' on all 22 rows is consistent with a share count and with "
            "a percent. Carried verbatim, never coerced to a number."
        ),
        "XBRLFileSize": (
            "formatted string such as '510.29 KB' (5 of 22 rows non-null). NOT parsed to bytes: "
            "NSE does not state whether KB is 1000 or 1024."
        ),
        "BroadcastLag": (
            "elapsed HH:MM:SS in seconds; measured: equals systemDate - broadcastDate to the "
            "second on all 15 rows carrying both, range 3s-27,898s. Sentinel '::' on 7 of 22 rows "
            "reads as None, not 0."
        ),
        "RecordId": "string identifier; all 22 captured values are digits, kept as text verbatim",
    },
    "deals": {
        "Qty": (
            "share_count; measured: every value is a digit string. Bulk 20,000-14,550,924, block "
            "357,000-8,600,000, short 1-157,502. The 20,000 bulk floor is the SEBI bulk-deal "
            "screen, an independent check on the reading."
        ),
        "WATP": (
            "INR_per_share (weighted average traded price); measured 8.97-2,905.0, and "
            "Qty x WATP lands at 0.03-2,498.30 INR crore, the right order for these deal sizes."
        ),
        "NotionalCr": (
            "DERIVED: Qty x WATP / 1e7, in INR crore. NSE publishes no notional on this route."
        ),
        "declared_counters": (
            "row_count; measured: the string counters equal len() of their _DATA array on all "
            "three kinds, which is what makes them a completeness check."
        ),
    },
}

#: The earliest ``date`` in the captured RELIANCE series. The feed reaches back about five years,
#: not to listing: 30-SEP-2021 is the oldest of 22 rows. Recorded so a caller can tell "no
#: history" from "not asked".
SHAREHOLDING_FIRST_OBSERVED_DATE = "2021-09-30"

#: Latest ``date`` in the captured RELIANCE series, i.e. the last filed quarter.
SHAREHOLDING_LAST_OBSERVED_DATE = "2026-06-30"

#: Filings in the captured RELIANCE series whose ``date`` is not a calendar quarter end. **Two**,
#: not one: a parser that assumes one row per calendar quarter loses both.
SHAREHOLDING_OFF_CYCLE_DATES: Tuple[str, ...] = ("2021-12-08", "2024-10-29")


def validate_index(index: str) -> str:
    """Lowercase and strip a segment key. **No whitelist**, and that is deliberate.

    ``equities`` is the only value measured for these routes. A whitelist would imply the other
    segments are known to fail, which this module has not established; an unmeasured dimension is
    labelled unmeasured instead (see :func:`shareholding_url`). The value is still normalised,
    because ``?index=Equities`` and ``?index=equities`` are the same request and must not become
    two cache keys.
    """
    if not isinstance(index, str):
        raise ValueError(f"index must be a string, got {type(index).__name__}")
    clean = index.strip().lower()
    if not clean:
        raise ValueError("index must be a non-empty string")
    return clean


def shareholding_url(symbol: Optional[str] = None, index: str = "equities") -> str:
    """Build the shareholding request URL. Pure and offline — no request is issued.

    ``symbol=None`` omits the parameter, which is the only way to ask for more than one company.
    **Unmeasured:** whether NSE returns a market-wide answer to the symbol-less form, and whether
    any other ``index`` resolves. The parameter exists because the route takes one; this module
    has never sent it either way, so it does not claim to know.

    A ``.NS`` suffix is stripped by :func:`~bfinance.nse.marketlens.validate_nse_symbol`; the
    route does not understand it.
    """
    parts = [f"index={validate_index(index)}"]
    if symbol is not None:
        parts.append(f"symbol={validate_nse_symbol(symbol)}")
    return f"{BASE_URL}{SHAREHOLDING_API_PATH}?" + "&".join(parts)


def shareholding_rows(payload: Any, url: str = "") -> List[Dict[str, Any]]:
    """Normalise the shareholding payload into row dicts.

    **The measured shape is a bare JSON array.** ``resp.json()["data"]`` raises ``TypeError``
    and ``resp.json().get("data")`` raises ``AttributeError`` here, and the habit is easy to form
    because the sibling corporate-actions feed answers with a dict for some segments. This
    function is the only place the shape is decided, so the trap has one place to live.

    A dict carrying a list under ``"data"`` is accepted as forward-compatibility only, and is
    **not** a shape this module has observed. Anything else raises
    :class:`~bfinance.utils.exceptions.ParsingError` rather than being iterated blindly.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is None:
            raise ParsingError(
                f"shareholding payload for {url} is an object without a 'data' list. The measured "
                f"shape is a BARE JSON ARRAY; observed keys were {sorted(payload)[:8]}. Upstream "
                f"schema changed."
            )
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        raise ParsingError(
            f"shareholding payload for {url} has a 'data' of type {type(data).__name__}. "
            f"Upstream schema changed."
        )
    raise ParsingError(
        f"shareholding payload for {url} is a {type(payload).__name__}, expected a bare JSON array "
        f"of filing objects. Upstream schema changed."
    )


def parse_shareholding(
    payload: Any,
    *,
    symbol: Optional[str] = None,
    url: str = "",
) -> pd.DataFrame:
    """Turn a raw shareholding payload into the parsed frame. Pure — no network, no clock.

    Index: a naive ``DatetimeIndex`` named ``AsOf``, built from ``date``. Rows are **never
    dropped** for being unparseable: a filing with no usable date keeps a ``NaT`` index, is
    counted in ``attrs["bfinance_shareholding_undated_rows"]`` and is therefore excluded from
    every time-series join by construction — invisible to the arithmetic, visible to the reader.

    Sorting is ascending by ``AsOf`` with undated rows last, so the two off-cycle filings
    (29-OCT-2024, 08-DEC-2021) land where they belong rather than being appended at the end. NSE
    serves this array **descending**; the reversal is deliberate and asserted in the tests.

    The revision fields are carried, not interpreted. ``revisedData``/``revisedStatus``/
    ``revisedDate``/``revisionDate``/``revisedRemark``/``revisionRemark`` are all exposed, and
    ``attrs["bfinance_shareholding_revised_data"]`` is the observed ``revisedData`` histogram —
    ``{"N": 22}`` in the captured data. **That is the point:** restatement is modelled upstream
    and unobserved in practice, and a caller can see both facts without trusting this docstring.

    One precision note that is a decision rather than a measurement: ``revisedDate`` and
    ``revisionDate`` are read as **dates**, with any clock dropped, because their names say
    ``Date`` and no value for either has ever been observed. ``broadcastDate`` and ``systemDate``
    are read as **timestamps** because a value for both has been observed carrying one. If NSE
    ever puts a time in ``revisionDate`` it will be truncated to midnight, and that is the one
    place in this module where a value is deliberately lost — stated here rather than discovered.
    """
    rows = shareholding_rows(payload, url=url)
    records: List[Dict[str, Any]] = []
    undated: List[str] = []
    unparsed: List[str] = []

    for row in rows:
        as_of_raw = row.get("date")
        as_of = normalise_upstream_date(as_of_raw)
        if as_of is None:
            undated.append(str(as_of_raw))
        for key in ("submissionDate", "revisedDate", "revisionDate"):
            raw = row.get(key)
            if raw is not None and normalise_upstream_date(raw) is None:
                unparsed.append(f"{key}={raw!r}")
        for key in ("broadcastDate", "systemDate"):
            raw = row.get(key)
            if raw is not None and normalise_upstream_date(raw, with_clock=True) is None:
                unparsed.append(f"{key}={raw!r}")

        lag_raw = row.get("timeDifference")
        records.append(
            {
                "AsOf": as_of,
                "Symbol": (str(row.get("symbol")) or "").strip().upper() or None,
                "Company": _clean_text(row.get("name")),
                # Units measured, not inferred: see UNITS["shareholding"].
                "PromoterPct": _to_float(row.get("pr_and_prgrp")),
                "PublicPct": _to_float(row.get("public_val")),
                # Undeclared unit -- carried verbatim as text, never coerced.
                "EmployeeTrusts": _clean_text(row.get("employeeTrusts")),
                "Revised": _clean_text(row.get("revisedData")),
                "RevisedStatus": _clean_text(row.get("revisedStatus")),
                "RevisedDate": normalise_upstream_date(row.get("revisedDate")),
                "RevisionDate": normalise_upstream_date(row.get("revisionDate")),
                "RevisedRemark": _clean_text(row.get("revisedRemark")),
                "RevisionRemark": _clean_text(row.get("revisionRemark")),
                "SubmissionDate": normalise_upstream_date(row.get("submissionDate")),
                "BroadcastAt": normalise_upstream_date(row.get("broadcastDate"), with_clock=True),
                "BroadcastLag": _clean_text(lag_raw),
                "BroadcastLagSeconds": parse_broadcast_lag(lag_raw),
                "SystemAt": normalise_upstream_date(row.get("systemDate"), with_clock=True),
                "TypeOfSubmission": _clean_text(row.get("typeOfSubmission")),
                "RecordId": _clean_text(row.get("recordId")),
                "XBRLUrl": _clean_text(row.get("xbrl")),
                "XBRLFileSize": _clean_text(row.get("xbrlFileSize")),
                "ShareholdingNotes": _clean_text(row.get("shareholdingPatternNotes")),
                "PromoterDematNotes": _clean_text(row.get("promoterDematNotes")),
                "PublicNotes": _clean_text(row.get("publicShareholdingNotes")),
                "Remarks": _clean_text(row.get("remarksWeb")),
            }
        )

    if records:
        frame = pd.DataFrame.from_records(records)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("AsOf"), errors="coerce"))
        frame.index.name = "AsOf"
        frame = frame.sort_index(kind="stable", na_position="last")
    else:
        frame = pd.DataFrame(
            {c: pd.Series(dtype=object) for c in SHAREHOLDING_COLUMNS},
            index=pd.DatetimeIndex([], name="AsOf"),
        )

    seen_keys = {key for row in rows for key in row}
    off_cycle = (
        sorted(
            {
                stamp.strftime("%Y-%m-%d")
                for stamp in frame.index
                if not pd.isna(stamp)
                and (stamp.month, stamp.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}
            }
        )
        if not frame.empty
        else []
    )
    revised = frame["Revised"].value_counts().to_dict() if not frame.empty else {}

    frame.attrs["bfinance_provenance"] = PROVENANCE_SHAREHOLDING
    frame.attrs["bfinance_capability"] = "shareholding_patterns"
    frame.attrs["bfinance_source_url"] = url or None
    frame.attrs["bfinance_symbol"] = (symbol or "").upper() or None
    frame.attrs["bfinance_source_row_count"] = len(rows)
    frame.attrs["bfinance_columns"] = SHAREHOLDING_COLUMNS
    frame.attrs["bfinance_units"] = UNITS["shareholding"]
    # Every date spelling this payload is known to use, so a reader of attrs can see the
    # normaliser was pointed at the right formats.
    frame.attrs["bfinance_upstream_date_formats"] = (
        UPSTREAM_DATE, UPSTREAM_DATETIME, UPSTREAM_ELAPSED
    )
    frame.attrs["bfinance_shareholding_undated_rows"] = len(undated)
    frame.attrs["bfinance_shareholding_undated_values"] = undated
    frame.attrs["bfinance_shareholding_unparsed_dates"] = unparsed
    frame.attrs["bfinance_shareholding_upstream_keys"] = sorted(seen_keys)
    # An upstream key this module has never seen -- the loud signal that the schema moved.
    frame.attrs["bfinance_shareholding_unexpected_keys"] = sorted(
        key for key in seen_keys if key not in SHAREHOLDING_KEY_MAP
    )
    # A key this module saw and chose not to map. A decision on the record, not a gap.
    frame.attrs["bfinance_shareholding_unmapped_keys"] = sorted(
        key for key in seen_keys if SHAREHOLDING_KEY_MAP.get(key, "?") is None
    )
    # Modelled, exposed, unobserved. See the module docstring.
    frame.attrs["bfinance_shareholding_revised_data"] = {
        str(k): int(v) for k, v in revised.items()
    }
    frame.attrs["bfinance_shareholding_restatement_observed"] = any(
        str(k).upper() == "Y" for k in revised
    )
    frame.attrs["bfinance_shareholding_off_cycle_dates"] = off_cycle
    frame.attrs["bfinance_shareholding_first_date"] = (
        None if frame.empty or frame.index.isna().all()
        else frame.index.min().strftime("%Y-%m-%d")
    )
    frame.attrs["bfinance_shareholding_last_date"] = (
        None if frame.empty or frame.index.isna().all()
        else frame.index.max().strftime("%Y-%m-%d")
    )
    frame.attrs["bfinance_shareholding_asof_key_is_unique"] = bool(
        not frame.empty
        and not frame.reset_index()[["Symbol", "AsOf"]].duplicated().any()
    )
    frame.attrs["bfinance_shareholding_identity_key"] = ["Symbol", "AsOf", "BroadcastAt"]
    return frame


def shareholding_identity(frame: pd.DataFrame) -> pd.DataFrame:
    """The composite key :func:`parse_shareholding` is safe to join on.

    Returns columns ``Symbol``, ``AsOf``, ``BroadcastAt``, ``SubmissionDate`` — the three values
    that together are provably unique, plus the submission date for context. ``BroadcastAt`` is
    what earns its place: in the captured RELIANCE series the ``30-SEP-2021`` quarter-end and the
    off-cycle ``08-DEC-2021`` filing were **both broadcast on 07-JAN-2022** (02:55:01 and
    03:25:01), so any key built from the broadcast *day* collapses them and only the timestamp
    separates them.

    ``(Symbol, AsOf)`` alone happened to be unique in that capture and is asserted unique in the
    tests, but it is a two-part key over a feed that files off-cycle, and this function exists so
    that the assumption is not load-bearing.
    """
    columns = ["Symbol", "AsOf", "BroadcastAt", "SubmissionDate"]
    if frame is None or frame.empty:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
    return frame.reset_index()[["Symbol", "AsOf", "BroadcastAt", "SubmissionDate"]].copy()


# ======================================================================== group 2: deals


class DealsSnapshotMismatch(ValueError):
    """The payload's ``as_on_date`` is not the date the caller asked to verify.

    Raised by :func:`parse_deals` when ``expected_date`` is supplied and disagrees with the
    payload. The route takes no date argument and a sibling route was measured to ignore one and
    answer 200 with the current day, so an unverifiable request is turned into a checkable
    assertion rather than a silent wrong-day answer.
    """


#: The three kinds, in the order the payload lists them.
DEAL_KINDS: Tuple[str, ...] = ("bulk", "short", "block")

#: ``kind`` -> the array of rows for that kind.
DEAL_ARRAY_KEYS: Dict[str, str] = {
    "bulk": "BULK_DEALS_DATA",
    "short": "SHORT_DEALS_DATA",
    "block": "BLOCK_DEALS_DATA",
}

#: ``kind`` -> its row-count counter. The value is a **string** on all three kinds.
DEAL_COUNTER_KEYS: Dict[str, str] = {
    "bulk": "BULK_DEALS",
    "short": "SHORT_DEALS",
    "block": "BLOCK_DEALS",
}

#: Output columns of :func:`parse_deals`. The per-row ``date`` is the index — **not**
#: ``as_on_date`` — because ``as_on_date`` does not cover the short deals.
DEAL_COLUMNS: Tuple[str, ...] = (
    "Kind", "Symbol", "Name", "Side", "ClientName", "Qty", "WATP", "NotionalCr", "Remarks",
)

#: A kind counts as "unpopulated" at or above this null share. 0.25 is chosen *between* the
#: measured 0.000 / 0.125 / 0.500 triple rather than fitted to it: bulk is fully populated,
#: short is exactly half null, block is an eighth (one always-null ``remarks`` column). The
#: threshold is a module constant so a caller who disagrees can pass their own opinion to
# :func:`is_populated` rather than re-deriving it.
DEAL_NULL_DENSITY_UNPOPULATED = 0.25

#: The payload's own snapshot stamp. Snapshot-only: no history parameter and no ``date`` argument.
DEALS_AS_ON_DATE_KEY = "as_on_date"

#: The output columns **upstream** leaves nullable, i.e. the only ones a payload-level null
#: density can be about. ``Qty`` is not among them: every row in the capture carries a quantity,
#: on every kind. ``NotionalCr`` is excluded because it is derived and inherits ``WATP``'s nulls,
#: so counting it would double-count a price that was never sent.
DEAL_NULLABLE_SOURCE_COLUMNS: Tuple[str, ...] = ("Side", "ClientName", "WATP", "Remarks")


def _frame_null_density(frame: pd.DataFrame, kinds: Sequence[str]) -> Dict[str, float]:
    """Null share of :data:`DEAL_NULLABLE_SOURCE_COLUMNS` per kind, on the **parsed frame**.

    Reported beside the payload-level figure because the two are different numbers whenever a
    sentinel normalised: bulk ``remarks`` is the string ``"-"`` on 237/237 rows upstream, so the
    frame shows 237 more nulls than the payload does. A consumer who recomputes from the frame
    and compares it with ``attrs["bfinance_deals_null_density"]`` should find both numbers and
    their scope recorded, not one number and a puzzle.

    The column subset is taken **before** counting, not after: counting over the whole frame and
    dividing by four columns silently adds ``NotionalCr``'s nulls — 75 more on the short deals,
    which is a 1.25 "share" of nulls, a number that cannot mean anything and is exactly the sort
    of thing a division-by-the-wrong-denominator produces.
    """
    out: Dict[str, float] = {}
    columns = list(DEAL_NULLABLE_SOURCE_COLUMNS)
    for kind in kinds:
        if frame.empty:
            out[kind] = 0.0
            continue
        subset = frame.loc[frame["Kind"] == kind, columns]
        cells = len(subset) * len(columns)
        nulls = int(subset.isna().sum().sum())
        out[kind] = round(nulls / cells, 6) if cells else 0.0
    return out


def deals_url() -> str:
    """The deals URL. Pure and offline. There are no parameters, by measurement, not by omission."""
    return f"{BASE_URL}{DEALS_API_PATH}"


def parse_deals(
    payload: Any,
    *,
    url: str = "",
    kinds: Optional[Sequence[str]] = None,
    expected_date: Optional[Union[str, pd.Timestamp]] = None,
) -> pd.DataFrame:
    """All three deal kinds out of **one** request. Pure — no network, no clock.

    The frame is long: one row per deal, with a ``Kind`` column, indexed by the **per-row**
    ``date``. That index choice is the module's first measured trap, so it is worth the sentence:
    ``as_on_date`` is ``25-Sep-2026`` while every one of the 75 short-deal rows is
    ``24-Sep-2026``, so filtering on ``as_on_date`` drops 100% of the short deals silently.
    :func:`deals_for_date` is the supported filter; the count of off-``as_on_date`` rows is in
    ``attrs["bfinance_deals_off_as_on_date_rows"]``.

    The three counters (``BULK_DEALS``/``SHORT_DEALS``/``BLOCK_DEALS``) are **strings**
    (``"237"``, not ``237``). :func:`deal_counts` parses them to ``int`` and keeps the verbatim
    strings next to the parsed ones, and the declared count is compared against the observed row
    count per kind — measured equal on all three, which is what makes them a completeness check
    rather than a decoration.

    Args:
        payload: the raw JSON object.
        url: the URL this came from, recorded in ``attrs``.
        kinds: subset of :data:`DEAL_KINDS` to keep. ``None`` keeps all three.
        expected_date: **not a request parameter** — the route has none. This is an assertion:
            the payload's own ``as_on_date`` must match it, or
            :class:`DealsSnapshotMismatch` is raised. It is the only honest way to accept a date
            on a snapshot-only route.

    Raises:
        DealsSnapshotMismatch: ``expected_date`` disagrees with the payload's ``as_on_date``.
        ParsingError: the payload is not an object carrying the documented keys.
    """
    if not isinstance(payload, dict):
        raise ParsingError(
            f"deals payload for {url} is a {type(payload).__name__}, expected a JSON object with "
            f"{DEALS_AS_ON_DATE_KEY!r} and the three *_DATA arrays. Upstream schema changed."
        )

    wanted = DEAL_KINDS if kinds is None else tuple(kinds)
    unknown = [kind for kind in wanted if kind not in DEAL_KINDS]
    if unknown:
        raise ValueError(f"kinds={unknown} not in {DEAL_KINDS}")

    as_on_raw = payload.get(DEALS_AS_ON_DATE_KEY)
    as_on = normalise_upstream_date(as_on_raw)
    if expected_date is not None:
        wanted_day = _parse_request_date(expected_date)
        if as_on is None or wanted_day is None or as_on != wanted_day:
            raise DealsSnapshotMismatch(
                f"deals snapshot for {url} is as_on {as_on_raw!r}, not the expected "
                f"{expected_date!r}. This route is snapshot-only: it has no date parameter and "
                f"no history, so a request for another day cannot be honoured and must not be "
                f"silently answered with today's data."
            )

    records: List[Dict[str, Any]] = []
    observed_counts: Dict[str, int] = {}
    declared_raw: Dict[str, Optional[str]] = {}
    declared: Dict[str, Optional[int]] = {}
    unparsed_dates: List[str] = []
    null_cells: Dict[str, int] = {}
    total_cells: Dict[str, int] = {}
    all_null_columns: Dict[str, List[str]] = {}

    for kind in wanted:
        array_key = DEAL_ARRAY_KEYS[kind]
        counter_key = DEAL_COUNTER_KEYS[kind]
        raw_counter = payload.get(counter_key)
        declared_raw[kind] = None if raw_counter is None else str(raw_counter)
        declared[kind] = _to_int(raw_counter)
        raw_rows = payload.get(array_key)
        if raw_rows is None:
            raw_rows = []
        if not isinstance(raw_rows, list):
            raise ParsingError(
                f"deals payload for {url} has {array_key} of type {type(raw_rows).__name__}, "
                f"expected a list. Upstream schema changed."
            )
        observed_counts[kind] = len(raw_rows)

        kind_nulls: Dict[str, int] = {}
        kind_total = 0
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if value is None:
                    kind_nulls[key] = kind_nulls.get(key, 0) + 1
            kind_total += len(row)

            row_date = normalise_upstream_date(row.get("date"))
            if row_date is None:
                unparsed_dates.append(f"{kind}.date={row.get('date')!r}")
            qty = _to_int(row.get("qty"))
            watp = _to_float(row.get("watp"))
            notional = (
                round(qty * watp / 1e7, 6) if (qty is not None and watp is not None) else None
            )
            records.append(
                {
                    "Date": row_date,
                    "Kind": kind,
                    "Symbol": (str(row.get("symbol")) or "").strip().upper() or None,
                    "Name": _clean_text(row.get("name")),
                    "Side": _clean_text(row.get("buySell")),
                    # The differentiator. See the module docstring: all 318 rows are distinct
                    # with it and 317 without it, and no other free source this package has
                    # names the counterparty on a market-wide deal.
                    "ClientName": _clean_text(row.get("clientName")),
                    "Qty": qty,
                    "WATP": watp,
                    "NotionalCr": notional,
                    "Remarks": _clean_text(row.get("remarks")),
                }
            )

        null_cells[kind] = sum(kind_nulls.values())
        total_cells[kind] = kind_total
        all_null_columns[kind] = sorted(
            key for key, count in kind_nulls.items() if raw_rows and count == len(raw_rows)
        )

    if records:
        frame = pd.DataFrame.from_records(records)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("Date"), errors="coerce"))
        frame.index.name = "Date"
        order = pd.Categorical(frame["Kind"], categories=list(DEAL_KINDS), ordered=True)
        frame = (
            frame.assign(_order=order)
            .sort_values(["_order", "Symbol", "ClientName"], kind="stable", na_position="last")
            .drop(columns="_order")
        )
        # list(), not the tuple: frame[tuple] is a single key lookup and raises KeyError.
        frame = frame[list(DEAL_COLUMNS)]
    else:
        frame = pd.DataFrame(
            {c: pd.Series(dtype=object) for c in DEAL_COLUMNS},
            index=pd.DatetimeIndex([], name="Date"),
        )
        for column in ("Qty", "WATP", "NotionalCr"):
            frame[column] = frame[column].astype("float64")

    density = {
        kind: (null_cells[kind] / total_cells[kind] if total_cells[kind] else 0.0)
        for kind in wanted
    }
    unpopulated = tuple(
        kind for kind in wanted
        if density.get(kind, 0.0) >= DEAL_NULL_DENSITY_UNPOPULATED
    )
    if unpopulated:
        logger.warning(
            "NSE deals snapshot %s carries UNPOPULATED kinds %s (null share %s vs threshold %.2f). "
            "Rows exist, but they are not usable data: an empty set and an unpopulated set are "
            "different facts and only attrs can tell them apart.",
            url or "(no url)", list(unpopulated),
            {k: round(density[k], 3) for k in unpopulated},
            DEAL_NULL_DENSITY_UNPOPULATED,
        )

    off_as_on = 0
    dates_seen: List[str] = []
    if not frame.empty:
        per_row_dates = frame.index.dropna()
        if not per_row_dates.empty:
            dates_seen = sorted({stamp.strftime("%Y-%m-%d") for stamp in per_row_dates})
            if as_on is not None:
                off_as_on = int((per_row_dates != as_on).sum())

    frame.attrs["bfinance_provenance"] = PROVENANCE_DEALS
    frame.attrs["bfinance_capability"] = "bulk_short_block_deals"
    frame.attrs["bfinance_source_url"] = url or None
    frame.attrs["bfinance_columns"] = DEAL_COLUMNS
    frame.attrs["bfinance_units"] = UNITS["deals"]
    frame.attrs["bfinance_deals_snapshot_only"] = True
    frame.attrs["bfinance_deals_accepts_date_parameter"] = False
    frame.attrs["bfinance_deals_as_on_date"] = (
        None if as_on is None else as_on.strftime("%Y-%m-%d")
    )
    frame.attrs["bfinance_deals_as_on_date_raw"] = as_on_raw
    frame.attrs["bfinance_deals_row_dates"] = dates_seen
    frame.attrs["bfinance_deals_mixed_dates"] = len(dates_seen) > 1
    # Trap 1, made countable: rows an as_on_date filter would silently discard.
    frame.attrs["bfinance_deals_off_as_on_date_rows"] = off_as_on
    frame.attrs["bfinance_deals_counts_declared"] = dict(declared)
    frame.attrs["bfinance_deals_counts_declared_raw"] = dict(declared_raw)
    frame.attrs["bfinance_deals_counts_observed"] = dict(observed_counts)
    frame.attrs["bfinance_deals_counter_matches_rows"] = {
        kind: (declared[kind] == observed_counts[kind]) for kind in wanted
    }
    # Trap 2, made countable. Counted on the **raw payload**, before sentinel normalisation --
    # so a cell holding "-" is a populated cell here, which is what "NSE sent nothing" means.
    # The frame-level count is reported separately below, because the two differ and a consumer
    # recomputing from the frame must not be surprised by it.
    frame.attrs["bfinance_deals_null_density"] = {k: round(v, 6) for k, v in density.items()}
    frame.attrs["bfinance_deals_null_density_scope"] = "raw_payload"
    frame.attrs["bfinance_deals_null_cells"] = dict(null_cells)
    frame.attrs["bfinance_deals_total_cells"] = dict(total_cells)
    frame.attrs["bfinance_deals_all_null_columns"] = dict(all_null_columns)
    # Frame-level null share per kind, over the four output columns upstream leaves nullable
    # (Side, ClientName, WATP, Remarks). Differs from the payload count wherever a sentinel
    # normalised: bulk ``remarks`` is the string "-" on 237/237 rows, so the frame shows 237 more
    # nulls than the payload does. Both numbers are on the record rather than one of them.
    frame.attrs["bfinance_deals_frame_null_density"] = _frame_null_density(frame, wanted)
    frame.attrs["bfinance_deals_unpopulated_kinds"] = list(unpopulated)
    frame.attrs["bfinance_deals_null_density_threshold"] = DEAL_NULL_DENSITY_UNPOPULATED
    frame.attrs["bfinance_deals_notional_basis"] = (
        "derived: Qty * WATP / 1e7, not published by NSE"
    )
    frame.attrs["bfinance_deals_unparsed_dates"] = unparsed_dates
    frame.attrs["bfinance_deals_kinds"] = list(wanted)
    return frame


def deal_counts(frame: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """Declared vs observed row count per kind, and whether they agree.

    ``{"bulk": {"declared": 237, "declared_raw": "237", "observed": 237, "agrees": True}, ...}``

    The declared side comes from the payload's string counters, parsed to ``int``; the raw string
    is kept because **the counters arrive as strings** and a caller comparing them to an ``int``
    would find they never compare equal. ``agrees`` is the completeness check: measured ``True``
    on all three kinds, and ``False`` is a real upstream truncation signal rather than a parsing
    bug.
    """
    if frame is None or not frame.attrs:
        return {}
    declared = frame.attrs.get("bfinance_deals_counts_declared", {})
    declared_raw = frame.attrs.get("bfinance_deals_counts_declared_raw", {})
    observed = frame.attrs.get("bfinance_deals_counts_observed", {})
    out: Dict[str, Dict[str, Any]] = {}
    for kind in frame.attrs.get("bfinance_deals_kinds", DEAL_KINDS):
        out[kind] = {
            "declared": declared.get(kind),
            "declared_raw": declared_raw.get(kind),
            "observed": observed.get(kind),
            "agrees": declared.get(kind) == observed.get(kind),
        }
    return out


def is_populated(
    frame: pd.DataFrame,
    kind: str,
    *,
    threshold: float = DEAL_NULL_DENSITY_UNPOPULATED,
) -> bool:
    """Whether a kind carries usable rows, as opposed to existing-but-empty rows.

    ``True`` requires rows **and** a null share below ``threshold``
    (:data:`DEAL_NULL_DENSITY_UNPOPULATED` by default). On the captured snapshot this returns
    ``False`` for ``short`` (75 rows, half of every cell null, no client and no price on any of
    them) and ``True`` for ``bulk`` and ``block``. An empty set and an unpopulated set are
    different failures, and this predicate is how a caller tells them apart before writing
    "NSE reported no short deals".
    """
    if kind not in DEAL_KINDS:
        raise ValueError(f"kind={kind!r} not in {DEAL_KINDS}")
    if frame is None or not frame.attrs:
        return False
    observed = frame.attrs.get("bfinance_deals_counts_observed", {})
    density = frame.attrs.get("bfinance_deals_null_density", {})
    return bool(observed.get(kind, 0)) and density.get(kind, 1.0) < threshold


def deals_for_date(frame: pd.DataFrame, date: Union[str, pd.Timestamp]) -> pd.DataFrame:
    """Filter a deals frame to one trading day, **on the per-row ``date``**.

    This is the supported filter, and using ``as_on_date`` instead is the trap the module exists
    to document: in the captured payload ``as_on_date`` is ``25-Sep-2026`` while every short-deal
    row is ``24-Sep-2026``, so an ``as_on_date`` filter returns **zero short deals** and looks
    like a quiet market rather than a missed filter. Filter on the row, and compare
    ``attrs["bfinance_deals_off_as_on_date_rows"]`` to see whether it mattered.

    The returned frame keeps its ``attrs``, so a filtered slice is still self-describing.
    """
    stamp = _parse_request_date(date)
    if stamp is None:
        raise ValueError(f"date={date!r} is not a parseable deal date")
    return frame[frame.index == stamp]


# ============================================================== group 3a: pledge (a probe)


@dataclass(frozen=True)
class PledgeProbe:
    """What is *known* about ``/api/corporate-pledgedata`` — and nothing more.

    This is a probe result, not a pledge record, and the difference is the whole point. The route
    answers **HTTP 200** with ``{"comNameList": [], "data": []}`` — 28 bytes, re-measured
    byte-identically on 2026-09-28 — for a single symbol over 2018-2026 **and** market-wide. Zero
    rows have ever been observed, so **row-level field names are UNVERIFIED** and this module
    names none.

    Attributes:
        reachable: the route answered. ``True`` here — a 200 is not a 404.
        row_count: rows in the ``data`` array. ``0`` is the measured value.
        observed_keys: the top-level keys actually present. These **were** observed, so naming
            them is not invention; a pledge *row* would be a different matter.
        row_schema_verified: always ``False`` while ``row_count == 0``. There is nothing to
            verify against.
        http_status: the status the client saw, or ``None`` when the payload was handed in.
        url: the request the probe describes.
        provenance: :data:`PROVENANCE_PLEDGE`, so all three capability groups in this module
            identify themselves the same way and a result can be traced back to a feed without
            knowing which function produced it.
        cache_status: ``"hit"``/``"miss"``/``"expired"``/``"disabled"``, so "no request was made"
            is visible on the result itself.
        verdict: one line saying route-reachable / payload-empty / schema-unverified.
    """

    reachable: bool
    row_count: int
    observed_keys: Tuple[str, ...]
    row_schema_verified: bool
    http_status: Optional[int] = None
    url: Optional[str] = None
    cache_status: Optional[str] = None
    provenance: str = PROVENANCE_PLEDGE
    verdict: str = ""

    def __post_init__(self) -> None:
        if not self.verdict:
            object.__setattr__(
                self,
                "verdict",
                f"route {'reachable' if self.reachable else 'NOT reachable'}; "
                f"payload {'empty' if self.row_count == 0 else f'{self.row_count} rows'}; "
                f"row schema {'verified' if self.row_schema_verified else 'UNVERIFIED'}",
            )


def pledge_url(
    symbol: Optional[str] = None,
    from_date: Optional[Union[str, pd.Timestamp]] = None,
    to_date: Optional[Union[str, pd.Timestamp]] = None,
    index: str = "equities",
) -> str:
    """Build the pledge probe URL. Pure and offline.

    Dates are sent in the ``DD-MM-YYYY`` query spelling this route takes (measured: the
    ``01-01-2018``-to-``27-09-2026`` request is the one that returned the empty 200). The
    parameters are accepted so the probe can state *what was asked for*; they do not make the
    route produce rows, and the module does not pretend otherwise.
    """
    parts = [f"index={validate_index(index)}"]
    if symbol is not None:
        parts.append(f"symbol={validate_nse_symbol(symbol)}")
    for name, value in (("from_date", from_date), ("to_date", to_date)):
        if value is None:
            continue
        # Deliberately not `normalise(...) or pd.to_datetime(...)`: `bool(pd.Timestamp)` is
        # ambiguous, so that `or` raises on the success path.
        stamp = _parse_request_date(value)
        if stamp is None:
            raise ValueError(f"{name}={value!r} is not parseable")
        parts.append(f"{name}={pd.Timestamp(stamp).strftime('%d-%m-%Y')}")
    return f"{BASE_URL}{PLEDGE_API_PATH}?" + "&".join(parts)


def probe_pledge_payload(
    payload: Any,
    *,
    url: str = "",
    http_status: Optional[int] = None,
    cache_status: Optional[str] = None,
) -> PledgeProbe:
    """Describe a pledge payload's reachability and emptiness. **No row parsing.**

    Suffixed ``_payload`` because :func:`probe_pledge` is the client-backed entry point and the
    two are different things: this one is pure and takes a payload the caller already has.

    Returns a :class:`PledgeProbe`. It never returns a ``DataFrame`` and it never names a pledge
    field, because the only pledge payload ever observed is 28 bytes of empty arrays — a parser
    written against it would be a parser for a payload that has never existed, and it would fail
    on the first real row while looking like it had been tested.

    ``reachable`` here means "the payload has the documented envelope", not "a request was
    made": a 28-byte empty body is a reachable route. ``http_status`` is filled in by the client
    from the response it actually got, and is ``None`` when the payload was supplied directly.
    """
    if isinstance(payload, dict):
        data = payload.get("data")
        rows = data if isinstance(data, list) else []
        return PledgeProbe(
            reachable=True,
            row_count=len(rows),
            observed_keys=tuple(sorted(payload)),
            row_schema_verified=len(rows) > 0,
            http_status=http_status,
            url=url or None,
            cache_status=cache_status,
        )
    return PledgeProbe(
        reachable=False,
        row_count=0,
        observed_keys=(),
        row_schema_verified=False,
        http_status=http_status,
        url=url or None,
        cache_status=cache_status,
    )


# ================================================================ group 3b: FPI (blocked)


class FpiOwnershipUnavailableError(UpstreamServiceError):
    """FPI / foreign-ownership limit and utilisation cannot be retrieved from this host.

    The data lives in ``/api/quote-equity``, which returns **403 Access Denied (Akamai
    edgesuite)**, primed or not — the known blocked route already recorded in
    ``nse/marketlens.py``. This module never issues that request: a parser for a payload nobody
    has seen is fiction, and the honest answer is a typed refusal naming the route, the status
    and the edge.
    """


@dataclass(frozen=True)
class FpiOwnershipStatus:
    """A zero-I/O description of why FPI ownership is unavailable.

    Every field is a fact about an observation made elsewhere (a 403 from ``/api/quote-equity``),
    not about anything this module did. ``parser_implemented`` is ``False`` on purpose and is
    asserted in the tests: the capability is absent by decision, not by an oversight waiting to be
    filled in.

    The block is **selective per route** — ``/api/quote-equity`` 403s while three sibling
    ``/api/*`` routes returned 200 from the same session — so this status says nothing about any
    other route.
    """

    available: bool = False
    capability: str = "fpi_ownership_limit_and_utilisation"
    route: str = FPI_API_PATH
    http_status_observed: int = 403
    edge: str = "Akamai edgesuite (Access Denied)"
    primed: bool = True
    parser_implemented: bool = False
    reason: str = (
        "/api/quote-equity returns 403 Access Denied from this host, primed or not. The payload "
        "has never been seen, so no parser exists and none is invented."
    )


#: The single source of truth for the FPI refusal, so a caller and a test read the same object.
FPI_BLOCKED_STATUS = FpiOwnershipStatus()


def fpi_ownership_status() -> FpiOwnershipStatus:
    """Report FPI ownership as unavailable-blocked. **No request, ever.**

    Safe to call in a hot path: it builds nothing and opens nothing, so it cannot cost a request
    or trip a rate limit. The result is a frozen :class:`FpiOwnershipStatus`.
    """
    return FPI_BLOCKED_STATUS


def fpi_ownership(symbol: Optional[str] = None) -> NoReturn:
    """Always raise :class:`FpiOwnershipUnavailableError`. **This function never requests.**

    There is no code path from here to a socket, and that is the point: a caller who asks for
    foreign-ownership limits gets a typed, explanatory failure at the call site instead of a
    silent empty frame, and no amount of retrying turns a 403 into data.

    Args:
        symbol: accepted and ignored, so the error names what was being asked for.
    """
    raise FpiOwnershipUnavailableError(
        f"FPI / foreign-ownership limit and utilisation for {symbol or 'any symbol'} is "
        f"unavailable: {FPI_BLOCKED_STATUS.route} returns HTTP "
        f"{FPI_BLOCKED_STATUS.http_status_observed} {FPI_BLOCKED_STATUS.edge} from this host. "
        f"Use bfinance.ownership.fpi_ownership_status() for the typed reason. No parser exists "
        f"because no payload has ever been observed."
    )


# --------------------------------------------------------------------------- async bridge


def _run_async(coro):
    """Bridge to sync, matching ``nse.corporate_actions._run_async``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class _DefaultCache:
    """Sentinel for the ``cache`` argument, matching ``screener.client`` in meaning.

    An omitted ``cache`` and an explicit ``None`` must not collapse into one behaviour: ``None``
    is how a caller says "do not cache", and a ``cache or ...`` default would silently build the
    home-directory cache for them.
    """

    def __repr__(self) -> str:
        return "<default cache>"


USE_DEFAULT_CACHE = _DefaultCache()


# --------------------------------------------------------------------------- the client


class OwnershipClient:
    """Client for NSE's ownership feeds: shareholding patterns and the deals snapshot.

    Stateless per call and safe to reuse. Cookie priming is a **single** retry on 403/503, the
    same bounded pattern as ``nse.corporate_actions.CorporateActionsClient``; measured 2026-09-28,
    all three routes here returned **200 with no priming at all**, so priming is a fallback and not
    a prerequisite. The block on ``/api/quote-equity`` is per route, not per client — one 200
    here says nothing about the next route.
    """

    BASE_URL = BASE_URL
    LANDING_URL = LANDING_URL

    #: TTL, in hours, per cache category — the whole vocabulary in one place, so no cached
    #: payload's age is decided by a call site. The rule is the one ``screener.client`` uses: a
    #: TTL may be no longer than the fastest-moving thing in the payload.
    #:
    #: * ``shareholding`` 24.0h — a quarterly filing. The only thing that moves faster is a
    #:   revision, which is *modelled upstream and unobserved* (see the module docstring); 24h is
    #:   the filing cadence with a day's slack, not a claim that revisions happen daily.
    #: * ``deals`` 1.0h — a single-day snapshot NSE revises in place: the same ``as_on_date`` was
    #:   served with a different digest on consecutive days at the identical byte length. Price,
    #:   side and client can all change within a session, so this sits at an hour, not a day.
    #: * ``pledge`` 24.0h — a 28-byte empty probe. Re-probing hourly would spend requests to
    #:   learn the same nothing; :func:`probe_pledge` is the thing to call when a fresh answer
    #:   matters, and it takes ``force_refresh``.
    CATEGORY_TTL_HOURS: Dict[str, float] = {
        "shareholding": 24.0,
        "deals": 1.0,
        "pledge": 24.0,
    }

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 2500.0,
        prime_cookie_on_block: bool = True,
        cache_ttl_hours: float = 24.0,
        cache: Union[SQLiteCache, None, _DefaultCache] = USE_DEFAULT_CACHE,
    ) -> None:
        """
        Args:
            timeout: per-request HTTP timeout, seconds.
            max_retries: attempts before a 5xx/429 backoff gives up. The error text on exhaustion
                names this number.
            proxy: optional proxy URL; falls back to ``HTTPS_PROXY``/``HTTP_PROXY``.
            min_request_interval_ms: floor on the gap between two requests from any client in the
                process. The default is 2.5 s, not the 250-300 ms the read-only feeds use: these
                are the routes other agents poll at the same time, and an IP block on this edge is
                unrecoverable.
            prime_cookie_on_block: allow the single landing-page prime on a 403/503.
            cache_ttl_hours: the default for a category that does not name one. Every category
                this client caches *does* name one, so this applies only to a caller-supplied
                cache object; a value of ``0`` or less skips the cache entirely.
            cache: omit for the home-directory :class:`~bfinance.cache.sqlite_cache.SQLiteCache`;
                ``None`` for no caching; or pass your own.
        """
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self.prime_cookie_on_block = prime_cookie_on_block
        self._last_request_time = 0.0
        self._cookie_primed = False
        #: True when a blocked response forced a landing-page prime, so a caller can see the
        #: retry path ran rather than assuming it was needed.
        self.primed_cookie = False
        if isinstance(cache, _DefaultCache):
            self.cache = (
                SQLiteCache(default_ttl_hours=cache_ttl_hours) if cache_ttl_hours > 0 else None
            )
        else:
            self.cache = cache

    def ttl_hours(self, category: str) -> float:
        """The explicit TTL for a cache category, in hours. An unknown key raises."""
        return self.CATEGORY_TTL_HOURS[category]

    def _headers(self, ajax: bool = True) -> Dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*" if ajax else "text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": LANDING_URL,
        }
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    async def _throttle_pacing(self) -> None:
        """Pace requests against a process-global clock."""
        global _GLOBAL_LAST_REQUEST_TIME
        with _PACING_LOCK:
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
        with _PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(
                _GLOBAL_LAST_REQUEST_TIME, self._last_request_time
            )

    async def _prime_cookie(self, client: httpx.AsyncClient) -> None:
        """Fetch the landing page once so a retry carries a session cookie.

        Measured as unnecessary for these routes (3/3 HTTP 200 unprimed on 2026-09-28). It exists
        because ``/api/quote-equity`` on the same host 403s even primed, so which routes are
        blocked is a property of the edge, not of the client.
        """
        if not self.prime_cookie_on_block or self._cookie_primed:
            return
        try:
            await client.get(LANDING_URL, headers=self._headers(ajax=False))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.debug("ownership: cookie priming failed (%s); continuing.", exc)
        self._cookie_primed = True
        self.primed_cookie = True

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        """GET with retry. Raises typed errors; never returns ``None`` for a failure.

        A 403/503 is treated as "possibly needs a session" rather than as a hard failure: the
        cookie is primed **once** and the request retried. Bounded — a second block raises.
        """
        last_error: Optional[Exception] = None
        blocked_retry_used = False

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            try:
                response = await client.get(url, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code in (403, 503) and not blocked_retry_used:
                blocked_retry_used = True
                await self._prime_cookie(client)
                logger.debug(
                    "ownership: HTTP %d for %s; primed a session cookie and retrying once.",
                    response.status_code, url,
                )
                continue

            if response.status_code == 429:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RateLimitExceededError(
                    f"NSE ownership feed rate limited (HTTP 429) for {url}"
                )

            if response.status_code >= 500:
                last_error = UpstreamServiceError(
                    f"NSE ownership feed returned HTTP {response.status_code} for {url}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code != 200:
                raise UpstreamServiceError(
                    f"NSE ownership feed returned unexpected HTTP {response.status_code} for "
                    f"{url}. A 404 here means the route is gone, not that the client is blocked: "
                    f"{DEALS_API_PATH} already carries BLOCK_DEALS_DATA, and the sibling "
                    f"/api/snapshot-capital-market-blockdeal does not exist."
                )

            try:
                return response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ParsingError(
                    f"NSE ownership feed returned a non-JSON body for {url} "
                    f"(first 160 chars: {response.text[:160]!r}). An HTML page served with a 200 "
                    f"is the usual cause."
                ) from exc

        raise UpstreamServiceError(
            f"NSE ownership feed failed after {self.max_retries} attempts: {url}"
        ) from last_error

    def _cache_read(self, key: str) -> Tuple[Optional[Any], str]:
        """Read a cache entry and report *which* kind of miss it was.

        Returns ``(value, status)``, where ``status`` is the
        :class:`~bfinance.cache.sqlite_cache.CacheLookup` name. The status travels into the
        result's ``attrs``, so "no request was made" is observable from the output rather than
        only from a log line.
        """
        if not self.cache:
            return None, "disabled"
        lookup = self.cache.get_result(key)
        return lookup.value, lookup.status

    def _cache_write(self, key: str, category: str, payload: Any) -> None:
        if not self.cache:
            return
        self.cache.set(key, payload, category=category, ttl_hours=self.ttl_hours(category))

    # ------------------------------------------------------------------ public: group 1

    async def shareholding_patterns_async(
        self,
        symbol: Optional[str] = None,
        index: str = "equities",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Quarterly promoter/public shareholding pattern for one symbol (or unfiltered).

        Args:
            symbol: NSE symbol; a ``.NS`` suffix is stripped. ``None`` omits the parameter —
                **unmeasured**, see :func:`shareholding_url`.
            index: segment key, lowercased. Only ``equities`` has been measured.
            force_refresh: skip the cache read and re-request.

        Returns:
            Columns :data:`SHAREHOLDING_COLUMNS` on a naive ``DatetimeIndex`` named ``AsOf``,
            with ``attrs['bfinance_provenance'] = 'nse_ownership_shareholding'``,
            ``attrs['bfinance_source_url']`` and ``attrs['bfinance_cache_status']``.

        Raises:
            NotAnNseSymbolError: ``symbol`` is BSE-shaped, an index, or a scrip code.
            RateLimitExceededError: HTTP 429 after retries.
            UpstreamServiceError: transport failure, 5xx, or an unexpected status.
            ParsingError: a non-JSON body, or a payload that is not the measured bare array.
        """
        url = shareholding_url(symbol, index)
        key = f"ownership_shareholding_{validate_index(index)}_{(symbol or 'ALL').upper()}"
        cached, status = self._cache_read(key)
        if not force_refresh and cached is not None:
            frame = parse_shareholding(cached, symbol=symbol, url=url)
            frame.attrs["bfinance_cache_status"] = status
            return frame

        async with httpx.AsyncClient(
            timeout=self.timeout, proxy=self.proxy, follow_redirects=True
        ) as client:
            payload = await self._get_json(client, url)
        self._cache_write(key, "shareholding", payload)
        frame = parse_shareholding(payload, symbol=symbol, url=url)
        frame.attrs["bfinance_cache_status"] = "miss"
        return frame

    def shareholding_patterns(
        self,
        symbol: Optional[str] = None,
        index: str = "equities",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`shareholding_patterns_async`."""
        return _run_async(self.shareholding_patterns_async(symbol, index, force_refresh))

    # ------------------------------------------------------------------ public: group 2

    async def deals_async(
        self,
        kinds: Optional[Sequence[str]] = None,
        expected_date: Optional[Union[str, pd.Timestamp]] = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Bulk, short and block deals for the whole market, from **one** request.

        Args:
            kinds: subset of :data:`DEAL_KINDS`. ``None`` keeps all three. The request is the same
                either way — the route returns all three and this filters locally.
            expected_date: an **assertion**, not a request parameter. The route has none; if the
                payload's ``as_on_date`` does not match, :class:`DealsSnapshotMismatch` is raised
                rather than the wrong day being returned quietly.
            force_refresh: skip the cache read. Worth passing here: the snapshot is revised in
                place and the default TTL is an hour.

        Returns:
            Columns :data:`DEAL_COLUMNS` on a naive ``DatetimeIndex`` named ``Date`` built from
            the **per-row** date, with the declared/observed counts, the null density per kind and
            the off-``as_on_date`` row count in ``attrs``.

        Filter with :func:`deals_for_date`, not on ``as_on_date``.
        """
        url = deals_url()
        key = "ownership_deals_snapshot"
        cached, status = self._cache_read(key)
        if not force_refresh and cached is not None:
            frame = parse_deals(cached, url=url, kinds=kinds, expected_date=expected_date)
            frame.attrs["bfinance_cache_status"] = status
            return frame

        async with httpx.AsyncClient(
            timeout=self.timeout, proxy=self.proxy, follow_redirects=True
        ) as client:
            payload = await self._get_json(client, url)
        self._cache_write(key, "deals", payload)
        frame = parse_deals(payload, url=url, kinds=kinds, expected_date=expected_date)
        frame.attrs["bfinance_cache_status"] = "miss"
        return frame

    def deals(
        self,
        kinds: Optional[Sequence[str]] = None,
        expected_date: Optional[Union[str, pd.Timestamp]] = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`deals_async`."""
        return _run_async(self.deals_async(kinds, expected_date, force_refresh))

    # ------------------------------------------------------------------ public: group 3a

    async def probe_pledge_async(
        self,
        symbol: Optional[str] = None,
        from_date: Optional[Union[str, pd.Timestamp]] = None,
        to_date: Optional[Union[str, pd.Timestamp]] = None,
        index: str = "equities",
        force_refresh: bool = False,
    ) -> PledgeProbe:
        """Ask the pledge route whether it is reachable, and whether it has rows. It does not.

        Returns a :class:`PledgeProbe` — never a frame, never a row. On the measured route the
        answer is always "reachable, 0 rows, schema unverified", and passing that through
        unchanged is the useful behaviour: a caller can log it, assert on it, or diff it next
        week, without this module pretending to a payload shape it has not seen.
        """
        url = pledge_url(symbol, from_date, to_date, index)
        key = (
            f"ownership_pledge_{validate_index(index)}_"
            f"{(symbol or 'ALL').upper()}_{from_date or ''}_{to_date or ''}"
        )
        cached, status = self._cache_read(key)
        if not force_refresh and cached is not None:
            # The route answers 200 to get this payload, and a cached read cannot prove the 200
            # was fresh, so the status is recorded on the probe rather than implied by silence.
            return probe_pledge_payload(cached, url=url, http_status=200, cache_status=status)
        async with httpx.AsyncClient(
            timeout=self.timeout, proxy=self.proxy, follow_redirects=True
        ) as client:
            payload = await self._get_json(client, url)
        self._cache_write(key, "pledge", payload)
        return probe_pledge_payload(payload, url=url, http_status=200, cache_status="miss")

    def probe_pledge(
        self,
        symbol: Optional[str] = None,
        from_date: Optional[Union[str, pd.Timestamp]] = None,
        to_date: Optional[Union[str, pd.Timestamp]] = None,
        index: str = "equities",
        force_refresh: bool = False,
    ) -> PledgeProbe:
        """Sync variant of :meth:`probe_pledge_async`."""
        return _run_async(
            self.probe_pledge_async(symbol, from_date, to_date, index, force_refresh)
        )


_DEFAULT_CLIENT: Optional[OwnershipClient] = None


def _default_client() -> OwnershipClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = OwnershipClient()
    return _DEFAULT_CLIENT


# ------------------------------------------------------------------- module shortcuts


def shareholding_patterns(
    symbol: Optional[str] = None,
    index: str = "equities",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`OwnershipClient.shareholding_patterns`.

    >>> from bfinance.ownership import shareholding_patterns          # doctest: +SKIP
    >>> shareholding_patterns("RELIANCE").tail()                      # doctest: +SKIP
    """
    return _default_client().shareholding_patterns(symbol, index, force_refresh)


def shareholding_patterns_async(
    symbol: Optional[str] = None,
    index: str = "equities",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Async variant of :func:`shareholding_patterns`."""
    return _default_client().shareholding_patterns_async(symbol, index, force_refresh)


def deals(
    kinds: Optional[Sequence[str]] = None,
    expected_date: Optional[Union[str, pd.Timestamp]] = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`OwnershipClient.deals`.

    All three kinds in one request. Filter on the per-row date with :func:`deals_for_date`, and
    read the null density in ``attrs`` before treating a kind as data.
    """
    return _default_client().deals(kinds, expected_date, force_refresh)


def deals_async(
    kinds: Optional[Sequence[str]] = None,
    expected_date: Optional[Union[str, pd.Timestamp]] = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Async variant of :func:`deals`."""
    return _default_client().deals_async(kinds, expected_date, force_refresh)


def probe_pledge(
    symbol: Optional[str] = None,
    from_date: Optional[Union[str, pd.Timestamp]] = None,
    to_date: Optional[Union[str, pd.Timestamp]] = None,
    index: str = "equities",
    force_refresh: bool = False,
) -> PledgeProbe:
    """Module-level shortcut for :meth:`OwnershipClient.probe_pledge`.

    A reachability probe, not a pledge parser. See :func:`probe_pledge` for why there is no
    parser.
    """
    return _default_client().probe_pledge(symbol, from_date, to_date, index, force_refresh)


def probe_pledge_async(
    symbol: Optional[str] = None,
    from_date: Optional[Union[str, pd.Timestamp]] = None,
    to_date: Optional[Union[str, pd.Timestamp]] = None,
    index: str = "equities",
    force_refresh: bool = False,
) -> PledgeProbe:
    """Async variant of :func:`probe_pledge`."""
    return _default_client().probe_pledge_async(
        symbol, from_date, to_date, index, force_refresh
    )
