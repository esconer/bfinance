"""
Indian transaction-cost schedule, date-keyed, transcribed from NSE circulars.

What this replaces
------------------
A hardcoded ``10 bps`` round-trip constant. That constant was wrong by a factor that is not
visible until the charge is decomposed: the real delivery round trip is **22.248 bps**, and the
dominant term is one the old constant never mentioned at all.

The single most important thing to know about this module
--------------------------------------------------------
**STT is 89.9% of the cost, and the exchange charge is 3.4%.** Measured on a Rs. 1 crore
delivery buy followed by a Rs. 1 crore delivery sell under the 2026 schedule::

    STT                10,000 /side      89.90% of the round trip
    stamp duty          1,500   (buy)     6.74%
    exchange + IPFT       307 /side       1.38%
    SEBI turnover          10 /side       0.04%
    GST                    57.06 /side    0.51%
                     ---------
                     22,248.12  =  22.248 bps

So a model that prices *only* the exchange transaction charge sees about **0.6 bps** and is
low by roughly **36x** — the same class of error as the 10 bps constant, one order of magnitude
worse, and it is the error this module exists to make impossible. :func:`quote` cannot be
handed a schedule with the STT term missing without the dominance assertion in the test suite
failing loudly, and the round-trip bps is only ever produced as a *sum of named components*,
never as a literal.

Keying on the total, not on "transaction charges"
-------------------------------------------------
``NSE/FA/73061`` (27-Feb-2026) moved Rs. 306.99 of transaction charge and Rs. 0.01 of IPFT into
a different column. **Both sides of the change total Rs. 307**, for all three segments it covers:
cash Rs. 307, equity futures Rs. 183, equity options Rs. 3,553. The change is cost-neutral, and a
schedule keyed on the *named component* breaks silently on 2026-03-01 — the day the line-item
description changes, the number does not. :class:`SegmentCharge` therefore keys the arithmetic on
``total`` and keeps ``transaction_charges`` / ``ipft`` as **provenance only**; they are recorded,
never summed. ``test_the_2026_03_01_boundary_is_cost_neutral_not_merely_equivalent`` pins that.

Brokerage is a parameter, not a constant
---------------------------------------
At 0.03% per side the same delivery round trip is **29.328 bps**. Brokerage is contractual: no
exchange publishes it, no circular fixes it, and it varies by broker, plan, segment and city. It
is a required-by-explicit-choice argument defaulting to ``0.0`` — never a module constant that a
caller could mistake for a published rate.

Not every number here comes from a circular, and the module says which is which
-----------------------------------------------------------------------------
:data:`EXCHANGE_PUBLISHED` numbers are transcribed out of a stored NSE circular and are
re-parsed from that text on every run. The rest are **statutory or contractual** and are
**not** in either downloaded circular:

======================== ============ ===============================================
component                authority    published by
======================== ============ ===============================================
STT                      exchange     NSE circular (Finance Act rates)
exchange charge + IPFT   exchange     NSE circular
SEBI turnover fee        statutory    SEBI Act 1972 s.15B — **no NSE circular**
stamp duty               statutory    State stamp acts — **no NSE or SEBI circular**
GST                      statutory    CGST/SGST/IGST — **no NSE circular**
brokerage                contractual  your broker — **nothing publishes it**
======================== ============ ===============================================

Every one of those is carried with ``fixture_sourced=False`` and a ``source`` string naming who
*does* publish it, so a caller can always tell an exchange tariff from a statutory levy from a
negotiated number. :func:`provenance` returns that table for a given date and shape.

GST base: brokerage + exchange charge + IPFT + SEBI fee. **Not** on STT and **not** on stamp duty.
The base is computed explicitly and exposed on the quote, so the test asserts the base and not
merely the total that happens to result from it.

Why there is no empirical bps anywhere in this module
-----------------------------------------------------
The obvious cross-check — measure a round trip and compare — is **structurally impossible** with
NSE's bhavcopy, and pretending otherwise would be the single worst thing this module could do.

* The bhavcopy has **no cost, fee or tax column**. All 34 UDiFF cash-market columns were checked.
  The only substring hit for ``stt`` is ``SttlmPric`` — the *settlement price*, a false positive
  that is worth knowing about precisely because it looks like a hit.
* The file is an all-participant quote/consensus aggregate. It has no side and no counterparty, so
  it cannot isolate one portfolio's round trip even in principle.

Accordingly **no "observed bps" is synthesised here.** Every figure is a statutory computation from
named, sourced rates. What the bhavcopy *does* support is the taxable base, and that is a
separate, honest claim: ``TtlTrfVal / TtlTradgVol`` is a true session VWAP lying inside
``[Low, High]`` for 7/7 recorded rows. Turning that into a cost observation would need a side and
a counterparty, and neither exists.

.. warning::
   :mod:`bfinance.nse.bhavcopy` currently maps volume to ``TtlTradgVol`` and carries **no
   turnover-value column at all**, so the VWAP above cannot be computed through this package yet.
   See :func:`taxable_base_requires_turnover_value`.

Schedule coverage, and what a date outside it does
-------------------------------------------------
The schedule answers dates in ``[2023-04-01, ...)`` and **raises**
:class:`CostScheduleCoverageError` outside it. A caller asking about 2014 gets an error naming
the covered window; it does **not** get 2023's rates clamped forward. Silence here is a
confidently wrong number, which is worse than a failure.

The floor is ``2023-04-01`` for a measured reason: it is the earliest effective date any
downloaded circular establishes (``NSE/FA/56129``, dated 2023-03-24, whose change the 2026
circular dates by reference to "the level levied prior to April 1, 2023"). The pre-2023 rate for
cash transaction charge and IPFT *is* derivable — ``NSE/FA/73061`` states the deltas — and it is
recorded as :data:`DERIVED_EXCHANGE_REGIMES`, but the derivation is labelled ``derived`` rather
than presented as published, and the schedule still refuses to answer before the floor.

Basis-point denominator — read this before comparing to a broker's number
-----------------------------------------------------------------------
:attr:`CostQuote.round_trip_bps` expresses the **round-trip** cost in basis points of the
**one-way** notional. That is the convention the 22.248 figure uses, and it is why a 1-crore buy
followed by a 1-crore sell (2 crores of total turnover) shows 22.248 rather than 11.124.
:attr:`CostQuote.round_trip_bps_on_turnover` gives the two-way convention, and
:attr:`CostQuote.one_way_bps` the per-leg figure. All three are always present so no caller has
to guess which one it is reading.

Sources
-------
* ``NSE/FATAX/73524``, Circular Ref. ``02/2026``, 31-Mar-2026, 358,718 B — STT rates, Finance
  Act 2026, w.e.f. 2026-04-01. Cites its own lineage: ``32385/2016-05-16``,
  ``38737/2018-08-30``, ``41263/2019-06-07``, ``41919/2019-08-20``, ``43604/2020-02-19``,
  ``56235/2023-04-01``, ``63809/2024-09-09``.
* ``NSE/FA/73061``, Circular Ref. ``1``, 27-Feb-2026, 163,365 B — exchange transaction charges and
  NSE IPFT for Cash Market, Equity futures and Equity options, w.e.f. 2026-03-01. Cites
  ``SEBI/HO/MRD/TPD-1/P/CIR/2024/92`` (2024-07-01, uniform MII charges) and predecessors
  ``NSE/FA/56129`` (2023-03-24), ``NSE/FA/64323`` (2024-09-27).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from bfinance.utils.exceptions import BFinanceError, ParsingError

logger = logging.getLogger("bfinance")

#: Provenance tag stamped on every quote this module produces.
PROVENANCE = "nse_transaction_cost_schedule"

#: One crore of rupees — the unit every exchange charge is published in.
CRORE = 10_000_000.0

#: Basis points in one unit fraction, i.e. ``bps = cost / notional * 10_000``.
BPS_PER_UNIT = 10_000.0

# ------------------------------------------------------------------ authority vocabulary
#
# The whole point of separating these is that a reader must be able to tell a published tariff
# from a statutory levy from a negotiated number. Every :class:`LineItem` carries exactly one.

#: Published by an exchange circular, and transcribed out of one.
EXCHANGE_PUBLISHED = "exchange_published"
#: Fixed by statute (SEBI Act, state stamp acts, CGST/SGST/IGST). No exchange circular states it.
STATUTORY = "statutory"
#: Agreed with a counterparty. Nothing publishes it.
CONTRACTUAL = "contractual"

#: Ordering used when reporting provenance, so the output is deterministic.
AUTHORITIES: Tuple[str, ...] = (EXCHANGE_PUBLISHED, STATUTORY, CONTRACTUAL)

#: Component names. These are the keys of every cost breakdown this module returns.
STT = "stt"
EXCHANGE_CHARGE = "exchange_charge"
IPFT = "ipft"
SEBI_TURNOVER = "sebi_turnover"
STAMP_DUTY = "stamp_duty"
GST = "gst"
BROKERAGE = "brokerage"

#: Every component, in the order a statement of account conventionally lists them.
COMPONENTS: Tuple[str, ...] = (
    STT, STAMP_DUTY, EXCHANGE_CHARGE, IPFT, SEBI_TURNOVER, GST, BROKERAGE,
)

#: Rupees per crore — the unit ``EXCHANGE_CHARGE``/``IPFT`` are published in.
RATE_UNIT = "Rs per crore of traded value, each side"


class CostScheduleError(BFinanceError):
    """Base class for this module's failures."""


class CostScheduleCoverageError(CostScheduleError):
    """A date outside the schedule's evidenced window was asked for.

    Raised instead of clamping to the nearest regime. A caller asking about 2014 must not be
    handed 2023's rates, and a cost model that silently returns a plausible number for an
    unknowable date is worse than one that refuses.
    """


class CostScheduleParseError(CostScheduleError, ParsingError):
    """A circular did not parse into the rate tables this module requires.

    This is an upstream-schema failure (NSE reissued or re-laid-out a circular), not a date
    problem, and it is deliberately a different type so a caller can tell "I asked for a date I
    cannot have" from "the document I am keyed on changed shape".

    Mixes in :class:`~bfinance.utils.exceptions.ParsingError` — the closest
    ``utils.exceptions`` sibling, and the one whose own docstring is this failure almost verbatim
    ("HTML/JSON parsing fails due to unexpected upstream schema changes"). This is the rule
    :mod:`bfinance.statements.errors` and :mod:`bfinance.sources.errors` both state, *keep the
    package base in the package, mix in the closest ``utils.exceptions`` sibling*, and this file
    was the one place in the package that took ``BFinanceError`` and stopped there. The effect is
    that a caller already writing ``except ParsingError`` around a cost fetch — the handler
    :mod:`bfinance.statements.errors` documents as the one that must keep working — now catches
    a re-laid-out NSE circular, and learns it is a schema change rather than a bad date.

    :class:`~bfinance.utils.exceptions.UpstreamServiceError` is deliberately **not** mixed in. Its
    own docstring scopes it to a provider that "fails or is unreachable", and a re-issued
    circular is neither: the exchange answered, with a document in a new shape. Folding it into
    the retry-worthy bucket is how a permanent failure gets retried forever, which is the exact
    conflation :class:`bfinance.sources.errors.SourceBasisError` and
    :class:`bfinance.sources.errors.BasisMismatchError` exist to prevent.
    """


# ------------------------------------------------------------------ transaction shapes
#
# Every shape names the STT row it reads and the exchange-charge segment it lands in. The two are
# independent: an option is taxed by row 4(a)/4(b)/4(c) of the STT table but is charged the
# *equity options* exchange fee, and that fee is on **premium**, not traded value.

DELIVERY_BUY = "delivery_buy"
DELIVERY_SELL = "delivery_sell"
INTRADAY_BUY = "intraday_buy"
INTRADAY_SELL = "intraday_sell"
EOF_UNIT_SELL = "eof_unit_sell"
OPTION_SALE = "option_sale"
OPTION_EXERCISED = "option_exercised"
FUTURES_SALE = "futures_sale"

CASH_MARKET = "cash_market"
EQUITY_FUTURES = "equity_futures"
EQUITY_OPTIONS = "equity_options"

#: Exchange charges are levied on traded value ...
BASE_TRADED_VALUE = "traded_value"
#: ... except equity options, where the circular's own column heading says "on premium".
BASE_PREMIUM = "premium"

SHAPE_KEYS: Tuple[str, ...] = (
    DELIVERY_BUY, DELIVERY_SELL, INTRADAY_BUY, INTRADAY_SELL,
    EOF_UNIT_SELL, OPTION_SALE, OPTION_EXERCISED, FUTURES_SALE,
)


@dataclass(frozen=True)
class TransactionShape:
    """One taxable leg: which STT row taxes it, which exchange segment charges it, and on what base.

    Attributes:
        key: The public shape name, e.g. :data:`DELIVERY_BUY`.
        segment: Exchange-charge segment — :data:`CASH_MARKET`, :data:`EQUITY_FUTURES` or
            :data:`EQUITY_OPTIONS`.
        exchange_base: What the Rs-per-crore charge applies to. :data:`BASE_PREMIUM` for options,
            which is why :func:`round_trip_cost` refuses to price an option without a premium.
        stt_row: The row number in the STT circular's table, as a string. ``None`` where the
            circular taxes no purchase at all (:data:`INTRADAY_BUY`).
        stt_pct_key: Which transcribed rate drives this shape's STT.
        stamp_duty_applies: Stamp duty is a levy on the *transfer of a scrip in delivery*, so it
            reaches delivery and intraday purchases and no other shape here.
        description: The taxable-transaction wording transcribed from the circular.
    """

    key: str
    segment: str
    exchange_base: str
    stt_pct_key: Optional[str]
    stt_row: Optional[str]
    stamp_duty_applies: bool
    description: str


#: The eight shapes, each bound to the STT circular row that taxes it.
#:
#: :data:`INTRADAY_BUY` carries ``stt_row=None`` because the circular's row 3 is worded "**Sale**
#: of an equity share ... settled otherwise than by the actual delivery" — it taxes the sale leg
#: at 0.025% and states no purchase rate. The zero is a transcription of an absent row, not a
#: guess, and ``stt_pct_key`` is ``None`` so the schedule can say so out loud.
SHAPES: Dict[str, TransactionShape] = {
    DELIVERY_BUY: TransactionShape(
        DELIVERY_BUY, CASH_MARKET, BASE_TRADED_VALUE, "delivery_purchase", "1", True,
        "Purchase of an equity share in a company or a unit of a business trust, settled by the "
        "actual delivery or transfer of such share or unit",
    ),
    DELIVERY_SELL: TransactionShape(
        DELIVERY_SELL, CASH_MARKET, BASE_TRADED_VALUE, "delivery_sale", "2", False,
        "Sale of an equity share in a company or a unit of a business trust, settled by the "
        "actual delivery or transfer of such share or unit",
    ),
    EOF_UNIT_SELL: TransactionShape(
        EOF_UNIT_SELL, CASH_MARKET, BASE_TRADED_VALUE, "eof_unit_sale", "2A", False,
        "Sale of a unit of an equity oriented fund, settled by the actual delivery or transfer "
        "of such unit",
    ),
    INTRADAY_BUY: TransactionShape(
        INTRADAY_BUY, CASH_MARKET, BASE_TRADED_VALUE, None, None, True,
        "Purchase settled otherwise than by actual delivery — the circular states no purchase "
        "rate for this leg (row 3 taxes the sale only)",
    ),
    INTRADAY_SELL: TransactionShape(
        INTRADAY_SELL, CASH_MARKET, BASE_TRADED_VALUE, "intraday_sale", "3", False,
        "Sale of an equity share, a unit of an equity oriented fund or a unit of a business "
        "trust, settled otherwise than by the actual delivery or transfer",
    ),
    OPTION_SALE: TransactionShape(
        OPTION_SALE, EQUITY_OPTIONS, BASE_PREMIUM, "option_sale", "4(a)", False,
        "Sale of an option in securities",
    ),
    OPTION_EXERCISED: TransactionShape(
        OPTION_EXERCISED, EQUITY_OPTIONS, BASE_PREMIUM, "option_exercised", "4(b)", False,
        "Sale of an option in securities, where option is exercised",
    ),
    FUTURES_SALE: TransactionShape(
        FUTURES_SALE, EQUITY_FUTURES, BASE_TRADED_VALUE, "futures_sale", "4(c)", False,
        "Sale of a futures in securities",
    ),
}


def validate_shape(shape: Union[str, TransactionShape]) -> TransactionShape:
    """Resolve a shape name to its :class:`TransactionShape` before any arithmetic runs.

    Raised locally, not clamped: an unrecognised shape would otherwise fall through to a
    zero-cost quote, which is the most expensive kind of silence this module could have.
    """
    if isinstance(shape, TransactionShape):
        return shape
    if not isinstance(shape, str):
        raise ValueError(
            f"shape must be a string or TransactionShape, got {type(shape).__name__}"
        )
    clean = shape.strip().lower()
    if clean not in SHAPES:
        raise ValueError(
            f"shape={shape!r} is not a known transaction shape. Must be one of "
            f"{', '.join(SHAPE_KEYS)}."
        )
    return SHAPES[clean]


# ------------------------------------------------------------------ circular parsing
#
# The two circulars are read as **text** and the rate tables are parsed out of them on every
# run. Nothing here is a hand-typed dict of numbers: a fixture whose rate is edited changes the
# computed bps, which is what makes the negative controls in the test suite mean something.

#: Rupees-per-crore columns. pypdf emits ``Rs. 1 82.99`` and ``Rs. 3 ,503`` — a space *inside* the
#: numeral — so the capture is deliberately permissive and cleaned afterwards. A quantified
#: group would capture only its last repetition, so all three columns per regime are written out.
_NUM = r"((?:[\d,.]|\s)+?)\s*each\s+side"
_SEGMENT_BLOCK = (
    r"(Cash\s+Market|Equity\s+futures|Equity\s+Options)\s*"
    r"(?:\(\s*on\s+premium\s*\))?\s*"
    r"Rs\.\s*" + _NUM + r"\s*"
    r"Rs\.\s*" + _NUM + r"\s*"
    r"Rs\.\s*" + _NUM + r"\s*"
    r"Rs\.\s*" + _NUM + r"\s*"
    r"Rs\.\s*" + _NUM + r"\s*"
    r"Rs\.\s*" + _NUM
)
_SEGMENT_RE = re.compile(_SEGMENT_BLOCK)

#: Segment label in the circular -> the segment key this module uses.
_SEGMENT_ALIAS = {
    "Cash Market": CASH_MARKET,
    "Equity futures": EQUITY_FUTURES,
    "Equity Options": EQUITY_OPTIONS,
}

_REF_RE = re.compile(r"Download Ref No:\s*(NSE/(?:FA|FATAX)/\d+)\s*Date:\s*([A-Za-z]+ \d+, \d+)")
#: The circular reference, which is **not** the download reference and not always the same shape:
#: NSE/FATAX/73524 reads ``02/2026`` and NSE/FA/73061 reads ``1``. The colon form is the header's
#: own; the period form (``Circular Ref. No. 2/2016``) belongs to the lineage paragraph, so only
#: the colon is read here and the lineage is read from its ``NSE/FATAX/nnnnn`` refs instead.
_CIRC_REF_RE = re.compile(r"Circular Ref\. No:\s*([\w/]+)")
_EFFECTIVE_RE = re.compile(r"applicable with effect from ([A-Za-z]+ \d+, \d+)")
_SEBI_CITE_RE = re.compile(
    r"(SEBI/HO/MRD/TPD\s*-?\s*1/P/CIR/\d+/\d+)\s*dated\s*([A-Za-z]+ \d+, \d+)"
)
_PREDECESSOR_RE = re.compile(r"(NSE/FA/\d+)\s*dated\s*([A-Za-z]+ \d+, \d+)")
_PRE2023_RE = re.compile(r"prior to ([A-Za-z]+ \d+, \d+)")
#: The NSE/FA/56129 deltas, the only place the pre-2023-04-01 level is stated -- as differences,
#: not as a rate. Two quirks of the PDF's text layer are absorbed here rather than by a looser
#: match that might catch the wrong number:
#:
#: * ``c\s*rore`` -- the layer breaks the word mid-way ("per c rore of traded value") in one
#:   sentence and not the next.
#: * the amounts are separated from the segment names by the base they apply to ("Rs 20 per crore
#:   of traded value , Equity Futures"), not by a bare comma, so the gap is matched as prose.
#:
#: Anything that fails to match leaves ``prior`` empty, which the tests assert against, so a
#: future upstream re-layout cannot quietly delete the reconstruction.
#: The three transaction-charge cuts, in one sentence: "reduced its transaction charges for cash
#: market segment by Rs 20 per crore of traded value , Equity Futures by Rs. 10 ... and for Equity
#: options by Rs. 300 ...". One pattern, because it is one sentence -- splitting it would let a
#: cut be matched from an unrelated sentence.
_TXN_CUT_RE = re.compile(
    r"reduced its transaction charges for cash market segment by (Rs ?[\d.]+)"
    r"[^;]*?Equity Futures by (Rs\.? ?[\d.]+)"
    r"[^;]*?and for Equity options by (Rs\.? ?[\d.]+)",
    re.S,
)
#: The two IPFT cuts, in the following sentence: "...by Rs. 9.99 per crore of traded value for Cash
#: Market Segment & Equity Futures and by Rs. 49.99 per crore of traded premium value for Equity
#: Options." Cash and futures **share** the first cut, which is why the routing below names the
#: index per segment rather than zipping positionally.
_IPFT_CUT_RE = re.compile(
    r"corpus of NSE IPFT by (Rs\.? ?[\d.]+) per c\s*rore of traded value"
    r" for Cash Market Segment & Equity Futures"
    r"[^;]*?and by (Rs\. ?[\d.]+) per c\s*rore of traded premium value for Equity Options",
    re.S,
)
#: Which cut feeds which segment's reconstruction: ``(segment, txn-cut index, IPFT-cut index)``,
#: 1-based over the concatenated ``(txn, txn, txn, ipft, ipft)`` tuple. Cash and futures share the
#: first IPFT cut -- the circular states it as "Cash Market Segment & Equity Futures" in one clause
#: -- so indices 1, 2, 3 for the transaction charge map to 1, 1, 2 for IPFT.
_DELTA_CUT_ORDER = (
    (CASH_MARKET, 1, 4), (EQUITY_FUTURES, 2, 4), (EQUITY_OPTIONS, 3, 5),
)

#: The STT revision sentences. The ``%`` here (not "per cent") is what distinguishes the prose
#: revision list from the table below it. pypdf mangles the en-dash into U+FFFD, so the separator
#: is matched as "one to three non-digit characters" rather than as a literal dash.
_STT_REVISION_RE = re.compile(
    r"\((?:a|b|c)\)\s*(Sale of [a-z ]+?(?:,\s*where option is exercised)?)\s*,?\s*"
    r"has been revised to\s+([\d.]+)\s*%\s*\(\s*upto March 31,\s*2026\s*\S{1,3}\s*([\d.]+)\s*%\)"
)

#: A delivery / intraday row of the STT table. Anchoring the description on
#: ``Purchase of``/``Sale of`` is what keeps ``Circular Ref. No. 2/2024`` in the lineage
#: paragraph from being read as table row "2024".
_STT_ROW_RE = re.compile(
    r"(\d+[A-Z]?)\.?\s+((?:Purchase|Sale) of [^0-9]*?)"
    r"([\d.]+)\s*per\s*cent\s*\(No\s*Change\)\s*([\d.]+)\s*per\s*cent\s*"
    r"(Purchaser|Seller)\s*\(No\s*Change\)"
)
_STT_LINEAGE_RE = re.compile(r"NSE/FATAX/(\d+)\s*dated\s*([A-Za-z]+ \d+, \d+)")
_STT_ASSENT_RE = re.compile(r"Presidential assent on ([A-Za-z]+ \d+, \d+)")
_STT_EFFECTIVE_RE = re.compile(r"on or after ([A-Za-z]+ \d+, \d{4}), shall be charged")
_STT_ACT_RE = re.compile(r"The ([A-Za-z ]+Act),\s*(\d{4})")
_STT_SUBJECT_RE = re.compile(r"Sub:\s*(.+?)\s+This has reference")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_upstream_date(text: Optional[str]) -> Optional[date]:
    """Parse NSE's ``March 31, 2026`` into a :class:`datetime.date`.

    Returns ``None`` rather than guessing when the month name is unknown — a date this module
    invents would become a schedule boundary, and a wrong boundary is a silent mispricing.
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", str(text)).strip().rstrip(".,")
    match = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", cleaned)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).strip().lower())
    if month is None:
        return None
    return date(int(match.group(3)), month, int(match.group(2)))


def _money(groups: Sequence[Optional[str]]) -> List[float]:
    """Turn the permissive ``Rs.`` captures into floats, dropping separators pypdf inserted."""
    out: List[float] = []
    for raw in groups:
        if raw is None:
            out.append(float("nan"))
            continue
        out.append(float(re.sub(r"[,\s]", "", raw)))
    return out


@dataclass(frozen=True)
class Circular:
    """The identity of one circular, as transcribed from its own header.

    ``ref`` and ``circular_ref`` are both kept because they are not the same thing:
    ``NSE/FA/73061`` is the download reference, ``1`` the circular reference, and only the second
    appears in a subject line. Losing either one makes the citation ambiguous.
    """

    ref: str
    circular_ref: str
    issued: date
    subject: str
    url: str
    original_bytes: int
    original_sha256_16: str

    def citation(self) -> str:
        return f"{self.ref} (Circular Ref. {self.circular_ref}, {self.issued.isoformat()})"


#: Where each circular lives. ``nsearchives`` serves both with no cookie prime.
CIRCULAR_URLS: Dict[str, str] = {
    "NSE/FATAX/73524": "https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf",
    "NSE/FA/73061": "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
}

#: Bytes and digest prefix of each original, so a fixture can be checked against the real thing.
CIRCULAR_ORIGINALS: Dict[str, Tuple[int, str]] = {
    "NSE/FATAX/73524": (358718, "f4cbd1f917f88dcb"),
    "NSE/FA/73061": (163365, "6085601a26177ed6"),
}


@dataclass(frozen=True)
class SegmentCharge:
    """One segment's exchange charge, **keyed on the total**.

    The 2026-03-01 change moved Rs. 306.99 of transaction charge and Rs. 0.01 of IPFT into
    different columns while leaving the total at Rs. 307. ``total`` is therefore the number the
    arithmetic uses; ``transaction_charges`` and ``ipft`` are provenance, and no code path sums
    them. That is the whole reason a schedule keyed on the *named component* would break
    silently on that date while this one does not.
    """

    segment: str
    total: float
    transaction_charges: float
    ipft: float
    unit: str = RATE_UNIT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment": self.segment,
            "total_rs_per_crore_each_side": self.total,
            "component_split_provenance_only": {
                "transaction_charges": self.transaction_charges,
                "ipft": self.ipft,
            },
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ExchangeChargeRegime:
    """Exchange charge + IPFT for every segment over one date window."""

    effective_from: date
    effective_to: Optional[date]
    segments: Dict[str, SegmentCharge]
    source: str
    derived: bool
    why: str

    def for_segment(self, segment: str) -> SegmentCharge:
        try:
            return self.segments[segment]
        except KeyError:  # pragma: no cover - guarded by validate_shape
            raise ValueError(
                f"segment={segment!r} is not in this regime. It covers "
                f"{', '.join(sorted(self.segments))}."
            ) from None

    def total_for(self, segment: str) -> float:
        return self.for_segment(segment).total


@dataclass(frozen=True)
class SttRate:
    """One STT rate with the party that pays it, both sides of the change."""

    pct: float
    payer: str
    previous_pct: Optional[float]
    row: str
    marked_no_change: bool


@dataclass(frozen=True)
class SttRegime:
    """STT rates for every shape over one date window."""

    effective_from: date
    effective_to: Optional[date]
    rates: Dict[str, SttRate]
    source: str
    why: str

    def for_key(self, pct_key: Optional[str]) -> SttRate:
        """The rate for a shape. ``None`` means the circular states no such rate.

        :data:`INTRADAY_BUY` reaches this with ``pct_key=None``, and the returned zero carries
        ``row=None`` and ``marked_no_change=False`` so a caller can see it is an absent row
        rather than a published nil rate.
        """
        if pct_key is None:
            return SttRate(0.0, "none", None, "", False)
        try:
            return self.rates[pct_key]
        except KeyError:  # pragma: no cover - a parser/schema failure
            raise CostScheduleParseError(
                f"STT regime from {self.effective_from} has no rate for {pct_key!r}. "
                f"It carries {sorted(self.rates)}. The circular's table shape changed."
            ) from None


# ------------------------------------------------------------------ declared (not transcribed)
#
# These four are NOT in either downloaded circular and are NOT published by any exchange. They
# are module constants so a caller gets a working number, and every one of them is flagged
# ``fixture_sourced=False`` with the authority that actually fixes it. They are overridable per
# call, and :func:`provenance` reports them as statutory/contractual rather than exchange rates.

#: SEBI Act 1972 s.15B turnover fee. Levied on turnover, so it scales with the notional.
SEBI_TURNOVER_PCT = 0.0001  # 0.0001% -> 10 rupees per crore
#: Statutory annual ceiling on the SEBI turnover fee, in rupees.
SEBI_TURNOVER_CAP_RS = 100_000_000.0  # Rs. 10 crore per year
#: Stamp duty on a scrip transfer in delivery. Buy side only. 0.015% from 2020-07-20.
STAMP_DUTY_PCT = 0.015
STAMP_DUTY_EFFECTIVE_FROM = date(2020, 7, 20)
STAMP_DUTY_PREVIOUS_PCT = 0.018
#: GST on brokerage + exchange charge + IPFT + SEBI fee. **Not** on STT, **not** on stamp duty.
GST_PCT = 18.0

#: What fixes each declared rate, and whether an exchange circular states it.
DECLARED_SOURCES: Dict[str, Tuple[str, str, bool]] = {
    STT: ("exchange_published", "NSE circular implementing the Finance Act", True),
    EXCHANGE_CHARGE: ("exchange_published", "NSE circular (transaction charges)", True),
    IPFT: ("exchange_published", "NSE circular (NSE IPFT corpus contribution)", True),
    SEBI_TURNOVER: (
        "statutory",
        "SEBI Act 1972 s.15B — no NSE circular publishes this rate",
        False,
    ),
    STAMP_DUTY: (
        "statutory",
        "State stamp acts — no NSE or SEBI circular exists for this; it is a state levy",
        False,
    ),
    GST: (
        "statutory",
        "CGST + SGST + IGST on brokerage and exchange/sebi/ipft charges — no NSE circular",
        False,
    ),
    BROKERAGE: (
        "contractual",
        "agreed with your broker; not published by any exchange and not fixed by any statute",
        False,
    ),
}


# ------------------------------------------------------------------ parsers


def parse_stt_circular(text: str, *, url: str = "", original_bytes: int = 0,
                       original_sha256_16: str = "") -> "SttCircularData":
    """Parse the STT circular's two rate tables out of its text.

    Two independent reads, deliberately:

    * the **revision sentences** (a)/(b)/(c) give option, exercised-option and futures rates for
      both the new and the previous regime; and
    * the **table** gives the delivery, equity-oriented-fund and intraday rows, each explicitly
      marked ``(No Change)``, together with the party that pays.

    A round trip on the delivery leg has no revision sentence at all — it is unchanged — so
    without the table the largest STT component of a normal equity trade would be missing. The
    ``(No Change)`` marker is itself transcribed, not assumed: a schedule that carried the old
    rate forward as if it had been re-published would be claiming something the circular does
    not say.
    """
    flat = re.sub(r"\s+", " ", text)
    ref_match = _REF_RE.search(flat)
    circ_match = _CIRC_REF_RE.search(flat)
    if not ref_match or not circ_match:
        raise CostScheduleParseError(
            "STT circular header did not parse: expected 'Download Ref No: NSE/FATAX/nnnnn "
            f"Date: <date>' and 'Circular Ref. No: nn/nnnn'. First 200 chars: {flat[:200]!r}"
        )
    issued = parse_upstream_date(ref_match.group(2))
    effective_match = _STT_EFFECTIVE_RE.search(flat)
    effective = parse_upstream_date(effective_match.group(1)) if effective_match else None
    if issued is None or effective is None:
        raise CostScheduleParseError(
            f"STT circular dates did not parse: issued={ref_match.group(2)!r} "
            f"effective={effective_match.group(1) if effective_match else None!r}"
        )
    act = _STT_ACT_RE.search(flat)
    # The circular's own column heading is "Rate upto March 31, 2026", so the previous regime ends
    # the day before the new one starts. That literal is transcribed rather than inferred from the
    # year, because deriving it by subtracting a year (a natural mistake) gives 2025-04-01 and
    # leaves a year-long hole in the schedule.
    previous_day_match = re.search(
        r"Rate\s+upto\s+([A-Za-z]+ \d+,\s*\d{4})", flat
    )
    previous_effective = (
        parse_upstream_date(previous_day_match.group(1)) if previous_day_match else None
    )
    if previous_effective is None or previous_effective >= effective:
        raise CostScheduleParseError(
            f"Could not read the STT circular's 'Rate upto <date>' column heading; the previous "
            f"regime's end date is transcribed from it rather than inferred. Found "
            f"{previous_day_match.group(1) if previous_day_match else None!r}, new rates from "
            f"{effective.isoformat()}."
        )

    # --- new regime, from the revision sentences
    revisions: Dict[str, Tuple[float, float]] = {}
    for match in _STT_REVISION_RE.finditer(flat):
        wording = match.group(1)
        new_pct, old_pct = float(match.group(2)), float(match.group(3))
        if "where option is exercised" in wording:
            key = "option_exercised"
        elif "an option" in wording:
            key = "option_sale"
        elif "futures" in wording:
            key = "futures_sale"
        else:  # pragma: no cover - the circular has exactly three
            raise CostScheduleParseError(
                f"Unrecognised STT revision sentence {wording!r}; the circular's shape changed."
            )
        revisions[key] = (new_pct, old_pct)

    if len(revisions) != 3:
        raise CostScheduleParseError(
            f"Expected 3 STT revision sentences (option, exercised option, futures) in "
            f"NSE/FATAX/73524, parsed {sorted(revisions)}. Refusing to guess the rest."
        )

    # --- unchanged rows, from the table
    unchanged: Dict[str, SttRate] = {}
    row_to_key = {
        "1": ("delivery_purchase", "Purchaser"),
        "2": ("delivery_sale", "Seller"),
        "2A": ("eof_unit_sale", "Seller"),
        "3": ("intraday_sale", "Seller"),
    }
    for match in _STT_ROW_RE.finditer(flat):
        row = match.group(1)
        if row not in row_to_key:
            continue
        key, payer = row_to_key[row]
        new_pct, old_pct = float(match.group(3)), float(match.group(4))
        unchanged[key] = SttRate(new_pct, payer, old_pct, row, True)
        if new_pct != old_pct:
            # A row marked "(No Change)" whose two columns disagree is a parse failure, and
            # shipping it would put a different rate in each regime for no reason.
            raise CostScheduleParseError(
                f"STT row {row} is marked '(No Change)' but its columns read "
                f"{new_pct} and {old_pct}. The '(No Change)' marker is load-bearing here."
            )

    if len(unchanged) != 4:
        raise CostScheduleParseError(
            f"Expected 4 '(No Change)' STT rows (1, 2, 2A, 3) in NSE/FATAX/73524, parsed "
            f"{sorted(unchanged)}. The delivery and intraday rates are the bulk of a normal "
            f"equity round trip and are not guessed."
        )

    # --- cross-check: the table's own six numbers for row 4 must match the revision sentences.
    row4 = flat[flat.find("4. (a) Sale of an option"): flat.find("This is for information")]
    row4_numbers = [float(n) for n in re.findall(r"([\d.]+)\s*per\s*cent", row4)]
    expected = [revisions["option_sale"][0], revisions["option_exercised"][0],
                revisions["futures_sale"][0], revisions["option_sale"][1],
                revisions["option_exercised"][1], revisions["futures_sale"][1]]
    if row4_numbers != expected:
        raise CostScheduleParseError(
            f"STT row 4 table numbers {row4_numbers} disagree with the revision sentences "
            f"{expected}. Two independent reads of one circular must not differ."
        )
    row4_parties = re.findall(r"(Purchaser|Seller)\s*\(No\s*Change\)", row4)
    if row4_parties != ["Seller", "Purchaser", "Seller"]:
        raise CostScheduleParseError(
            f"STT row 4 parties parsed {row4_parties}, expected "
            f"['Seller', 'Purchaser', 'Seller']: an exercised option is taxed on the Purchaser."
        )

    new_rates = dict(unchanged)
    for key, (new_pct, old_pct) in revisions.items():
        payer = "Purchaser" if key == "option_exercised" else "Seller"
        new_rates[key] = SttRate(new_pct, payer, old_pct, "4", False)
    previous_rates = dict(unchanged)
    for key, (new_pct, old_pct) in revisions.items():
        payer = "Purchaser" if key == "option_exercised" else "Seller"
        previous_rates[key] = SttRate(old_pct, payer, None, "4", False)

    assent_match = _STT_ASSENT_RE.search(flat)
    subject = _STT_SUBJECT_RE.search(flat)
    circular = Circular(
        ref=ref_match.group(1),
        circular_ref=circ_match.group(1),
        issued=issued,
        subject=subject.group(1) if subject else "",
        url=url or CIRCULAR_URLS.get(ref_match.group(1), ""),
        original_bytes=original_bytes or CIRCULAR_ORIGINALS.get(ref_match.group(1), (0, ""))[0],
        original_sha256_16=original_sha256_16
        or CIRCULAR_ORIGINALS.get(ref_match.group(1), (0, ""))[1],
    )
    return SttCircularData(
        circular=circular,
        effective_from=effective,
        effective_to=None,
        previous_effective_from=None,
        previous_effective_to=previous_effective,
        new_rates=new_rates,
        previous_rates=previous_rates,
        lineage=[
            (f"NSE/FATAX/{ref}", parse_upstream_date(when))
            for ref, when in _STT_LINEAGE_RE.findall(flat)
        ],
        statute=(f"{act.group(1).strip()} {act.group(2)}" if act else ""),
        assent_date=(
            parse_upstream_date(assent_match.group(1)) if assent_match else None
        ),
    )


@dataclass(frozen=True)
class SttCircularData:
    """Everything transcribed from the STT circular."""

    circular: Circular
    effective_from: date
    effective_to: Optional[date]
    previous_effective_from: Optional[date]
    previous_effective_to: Optional[date]
    new_rates: Dict[str, SttRate]
    previous_rates: Dict[str, SttRate]
    lineage: List[Tuple[str, Optional[date]]]
    statute: str
    assent_date: Optional[date]


def parse_exchange_charge_circular(
    text: str, *, url: str = "", original_bytes: int = 0, original_sha256_16: str = ""
) -> "ExchangeChargeCircularData":
    """Parse the transaction-charge circular's three-segment table.

    Reads, per segment, both the **existing** and the **revised** outflow as
    ``(transaction charge, IPFT contribution, total)``, and checks each triple adds up. The
    arithmetic identity is the load-bearing part: the 2026-03-01 change is *cost-neutral* by
    construction, and a table whose revised total did not equal the revised components would mean
    the extraction, not the tariff, had moved.

    The pre-2023 regime is **derived** from the deltas the same circular states ("reduced ... by
    Rs 20 per crore", "enhanced ... by Rs 9.99 per crore"), and is labelled ``derived=True`` so it
    can never be mistaken for a published rate.
    """
    flat = re.sub(r"\s+", " ", text)
    ref_match = _REF_RE.search(flat)
    circ_match = _CIRC_REF_RE.search(flat)
    eff_match = _EFFECTIVE_RE.search(flat)
    if not ref_match or not circ_match or not eff_match:
        raise CostScheduleParseError(
            "Exchange-charge circular header did not parse: expected 'Download Ref No: "
            "NSE/FA/nnnnn Date: <date>', 'Circular Ref. No: n' and 'applicable with effect "
            f"from <date>'. First 200 chars: {flat[:200]!r}"
        )
    effective = parse_upstream_date(eff_match.group(1))
    issued = parse_upstream_date(ref_match.group(2))
    if effective is None or issued is None:
        raise CostScheduleParseError(
            f"Exchange-charge circular dates did not parse: issued={ref_match.group(2)!r} "
            f"effective={eff_match.group(1)!r}"
        )

    #: The circular states two triples per segment: the outflow in force when it was issued, and
    #: the revised one. Both are transcribed; neither is inferred from the other.
    existing: Dict[str, SegmentCharge] = {}
    revised: Dict[str, SegmentCharge] = {}
    for match in _SEGMENT_RE.finditer(flat):
        label = _SEGMENT_ALIAS.get(match.group(1))
        if label is None:  # pragma: no cover
            raise CostScheduleParseError(f"Unknown segment label {match.group(1)!r}.")
        values = _money(match.groups()[1:])
        old_txn, old_ipft, old_total, new_txn, new_ipft, new_total = values
        if abs(old_txn + old_ipft - old_total) > 1e-6:
            raise CostScheduleParseError(
                f"{label}: existing {old_txn} + {old_ipft} != stated total {old_total}."
            )
        if abs(new_txn + new_ipft - new_total) > 1e-6:
            raise CostScheduleParseError(
                f"{label}: revised {new_txn} + {new_ipft} != stated total {new_total}."
            )
        # Both triples are kept. Discarding the 'existing' one would leave a schedule whose
        # pre-cutover regime quietly carried the *revised* split, which is the same class of
        # silent-wrongness this module exists to remove -- and it would make the cost-neutrality
        # claim untestable, because the two regimes would then be identical objects.
        existing[label] = SegmentCharge(label, old_total, old_txn, old_ipft)
        revised[label] = SegmentCharge(label, new_total, new_txn, new_ipft)

    missing = sorted({CASH_MARKET, EQUITY_FUTURES, EQUITY_OPTIONS} - set(existing))
    if missing:
        raise CostScheduleParseError(
            f"Exchange-charge circular is missing segments {missing}. NSE/FA/73061 covers cash "
            f"market, equity futures and equity options; a missing segment is not a zero charge."
        )

    txn_cuts = _TXN_CUT_RE.search(flat)
    ipft_cuts = _IPFT_CUT_RE.search(flat)
    cuts: Optional[Tuple[float, ...]] = None
    if txn_cuts and ipft_cuts:
        cuts = tuple(
            float(re.sub(r"[,\s]", "", re.sub(r"(?i)\brs\.?\s*", "", group)))
            for group in txn_cuts.groups() + ipft_cuts.groups()
        )
    prior: Dict[str, SegmentCharge] = {}
    cutover_match = _PRE2023_RE.search(flat)
    cutover = parse_upstream_date(cutover_match.group(1)) if cutover_match else None
    if cuts is not None and cutover is not None:
        # Undo the stated NSE/FA/56129 deltas against the EXISTING column, which is the outflow
        # that column describes. This reconstructs the pre-2023-04-01 level, which the circular
        # itself calls "the level levied prior to April 1, 2023".
        def _prior(segment: str, txn_cut: float, ipft_cut: float) -> SegmentCharge:
            # The deltas are the direction NSE moved *on 2023-04-01*: the transaction charge went
            # DOWN by txn_cut and the IPFT contribution went UP by ipft_cut. Reconstructing the
            # prior level therefore reverses both, and the round trip is a useful check --
            # cash 297 - 20 = 277 and 10 - 9.99 = 0.01, giving Rs 277.01, not Rs 317.01. Getting
            # this backwards produces a plausible-looking number, so the signs are written out
            # rather than folded into a single signed delta.
            base = existing[segment]
            txn = round(base.transaction_charges - txn_cut, 10)
            ipft = round(base.ipft - ipft_cut, 10)
            return SegmentCharge(segment, round(txn + ipft, 10), txn, ipft)

        prior = {
            segment: _prior(segment, cuts[txn - 1], cuts[ipft - 1])
            for segment, txn, ipft in _DELTA_CUT_ORDER
        }

    sebi = _SEBI_CITE_RE.search(flat)
    # Order-preserving de-duplication. A circular may name the same predecessor twice -- once in
    # its own reference line and once in the narrative describing the earlier change -- and a
    # list carrying the repeat reads as two distinct predecessors.
    predecessors = list(dict.fromkeys(_PREDECESSOR_RE.findall(flat)))
    circular = Circular(
        ref=ref_match.group(1),
        circular_ref=circ_match.group(1),
        issued=issued,
        subject="Revision in Transaction Charges",
        url=url or CIRCULAR_URLS.get(ref_match.group(1), ""),
        original_bytes=original_bytes or CIRCULAR_ORIGINALS.get(ref_match.group(1), (0, ""))[0],
        original_sha256_16=original_sha256_16
        or CIRCULAR_ORIGINALS.get(ref_match.group(1), (0, ""))[1],
    )
    return ExchangeChargeCircularData(
        circular=circular,
        effective_from=effective,
        effective_to=None,
        existing=existing,
        revised=revised,
        prior=prior,
        prior_effective_to=cutover,
        sebi_directive=(f"{sebi.group(1)} dated {sebi.group(2)}" if sebi else ""),
        predecessors=[(ref, parse_upstream_date(when)) for ref, when in predecessors],
    )


@dataclass(frozen=True)
class ExchangeChargeCircularData:
    """Everything transcribed from the exchange-charge circular."""

    circular: Circular
    effective_from: date
    effective_to: Optional[date]
    #: The outflow in force when the circular was issued -- its "Existing outflow" column.
    existing: Dict[str, SegmentCharge]
    #: The revised outflow -- its "Revised outflow" column, effective from ``effective_from``.
    revised: Dict[str, SegmentCharge]
    #: The pre-2023-04-01 level, reconstructed from the deltas the circular states. Recorded for
    #: audit; it is **not** a servable regime, because it precedes the schedule's evidenced floor.
    prior: Dict[str, SegmentCharge]
    prior_effective_to: Optional[date]
    sebi_directive: str
    predecessors: List[Tuple[str, Optional[date]]]


# ------------------------------------------------------------------ the schedule


@dataclass(frozen=True)
class CostSchedule:
    """A date-keyed, component-keyed cost schedule built from the two circulars.

    Construct with :func:`schedule_from_circulars`. Nothing here reaches the network, and nothing
    here is a hand-typed rate table: both STT and exchange-charge rates arrive as parsed text.
    """

    stt_circular: SttCircularData
    exchange_circular: ExchangeChargeCircularData
    stt_regimes: Tuple[SttRegime, ...]
    exchange_regimes: Tuple[ExchangeChargeRegime, ...]
    floor: date
    ceiling: Optional[date] = None
    sebi_pct: float = SEBI_TURNOVER_PCT
    gst_pct: float = GST_PCT
    stamp_duty_pct: float = STAMP_DUTY_PCT

    # ---------------------------------------------------------------- lookup

    @property
    def version(self) -> str:
        """A stable identifier for the exact regime pair in force, recorded on every quote.

        Contains both effective dates, so a quote is traceable to the two boundaries that
        produced it and two quotes on either side of 2026-04-01 are visibly different documents.
        """
        stt = self.stt_regime_for(self.floor)
        exch = self.exchange_regime_for(self.floor)
        return (
            f"{PROVENANCE}/stt={stt.source.split(',')[0]}@{stt.effective_from.isoformat()}"
            f"/exch={exch.source.split(',')[0]}@{exch.effective_from.isoformat()}"
        )

    def covers(self, day: date) -> bool:
        return self.floor <= day and (self.ceiling is None or day <= self.ceiling)

    def _require_covered(self, day: date) -> None:
        if self.covers(day):
            return
        raise CostScheduleCoverageError(
            f"{day.isoformat()} is outside this cost schedule's evidenced window "
            f"[{self.floor.isoformat()}, "
            f"{'open' if self.ceiling is None else self.ceiling.isoformat()}]. "
            f"No rate is returned, because the nearest regime is not this date's rate. "
            f"Evidence: the earliest effective date any downloaded circular establishes is "
            f"2023-04-01 (NSE/FA/56129, dated 2023-03-24, dated by reference in "
            f"{self.exchange_circular.circular.ref} as 'the level levied prior to April 1, "
            f"2023'). Ask for a date inside the window, or pass an explicit schedule."
        )

    def stt_regime_for(self, day: date) -> SttRegime:
        """The STT regime in force on ``day``. Raises rather than clamping."""
        self._require_covered(day)
        for regime in self.stt_regimes:
            last = regime.effective_to is None or day <= regime.effective_to
            if regime.effective_from <= day and last:
                return regime
        raise CostScheduleCoverageError(  # pragma: no cover - _require_covered guards this
            f"No STT regime covers {day.isoformat()}; regimes are "
            f"{[r.effective_from.isoformat() for r in self.stt_regimes]}."
        )

    def exchange_regime_for(self, day: date) -> ExchangeChargeRegime:
        """The exchange-charge regime in force on ``day``. Raises rather than clamping."""
        self._require_covered(day)
        for regime in self.exchange_regimes:
            last = regime.effective_to is None or day <= regime.effective_to
            if regime.effective_from <= day and last:
                return regime
        raise CostScheduleCoverageError(  # pragma: no cover
            f"No exchange-charge regime covers {day.isoformat()}."
        )

    # ---------------------------------------------------------------- provenance

    def provenance(self, day: date, shape: Union[str, TransactionShape] = DELIVERY_BUY) -> List[Dict[str, Any]]:
        """Every component that applies to ``shape`` on ``day``, with who publishes it.

        This is the caller-facing answer to "which of these numbers can I check against a
        circular?" — :data:`EXCHANGE_PUBLISHED` rows carry the circular and its date range, and
        the rest name the statute or the counterparty that actually fixes them.
        """
        resolved = validate_shape(shape)
        stt_regime = self.stt_regime_for(day)
        exch_regime = self.exchange_regime_for(day)
        rows: List[Dict[str, Any]] = []

        rate = stt_regime.for_key(resolved.stt_pct_key)
        authority, publisher, sourced = DECLARED_SOURCES[STT]
        rows.append({
            "component": STT,
            "authority": authority,
            "published_by": publisher,
            "fixture_sourced": sourced,
            "rate": rate.pct,
            "rate_unit": "percent of consideration",
            "payer": rate.payer,
            "stt_row": resolved.stt_row,
            "marked_no_change": rate.marked_no_change,
            "effective_from": stt_regime.effective_from.isoformat(),
            "effective_to": (
                "open" if stt_regime.effective_to is None
                else stt_regime.effective_to.isoformat()
            ),
            "source": stt_regime.source,
        })

        charge = exch_regime.for_segment(resolved.segment)
        for name in (EXCHANGE_CHARGE, IPFT):
            authority, publisher, sourced = DECLARED_SOURCES[name]
            rows.append({
                "component": name,
                "authority": authority,
                "published_by": publisher,
                "fixture_sourced": sourced,
                "rate": charge.total if name == EXCHANGE_CHARGE else 0.0,
                "rate_unit": RATE_UNIT,
                "component_split_provenance_only": {
                    "transaction_charges": charge.transaction_charges,
                    "ipft": charge.ipft,
                },
                "note": (
                    "charged and taxed as one figure; the component split is provenance only and "
                    "is never summed, because the 2026-03-01 change moved 306.99 and 0.01 between "
                    "columns at a constant Rs 307 total"
                    if name == EXCHANGE_CHARGE else
                    "inside the exchange-charge total, not a separate levy"
                ),
                "effective_from": exch_regime.effective_from.isoformat(),
                "effective_to": (
                    "open" if exch_regime.effective_to is None
                    else exch_regime.effective_to.isoformat()
                ),
                "source": exch_regime.source,
            })

        for name, rate_value, unit, payer, note in (
            (SEBI_TURNOVER, self.sebi_pct, "percent of turnover",
             "Investor", "SEBI Act 1972 s.15B; capped at Rs. 10 crore per year"),
            (STAMP_DUTY, self.stamp_duty_pct, "percent of consideration",
             "Purchaser" if resolved.stamp_duty_applies else "none",
             "state stamp act; buy side only, on delivery/intraday purchases; no NSE or SEBI "
             "circular publishes it, so it is a state levy sourced outside the exchange feed"),
            (GST, self.gst_pct, "percent of (brokerage + exchange charge + IPFT + SEBI fee)",
             "both", "not levied on STT and not on stamp duty"),
        ):
            authority, publisher, sourced = DECLARED_SOURCES[name]
            rows.append({
                "component": name,
                "authority": authority,
                "published_by": publisher,
                "fixture_sourced": sourced,
                "rate": rate_value,
                "rate_unit": unit,
                "payer": payer,
                "note": note,
                "source": f"not in {self.stt_circular.circular.ref} or "
                          f"{self.exchange_circular.circular.ref}",
            })

        authority, publisher, sourced = DECLARED_SOURCES[BROKERAGE]
        rows.append({
            "component": BROKERAGE,
            "authority": authority,
            "published_by": publisher,
            "fixture_sourced": sourced,
            "rate": None,
            "rate_unit": "percent of consideration, per side",
            "note": "a parameter; this module holds no default rate for it",
            "source": "caller-supplied",
        })
        rows.sort(key=lambda row: (AUTHORITIES.index(row["authority"]),
                                   COMPONENTS.index(row["component"])))
        return rows


def schedule_from_circulars(stt_text: str, exchange_text: str) -> CostSchedule:
    """Build a :class:`CostSchedule` from the two circulars' text.

    The schedule floor is ``2023-04-01`` — the earliest effective date either circular
    establishes. Below it this function will not invent a regime, and asking for such a date
    raises :class:`CostScheduleCoverageError`.
    """
    stt_data = parse_stt_circular(stt_text)
    exchange_data = parse_exchange_charge_circular(exchange_text)

    # STT: the pre-2026 regime runs from the floor to the day before the new rates; the new
    # regime is open-ended. The "Rate upto March 31, 2026" column is NSE's own statement of the
    # rates immediately before, and the circulars that set them are cited in `lineage` but were
    # not downloaded, so the earlier boundary is left undated rather than guessed.
    new_stt = SttRegime(
        effective_from=stt_data.effective_from,
        effective_to=None,
        rates=stt_data.new_rates,
        source=stt_data.circular.citation(),
        why=(
            f"{stt_data.statute} received Presidential assent on "
            f"{stt_data.assent_date.isoformat() if stt_data.assent_date else 'n/a'}; revised "
            f"rates apply on or after {stt_data.effective_from.isoformat()}. Delivery, "
            f"equity-oriented-fund and intraday rows are marked '(No Change)' in the same table."
        ),
    )
    previous_stt = SttRegime(
        effective_from=date(2023, 4, 1),
        effective_to=stt_data.previous_effective_to,
        rates=stt_data.previous_rates,
        source=(
            f"{stt_data.circular.citation()}, column 'Rate upto March 31, 2026' "
            f"(the circulars that set them are cited in the same document)"
        ),
        why=(
            "Carried forward from the 2026 circular's 'Rate upto March 31, 2026' column over "
            "the schedule's evidenced window. The option, exercised-option and futures rates are "
            "this circular's own 'upto March 31, 2026' figures; the delivery and intraday rows "
            "are marked '(No Change)'. The individual circulars that introduced them were not "
            "downloaded, so no earlier boundary is claimed."
        ),
    )
    stt_regimes = tuple(sorted((previous_stt, new_stt), key=lambda r: r.effective_from))

    # Exchange charge: two servable regimes, both keyed on the **total**.
    #
    # The first covers the schedule's evidenced window up to the day before the cutover, using the
    # circular's own "Existing outflow" column. The second covers the cutover onward, using its
    # "Revised outflow" column. Both are transcribed from a published table, so neither is marked
    # derived.
    #
    # The pre-2023-04-01 level is reconstructed from the deltas the circular states and is
    # recorded on ``exchange_data.prior`` for audit, but it is deliberately **not** a servable
    # regime: it precedes the schedule's floor, and making it one would mean answering a 2014
    # query with a reconstruction. ``CostSchedule._require_covered`` refuses those dates instead.
    floor = date(2023, 4, 1)
    regimes: List[ExchangeChargeRegime] = [
        ExchangeChargeRegime(
            effective_from=floor,
            effective_to=exchange_data.effective_from - timedelta(days=1),
            segments=exchange_data.existing,
            source=f"{exchange_data.circular.citation()}, column 'Existing outflow'",
            derived=False,
            why=(
                f"The outflow in force when {exchange_data.circular.ref} was issued, stated by "
                f"the circular itself in its 'Existing outflow' column. Published, not "
                f"reconstructed. The window starts at {floor.isoformat()}, the schedule's floor."
            ),
        ),
        ExchangeChargeRegime(
            effective_from=exchange_data.effective_from,
            effective_to=None,
            segments=exchange_data.revised,
            source=f"{exchange_data.circular.citation()}, column 'Revised outflow'",
            derived=False,
            why=(
                f"Rs 306.99 of transaction charge and Rs 0.01 of IPFT move between columns; both "
                f"sides of every segment total the same rupees, so the change is cost-neutral. The "
                f"schedule therefore keys on the total. Applicable with effect from "
                f"{exchange_data.effective_from.isoformat()}."
            ),
        ),
    ]
    exchange_regimes = tuple(sorted(regimes, key=lambda r: r.effective_from))
    return CostSchedule(
        stt_circular=stt_data,
        exchange_circular=exchange_data,
        stt_regimes=stt_regimes,
        exchange_regimes=exchange_regimes,
        floor=floor,
    )


# ------------------------------------------------------------------ quoting


@dataclass(frozen=True)
class LineItem:
    """One named component of one leg, with everything needed to audit it."""

    component: str
    authority: str
    rupees: float
    bps: float
    rate: Optional[float]
    rate_unit: str
    payer: str
    shape: str
    amount: float
    amount_basis: str
    effective_from: str
    effective_to: str
    source: str
    published_by: str
    fixture_sourced: bool
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "authority": self.authority,
            "rupees": round(self.rupees, 4),
            "bps": round(self.bps, 6),
            "rate": self.rate,
            "rate_unit": self.rate_unit,
            "payer": self.payer,
            "shape": self.shape,
            "amount": self.amount,
            "amount_basis": self.amount_basis,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "source": self.source,
            "published_by": self.published_by,
            "fixture_sourced": self.fixture_sourced,
            "note": self.note,
        }


@dataclass(frozen=True)
class Leg:
    """One side of a round trip."""

    side: str
    shape: str
    amount: float
    exchange_base: float
    exchange_base_kind: str
    stamp_duty_pct: float
    brokerage_pct: float
    items: Tuple[LineItem, ...]
    gst_base: float
    gst_pct: float

    @property
    def total_rupees(self) -> float:
        return round(sum(item.rupees for item in self.items), 6)

    def bps_on(self, denominator: float) -> float:
        return self.total_rupees / denominator * BPS_PER_UNIT

    def component_rupees(self, component: str) -> float:
        return round(sum(i.rupees for i in self.items if i.component == component), 6)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "shape": self.shape,
            "amount": self.amount,
            "exchange_base": self.exchange_base,
            "exchange_base_kind": self.exchange_base_kind,
            "total_rupees": self.total_rupees,
            "bps_on_amount": round(self.bps_on(self.amount), 6),
            "gst_base_rupees": round(self.gst_base, 4),
            "gst_pct": self.gst_pct,
            "brokerage_pct": self.brokerage_pct,
            "stamp_duty_pct": self.stamp_duty_pct,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class CostQuote:
    """A priced round trip: two :class:`Leg` objects plus the totals in rupees and bps.

    Denominator convention is explicit because it is the easiest thing to get wrong when
    comparing against a broker's number. See the module docstring.
    """

    trade_date: date
    schedule_version: str
    buy: Leg
    sell: Leg
    why: str
    bps_denominator: float
    provenance_tag: str = PROVENANCE

    @property
    def round_trip_rupees(self) -> float:
        return round(self.buy.total_rupees + self.sell.total_rupees, 6)

    @property
    def one_way_rupees(self) -> float:
        """Half the round trip, i.e. the average cost of a single leg."""
        return self.round_trip_rupees / 2.0

    @property
    def round_trip_bps(self) -> float:
        """Round-trip cost in bps of the **one-way** notional. This is the 22.248 convention."""
        return self.round_trip_rupees / self.bps_denominator * BPS_PER_UNIT

    @property
    def round_trip_bps_on_turnover(self) -> float:
        """Round-trip cost in bps of **two-way** turnover — half of :attr:`round_trip_bps`."""
        return self.round_trip_rupees / (2.0 * self.bps_denominator) * BPS_PER_UNIT

    @property
    def one_way_bps(self) -> float:
        """Per-leg cost in bps of that leg's own notional."""
        return (self.buy.bps_on(self.buy.amount) + self.sell.bps_on(self.sell.amount)) / 2.0

    def component_rupees(self, component: str) -> float:
        return round(
            self.buy.component_rupees(component) + self.sell.component_rupees(component), 6
        )

    def component_share(self, component: str) -> float:
        """A component's share of the round trip, 0..1. Raises if the total is zero."""
        total = self.round_trip_rupees
        if total <= 0.0:
            raise CostScheduleError(
                f"round trip on {self.trade_date.isoformat()} costs nothing, so no component "
                f"share is defined. A zero-cost round trip means a rate is missing, not free."
            )
        return self.component_rupees(component) / total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provenance": self.provenance_tag,
            "trade_date": self.trade_date.isoformat(),
            "schedule_version": self.schedule_version,
            "bps_denominator": self.bps_denominator,
            "bps_convention": "round-trip cost in bps of the one-way notional",
            "why": self.why,
            "buy": self.buy.to_dict(),
            "sell": self.sell.to_dict(),
            "round_trip_rupees": self.round_trip_rupees,
            "round_trip_bps": round(self.round_trip_bps, 6),
            "round_trip_bps_on_turnover": round(self.round_trip_bps_on_turnover, 6),
            "one_way_bps": round(self.one_way_bps, 6),
            "component_rupees": {
                name: self.component_rupees(name) for name in COMPONENTS
            },
            "component_share": {
                name: round(self.component_share(name), 6) for name in COMPONENTS
                if self.component_rupees(name) > 0.0
            },
        }


def _leg(
    schedule: CostSchedule,
    day: date,
    side: str,
    shape: TransactionShape,
    amount: float,
    exchange_base: float,
    brokerage_pct: float,
    stamp_duty_pct: float,
    apply_stamp_duty: bool,
) -> Leg:
    """Price one leg, component by component. Pure; no clock, no network."""
    stt_regime = schedule.stt_regime_for(day)
    exch_regime = schedule.exchange_regime_for(day)
    stt_rate = stt_regime.for_key(shape.stt_pct_key)
    charge = exch_regime.for_segment(shape.segment)

    stt_rupees = amount * stt_rate.pct / 100.0
    charge_rupees = exchange_base / CRORE * charge.total
    sebi_rupees = amount * schedule.sebi_pct / 100.0
    brokerage_rupees = amount * brokerage_pct / 100.0
    stamp_rupees = amount * stamp_duty_pct / 100.0 if apply_stamp_duty else 0.0

    # The GST base is built here, explicitly, from the four components it actually covers. STT
    # and stamp duty are absent by design and the test asserts the base rather than the total.
    gst_base = brokerage_rupees + charge_rupees + sebi_rupees
    gst_rupees = round(gst_base * schedule.gst_pct / 100.0, 2)

    def _bounds(regime) -> Tuple[str, str]:
        return (
            regime.effective_from.isoformat(),
            "open" if regime.effective_to is None else regime.effective_to.isoformat(),
        )

    stt_from, stt_to = _bounds(stt_regime)
    exch_from, exch_to = _bounds(exch_regime)
    # Stamp duty is a state levy with no NSE or SEBI circular, so its own window is reported
    # separately from the exchange regimes rather than borrowing the STT regime's dates. It is
    # currently 0.015% from 2020-07-20 and 0.018% before, and the schedule states the window it
    # applied the current rate over rather than implying a single timeless rate.
    stamp_from = STAMP_DUTY_EFFECTIVE_FROM.isoformat()
    stamp_to = "open" if day >= STAMP_DUTY_EFFECTIVE_FROM else STAMP_DUTY_EFFECTIVE_FROM.isoformat()
    items: List[LineItem] = [
        LineItem(
            STT, EXCHANGE_PUBLISHED, stt_rupees,
            stt_rupees / amount * BPS_PER_UNIT if amount else 0.0,
            stt_rate.pct, "percent of consideration", stt_rate.payer, shape.key, amount,
            "consideration", stt_from, stt_to, stt_regime.source,
            DECLARED_SOURCES[STT][1], True,
            note=(
                f"circular table row {stt_rate.row} marked '(No Change)'"
                if stt_rate.marked_no_change else
                (f"circular table row {stt_rate.row}" if stt_rate.row else
                 "the circular states no purchase rate for this leg")
            ),
        ),
        LineItem(
            EXCHANGE_CHARGE, EXCHANGE_PUBLISHED, charge_rupees,
            charge_rupees / amount * BPS_PER_UNIT if amount else 0.0,
            charge.total, RATE_UNIT, "Trader", shape.key, exchange_base, shape.exchange_base,
            exch_from, exch_to, exch_regime.source, DECLARED_SOURCES[EXCHANGE_CHARGE][1], True,
            note=(
                f"total Rs {charge.total}/crore each side; component split "
                f"(txn {charge.transaction_charges} + IPFT {charge.ipft}) is provenance only"
            ),
        ),
        LineItem(
            SEBI_TURNOVER, STATUTORY, sebi_rupees,
            sebi_rupees / amount * BPS_PER_UNIT if amount else 0.0,
            schedule.sebi_pct, "percent of turnover", "Investor", shape.key, amount, "turnover",
            stt_from, stt_to, "SEBI Act 1972 s.15B", DECLARED_SOURCES[SEBI_TURNOVER][1], False,
            note="SEBI Act 1972 s.15B; statutory, capped at Rs 10 crore per year",
        ),
    ]
    if apply_stamp_duty:
        items.append(LineItem(
            STAMP_DUTY, STATUTORY, stamp_rupees,
            stamp_rupees / amount * BPS_PER_UNIT if amount else 0.0,
            stamp_duty_pct, "percent of consideration", "Purchaser", shape.key, amount,
            "consideration", stamp_from, stamp_to, "state stamp acts",
            DECLARED_SOURCES[STAMP_DUTY][1], False,
            note="state stamp act; buy side only; no NSE or SEBI circular publishes it",
        ))
    if brokerage_rupees > 0.0:
        items.append(LineItem(
            BROKERAGE, CONTRACTUAL, brokerage_rupees,
            brokerage_rupees / amount * BPS_PER_UNIT if amount else 0.0,
            brokerage_pct, "percent of consideration, per side", "Investor", shape.key, amount,
            "consideration", stt_from, stt_to, "caller-supplied", DECLARED_SOURCES[BROKERAGE][1],
            False, note="contractual; no exchange publishes this rate",
        ))
    items.append(LineItem(
        GST, STATUTORY, gst_rupees,
        gst_rupees / amount * BPS_PER_UNIT if amount else 0.0,
        schedule.gst_pct,
        "percent of (brokerage + exchange charge + IPFT + SEBI fee)", "Investor", shape.key,
        gst_base, "gst base", stt_from, stt_to, DECLARED_SOURCES[GST][1], GST, False,
        note=(
            f"base Rs {gst_base:,.2f} = brokerage + exchange charge + IPFT + SEBI fee; "
            f"not on STT, not on stamp duty"
        ),
    ))
    return Leg(
        side=side, shape=shape.key, amount=amount, exchange_base=exchange_base,
        exchange_base_kind=shape.exchange_base, stamp_duty_pct=stamp_duty_pct if apply_stamp_duty
        else 0.0, brokerage_pct=brokerage_pct, items=tuple(items),
        gst_base=gst_base, gst_pct=schedule.gst_pct,
    )


def quote(
    buy_shape: Union[str, TransactionShape],
    sell_shape: Union[str, TransactionShape],
    *,
    trade_date: Union[str, date, datetime],
    buy_amount: float,
    sell_amount: Optional[float] = None,
    premium: Optional[float] = None,
    buy_premium: Optional[float] = None,
    sell_premium: Optional[float] = None,
    brokerage_pct: float = 0.0,
    schedule: Optional[CostSchedule] = None,
) -> CostQuote:
    """Price a round trip on ``trade_date``: two legs, rupees and bps, fully attributed.

    Args:
        buy_shape: The taxable purchase leg, e.g. :data:`DELIVERY_BUY` or
            :data:`OPTION_EXERCISED` (an *exercised* option is taxed on the **Purchaser**).
        sell_shape: The taxable sale leg, e.g. :data:`DELIVERY_SELL` or :data:`OPTION_SALE`.
        trade_date: The trade date. Selects the regime. Outside the schedule's window this
            **raises** :class:`CostScheduleCoverageError`; it is never clamped.
        buy_amount: Rupees of consideration on the buy leg — what the percentage charges apply to.
        sell_amount: Rupees on the sell leg. Defaults to ``buy_amount`` (a round trip at one
            price).
        premium: Total premium turnover for an options round trip. The equity-options exchange
            charge is levied **on premium**, not on traded value, so it is a separate amount and
            :data:`BASE_PREMIUM` shapes refuse to price without it.
        buy_premium / sell_premium: Split the premium across the legs. Defaults to half of
            ``premium``.
        brokerage_pct: Contractual brokerage, percent per side. Defaults to ``0.0``, which is
            an explicit choice, not a published rate.
        schedule: An explicit :class:`CostSchedule`. One is built from the bundled fixture text
            when omitted, and building it reads no files outside this package.

    Returns:
        A :class:`CostQuote`.

    Raises:
        CostScheduleCoverageError: ``trade_date`` is outside the evidenced window.
        ValueError: a shape is unknown, an amount is negative or zero, or a premium-based shape
            was given no premium.
    """
    resolved_buy = validate_shape(buy_shape)
    resolved_sell = validate_shape(sell_shape)
    day = _coerce_date(trade_date)
    active = schedule if schedule is not None else bundled_schedule()

    for name, value in (("buy_amount", buy_amount), ("sell_amount", sell_amount)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")
    if buy_amount <= 0:
        raise ValueError(f"buy_amount must be positive, got {buy_amount}")
    if sell_amount is None:
        sell_amount = buy_amount

    bases: Dict[str, float] = {}
    for resolved, side, amount in (
        (resolved_buy, "buy", buy_amount), (resolved_sell, "sell", sell_amount)
    ):
        if resolved.exchange_base != BASE_PREMIUM:
            bases[side] = amount
            continue
        supplied = buy_premium if side == "buy" else sell_premium
        if supplied is None and premium is None:
            raise ValueError(
                f"shape={resolved.key!r} is charged the equity-options exchange fee **on "
                f"premium**, not on traded value, so `premium` (or `buy_premium`/"
                f"`sell_premium`) is required. Falling back to the consideration would price "
                f"the charge on the wrong base — the exact class of error this module exists to "
                f"prevent."
            )
        if supplied is not None and supplied <= 0:
            raise ValueError(f"{side}_premium must be positive, got {supplied}")
        if supplied is not None:
            bases[side] = supplied
        else:
            bases[side] = (premium or 0.0) / 2.0
    if any(v <= 0 for v in bases.values()):
        raise ValueError(f"exchange-charge base resolved to a non-positive value: {bases}")

    if not (0.0 <= brokerage_pct < 1.0):
        raise ValueError(
            f"brokerage_pct={brokerage_pct!r} is not a per-side percentage of consideration. "
            f"It is contractual and is supplied by the caller; it is never defaulted here."
        )

    stt_regime = active.stt_regime_for(day)
    exch_regime = active.exchange_regime_for(day)
    buy_leg = _leg(
        active, day, "buy", resolved_buy, buy_amount, bases["buy"], brokerage_pct,
        active.stamp_duty_pct, resolved_buy.stamp_duty_applies,
    )
    sell_leg = _leg(
        active, day, "sell", resolved_sell, sell_amount, bases["sell"], brokerage_pct,
        active.stamp_duty_pct, resolved_sell.stamp_duty_applies,
    )
    return CostQuote(
        trade_date=day,
        schedule_version=active.version,
        buy=buy_leg,
        sell=sell_leg,
        why=(
            f"STT {stt_regime.effective_from.isoformat()}"
            f"..{stt_regime.effective_to.isoformat() if stt_regime.effective_to else 'open'} "
            f"({stt_regime.source}); exchange charge "
            f"{exch_regime.effective_from.isoformat()}"
            f"..{exch_regime.effective_to.isoformat() if exch_regime.effective_to else 'open'} "
            f"({exch_regime.source}); stamp duty, SEBI turnover fee and GST are statutory and "
            f"brokerage is contractual"
        ),
        bps_denominator=float(buy_amount),
    )


def _coerce_date(value: Union[str, date, datetime]) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    raise ValueError(
        f"trade_date must be a date or a 'YYYY-MM-DD' string, got {value!r}"
    )


# ------------------------------------------------------------------ bundled fixture text
#
# The two trimmed circular excerpts ship with the tests, not with the package, so the package
# stays import-safe and the fixtures stay auditable. ``BFINANCE_COSTS_STT_TEXT`` /
# ``BFINANCE_COSTS_EXCHANGE_TEXT`` let a caller inject text; otherwise the paths below are read
# once and cached. Reading two small local files is not a network request, which is what keeps
# the offline suite hermetic.

_DEFAULT_STT_PATH = "tests/fixtures/nse/costs/NSE_FATAX_73524_stt_rates.txt"
_DEFAULT_EXCHANGE_PATH = "tests/fixtures/nse/costs/NSE_FA_73061_exchange_charges.txt"


def _fixture_text(relative: str) -> str:
    """Locate a bundled fixture by walking up from this file.

    The package is installed inside ``src/`` and the fixtures live at the repository root, so the
    search walks parent directories for the ``tests/fixtures`` marker rather than assuming a
    layout. Returns ``""`` when not found, which makes the schedule builder raise a parse error
    naming the missing file instead of quietly pricing with empty rates.
    """
    import pathlib

    here = pathlib.Path(__file__).resolve()
    name = relative.split("/")[-1]
    for parent in here.parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        sibling = parent / "tests" / "fixtures" / "nse" / "costs" / name
        if sibling.is_file():
            return sibling.read_text(encoding="utf-8")
    return ""


_BUNDLED: Optional[CostSchedule] = None


def bundled_schedule(*, refresh: bool = False,
                     stt_text: Optional[str] = None,
                     exchange_text: Optional[str] = None) -> CostSchedule:
    """The schedule built from the bundled circular excerpts. Cached; issues no request.

    Pass ``refresh=True`` to rebuild, or the two texts to build from injected content — which is
    how the negative controls mutate a transcribed rate and watch the bps move.
    """
    global _BUNDLED
    if stt_text is not None or exchange_text is not None or refresh:
        if not stt_text or not exchange_text:
            raise ValueError(
                "bundled_schedule(stt_text=..., exchange_text=...) needs BOTH texts; a partial "
                "override would silently reuse the other circular from the fixture."
            )
        schedule = schedule_from_circulars(stt_text, exchange_text)
        if stt_text is None and exchange_text is None:
            _BUNDLED = schedule
        return schedule
    if _BUNDLED is None:
        stt_text = os.environ.get("BFINANCE_COSTS_STT_TEXT") or _fixture_text(_DEFAULT_STT_PATH)
        exchange_text = (
            os.environ.get("BFINANCE_COSTS_EXCHANGE_TEXT")
            or _fixture_text(_DEFAULT_EXCHANGE_PATH)
        )
        if not stt_text or not exchange_text:
            raise CostScheduleParseError(
                f"Bundled circular excerpts not found (looked for "
                f"{_DEFAULT_STT_PATH!r} and {_DEFAULT_EXCHANGE_PATH!r} walking up from "
                f"{__file__}). Pass stt_text=/exchange_text= explicitly, or build a "
                f"CostSchedule with schedule_from_circulars()."
            )
        _BUNDLED = schedule_from_circulars(stt_text, exchange_text)
    return _BUNDLED


# ------------------------------------------------------------------ client
#
# The only part of this module that can touch the network, and it is never reached implicitly:
# every function above works from text. A client exists so the circulars can be re-fetched when
# NSE reissues one, and so the cache can be tested.

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

MIN_REQUEST_INTERVAL_MS = 2000.0
_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


class CostsClient:
    """Fetches the two circulars, with a TTL cache and an injectable transport.

    ``ttl_hours=0`` means "never reuse", which is a legitimate way to say "re-read the tariff",
    and :meth:`schedule` honours it exactly rather than quietly serving a cached parse.

    Nothing in this module constructs a client implicitly, so importing :mod:`bfinance.costs` and
    calling :func:`quote` opens no socket. That is what keeps the ``-m "not live"`` selection
    hermetic.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        ttl_hours: float = 24.0,
        max_retries: int = 3,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        min_request_interval_ms: float = MIN_REQUEST_INTERVAL_MS,
        text_extractor: Optional[Any] = None,
    ) -> None:
        self.timeout = timeout
        self.ttl_seconds = max(0.0, float(ttl_hours) * 3600.0)
        self.max_retries = max(1, int(max_retries))
        self.transport = transport
        self.min_request_interval_ms = max(0.0, float(min_request_interval_ms))
        #: Bytes -> text. Defaults to :func:`extract_pdf_text`, which needs ``pypdf``. Injectable
        #: so the cache logic can be tested without a PDF dependency, and so a caller with a
        #: different PDF stack can supply it.
        self.text_extractor = text_extractor or extract_pdf_text
        self._last_request_time = 0.0
        self._texts: Dict[str, Tuple[str, float]] = {}
        self.request_count = 0

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/pdf,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
        }

    async def _throttle(self) -> None:
        global _GLOBAL_LAST_REQUEST_TIME
        with _PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            wait = 0.0
            if elapsed_ms < self.min_request_interval_ms:
                wait = (self.min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(
                    0.01, 0.05
                )
                _GLOBAL_LAST_REQUEST_TIME = now + wait
            else:
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait > 0.0:
            await asyncio.sleep(wait)
        self._last_request_time = time.monotonic()

    def _cached(self, ref: str) -> Optional[str]:
        if self.ttl_seconds <= 0.0:
            return None
        entry = self._texts.get(ref)
        if entry is None:
            return None
        text, at = entry
        if (time.monotonic() - at) > self.ttl_seconds:
            return None
        return text

    async def circular_text_async(self, ref: str, *, force_refresh: bool = False) -> str:
        """Return the circular's extracted text, honouring the TTL cache.

        A cached hit issues **no** request; a stale or absent entry issues exactly one. PDFs are
        reduced to text here because every parser in this module is a text parser, and the
        stored fixtures are those texts.
        """
        if ref not in CIRCULAR_URLS:
            raise ValueError(
                f"ref={ref!r} is not a known circular. This module reads "
                f"{sorted(CIRCULAR_URLS)}."
            )
        if not force_refresh:
            cached = self._cached(ref)
            if cached is not None:
                return cached
        await self._throttle()
        kwargs: Dict[str, Any] = {"timeout": self.timeout, "follow_redirects": True}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        last_error: Optional[Exception] = None
        async with httpx.AsyncClient(**kwargs) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.get(CIRCULAR_URLS[ref], headers=self._headers())
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(0.5 * (2**attempt))
                    continue
                self.request_count += 1
                if response.status_code == 200:
                    text = self.text_extractor(response.content)
                    self._texts[ref] = (text, time.monotonic())
                    return text
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise CostScheduleError(
                    f"{ref} returned HTTP {response.status_code} from "
                    f"{CIRCULAR_URLS[ref]} after {self.max_retries} attempts."
                )
        raise CostScheduleError(f"Could not fetch {ref}: {last_error}")

    def circular_text(self, ref: str, *, force_refresh: bool = False) -> str:
        """Sync variant of :meth:`circular_text_async`."""
        return _run(self.circular_text_async(ref, force_refresh=force_refresh))

    async def schedule_async(self, *, force_refresh: bool = False) -> CostSchedule:
        """Fetch both circulars and build a :class:`CostSchedule`.

        Cached per circular, so a second call costs no request while the TTL holds. Returns the
        same object for the same text, so identity can be asserted.
        """
        stt_text, exchange_text = await asyncio.gather(
            self.circular_text_async("NSE/FATAX/73524", force_refresh=force_refresh),
            self.circular_text_async("NSE/FA/73061", force_refresh=force_refresh),
        )
        return schedule_from_circulars(stt_text, exchange_text)

    def schedule(self, *, force_refresh: bool = False) -> CostSchedule:
        """Sync variant of :meth:`schedule_async`."""
        return _run(self.schedule_async(force_refresh=force_refresh))


def _run(coro):
    """Bridge to sync, matching ``nse.corporate_actions._run_async``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():  # pragma: no cover - notebook re-entry
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def extract_pdf_text(payload: bytes) -> str:
    """Reduce a PDF's bytes to text.

    The circulars are text-layer PDFs (measured: 4,087 characters out of 358,718 bytes across
    two pages — the bulk is an embedded letterhead image, which is why the stored fixtures are
    text and not the PDFs). A scanned circular with no text layer yields ``""``, and the parsers
    then raise :class:`CostScheduleParseError` naming the failure rather than returning zeros.
    """
    try:
        from pypdf import PdfReader  # type: ignore
        import io

        reader = PdfReader(io.BytesIO(payload))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError as exc:
        raise CostScheduleError(
            "Reading a circular PDF needs pypdf, which is not a bfinance dependency. The "
            "bundled fixtures are already text, so this is only required to re-fetch a "
            "circular: install it in your own environment and pass the extracted text to "
            "schedule_from_circulars()."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - upstream document, any parse failure is one thing
        raise CostScheduleParseError(f"Could not read the circular PDF: {exc}") from exc


def taxable_base_requires_turnover_value() -> str:
    """Why this module has no empirical bps, and the one claim the bhavcopy *does* support.

    Kept as a function returning prose so the statement is greppable and testable rather than
    buried in a docstring. The substance:

    * The bhavcopy carries **no cost, fee or tax column**. All 34 UDiFF cash-market columns were
      checked; the only substring hit for ``stt`` is ``SttlmPric``, the *settlement price*.
    * The file is an all-participant aggregate with no side and no counterparty, so it cannot
      isolate one portfolio's round trip. **No "observed bps" is therefore synthesised here.**
    * What it does support is the taxable base: ``TtlTrfVal / TtlTradgVol`` is a true session
      VWAP inside ``[Low, High]`` for 7/7 recorded rows.
    * :mod:`bfinance.nse.bhavcopy` maps volume to ``TtlTradgVol`` and has **no turnover-value
      column**, so that VWAP cannot be computed through this package yet. The change needed is a
      ``'turnover': ('TtlTrfVal',)`` entry in ``_SCHEMAS['udiff']`` (and a legacy equivalent if
      the legacy CSV is still read) plus a ``Turnover`` output column. **REPORTED, NOT EDITED**:
      that file belongs to another module's owner, so the change is stated here and left for them.
    """
    return (
        "No empirical bps: the NSE bhavcopy has no cost, fee or tax column (34 UDiFF CM columns "
        "checked; the only 'stt' substring hit is SttlmPric, the settlement price, a false "
        "positive), and it is an all-participant aggregate with no side and no counterparty, so "
        "one portfolio's round trip cannot be isolated from it. Every figure in this module is "
        "a statutory computation from named, sourced rates. What the bhavcopy does support is "
        "the taxable base: TtlTrfVal / TtlTradgVol is a true session VWAP within [Low, High] "
        "for 7/7 recorded rows. bfinance.nse.bhavcopy currently has no turnover-value column, "
        "so that VWAP is not yet reachable through this package: _SCHEMAS['udiff'] needs a "
        "'turnover': ('TtlTrfVal',) entry and COLUMNS a 'Turnover' column. REPORTED, NOT EDITED "
        "-- bhavcopy.py belongs to another module's owner."
    )


__all__ = [
    "PROVENANCE", "CRORE", "BPS_PER_UNIT", "RATE_UNIT",
    "EXCHANGE_PUBLISHED", "STATUTORY", "CONTRACTUAL", "AUTHORITIES",
    "STT", "EXCHANGE_CHARGE", "IPFT", "SEBI_TURNOVER", "STAMP_DUTY", "GST", "BROKERAGE",
    "COMPONENTS", "DECLARED_SOURCES",
    "DELIVERY_BUY", "DELIVERY_SELL", "INTRADAY_BUY", "INTRADAY_SELL", "EOF_UNIT_SELL",
    "OPTION_SALE", "OPTION_EXERCISED", "FUTURES_SALE", "SHAPE_KEYS", "SHAPES",
    "CASH_MARKET", "EQUITY_FUTURES", "EQUITY_OPTIONS", "BASE_TRADED_VALUE", "BASE_PREMIUM",
    "SEBI_TURNOVER_PCT", "SEBI_TURNOVER_CAP_RS", "STAMP_DUTY_PCT", "STAMP_DUTY_EFFECTIVE_FROM",
    "STAMP_DUTY_PREVIOUS_PCT", "GST_PCT",
    "CostScheduleError", "CostScheduleCoverageError", "CostScheduleParseError",
    "Circular", "SegmentCharge", "SttRate", "SttRegime", "ExchangeChargeRegime",
    "SttCircularData", "ExchangeChargeCircularData", "TransactionShape", "CostSchedule",
    "LineItem", "Leg", "CostQuote", "CostsClient",
    "CIRCULAR_URLS", "CIRCULAR_ORIGINALS",
    "parse_upstream_date", "parse_stt_circular", "parse_exchange_charge_circular",
    "schedule_from_circulars", "bundled_schedule", "quote", "validate_shape",
    "extract_pdf_text", "taxable_base_requires_turnover_value",
]
