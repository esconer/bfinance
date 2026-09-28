"""
Flow-of-funds: NSE participant positions, and AMFI mutual-fund monthly flows.

Two capability groups, one module
--------------------------------
This module holds two things that share nothing but a subject heading:

1. :func:`participant_flows` / :func:`participant_flows_range` / :class:`ParticipantFlowsClient`
   — **NSE participant flow-of-funds.** Who is long and who is short in equity derivatives,
   split by FII, DII, Client, Pro and the market total, by asset class, and by call/put.
2. :func:`amfi_monthly_index` / :func:`amfi_monthly_report` / :class:`MutualFundClient`
   — **AMFI mutual-fund flows.** Category-level net inflows/outflows and the AUM picture,
   monthly, in INR crore.

A caller who wants FII/DII flows never touches :data:`UNITS_INR_CRORE`; a caller who wants
mutual-fund AUM never touches :data:`UNITS_CONTRACTS`. The two groups share the
:class:`HttpResult` transport seam, the ``attrs`` provenance contract, and nothing else.
Every public name is listed in :data:`__all__`, and each group is internally complete — there
is no "one function of the other group" that a caller must know about.

Provenance is on every result, and the as-of date is *checked*, not restated
-------------------------------------------------------------------------
Every frame carries::

    attrs["bfinance_capability"]  "participant_flows" | "mutual_fund_flows"
    attrs["bfinance_source_url"]  the exact URL the bytes came from
    attrs["bfinance_as_of"]       the date the *payload* claims, ISO-8601
    attrs["bfinance_as_of_verified"]  True — it was compared against the request
    attrs["bfinance_units"]       "contracts" | "inr_crore"
    attrs["bfinance_status"]      "ok" | "unavailable" | "degraded"

**The as-of date is extracted from the payload and compared to the request, and a mismatch
raises :class:`ParticipantAsOfMismatch`.** It is not copied from the URL. This is not
theoretical care: a sibling NSE route, ``snapshot-capital-market-largedeal``, was measured
to silently **ignore** a ``date=`` parameter and return *today's* data with an HTTP 200. A
URL-derived as-of would have recorded the requested date against someone else's numbers, and
a reader would have had no way to detect it. The participant CSV additionally carries its
date in the title row (``... in Equity Derivatives as on Sep 25, 2026``), which is an
independent statement of the period by the publisher — so the check has something real to
check against. :func:`parse_participant_oi` takes ``expected_date`` for exactly this reason.

Participant routes — measured, and the brief was wrong about two of the details
---------------------------------------------------------------------------
Verified live 2026-09-28 against ``nsearchives.nseindia.com`` (no ``robots.txt`` served,
HTTP 404; RFC 9309 §2.3.1.3 therefore leaves it unrestricted)::

    /content/nsccl/fao_participant_oi_{DDMMYYYY}.csv      -> 200, 982 B  (2026-09-25)
    /content/nsccl/fao_participant_oi_{DDMMYYYY}.csv      -> 200, 917 B  (2020-06-15)
    /content/nsccl/fao_participant_oi_27092026.csv         -> 404         (a Sunday)
    /content/nsccl/fao_participant_vol_{DDMMYYYY}.csv     -> 200, 999 B  (2026-09-25)
    /content/nsccl/fao_participant_turnover_{DDMMYYYY}.csv-> 404

Two corrections to the ticket, both settled by measurement:

**1. There is no ``Arb`` row, and there never was in this file.** All three files that
returned 200 carry exactly four participant categories — ``Client``, ``DII``, ``FII``,
``Pro`` — plus a market-wide ``TOTAL`` row. The distinguishing value of this feed is the
*Client* and *Pro* split alongside FII/DII (proprietary vs arbitrageurage-adjacent book),
which is real and is built. But an ``Arb`` row would have to be invented, so it is not
invented. :data:`PARTICIPANT_CATEGORIES` is the measured tuple, and every frame also carries
``attrs["bfinance_participants_present"]``, measured from the rows, so a future NSE file that
*does* add a category is reported rather than silently dropped.

**2. This is not a one-day snapshot, and it does not need local accumulation.**
``/api/fiidiiTradeReact`` is real and is built here too (:func:`parse_participant_snapshot`,
:class:`FlowArchive`) — but it is a *different route*. It returns 217 bytes, one trading day,
two categories (``DII``, ``FII/FPI``), and accepts no date parameters at all. The dated CSV
route above is separate, carries real history back to at least 2020, and is what
:func:`participant_flows` uses. Both readings were partly right because both agents probed
different routes and each generalised from one. The accumulation machinery is therefore
scoped to the snapshot route, where it is genuinely required, and is *not* imposed on the
dated route that does not need it.

The payload, verbatim (2026-09-25, CRLF, 15 fields)::

    ""Participant wise Open Interest (no. of contracts) ... as on Sep 25, 2026"",,,,,,,
    Client Type,Future Index Long,...,Total Long Contracts      ,Total Short Contracts
    Client,310325,65926,...,14412461,10681425
    DII,40198,33627,...,562860,5095162
    FII,46305,358306,...,6487239,5930483
    Pro,91203,30172,...,6070992,5826483
    TOTAL,488031,488031,...,27533553,27533553

Upstream traps recorded here, because each one silently corrupts the data if ignored
----------------------------------------------------------------------------
1. **Header fields carry padding, and the padding changes with the file's vintage.** The
   2026 file pads with **spaces** (``'Future Stock Short       '``,
   ``'Total Long Contracts      '``) while the 2020 file pads the same fields with a **TAB**
   (``'Future Stock Short\\t'``). Every header is therefore ``.strip()``ed before it is looked
   up, and columns are found **by name, never by offset**. A name lookup on the raw header
   would raise on the 2020 file and a positional read would silently mis-assign a column on
   any future re-ordering.
2. **The date format in the title row is not stable.** ``as on Sep 25, 2026`` (space after
   comma) in 2026, ``as on Jun 15,2020`` (**no** space) in 2020. Parsed by regex, tolerant of
   the missing space, and *required* to be present — an absent title date is a
   :class:`~bfinance.utils.exceptions.ParsingError`, not a silent fall back to the URL's date.
3. **The publisher's ``TOTAL`` row is not exactly the sum of its own detail rows.** Measured
   on the 2026-09-25 OI file: reported ``Total Long Contracts`` 27,533,553 against
   27,533,552 summed from the four categories (**residual -1**), and the short side **+1**.
   The 2020-06-15 file and the 2026-09-25 volume file both reconcile to **0**. So a
   "totals must add up" assertion is a test that would fail on real upstream data, and this
   module does not make it. The residual is published in
   ``attrs["bfinance_total_reconciliation_residual"]`` instead of being hidden or used to
   correct a published number.
4. **The ``TOTAL`` row is not a participant.** Left in a tidy frame it double-counts under
   any group-by. It is excluded from the rows and reported separately in ``attrs``.
5. **Units are ``contracts``, and that is measured, not read off the title.** Three
   independent measurements, in :data:`MEASURE_UNITS` and asserted in the tests: NSE
   equity-index future open interest on 2026-09-25 is 488,031 — at the 75-lot Nifty contract
   that is ~36.6 M contracts, ~INR 91,500 crore notional; read as INR crore the same cell
   would be INR 4.88 trillion of open interest in one product, which exceeds a normal day's
   entire NSE derivatives turnover. The file's per-category long and short legs both
   reconcile to the market total, and ``Total Long == Total Short`` exactly, which a
   rupee-denominated file has no reason to do. The sibling ``_vol`` file is the same order
   of magnitude (170,177 vs 488,031), consistent with both being counts.
6. **A 404 is an expected outcome, never an error to be confused with a failure.** A weekend,
   an exchange holiday, or a date before the archive begins all yield 404. Network failure
   and 5xx are real problems and raise. The two are never conflated — see *Missing days*.

Missing days are a typed state, never a zero and never a gap
------------------------------------------------------------
A flow series with a hole is not a flow series with a zero, so this module never invents one.

* **A weekend costs no request at all.** :func:`is_weekend` is pure and local, so a Saturday
  is reported as ``reason="weekend"`` without a 404 ever being spent. Exchange holidays are
  *not* bundled as a calendar — a stale holiday list is worse than an honest 404, the same
  reasoning as :mod:`bfinance.nse.bhavcopy` — so a holiday costs one cheap 404 and is
  reported as ``reason="no_file"``.
* **A single-day read raises :class:`ParticipantFlowsUnavailable`** by default. Under
  ``on_missing="frame"`` it returns an empty frame that is *still explicitly typed*:
  ``bfinance_status="unavailable"`` and ``bfinance_unavailable_reason`` are set, so an empty
  frame can never be confused with a genuine zero-row day. There is no code path in this
  module that returns a zero-filled row for a day NSE did not publish.
* **A range read** lists every unrequested-for date in ``attrs["bfinance_missing_dates"]`` as
  ``{"date": ..., "reason": ...}``, so a caller can see the holes instead of inferring them
  from a short series.

AMFI — monthly, and only ~a third of the months are numeric
--------------------------------------------------------------
The AMFI monthly-report index is a Next.js RSC flight payload under the key ``monthlyData``:
``Year`` -> ``Pages`` -> ``{Title, month, year, pdf_url, excel_url, financial_year}``.
**28 financial-year buckets, 326 months, April 1999 to August 2026 — and only 101 of those
326 months have an ``excel_url``** (the numeric workbooks start April 2018; older months are
PDF/HTML only). So a numeric MF flow series is realistically **2018-onward, not 1999**, and
:func:`amfi_workbook_coverage` reports exactly that split instead of letting a caller assume
a 27-year history exists. The PDFs are not parsed.

The workbook carries genuine net flows in **INR crore**, declared in the verbatim column
header ``'Net Inflow (+ve)/Outflow (-ve) for the month of August 2026 (INR in crore)'``.
Known-value controls, both verified against the published figures: Grand Total
**+41,353.60** INR crore for August 2026 and **+70,813.41** for May 2020.

**Category labels are not stable across months, so only two rows may be joined over time.**
Measured: the same debt category is ``"Ultra Short Term Fund"`` in August 2026 and
``"Ultra Short Duration Fund"`` in May 2020, and ``"Short Term Fund"`` versus
``"Short Duration Fund"``. :data:`AMFI_STABLE_ANCHORS` therefore holds only
``("Grand Total", "Total A-Open ended Schemes")`` — the rows that mean the same thing in
every month. A category-level time series built by label-joining would silently drop the debt
buckets the moment a label was reworded.

The 1.5 MB download is not repeated per call
----------------------------------------------
:class:`MutualFundClient` takes an optional :class:`~bfinance.cache.sqlite_cache.SQLiteCache`
and uses the existing TTL discipline — ``ttl_hours`` is passed **explicitly at every call
site** rather than inherited from the cache's construction default, so no payload's age is an
accident of how the cache was built. Freshness is published, not inferred:
``attrs["bfinance_cache"]`` is ``"hit"`` or ``"miss"``, with ``bfinance_fetched_utc`` and
``bfinance_age_seconds`` alongside it.

Sync only
---------
Every public entry point is synchronous. Unlike :mod:`bfinance.nse.bhavcopy` there is no
``async`` variant, because the transport seam is an injectable callable
(:data:`Transport`) and the two workloads — a few hundred bytes per trading day, and one
monthly file — are both I/O-bound at a rate no async machinery would change. The
accumulation archive is the only stateful piece and it is a synchronous SQLite file.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import httpx
import pandas as pd

from bfinance.utils.exceptions import BFinanceError, ParsingError, UpstreamServiceError

logger = logging.getLogger("bfinance")

__all__ = [
    # ---- capability group 1: NSE participant flow-of-funds ----
    "PARTICIPANT_CATEGORIES",
    "PARTICIPANT_MEASURES",
    "MEASURE_UNITS",
    "UNITS_CONTRACTS",
    "PARTICIPANT_BASE_URL",
    "PARTICIPANT_SNAPSHOT_URL",
    "SNAPSHOT_FIELDS",
    "SOURCE_DATE_FIELDS",
    "PARTICIPANT_TIDY_COLUMNS",
    "TidyRow",
    "HttpResult",
    "Transport",
    "ParticipantFlowsError",
    "ParticipantFlowsUnavailable",
    "ParticipantAsOfMismatch",
    "participant_url",
    "is_weekend",
    "missing_day_reason",
    "parse_participant_oi",
    "parse_participant_snapshot",
    "empty_participant_frame",
    "FlowArchive",
    "ParticipantFlowsClient",
    "participant_flows",
    "participant_flows_range",
    # ---- capability group 2: AMFI mutual-fund flows ----
    "UNITS_INR_CRORE",
    "AMFI_STABLE_ANCHORS",
    "AMFI_BASE_URL",
    "AMFI_REPORT_COLUMNS",
    "MutualFundClient",
    "MutualFundWorkbookUnavailable",
    "parse_amfi_monthly_index",
    "parse_amfi_monthly_report",
    "amfi_workbook_coverage",
    "amfi_monthly_index",
    "amfi_monthly_report",
    # ---- shared ----
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "STATUS_DEGRADED",
    "AS_OF_ATTR",
    "AS_OF_VERIFIED_ATTR",
    "SOURCE_URL_ATTR",
    "CAPABILITY_ATTR",
    "UNITS_ATTR",
    "STATUS_ATTR",
    "MISSING_DATES_ATTR",
    "UNAVAILABLE_REASON_ATTR",
    "to_date",
]

# --------------------------------------------------------------------------- shared contract

#: ``attrs["bfinance_status"]`` — the payload was fetched and parsed.
STATUS_OK = "ok"

#: ``attrs["bfinance_status"]`` — the source has nothing for what was asked. A **state**, not an
#: error swallowed and not an empty frame: it is always paired with
#: :data:`UNAVAILABLE_REASON_ATTR` and a zero-row body.
STATUS_UNAVAILABLE = "unavailable"

#: ``attrs["bfinance_status"]`` — real data was returned but something was incomplete. Used when
#: a transport failure is tolerated, and when the publisher's own totals do not reconcile.
STATUS_DEGRADED = "degraded"

CAPABILITY_ATTR = "bfinance_capability"
SOURCE_URL_ATTR = "bfinance_source_url"
AS_OF_ATTR = "bfinance_as_of"
AS_OF_VERIFIED_ATTR = "bfinance_as_of_verified"
UNITS_ATTR = "bfinance_units"
STATUS_ATTR = "bfinance_status"
MISSING_DATES_ATTR = "bfinance_missing_dates"
UNAVAILABLE_REASON_ATTR = "bfinance_unavailable_reason"

#: The two capability identifiers. A frame says which group produced it, so a caller holding a
#: frame from a shared pipeline never has to guess which set of units to read it with.
CAPABILITY_PARTICIPANT = "participant_flows"
CAPABILITY_MUTUAL_FUND = "mutual_fund_flows"

#: Contract-count unit for the NSE participant files. See the module docstring, trap 5, for the
#: three measurements that establish it.
UNITS_CONTRACTS = "contracts"

#: Rupee-crore unit for the AMFI workbook columns and the NSE FII/DII cash-flow snapshot.
UNITS_INR_CRORE = "inr_crore"

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Lower-cased for matching, because upstream's month spelling is not casing-stable: the title row
#: says "Sep 25, 2026" while the snapshot route's ``date`` field says "25-Sep-2026".
_MONTH_LOOKUP: Dict[str, int] = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}

#: ``as on Sep 25, 2026`` **and** ``as on Jun 15,2020`` — the space after the comma is not
#: reliable across vintages (trap 2), so it is optional in the pattern.
_TITLE_DATE_RE = re.compile(r"as\s+on\s+([A-Za-z]{3,9})\s+(\d{1,2})\s*,?\s*(\d{4})", re.IGNORECASE)

#: ``25-Sep-2026`` as used by the snapshot route.
_SNAPSHOT_DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


# ------------------------------------------------------------------ capability group 1: NSE


#: Archive host for the dated participant files. It serves **no** ``robots.txt`` (HTTP 404), so
#: RFC 9309 §2.3.1.3 leaves it unrestricted; verified 2026-09-28. It is also the host that
#: absorbs heavy traffic without friction, which is why nothing here touches ``www.nseindia.com``
#: and its cookie/Akamai prime.
PARTICIPANT_BASE_URL = "https://nsearchives.nseindia.com"

#: The single-day cash-flow snapshot. A genuinely different route: 217 bytes, one trading day,
#: two categories, and **no date parameters are accepted**. Used by the accumulation path
#: because it is the only one of the two with no dated archive behind it.
PARTICIPANT_SNAPSHOT_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

#: ``{'buyValue', 'category', 'date', 'netValue', 'sellValue'}`` — verbatim, all five present.
SNAPSHOT_FIELDS = ("buyValue", "category", "date", "netValue", "sellValue")

#: Columns the snapshot rows must carry. A missing one is a schema change, not a zero.
SOURCE_DATE_FIELDS = SNAPSHOT_FIELDS

#: The participant categories the file actually carries, **measured** on 2026-09-25 (open
#: interest), 2020-06-15 (open interest) and 2026-09-25 (volume): all three are exactly these
#: four, plus a market-wide ``TOTAL`` row that is not a participant.
#:
#: The original ticket named five and included ``Arb``. There is no ``Arb`` row in this file on
#: any of the three vintages checked, so it is not in this tuple and is not fabricated. What the
#: feed really gives beyond FII/DII is the ``Client``/``Pro`` split, which is present and is the
#: reason to use it. A frame also carries ``attrs["bfinance_participants_present"]`` measured
#: from the rows, so a future category appears in the data rather than being dropped by a
#: hard-coded allow-list.
PARTICIPANT_CATEGORIES: Tuple[str, ...] = ("Client", "DII", "FII", "Pro")

#: The market-wide row NSE adds to the file. Excluded from the tidy rows (it is an aggregate,
#: not a participant) and published in ``attrs`` instead.
PARTICIPANT_TOTAL_ROW = "TOTAL"

#: Which file to read. ``oi`` is open interest (a position), ``vol`` is traded volume.
PARTICIPANT_MEASURES: Tuple[str, ...] = ("oi", "vol")

#: The unit of every numeric column in every participant file, per measure.
#:
#: **Both are ``contracts``, and that is a measurement.** See the module docstring, trap 5. The
#: decision this guards against is a future reader inferring rupees from the word "Total" in a
#: column header; the honest answer is that this file counts contracts, and
#: ``attrs["bfinance_units"]`` says so on every frame.
MEASURE_UNITS: Dict[str, str] = {"oi": UNITS_CONTRACTS, "vol": UNITS_CONTRACTS}

#: Tidy output contract. One row per (date, participant, measure, asset class, option type, side).
PARTICIPANT_TIDY_COLUMNS: Tuple[str, ...] = (
    "Date",
    "Participant",
    "Measure",
    "AssetClass",
    "OptionType",
    "Side",
    "Value",
)

#: A participant file's ``Client Type`` column, and the leg suffixes, are the only vocabulary the
#: parser needs. Anything else is a schema change and raises.
_ASSET_CLASSES = ("Index", "Stock")
_OPTION_TYPES = ("Call", "Put")
_SIDES = ("Long", "Short")
_KINDS = ("Future", "Option")


class ParticipantFlowsError(BFinanceError):
    """Base for every failure raised by the NSE participant group in this module.

    The package base lives in the package, per the rule
    :mod:`bfinance.statements.errors` and :mod:`bfinance.sources.errors` both state: each leaf
    also mixes in the closest :mod:`bfinance.utils.exceptions` sibling, so a caller can write
    one ``except`` for this module and a second, broader one for the package, and both hold.
    """


class ParticipantFlowsUnavailable(ParticipantFlowsError, UpstreamServiceError):
    """NSE published no participant file for the requested date.

    A **date** refusal, and typed as one. It was previously a subclass of
    :class:`~bfinance.utils.exceptions.TickerNotFoundError`, on the argument that a symbol was
    never named anywhere in the request, so no symbol error could apply. That argument survives
    the fix and the inheritance did not: a caller writing ``except TickerNotFoundError`` around a
    date-keyed archive read would report **a bad symbol** for what is **a trading holiday or a
    weekend** — a confidently-wrong diagnosis, handed over as if it were a fact, for a request
    that contained no symbol at all. The same conflation was the one place two vocabularies
    existed for one idea, and :mod:`bfinance.nse.macro` already answers the identical question
    the other way: its ``SourceUnavailableError`` states in its own docstring that it is
    *"not a subclass of ``TickerNotFoundError`` either, because nothing is missing — the route
    does not exist"*. This is the same idea under the same package, so it gets the same shape
    as :class:`~bfinance.sources.errors.SourceUnavailableError`:
    ``ParticipantFlowsError`` for the module, ``UpstreamServiceError`` for the package-wide
    cascade, and **not** ``TickerNotFoundError``.

    The message names the trading-day reason explicitly and separates the two states a caller
    must be able to tell apart: **the exchange was reached and had nothing to publish for this
    date**, which is what this is, and **the exchange could not be reached**, which raises
    :class:`UpstreamServiceError` instead and never lands here. A caller must not read a
    holiday as an outage and retry it.

    Raised by default for a single-day read. **Never** a zero-filled row: a flow series with a
    hole is a different object from a flow series with a zero, and returning one as the other is
    the defect this module exists to prevent.
    """

    def __init__(self, day: _date, reason: str, url: str) -> None:
        self.day = day
        self.reason = reason
        self.url = url
        super().__init__(
            f"NSE published no participant file for {day:%Y-%m-%d} (reason={reason!r}). "
            f"The request completed: {url} returned no document, so the exchange was reached "
            f"and had nothing to publish for that date. Expected: a weekend, an exchange "
            f"holiday, or a date before the archive begins. This is NOT a transport failure "
            f"(an unreachable or 5xx exchange raises UpstreamServiceError instead), and it is "
            f"not a data error, and not a zero."
        )


class ParticipantAsOfMismatch(ParticipantFlowsError, UpstreamServiceError):
    """The payload's own date disagrees with the date that was asked for.

    Raised instead of returning a frame, because either answer is a confidently-wrong result: the
    bytes are for some other day, and recording the *requested* date against them would hide it.
    A sibling NSE route was measured to ignore a ``date=`` parameter and return today's data with
    an HTTP 200, so "the URL said so" is not evidence that the numbers are for the day requested.
    """

    def __init__(self, expected: _date, found: Optional[_date], url: str) -> None:
        self.expected = expected
        self.found = found
        self.url = url
        super().__init__(
            f"Participant payload as-of mismatch for {url}: the file's own date is "
            f"{found.isoformat() if found else 'absent'} but {expected:%Y-%m-%d} was requested. "
            f"Refusing to return one day's positions labelled as another's."
        )


@dataclass(frozen=True)
class HttpResult:
    """One HTTP outcome, as the modules in this package see it.

    Wrapping the transport in a value type is what lets every test in ``tests/test_flows.py`` be
    offline and still assert that a request was or was not made: a test substitutes a
    :data:`Transport` that counts calls and returns canned :class:`HttpResult` objects, so
    "no request while the cache is fresh" is checked by an assertion rather than by inspecting
    a mock's call list after the fact.

    Attributes:
        url: The final URL, after redirects. This is what goes in ``attrs``.
        status: HTTP status code. ``404`` is meaningful and is *not* an error here.
        content: Raw response bytes, untouched.
        content_type: Response ``Content-Type``, for diagnostics.
        from_cache: True when no request was made. Set by the cache, never by a transport.
    """

    url: str
    status: int
    content: bytes
    content_type: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def not_found(self) -> bool:
        return self.status == 404

    def text(self) -> str:
        """Decode the body as UTF-8, stripping a BOM. Errors are replaced, never raised.

        ``errors="replace"`` is deliberate: a truncated or mis-encoded byte must not abort a
        parse that might still be 99% intact, and the substitution is visible in the output
        rather than hidden.
        """
        return self.content.decode("utf-8-sig", errors="replace")


#: A transport is any callable taking a URL and returning an :class:`HttpResult`. This is the
#: module's entire network seam.
Transport = Callable[[str], HttpResult]

#: One tidy participant observation, as produced by :func:`_tidy_from_frame`.
TidyRow = Dict[str, Any]


def _default_transport(timeout: float = 30.0) -> Transport:
    """Build the real HTTP transport: ``httpx``, browser ``User-Agent``, redirects followed.

    The ``nsearchives`` archive host needs no cookie priming — it is not ``www.nseindia.com``
    and does not sit behind Akamai. The priming approach in
    :mod:`bfinance.nse.corporate_actions` is deliberately *not* reused here because there is
    nothing to prime against; adding it would be a second, private scheme for a host that does
    not need one.
    """

    def _get(url: str) -> HttpResult:
        headers = {"User-Agent": USER_AGENTS[hash(url) % len(USER_AGENTS)], "Accept": "*/*"}
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
        return HttpResult(
            url=str(response.url),
            status=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type", ""),
        )

    return _get


def to_date(value: Union[str, _date, datetime, pd.Timestamp]) -> _date:
    """Coerce ``str`` / ``date`` / ``datetime`` / ``Timestamp`` to a plain :class:`datetime.date`.

    ``date`` rather than ``Timestamp``: the archive URL is built from calendar fields, so a
    tz-aware input must not shift the day.
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
                f"date={value!r} is not parseable. Expected 'YYYY-MM-DD', 'DDMMYYYY' or "
                f"'DD-MMM-YYYY', or a date/datetime."
            )
        return parsed.date()
    raise ValueError(f"date must be a str, date or datetime, got {type(value).__name__}")


def participant_url(
    day: Union[str, _date, datetime, pd.Timestamp], measure: str = "oi"
) -> str:
    """The archive URL for one trading day and one measure. Pure and offline.

    No request is made, so a caller can see exactly which URL a date will cost before spending
    anything on it. Exposed for the same reason :func:`bfinance.nse.bhavcopy.archive_url` is.

    Raises:
        ValueError: ``measure`` is not one of :data:`PARTICIPANT_MEASURES`.
    """
    if measure not in PARTICIPANT_MEASURES:
        raise ValueError(
            f"measure={measure!r} is not supported. Must be one of "
            f"{', '.join(PARTICIPANT_MEASURES)}."
        )
    clean = to_date(day)
    return (
        f"{PARTICIPANT_BASE_URL}/content/nsccl/"
        f"fao_participant_{measure}_{clean:%d%m%Y}.csv"
    )


def is_weekend(day: Union[str, _date, datetime, pd.Timestamp]) -> bool:
    """True for Saturday or Sunday. Pure and local — no request, no calendar, no I/O.

    NSE does not trade at the weekend, so a weekend is answered from this and never costs a 404.
    Exchange **holidays** are deliberately not bundled: a maintained holiday calendar goes stale,
    and a stale one silently swallows a real trading day. A holiday is discovered by a cheap 404
    and reported as ``reason="no_file"``, which is honest.
    """
    return to_date(day).weekday() >= 5


def missing_day_reason(day: Union[str, _date, datetime, pd.Timestamp]) -> Optional[str]:
    """Why a date is known a priori to have no file, or ``None`` if it might.

    ``"weekend"`` is decided locally. ``None`` means *unknown* — the date may well be an exchange
    holiday, and only a request can tell. The distinction is why a weekend and a holiday can be
    reported differently even though both end in "no data".
    """
    clean = to_date(day)
    return "weekend" if clean.weekday() >= 5 else None


# ------------------------------------------------------------------ participant parsing


def _parse_title_date(title: str, url: str) -> _date:
    """Pull ``as on <Mon> <d>, <yyyy>`` out of the file's title row.

    Required, not optional. An absent date is a :class:`ParsingError` rather than a fall back to
    the URL's date, because the whole as-of guarantee is that the date comes from the payload.
    Tolerant of the missing space after the comma (``Jun 15,2020``) — measured to differ by
    vintage (trap 2).
    """
    match = _TITLE_DATE_RE.search(title or "")
    if not match:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} has no 'as on <Mon> <d>, <yyyy>' "
            f"date in its title row, which is the only as-of statement the publisher makes. "
            f"Title was {title.strip()[:120]!r}. Refusing to label these numbers with the "
            f"requested date instead of the file's own."
        )
    month_name, day_part, year_part = match.groups()
    prefix = month_name[:3].lower()
    if prefix not in _MONTH_LOOKUP:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} title names an unknown month "
            f"{month_name!r}."
        )
    try:
        return _date(int(year_part), _MONTH_LOOKUP[prefix], int(day_part))
    except ValueError as exc:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} title holds an impossible date "
            f"{month_name} {day_part}, {year_part}: {exc}"
        ) from exc


def _classify_leg(column: str, url: str) -> Optional[Tuple[str, str, str, str]]:
    """Map one header name to ``(kind, asset_class, option_type, side)``, or ``None`` for totals.

    Returns ``None`` for a ``Total ...`` column, which is an aggregate rather than a leg.

    Raises:
        ParsingError: the name is neither a recognised leg nor a total. A new column would
            otherwise be dropped silently, and a dropped long/short leg is a wrong net position.
    """
    clean = " ".join(column.split())  # collapse runs of spaces *and* tabs (trap 1)
    if clean.lower().startswith("total"):
        return None
    tokens = clean.split(" ")
    if tokens[0] not in _KINDS or len(tokens) < 3:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} has an unrecognised column "
            f"{column!r} (normalised {clean!r}). Expected 'Total ...' or a leg such as "
            f"'Option Stock Put Short'. Refusing to drop a column rather than guess at it."
        )
    kind = tokens[0]
    if tokens[1] not in _ASSET_CLASSES:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} column {clean!r} names asset class "
            f"{tokens[1]!r}; expected one of {', '.join(_ASSET_CLASSES)}."
        )
    asset_class = tokens[1]
    rest = tokens[2:]
    option_type = ""
    if rest and rest[0] in _OPTION_TYPES:
        option_type = rest.pop(0)
    if len(rest) != 1 or rest[0] not in _SIDES:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} column {clean!r} does not end in a "
            f"single side token; expected one of {', '.join(_SIDES)}."
        )
    return kind, asset_class, option_type, rest[0]


def _tidy_from_frame(
    frame: pd.DataFrame,
    category_column: str,
    measure: str,
    as_of: _date,
    url: str,
    include_total: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Melt a participant CSV into :data:`PARTICIPANT_TIDY_COLUMNS` and report what it found.

    ``include_total=False`` (the default) keeps the publisher's market-wide ``TOTAL`` row **out**
    of the returned rows, because it is an aggregate of the other four rather than a fifth
    participant: left in, any ``groupby("Participant").sum()`` double-counts the market, and a
    silent 2x on a flow series is precisely the failure this module is built against. The totals
    are published in ``attrs`` instead, and ``include_total=True`` gets them back as rows for a
    caller who genuinely wants the aggregate alongside the detail.

    Returns the tidy frame plus a dict of measured facts for ``attrs``: which participants were
    present, which columns were unclassified, how the publisher's own ``TOTAL`` row reconciles
    against its detail rows, and the market totals themselves.
    """
    rows: List[TidyRow] = []
    leg_columns: List[str] = []
    # The published "Total Long Contracts" / "Total Short Contracts" columns. These are the
    # only totals worth reporting: summing a leg would answer a different question, and picking
    # the first long leg is how a market total silently becomes one product's total.
    total_columns: Dict[str, Tuple[str, str]] = {}

    for raw_column in frame.columns:
        # The category column is the frame's index source, not a leg. Handing it to the leg
        # classifier would raise on a perfectly good file.
        if str(raw_column) == category_column:
            continue
        mapped = _classify_leg(raw_column, url)
        if mapped is None:
            normalised = " ".join(str(raw_column).split())
            lowered = normalised.lower()
            for side in _SIDES:
                # 'Total Long Contracts' -> side 'Long'
                if lowered.startswith(f"total {side.lower()}"):
                    total_columns[side] = (str(raw_column), normalised)
                    break
            continue
        else:
            leg_columns.append(str(raw_column))
            kind, asset_class, option_type, side = mapped
            for participant, raw_value in zip(frame[category_column], frame[raw_column]):
                rows.append(
                    {
                        "Date": as_of,
                        "Participant": str(participant).strip(),
                        "Measure": measure,
                        "AssetClass": asset_class,
                        "OptionType": option_type or "None",
                        "Side": side,
                        # A non-numeric cell becomes NaN and is counted, never coerced to 0.
                        "Value": pd.to_numeric(raw_value, errors="coerce"),
                        "_kind": kind,
                    }
                )

    tidy = pd.DataFrame(rows, columns=list(PARTICIPANT_TIDY_COLUMNS) + ["_kind"])
    tidy = tidy.sort_values(
        ["Date", "Participant", "Measure", "AssetClass", "OptionType", "Side"], kind="stable"
    )
    tidy = tidy.reset_index(drop=True)

    # Split the aggregate out before anything reads the index, so no later group-by can
    # double-count the market as if it were a fifth participant (trap 4).
    is_total = tidy["Participant"] == PARTICIPANT_TOTAL_ROW
    detail_tidy = tidy[~is_total]
    total_tidy = tidy[is_total]

    participants_present = tuple(sorted(detail_tidy["Participant"].unique()))

    # The publisher's TOTAL row is an aggregate, not a participant.
    measured: Dict[str, Any] = {
        "bfinance_participants_present": participants_present,
        "bfinance_participant_categories_documented": PARTICIPANT_CATEGORIES,
        "bfinance_leg_columns": len(leg_columns),
        "bfinance_unclassified_value_rows": int(tidy["Value"].isna().sum()),
        "bfinance_total_row_excluded_from_rows": not include_total,
    }

    if not total_tidy.empty:
        totals: Dict[str, int] = {}
        published_row = frame[
            frame[category_column].astype(str).str.strip() == PARTICIPANT_TOTAL_ROW
        ]
        for side in _SIDES:
            if side not in total_columns or published_row.empty:
                continue
            raw_column, _ = total_columns[side]
            reported = int(
                pd.to_numeric(published_row.iloc[0][raw_column], errors="coerce")
            )
            legs = detail_tidy[detail_tidy["Side"] == side]["Value"].sum()
            totals[side] = reported
            # NOT corrected, only disclosed (trap 3). The publisher's number wins.
            measured.setdefault("bfinance_total_reconciliation_residual", {})[side] = int(
                reported - legs
            )
        measured["bfinance_market_totals"] = totals

    measured.setdefault("bfinance_market_totals", {})
    measured.setdefault("bfinance_total_reconciliation_residual", {})

    kept = pd.concat([detail_tidy, total_tidy]) if include_total else detail_tidy
    return kept.reset_index(drop=True), measured


def _verify_as_of(
    found: Optional[_date], expected: Optional[_date], url: str
) -> None:
    """Compare the payload's own date against the request, and raise on disagreement.

    The single load-bearing check in this module. ``found`` is what the *bytes* say; ``expected``
    is what the caller asked for. Returning a frame when they differ would label one day's
    positions as another's — the confidently-wrong answer this module exists to refuse.
    """
    if expected is None:
        return
    if found is None:
        raise ParticipantAsOfMismatch(expected, None, url)
    if found != expected:
        raise ParticipantAsOfMismatch(expected, found, url)


def parse_participant_oi(
    csv_text: str,
    *,
    measure: str = "oi",
    expected_date: Optional[Union[str, _date, datetime, pd.Timestamp]] = None,
    url: str = "",
    include_total: bool = False,
) -> pd.DataFrame:
    """Turn one participant CSV into a tidy frame. Pure — no network, no clock.

    Args:
        csv_text: The decoded body. Line endings are irrelevant; upstream serves CRLF and the
            committed fixtures are LF, and the parse is identical either way.
        measure: Which file this is, recorded in the ``Measure`` column. One of
            :data:`PARTICIPANT_MEASURES`.
        expected_date: When given, the payload's own title-row date is compared against it and
            :class:`ParticipantAsOfMismatch` is raised on disagreement. **Pass it.** Omitting it
            records the date without checking it, which is the weaker guarantee.
        url: Recorded in ``attrs`` for provenance and used in error messages.
        include_total: Keep the publisher's market-wide ``TOTAL`` row as rows. ``False`` by
            default, because it is an aggregate of the other four and would double-count a
            ``groupby`` (see :func:`_tidy_from_frame`). The totals are on ``attrs`` either way.

    Returns:
        A frame indexed by ``Participant`` with columns
        :data:`PARTICIPANT_TIDY_COLUMNS` minus ``Participant`` — that is ``Date``, ``Measure``,
        ``AssetClass``, ``OptionType``, ``Side``, ``Value`` — one row per participant per leg.
        ``Value`` is a contract count in :data:`UNITS_CONTRACTS` and is ``int64`` when complete.

    Raises:
        ValueError: ``measure`` is unsupported.
        ParsingError: the title row carries no parseable date, the header is unrecognisable, or a
            leg column cannot be classified.
        ParticipantAsOfMismatch: the payload's date is not ``expected_date``.
    """
    if measure not in PARTICIPANT_MEASURES:
        raise ValueError(
            f"measure={measure!r} is not supported. Must be one of "
            f"{', '.join(PARTICIPANT_MEASURES)}."
        )
    expected = to_date(expected_date) if expected_date is not None else None

    # Split the title row off before anything else: it is a 15-field row whose only content is
    # the as-of statement, and handing it to read_csv as a header would produce a frame of NaN.
    text = csv_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} has {len(lines)} non-empty line(s); "
            f"expected a title row, a header row and at least one participant."
        )

    title = lines[0]
    as_of = _parse_title_date(title, url)

    frame = pd.read_csv(
        io.StringIO("\n".join(lines[1:])), dtype=str, keep_default_na=False, na_values=[""]
    )
    frame = frame.drop(columns=[c for c in frame.columns if str(c).startswith("Unnamed:")])
    if frame.empty:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} has a header but no data rows."
        )

    # Strip every header: the 2026 vintage pads with spaces and the 2020 vintage with a TAB
    # (trap 1). Doing this once here means no later lookup can miss on padding.
    frame = frame.rename(columns={c: str(c).strip() for c in frame.columns})
    category_column = next(
        (c for c in frame.columns if c.lower().replace(" ", "") == "clienttype"), None
    )
    if category_column is None:
        raise ParsingError(
            f"participant CSV{(' for ' + url) if url else ''} has no 'Client Type' column. "
            f"Columns were {list(frame.columns)!r}. Upstream schema changed."
        )

    # The check happens BEFORE any row is turned into a value, so a mismatch can never be
    # half-consumed by a consumer of the returned frame.
    _verify_as_of(as_of, expected, url)

    tidy, measured = _tidy_from_frame(
        frame, category_column, measure, as_of, url, include_total=include_total
    )

    values = tidy["Value"]
    tidy["Value"] = values.astype("int64") if not values.isna().any() else values.astype("float64")
    # A real datetime64, not a column of datetime.date objects: a range read concatenates these
    # frames and then does .max().date() and .nunique() on this column, and datetime.date has
    # no .date() method.
    tidy["Date"] = pd.to_datetime(tidy["Date"])
    tidy = tidy.reset_index()
    tidy = tidy[[c for c in PARTICIPANT_TIDY_COLUMNS]]
    tidy.index = pd.Index(tidy["Participant"], name="Participant")
    tidy = tidy.drop(columns=["Participant"])
    tidy = tidy.sort_index(kind="stable")

    tidy.attrs.update(
        {
            CAPABILITY_ATTR: CAPABILITY_PARTICIPANT,
            "bfinance_provenance": "nse_participant_oi",
            SOURCE_URL_ATTR: url,
            AS_OF_ATTR: as_of.isoformat(),
            AS_OF_VERIFIED_ATTR: expected is not None,
            UNITS_ATTR: MEASURE_UNITS[measure],
            STATUS_ATTR: STATUS_OK,
        }
    )
    tidy.attrs.update(measured)
    return tidy


def parse_participant_snapshot(
    payload: Union[str, bytes, Sequence[Dict[str, Any]]],
    *,
    expected_date: Optional[Union[str, _date, datetime, pd.Timestamp]] = None,
    url: str = PARTICIPANT_SNAPSHOT_URL,
) -> pd.DataFrame:
    """Parse the single-day FII/DII cash-flow snapshot. Pure — no network, no clock.

    The payload is a JSON array of flat objects with exactly
    :data:`SNAPSHOT_FIELDS` = ``buyValue``, ``category``, ``date``, ``netValue``, ``sellValue``.
    Measured: 217 bytes, **one trading day**, two categories (``DII``, ``FII/FPI``), and the
    route **accepts no date parameters** — which is precisely why :class:`FlowArchive` exists.

    Amounts are **INR crore**, and that is verified two ways rather than assumed: each row
    satisfies ``netValue == buyValue - sellValue`` exactly (14,035.77 - 11,197.60 = 2,838.17;
    12,327.36 - 16,021.29 = -3,693.93), and the daily magnitudes are consistent with crore and
    not with rupees or millions.

    Returns:
        A frame indexed by ``Category`` with columns ``Date``, ``Buy``, ``Sell``, ``Net`` in
        :data:`UNITS_INR_CRORE`. The ``Net`` column is the publisher's own, not a subtraction
        this module performed; the identity is *checked* in the tests rather than used to build
        it.

    Raises:
        ParsingError: not a JSON array, a field is absent, or a row's date does not parse.
        ParticipantAsOfMismatch: the rows' ``date`` disagrees with ``expected_date``.
    """
    expected = to_date(expected_date) if expected_date is not None else None

    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig", errors="replace")
    if isinstance(payload, str):
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParsingError(
                f"participant snapshot for {url} is not JSON ({exc}). An HTML error page served "
                f"with a 200 is the usual cause."
            ) from exc
    else:
        records = list(payload)

    if not isinstance(records, list):
        raise ParsingError(
            f"participant snapshot for {url} is a {type(records).__name__}, expected a JSON "
            f"array. Upstream response shape changed."
        )
    if not records:
        raise ParsingError(
            f"participant snapshot for {url} is an empty array. That is not the same as 'no "
            f"flows': the route always publishes the previous session, so an empty array is a "
            f"schema or outage signal."
        )

    rows: List[Dict[str, Any]] = []
    observed_dates: set = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ParsingError(
                f"participant snapshot row {index} for {url} is a "
                f"{type(record).__name__}, expected an object."
            )
        missing = [f for f in SNAPSHOT_FIELDS if f not in record]
        if missing:
            raise ParsingError(
                f"participant snapshot row {index} for {url} is missing field(s) {missing}. "
                f"Upstream schema changed. Not treated as zero."
            )
        raw_date = str(record["date"]).strip()
        match = _SNAPSHOT_DATE_RE.match(raw_date)
        if not match:
            raise ParsingError(
                f"participant snapshot row {index} for {url} has date {raw_date!r}, which is not "
                f"'DD-MMM-YYYY'."
            )
        day_part, month_name, year_part = match.groups()
        prefix = month_name[:3].lower()
        if prefix not in _MONTH_LOOKUP:
            raise ParsingError(
                f"participant snapshot row {index} for {url} has date {raw_date!r}, whose month "
                f"{month_name!r} is not one of the twelve."
            )
        observed_dates.add(_date(int(year_part), _MONTH_LOOKUP[prefix], int(day_part)))
        rows.append(
            {
                "Category": str(record["category"]).strip(),
                "Date": raw_date,
                "Buy": pd.to_numeric(record["buyValue"], errors="coerce"),
                "Sell": pd.to_numeric(record["sellValue"], errors="coerce"),
                # The publisher's own net, deliberately. Recomputing it here would hide an
                # upstream identity break instead of surfacing it.
                "Net": pd.to_numeric(record["netValue"], errors="coerce"),
            }
        )

    if len(observed_dates) != 1:
        raise ParsingError(
            f"participant snapshot for {url} spans {len(observed_dates)} dates "
            f"{sorted(d.isoformat() for d in observed_dates)}; it is a single-day route. "
            f"Refusing to merge them into one frame."
        )
    payload_date = next(iter(observed_dates))
    _verify_as_of(payload_date, expected, url)

    frame = pd.DataFrame(rows, columns=["Category", "Date", "Buy", "Sell", "Net"])
    frame.index = pd.Index(frame["Category"], name="Category")
    frame = frame.drop(columns=["Category"]).sort_index(kind="stable")
    # float64, always. These are INR *crore* to two decimals (14035.77), so an int64 cast would
    # silently truncate every amount to whole crore — a 0.005% error on every number, and a
    # destroyed net identity. Currency is never integer-cast here.
    for column in ("Buy", "Sell", "Net"):
        frame[column] = frame[column].astype("float64")

    frame.attrs.update(
        {
            CAPABILITY_ATTR: CAPABILITY_PARTICIPANT,
            "bfinance_provenance": "nse_fiidii_trade_react",
            SOURCE_URL_ATTR: url,
            AS_OF_ATTR: payload_date.isoformat(),
            AS_OF_VERIFIED_ATTR: expected is not None,
            UNITS_ATTR: UNITS_INR_CRORE,
            STATUS_ATTR: STATUS_OK,
            "bfinance_snapshot_is_single_day": True,
            "bfinance_snapshot_categories": tuple(sorted(frame.index.unique())),
        }
    )
    return frame


def empty_participant_frame(
    attrs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """An empty frame with the tidy columns and an **explicit** unavailable status.

    This is what a 404 looks like under ``on_missing="frame"``. It is never a zero-filled row:
    ``Value`` has no rows at all, and :data:`STATUS_ATTR` is :data:`STATUS_UNAVAILABLE` with a
    reason, so this can never be mistaken for a genuine day on which every participant held zero
    contracts. That distinction is the point.
    """
    out = pd.DataFrame(
        {
            "Date": pd.Series(dtype="datetime64[ns]"),
            "Measure": pd.Series(dtype="object"),
            "AssetClass": pd.Series(dtype="object"),
            "OptionType": pd.Series(dtype="object"),
            "Side": pd.Series(dtype="object"),
            "Value": pd.Series(dtype="int64"),
        },
        index=pd.Index([], name="Participant", dtype="object"),
    )
    out.attrs.update(
        {
            CAPABILITY_ATTR: CAPABILITY_PARTICIPANT,
            "bfinance_provenance": "nse_participant_oi",
            STATUS_ATTR: STATUS_UNAVAILABLE,
        }
    )
    if attrs:
        out.attrs.update(attrs)
    # Set last so a caller-supplied dict can never suppress the status. This key is the reason
    # the empty frame is safe, so it is not overridable.
    out.attrs[STATUS_ATTR] = STATUS_UNAVAILABLE
    return out


# ------------------------------------------------------------------ accumulation archive


class FlowArchive:
    """A local, **non-expiring** store of daily flow observations.

    :class:`~bfinance.cache.sqlite_cache.SQLiteCache` is deliberately not used here, for one
    reason: it is a TTL cache, and a flow record that expires is a record that stops existing.
    A TTL would silently truncate the accumulated history to the cache's horizon and re-fetch it,
    which for a route that only ever publishes *today* means the history is simply gone. This
    archive has no expiry at all, and a re-record of the same ``(date, category, measure)``
    overwrites rather than appends, so re-running a day is idempotent.

    It is the answer to "a flows series must be accumulated locally from first run". The snapshot
    route publishes one day and forgets it, so without this a backtest has no history and no way
    to know it has none.

    Args:
        db_path: SQLite file. Defaults to ``~/.bfinance/flow_archive.db``, matching the existing
            cache's home-directory convention.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            home = Path.home() / ".bfinance"
            home.mkdir(parents=True, exist_ok=True)
            db_path = home / "flow_archive.db"
        self.db_path = str(db_path)
        with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bfinance_flow_archive (
                    day TEXT NOT NULL,
                    category TEXT NOT NULL,
                    measure TEXT NOT NULL,
                    buy REAL,
                    sell REAL,
                    net REAL,
                    source_url TEXT NOT NULL,
                    recorded_utc TEXT NOT NULL,
                    PRIMARY KEY (day, category, measure)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_flow_archive_day ON bfinance_flow_archive(day)"
            )
            conn.commit()

    def record(self, frame: pd.DataFrame, *, source_url: str, measure: str = "trade") -> int:
        """Store every row of a snapshot frame. Idempotent per ``(day, category, measure)``.

        Returns the number of rows written. ``upsert`` semantics, so re-recording a corrected
        publication replaces the earlier value instead of leaving two contradictory rows.
        """
        as_of = str(frame.attrs.get(AS_OF_ATTR, ""))
        if not as_of:
            raise ValueError(
                "FlowArchive.record needs attrs['bfinance_as_of']; without a day to file the row "
                "under it would be unattributable."
            )
        recorded = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = [
            (
                as_of,
                str(category),
                measure,
                float(frame.at[category, "Buy"]) if "Buy" in frame.columns else None,
                float(frame.at[category, "Sell"]) if "Sell" in frame.columns else None,
                float(frame.at[category, "Net"]) if "Net" in frame.columns else None,
                source_url,
                recorded,
            )
            for category in frame.index
        ]
        with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
            conn.executemany(
                """
                INSERT INTO bfinance_flow_archive
                    (day, category, measure, buy, sell, net, source_url, recorded_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, category, measure) DO UPDATE SET
                    buy=excluded.buy, sell=excluded.sell, net=excluded.net,
                    source_url=excluded.source_url, recorded_utc=excluded.recorded_utc
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def read(
        self, start: Optional[Union[str, _date, datetime]] = None,
        end: Optional[Union[str, _date, datetime]] = None,
    ) -> pd.DataFrame:
        """Read accumulated rows in ``[start, end]``, ascending by day.

        Returns an empty frame with the right columns when the archive holds nothing — which on a
        first run is the normal case, and is reported as such by
        ``attrs["bfinance_archive_is_empty"]`` rather than as a short series.
        """
        clauses, params = [], []
        if start is not None:
            clauses.append("day >= ?")
            params.append(to_date(start).isoformat())
        if end is not None:
            clauses.append("day <= ?")
            params.append(to_date(end).isoformat())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT day, category, measure, buy, sell, net, source_url, recorded_utc "
                f"FROM bfinance_flow_archive{where} ORDER BY day, category",
                params,
            ).fetchall()

        frame = pd.DataFrame(
            [dict(r) for r in rows],
            columns=[
                "day",
                "category",
                "measure",
                "buy",
                "sell",
                "net",
                "source_url",
                "recorded_utc",
            ],
        )
        if frame.empty:
            frame["day"] = pd.Series(dtype="object")
        frame.attrs.update(
            {
                CAPABILITY_ATTR: CAPABILITY_PARTICIPANT,
                "bfinance_provenance": "local_flow_archive",
                UNITS_ATTR: UNITS_INR_CRORE,
                STATUS_ATTR: STATUS_OK,
                "bfinance_archive_is_empty": True,
            }
        )
        if not frame.empty:
            frame["day"] = pd.to_datetime(frame["day"])
            frame.attrs["bfinance_archive_is_empty"] = False
            frame.attrs[AS_OF_ATTR] = frame["day"].max().date().isoformat()
            frame.attrs[AS_OF_VERIFIED_ATTR] = False
            frame.attrs["bfinance_archive_coverage_start"] = frame["day"].min().date().isoformat()
            frame.attrs["bfinance_archive_coverage_end"] = frame["day"].max().date().isoformat()
            frame.attrs["bfinance_archive_row_count"] = int(len(frame))
            frame.attrs[SOURCE_URL_ATTR] = "sqlite:" + self.db_path
        return frame

    def coverage(self) -> Dict[str, Any]:
        """What this archive actually holds, as a dict for ``attrs``.

        The point of this method is the ``"never_backfillable"`` key. The snapshot route
        publishes one day and overwrites it, so **the history before the archive's first day was
        never obtainable by anyone, including this library.** A caller must be able to read that
        off the result rather than infer a complete series from a short frame.
        """
        with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
            row = conn.execute(
                "SELECT MIN(day), MAX(day), COUNT(*), COUNT(DISTINCT day) "
                "FROM bfinance_flow_archive"
            ).fetchone()
        first, last, total, distinct = row
        return {
            "bfinance_archive_first_day": first,
            "bfinance_archive_last_day": last,
            "bfinance_archive_row_count": int(total or 0),
            "bfinance_archive_day_count": int(distinct or 0),
            "bfinance_archive_path": self.db_path,
            # Stated as a fact about the source, not a caveat in a docstring.
            "bfinance_archive_history_never_backfillable": True,
            "bfinance_archive_backfill_note": (
                "The FII/DII snapshot route publishes one trading day and accepts no date "
                "parameters, so no day before this archive's first_day can ever be retrieved. A "
                "series from here is complete only from first_day forward; treat anything earlier "
                "as unavailable, not as zero."
            ),
        }


# ------------------------------------------------------------------ NSE client


class ParticipantFlowsClient:
    """NSE participant flow-of-funds, over two genuinely different routes.

    Args:
        transport: A :data:`Transport`. Defaults to a real ``httpx`` GET. Injecting one is how
            the tests stay offline while still asserting on whether a request happened.
        archive: Optional :class:`FlowArchive`. When supplied, every snapshot read is recorded,
            which is what makes the accumulate-from-first-run behaviour work.
        raise_errors: Default for ``on_missing`` on single-day reads. ``True`` means a date with
            no file raises :class:`ParticipantFlowsUnavailable`; ``False`` returns the typed
            empty frame.
        timeout: Seconds, forwarded to the default transport only.
    """

    def __init__(
        self,
        transport: Optional[Transport] = None,
        *,
        archive: Optional[FlowArchive] = None,
        raise_errors: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.transport = transport or _default_transport(timeout)
        self.archive = archive
        self.raise_errors = raise_errors

    # ------------------------------------------------------------ internals

    def _get(self, url: str) -> HttpResult:
        result = self.transport(url)
        if result.status in (200, 404):
            return result
        if result.status == 429:
            raise UpstreamServiceError(
                f"NSE participant archive rate limited (HTTP 429) for {url}. Slow down."
            )
        raise UpstreamServiceError(
            f"NSE participant archive returned unexpected HTTP {result.status} for {url}"
        )

    # -------------------------------------------------------------- public

    def participant_flows(
        self,
        day: Union[str, _date, datetime, pd.Timestamp],
        *,
        measure: str = "oi",
        on_missing: Optional[str] = None,
    ) -> pd.DataFrame:
        """Positions for one trading day, for every participant category.

        Args:
            day: ``'YYYY-MM-DD'``, ``'DDMMYYYY'``, ``date`` or ``datetime``.
            measure: ``'oi'`` (open interest, a position) or ``'vol'`` (traded volume). Both are
                counted in :data:`UNITS_CONTRACTS`.
            on_missing: ``'error'`` raises :class:`ParticipantFlowsUnavailable`; ``'frame'``
                returns the typed empty frame. Defaults to the client's ``raise_errors``.

        Returns:
            A frame indexed by ``Participant`` with ``Date``, ``Measure``, ``AssetClass``,
            ``OptionType``, ``Side``, ``Value``, carrying
            ``attrs["bfinance_units"] == "contracts"`` and a payload-derived, request-checked
            ``attrs["bfinance_as_of"]``.

        Raises:
            ParticipantFlowsUnavailable: no file for that date, under the default policy.
            ParticipantAsOfMismatch: the file's own date is not the date asked for.
            ParsingError: the payload does not match the recorded schema.
        """
        policy = on_missing or ("error" if self.raise_errors else "frame")
        if policy not in ("error", "frame"):
            raise ValueError(f"on_missing={policy!r} must be 'error' or 'frame'.")
        clean = to_date(day)
        url = participant_url(clean, measure)

        local_reason = missing_day_reason(clean)
        if local_reason is not None:
            # No request at all for a weekend. Spent deliberately: a weekend is decidable locally.
            return self._unavailable(clean, local_reason, url, policy, measure)

        result = self._get(url)
        if result.not_found:
            return self._unavailable(clean, "no_file", url, policy, measure)

        frame = parse_participant_oi(
            result.text(),
            measure=measure,
            expected_date=clean,
            url=result.url or url,
        )
        return frame

    def _unavailable(
        self, day: _date, reason: str, url: str, policy: str, measure: str = "oi"
    ) -> pd.DataFrame:
        if policy == "error":
            raise ParticipantFlowsUnavailable(day, reason, url)
        message = (
            f"NSE published no participant {reason.replace('_', ' ')} file for {day:%Y-%m-%d} "
            f"(expected: a weekend, an exchange holiday, or a date before the archive begins). "
            f"Not a zero."
        )
        logger.debug("%s", message)
        return empty_participant_frame(
            {
                SOURCE_URL_ATTR: url,
                AS_OF_ATTR: day.isoformat(),
                AS_OF_VERIFIED_ATTR: False,
                UNITS_ATTR: MEASURE_UNITS.get(measure, UNITS_CONTRACTS),
                UNAVAILABLE_REASON_ATTR: reason,
                MISSING_DATES_ATTR: [{"date": day.isoformat(), "reason": reason}],
            }
        )

    def participant_flows_range(
        self,
        start: Union[str, _date, datetime, pd.Timestamp],
        end: Union[str, _date, datetime, pd.Timestamp],
        *,
        measure: str = "oi",
        on_missing: str = "skip",
    ) -> pd.DataFrame:
        """Walk ``[start, end]`` and stack every day that has an archive.

        Saturdays and Sundays cost no request. Exchange holidays are not bundled as a calendar,
        so each costs one cheap 404 and is listed in
        ``attrs["bfinance_missing_dates"]`` as ``{"date": ..., "reason": ...}``.

        A short result is therefore never mistaken for a complete one: the requested span and
        the days actually returned are both on the frame, so the holes are visible rather than
        inferred.
        """
        if on_missing not in ("skip", "raise"):
            raise ValueError(f"on_missing={on_missing!r} must be 'skip' or 'raise'.")
        first, last = to_date(start), to_date(end)
        if last < first:
            raise ValueError(f"end={last} is before start={first}")

        frames: List[pd.DataFrame] = []
        missing: List[Dict[str, str]] = []
        cursor = first
        while cursor <= last:
            try:
                day_frame = self.participant_flows(cursor, measure=measure, on_missing="frame")
            except ParticipantAsOfMismatch as exc:
                # A wrong-day payload is a real integrity failure, not a missing day. It must not
                # be swallowed into the missing list, which would read as "no data".
                raise exc
            if day_frame.attrs.get(STATUS_ATTR) == STATUS_UNAVAILABLE:
                missing.append(
                    {
                        "date": cursor.isoformat(),
                        "reason": str(day_frame.attrs.get(UNAVAILABLE_REASON_ATTR, "no_file")),
                    }
                )
                if on_missing == "raise":
                    raise ParticipantFlowsUnavailable(
                        cursor, str(day_frame.attrs[UNAVAILABLE_REASON_ATTR]), participant_url(cursor, measure)
                    )
            else:
                day_frame.attrs.clear()
                frames.append(day_frame)
            cursor = _date.fromordinal(cursor.toordinal() + 1)

        combined_attrs: Dict[str, Any] = {
            CAPABILITY_ATTR: CAPABILITY_PARTICIPANT,
            "bfinance_provenance": "nse_participant_oi",
            UNITS_ATTR: MEASURE_UNITS[measure],
            MISSING_DATES_ATTR: missing,
            "bfinance_requested_start": first.isoformat(),
            "bfinance_requested_end": last.isoformat(),
            "bfinance_calendar_days_in_span": (last.toordinal() - first.toordinal()) + 1,
            "bfinance_source_url": f"{PARTICIPANT_BASE_URL}/content/nsccl/"
            f"fao_participant_{measure}_<DDMMYYYY>.csv",
        }

        if not frames:
            # Every day in the span had no file. Still typed, still not zeros.
            out = empty_participant_frame(combined_attrs)
            out.attrs[STATUS_ATTR] = STATUS_UNAVAILABLE
            out.attrs[UNAVAILABLE_REASON_ATTR] = "no_file"
            out.attrs["bfinance_days_returned"] = 0
            out.attrs["bfinance_source_url"] = combined_attrs["bfinance_source_url"]
            return out

        # NOT ignore_index: Participant is this frame's *index*, and ignoring it would drop the
        # one column that says whose position a row is. The per-day frames are concatenated
        # index-first, then sorted, so Participant survives.
        combined = pd.concat(frames)
        combined = combined.sort_index(kind="stable")
        combined.attrs.update(combined_attrs)
        combined.attrs[STATUS_ATTR] = STATUS_OK
        combined.attrs["bfinance_days_returned"] = int(combined["Date"].nunique())
        # The as-of of a span is its newest day; verified=True because each day was individually
        # checked against its own file's title row.
        combined.attrs[AS_OF_ATTR] = combined["Date"].max().date().isoformat()
        combined.attrs[AS_OF_VERIFIED_ATTR] = True
        return combined

    def snapshot(
        self,
        *,
        expected_date: Optional[Union[str, _date, datetime, pd.Timestamp]] = None,
        record: bool = True,
    ) -> pd.DataFrame:
        """Read today's FII/DII cash-flow snapshot and, by default, file it in the archive.

        This is the route that publishes one day and forgets it. ``record=True`` (the default) is
        what makes the series usable at all; without it every call re-reads the same day and the
        history never grows. The recorded frame's ``attrs`` are the archive's, so the caller
        immediately learns how far back the history actually reaches.
        """
        result = self._get(PARTICIPANT_SNAPSHOT_URL)
        if result.not_found:
            raise ParticipantFlowsUnavailable(
                to_date(expected_date) if expected_date else _date.today(),
                "no_file",
                PARTICIPANT_SNAPSHOT_URL,
            )
        frame = parse_participant_snapshot(
            result.text(),
            expected_date=expected_date,
            url=result.url or PARTICIPANT_SNAPSHOT_URL,
        )
        if record and self.archive is not None:
            written = self.archive.record(frame, source_url=result.url or PARTICIPANT_SNAPSHOT_URL)
            frame.attrs["bfinance_archive_rows_written"] = written
            frame.attrs.update(self.archive.coverage())
        return frame

    def accumulated_flows(
        self,
        start: Optional[Union[str, _date, datetime]] = None,
        end: Optional[Union[str, _date, datetime]] = None,
    ) -> pd.DataFrame:
        """Serve the accumulated local series, with its coverage stated on the frame.

        Raises:
            ValueError: no archive was supplied to the client.
        """
        if self.archive is None:
            raise ValueError(
                "accumulated_flows needs an archive. Construct the client with "
                "archive=FlowArchive(...) and call snapshot(record=True) to populate it."
            )
        frame = self.archive.read(start, end)
        frame.attrs.update(self.archive.coverage())
        return frame


_DEFAULT_PARTICIPANT_CLIENT: Optional[ParticipantFlowsClient] = None
_DEFAULT_MUTUAL_FUND_CLIENT: Optional["MutualFundClient"] = None


def _default_participant_client() -> ParticipantFlowsClient:
    global _DEFAULT_PARTICIPANT_CLIENT
    if _DEFAULT_PARTICIPANT_CLIENT is None:
        _DEFAULT_PARTICIPANT_CLIENT = ParticipantFlowsClient(
            archive=FlowArchive()
        )
    return _DEFAULT_PARTICIPANT_CLIENT


def participant_flows(
    day: Union[str, _date, datetime, pd.Timestamp],
    *,
    measure: str = "oi",
    on_missing: Optional[str] = None,
) -> pd.DataFrame:
    """Module shortcut for :meth:`ParticipantFlowsClient.participant_flows`.

    >>> from bfinance.flows import participant_flows              # doctest: +SKIP
    >>> participant_flows("2026-09-25").xs("FII", level="Participant")  # doctest: +SKIP
    """
    return _default_participant_client().participant_flows(
        day, measure=measure, on_missing=on_missing
    )


def participant_flows_range(
    start: Union[str, _date, datetime, pd.Timestamp],
    end: Union[str, _date, datetime, pd.Timestamp],
    *,
    measure: str = "oi",
    on_missing: str = "skip",
) -> pd.DataFrame:
    """Module shortcut for :meth:`ParticipantFlowsClient.participant_flows_range`."""
    return _default_participant_client().participant_flows_range(
        start, end, measure=measure, on_missing=on_missing
    )


# ------------------------------------------------------------------ capability group 2: AMFI


#: Rows that mean the same thing in every month's workbook, and are therefore the only ones that
#: may be joined into a time series. Measured drift: the debt category is
#: ``"Ultra Short Term Fund"`` in August 2026 and ``"Ultra Short Duration Fund"`` in May 2020,
#: and ``"Short Term Fund"`` versus ``"Short Duration Fund"`` — so a category-level series
#: built by label-join would drop the debt buckets the moment a label was reworded.
AMFI_STABLE_ANCHORS: Tuple[str, ...] = ("Grand Total", "Total A-Open ended Schemes")

AMFI_BASE_URL = "https://portal.amfiindia.com/spages"

#: Canonical output of :func:`parse_amfi_monthly_report`. ``Net`` is the publisher's own
#: ``Net Inflow (+ve)/Outflow (-ve)`` figure in INR crore.
AMFI_REPORT_COLUMNS: Tuple[str, ...] = (
    "Schemes",
    "Folios",
    "FundsMobilised",
    "RepurchaseRedemption",
    "NetFlow",
)


class MutualFundWorkbookUnavailable(UpstreamServiceError):
    """An AMFI workbook could not be turned into records.

    Raised with the reason attached, because there are two very different causes and they are not
    interchangeable: the ``.xls`` workbooks are BIFF8 binary, and no reader for them is in this
    project's dependency set (``openpyxl`` is OOXML only). The monthly *index* — which months
    exist, which of them have a numeric workbook — is fully supported, so a caller is never
    blocked from finding out what is available.
    """


def parse_amfi_monthly_index(
    payload: Union[str, bytes, Sequence[Any]], *, source_url: str = ""
) -> pd.DataFrame:
    """Parse the AMFI monthly-report index into a coverage table. Pure — no network.

    The upstream shape is a Next.js RSC flight payload under the key ``monthlyData``:
    ``Year`` -> ``Pages`` -> ``{Title, month, year, pdf_url, excel_url, financial_year}``. A flat
    list is accepted as well, so a caller holding an already-unwrapped list is served too.

    The load-bearing column is ``HasWorkbook``: only **101 of 326 months (April 1999 - August
    2026)** have an ``excel_url``; the numeric series starts April 2018 and the rest are
    PDF/HTML. A caller that ignored this would believe a 27-year numeric history exists.

    Returns:
        A frame indexed by ``Month`` (``datetime64[ns]``) with ``Title``, ``FinancialYear``,
        ``ExcelUrl``, ``PdfUrl``, ``HasWorkbook``.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig", errors="replace")
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParsingError(
                f"AMFI monthly index{(' from ' + source_url) if source_url else ''} is not JSON "
                f"({exc}). The payload is a Next.js RSC flight, not a clean JSON endpoint."
            ) from exc
        if isinstance(parsed, dict):
            for key in ("monthlyData", "data", "Pages"):
                if key in parsed:
                    parsed = parsed[key]
                    break
    if not isinstance(parsed, list):
        raise ParsingError(
            f"AMFI monthly index is a {type(parsed).__name__}, expected a list of "
            f"'financial year' buckets."
        )

    rows: List[Dict[str, Any]] = []
    for bucket in parsed:
        if not isinstance(bucket, dict):
            continue
        pages = bucket.get("Pages") or []
        for page in pages:
            if not isinstance(page, dict):
                continue
            excel_url = page.get("excel_url")
            title = str(page.get("Title", "")).strip()
            month, year = page.get("month"), page.get("year")
            try:
                stamp = pd.Timestamp(int(year), int(month), 1)
            except (TypeError, ValueError) as exc:
                raise ParsingError(
                    f"AMFI monthly index entry {title!r} has an unusable month/year "
                    f"({month!r}/{year!r}): {exc}"
                ) from exc
            rows.append(
                {
                    "Month": stamp,
                    "Title": title,
                    "FinancialYear": str(page.get("financial_year", "")),
                    "ExcelUrl": excel_url,
                    "PdfUrl": page.get("pdf_url"),
                    # The whole point: a null excel_url is a month with no numbers in it.
                    "HasWorkbook": bool(excel_url),
                }
            )

    frame = pd.DataFrame(
        rows, columns=["Month", "Title", "FinancialYear", "ExcelUrl", "PdfUrl", "HasWorkbook"]
    )
    frame = frame.drop_duplicates(subset=["Month"], keep="first")
    frame = frame.sort_values("Month", kind="stable").set_index("Month")
    frame.index.name = "Month"

    frame.attrs.update(
        {
            CAPABILITY_ATTR: CAPABILITY_MUTUAL_FUND,
            "bfinance_provenance": "amfi_monthly_index",
            SOURCE_URL_ATTR: source_url,
            UNITS_ATTR: UNITS_INR_CRORE,
            STATUS_ATTR: STATUS_OK,
            "bfinance_month_count": int(len(frame)),
            "bfinance_months_with_workbook": int(frame["HasWorkbook"].sum()),
            "bfinance_index_coverage_start": frame.index.min().date().isoformat()
            if len(frame)
            else None,
            "bfinance_index_coverage_end": frame.index.max().date().isoformat()
            if len(frame)
            else None,
            "bfinance_workbook_coverage_start": (
                frame.index[frame["HasWorkbook"]].min().date().isoformat()
                if frame["HasWorkbook"].any()
                else None
            ),
            "bfinance_numeric_history_note": (
                "Only months with HasWorkbook=True carry numbers. Older months are PDF/HTML "
                "only and are not parsed, so a numeric MF flow series starts at "
                "bfinance_workbook_coverage_start - not at bfinance_index_coverage_start."
            ),
        }
    )
    if len(frame):
        frame.attrs[AS_OF_ATTR] = frame.index.max().date().isoformat()
        frame.attrs[AS_OF_VERIFIED_ATTR] = False
    return frame


def parse_amfi_monthly_report(
    payload: Union[Dict[str, Any], str, bytes],
    *,
    source_url: str = "",
    month: Optional[Union[str, _date, datetime]] = None,
) -> pd.DataFrame:
    """Parse one AMFI monthly report into a category-level frame. Pure — no network, no clock.

    The input is the workbook's record structure: a mapping whose ``records`` list carries one
    entry per published row, each with a ``_row_label`` (the category or anchor name) and the
    numeric cells. The verbatim header row and the workbook's section taxonomy are also carried
    on the mapping, and are **kept out of the data**: they are the publisher's headings and
    hierarchy, not observations. The count of what was kept out is published in ``attrs``, so a
    reader can see that the headings were handled rather than silently promoted to rows.

    Units are INR crore, declared in the verbatim column header
    ``'Net Inflow (+ve)/Outflow (-ve) for the month of August 2026 (INR in crore)'``, and
    recorded on the frame as ``attrs["bfinance_units"]``.

    Args:
        month: The reporting month. When given it is checked against the month named in the
            workbook's own title, and a disagreement raises :class:`ParticipantAsOfMismatch` —
            the same payload-level check the participant files get.

    Returns:
        A frame indexed by ``Category`` with :data:`AMFI_REPORT_COLUMNS`.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParsingError(f"AMFI monthly report is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParsingError(
            f"AMFI monthly report is a {type(payload).__name__}, expected a mapping with a "
            f"'records' list."
        )

    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ParsingError(
            f"AMFI monthly report{(' from ' + source_url) if source_url else ''} has no "
            f"'records' list. Upstream structure changed."
        )

    title = str(payload.get("_workbook_title_cell_xls_row2", "")).strip()
    header = payload.get("_verbatim_header_row_from_xls_row3") or []
    taxonomy = payload.get("_section_taxonomy_rows_in_workbook") or []
    months_covered = str(payload.get("_months_covered", "")).strip()

    if month is not None:
        wanted = to_date(month)
        # Verified against the workbook's own title, not the URL: same reasoning as the
        # participant files, where a sibling route was measured to ignore a date parameter.
        found_pair = _amfi_month_from_title(title or months_covered)
        found = _date(found_pair[0], found_pair[1], 1) if found_pair else None
        _verify_as_of(found, _date(wanted.year, wanted.month, 1), source_url or "amfi monthly report")

    rows: List[Dict[str, Any]] = []
    for record in records:
        label = str(record.get("_row_label", "")).strip()
        if not label:
            continue
        rows.append(
            {
                "Category": label,
                "Schemes": pd.to_numeric(record.get("n_schemes"), errors="coerce"),
                "Folios": pd.to_numeric(record.get("n_folios"), errors="coerce"),
                "FundsMobilised": pd.to_numeric(
                    record.get("funds_mobilized_inr_crore"), errors="coerce"
                ),
                "RepurchaseRedemption": pd.to_numeric(
                    record.get("repurchase_redemption_inr_crore"), errors="coerce"
                ),
                # The publisher's own net figure, not mobilised minus repurchase.
                "NetFlow": pd.to_numeric(record.get("net_inflow_outflow_inr_crore"), errors="coerce"),
            }
        )

    frame = pd.DataFrame(rows, columns=["Category"] + list(AMFI_REPORT_COLUMNS))
    duplicates = int(frame["Category"].duplicated().sum())
    if duplicates:
        frame = frame[~frame["Category"].duplicated(keep="last")]
    frame = frame.set_index("Category").sort_index(kind="stable")
    frame.index.name = "Category"

    # Counts are integral; money is not and is never integer-cast. Split declared, not inferred,
    # because a float64 dtype is what pd.to_numeric returns for a mixed column and inferring from
    # it would leave 'Folios' as 2.8e8 rather than an exact count.
    for column in ("Schemes", "Folios"):
        values = frame[column]
        if values.notna().all() and bool((values % 1 == 0).all()):
            frame[column] = values.astype("int64")
    for column in ("FundsMobilised", "RepurchaseRedemption", "NetFlow"):
        frame[column] = frame[column].astype("float64")

    as_of: Optional[str] = None
    found_month = _amfi_month_from_title(title or months_covered)
    if found_month:
        as_of = _date(found_month[0], found_month[1], 1).isoformat()

    frame.attrs.update(
        {
            CAPABILITY_ATTR: CAPABILITY_MUTUAL_FUND,
            "bfinance_provenance": "amfi_monthly_report",
            SOURCE_URL_ATTR: source_url,
            AS_OF_ATTR: as_of,
            AS_OF_VERIFIED_ATTR: month is not None,
            UNITS_ATTR: UNITS_INR_CRORE,
            STATUS_ATTR: STATUS_OK,
            "bfinance_workbook_title": title or None,
            # The header/summary rows were handled, and the count says so.
            "bfinance_header_row_cells": len(header),
            "bfinance_section_taxonomy_rows": len(taxonomy),
            "bfinance_records_parsed": int(len(frame)),
            "bfinance_duplicate_category_rows": duplicates,
            "bfinance_stable_anchors_present": tuple(
                a for a in AMFI_STABLE_ANCHORS if a in frame.index
            ),
            "bfinance_category_labels_drift_note": (
                "Category labels are NOT stable across months (measured: 'Ultra Short Term Fund' "
                "2026-08 vs 'Ultra Short Duration Fund' 2020-05). Only "
                "bfinance_stable_anchors_present may be joined into a time series."
            ),
        }
    )
    return frame


def _amfi_month_from_title(text: str) -> Optional[Tuple[int, int]]:
    """Extract ``(year, month)`` from ``'Monthly Report for the month of August 2026'``.

    Returns ``None`` when no month is named, which is a legitimate state for a payload with no
    title and leaves the as-of unknown rather than guessed.
    """
    if not text:
        return None
    match = re.search(r"([A-Za-z]{3,9})\s+(\d{4})", text)
    if not match:
        return None
    name, year = match.group(1), match.group(2)
    prefix = name[:3].lower()
    if prefix not in _MONTH_LOOKUP:
        return None
    return int(year), _MONTH_LOOKUP[prefix]


def amfi_workbook_coverage(index: pd.DataFrame) -> Dict[str, Any]:
    """The numeric-coverage summary of a parsed monthly index.

    Exists so the 2018-onward boundary is a value a caller can print, rather than something they
    have to know about AMFI's publication history.
    """
    if not isinstance(index, pd.DataFrame) or "HasWorkbook" not in index.columns:
        raise ValueError("expected a frame from parse_amfi_monthly_index")
    with_workbook = int(index["HasWorkbook"].sum())
    total = int(len(index))
    return {
        "bfinance_month_count": total,
        "bfinance_months_with_workbook": with_workbook,
        "bfinance_months_pdf_only": total - with_workbook,
        "bfinance_workbook_coverage_start": index.attrs.get("bfinance_workbook_coverage_start"),
        "bfinance_index_coverage_start": index.attrs.get("bfinance_index_coverage_start"),
    }


class MutualFundClient:
    """AMFI mutual-fund data, with the 1.5 MB download cached rather than repeated.

    A call for a scheme's NAV series or the category picture would otherwise re-download the
    whole monthly file every time, so the payload goes through
    :class:`~bfinance.cache.sqlite_cache.SQLiteCache` under the existing TTL discipline:
    ``ttl_hours`` is passed **explicitly at every call site**, never inherited from the cache's
    construction default, so no payload's age is an accident of how the cache was constructed.

    Freshness is published rather than inferred — ``attrs["bfinance_cache"]`` is ``"hit"`` or
    ``"miss"``, with ``bfinance_fetched_utc`` and ``bfinance_age_seconds`` beside it.

    Args:
        transport: A :data:`Transport`; defaults to a real ``httpx`` GET.
        cache: A :class:`~bfinance.cache.sqlite_cache.SQLiteCache`, or ``None`` to disable
            caching entirely (every call then fetches, and ``attrs["bfinance_cache"]`` says
            ``"disabled"``).
        ttl_hours: Default TTL for payloads. 24 h: AMFI publishes monthly, so a day's freshness
            buys nothing and a stale-tolerant month does.
        timeout: Seconds, forwarded to the default transport only.
    """

    #: AMFI monthly data is published once a month.
    DEFAULT_TTL_HOURS = 24.0

    CATEGORY_TTL_HOURS = {"amfi_monthly_index": 24.0, "amfi_monthly_report": 24.0}

    def __init__(
        self,
        transport: Optional[Transport] = None,
        *,
        cache: Optional[Any] = None,
        ttl_hours: Optional[float] = None,
        timeout: float = 60.0,
    ) -> None:
        self.transport = transport or _default_transport(timeout)
        self.cache = cache
        self.ttl_hours = self.DEFAULT_TTL_HOURS if ttl_hours is None else float(ttl_hours)

    # ------------------------------------------------------------ internals

    def _ttl_for(self, category: str) -> float:
        # Explicit at the call site by construction: DEFAULT_TTL_HOURS would be a second source
        # of truth that can drift from the documented one.
        return float(self.CATEGORY_TTL_HOURS.get(category, self.ttl_hours))

    @staticmethod
    def _cache_key(url: str, category: str) -> str:
        return f"bfinance.flows:{category}:{url}"

    def _fetch_cached(self, url: str, category: str) -> Tuple[bytes, Dict[str, Any]]:
        """Return ``(content, freshness_attrs)``, fetching only when the cache cannot serve it.

        The freshness dict is the observable half of this method and is published on the frame:
        a consumer must be able to tell a served payload from a fetched one without timing a
        request or reading the cache file.
        """
        now = time.time()
        key = self._cache_key(url, category)
        if self.cache is not None:
            lookup = self.cache.get_result(key)
            if lookup.status == "hit" and isinstance(lookup.value, str):
                logger.debug("amfi cache hit %s", key)
                return (
                    lookup.value.encode("utf-8"),
                    {
                        "bfinance_cache": "hit",
                        "bfinance_cache_key": key,
                        "bfinance_cache_ttl_hours": self._ttl_for(category),
                    },
                )
            status = lookup.status
        else:
            status = "disabled"

        result = self.transport(url)
        if not result.ok:
            raise UpstreamServiceError(
                f"AMFI returned HTTP {result.status} for {url}"
                + (" (cached copy was absent or expired)" if status != "disabled" else "")
            )
        if self.cache is not None:
            self.cache.set(
                key,
                result.text(),
                category=category,
                ttl_hours=self._ttl_for(category),
            )
        return (
            result.content,
            {
                "bfinance_cache": "miss",
                "bfinance_cache_key": key,
                "bfinance_cache_ttl_hours": self._ttl_for(category),
                "bfinance_fetched_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                ),
            },
        )

    # -------------------------------------------------------------- public

    def monthly_index(self, *, url: str = "") -> pd.DataFrame:
        """The monthly-report index: which months exist, and which carry numbers.

        Returns a frame from :func:`parse_amfi_monthly_index` carrying ``bfinance_cache``
        and, for a freshly fetched payload, ``bfinance_fetched_utc``.
        """
        target = url or f"{AMFI_BASE_URL}/../amfi-data (monthlyData RSC payload)"
        content, freshness = self._fetch_cached(target, "amfi_monthly_index")
        frame = parse_amfi_monthly_index(content, source_url=target)
        frame.attrs.update(freshness)
        return frame

    def monthly_report(
        self,
        workbook_url: str,
        *,
        month: Optional[Union[str, _date, datetime]] = None,
    ) -> pd.DataFrame:
        """One month's category-level flows, in INR crore.

        Raises:
            MutualFundWorkbookUnavailable: the payload is a BIFF8 ``.xls`` and no reader for it
                is in this project's dependency set. The *index* remains fully available, so a
                caller can still discover which months have numbers. The reason is in the
                message rather than left to be inferred from a traceback.
        """
        content, freshness = self._fetch_cached(workbook_url, "amfi_monthly_report")
        if not content.lstrip()[:1] in (b"{", b"["):
            raise MutualFundWorkbookUnavailable(
                f"AMFI workbook {workbook_url} arrived as "
                f"{content[:8]!r} (BIFF8 .xls). This project's dependency set has no reader for "
                f"that format - openpyxl is OOXML only - so the workbook is not converted here. "
                f"Use monthly_index() to find which months have numeric workbooks, and parse the "
                f"published figures from the AMFI page or the accompanying PDF."
            )
        frame = parse_amfi_monthly_report(content, source_url=workbook_url, month=month)
        frame.attrs.update(freshness)
        return frame

    def coverage(self) -> Dict[str, Any]:
        """Numeric-coverage summary of the live index."""
        return amfi_workbook_coverage(self.monthly_index())


def _default_mutual_fund_client() -> MutualFundClient:
    global _DEFAULT_MUTUAL_FUND_CLIENT
    if _DEFAULT_MUTUAL_FUND_CLIENT is None:
        from bfinance.cache.sqlite_cache import SQLiteCache

        _DEFAULT_MUTUAL_FUND_CLIENT = MutualFundClient(cache=SQLiteCache())
    return _DEFAULT_MUTUAL_FUND_CLIENT


def amfi_monthly_index(*, url: str = "") -> pd.DataFrame:
    """Module shortcut for :meth:`MutualFundClient.monthly_index`."""
    return _default_mutual_fund_client().monthly_index(url=url)


def amfi_monthly_report(
    workbook_url: str, *, month: Optional[Union[str, _date, datetime]] = None
) -> pd.DataFrame:
    """Module shortcut for :meth:`MutualFundClient.monthly_report`."""
    return _default_mutual_fund_client().monthly_report(workbook_url, month=month)
