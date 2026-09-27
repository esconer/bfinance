"""
NSE corporate actions — real ex-dated dividends, bonus issues and face-value sub-divisions.

Why this module exists
----------------------
``Adj Close`` is arithmetic, not a price lookup::

    raw O/H/L/C/V  (real, from the NSE bhavcopy)
      + Dividends, Stock Splits   <-- this module
      -> Adj Close = product of back-adjustment factors

Until now the *factors* had no real input: :class:`bfinance.market.corporate.CorporateActionsEngine`
derived dividends from Screener.in's ``Dividend Payout % x EPS`` and dated them at the **fiscal
year end**, which is 4-5 months before the actual ex-date. Every back-adjusted price before the
ex-date was therefore rescaled on the wrong day, by an amount that happened to be roughly right.
Plausible numbers, wrong provenance — the same class of defect the fabricated OHLC had.

This module replaces that input with NSE's own corporate-filings feed, which carries the
**true ex-date** for every event.

Endpoint — verified live 2026-09-27
-----------------------------------
::

    https://www.nseindia.com/api/corporates-corporateActions?index=equities
        [&symbol=RELIANCE][&from_date=DD-MM-YYYY][&to_date=DD-MM-YYYY]

The route is ``corporates-corporateActions`` — **camelCase**. The kebab-case spellings
``corporates-corporate-actions`` and ``corporate-actions`` look right, are quoted in older
integration notes, and return **404**. That 404 is genuine, not a block: a deliberately nonsense
route ``/api/zzz-definitely-not-a-real-route-xyz`` returns the identical 272-byte
``Resource not found`` body, while ``/api/marketStatus`` and ``/api/allIndices`` on the same
cookie-primed session return **200**. So 404 here means "route gone", and it must not be read as
"blocked".

The path was recovered from NSE's own bundle,
``/dist/js/sections/corporate-filings.js``, which contains the literal
``ensureSymbolAndDates("/api/corporates-corporateActions?index=equities", ...)``. The column
vocabulary comes from ``/json/CorporateFiling/CF-corpactions-equity.json``.

Cookie priming is **not required**
---------------------------------
The ``jugaad-data`` ``bhavcopy_old_raw`` pattern (prime ``https://www.nseindia.com`` on a 403,
then retry) is the right instinct for the *other* NSE routes, and the generic ``/api/quote-equity``
still 403s even with cookies. This route does not need it: measured 4/4 HTTP 200 on a fresh
``httpx.Client`` with **zero cookies primed**, including at a 0.4 s cadence. Priming is still
implemented here as a **single** retry on 403/503, because a residential IP can be blocked where a
datacenter one is not and the inverse is equally possible — but it is a fallback, not a
prerequisite, and the module docstring records that it was measured as unnecessary.

Response schema
---------------
A JSON **array** of flat objects, keys verified on every field::

    symbol, comp, series, subject, faceVal, exDate, recDate,
    bcStartDate, bcEndDate, ndStartDate, ndEndDate, ind, isin, caBroadcastDate

``exDate``/``recDate`` are ``"DD-MMM-YYYY"``; an absent date is the string ``"-"``. ``subject`` is
free text. Verified RELIANCE row::

    {"symbol": "RELIANCE", "comp": "Reliance Industries Limited", "series": "EQ",
     "subject": "Dividend - Rs 6 Per Share", "faceVal": "10",
     "exDate": "05-Jun-2026", "recDate": "05-Jun-2026", "isin": "INE002A01018",
     "bcStartDate": "-", "bcEndDate": "-", "ndStartDate": "-", "ndEndDate": "-",
     "ind": "-", "caBroadcastDate": null}

**The array is not the only shape.** ``index=debt`` answers ``{"data": [], "msg": "no data
found"}`` — a dict, not a list. :func:`parse_corporate_actions` handles both; treating the dict as
rows would raise a confusing ``TypeError`` deep inside pandas.

Request cost — one request per symbol
-------------------------------------
``symbol=`` omitted returns the **whole market** in one request (measured: 43,789 rows / 3,930
symbols for 1990-2026, 26,244 rows / 2,770 symbols for 2015-2026). That is a genuinely useful
bulk form and it is exposed as ``symbol=None``.

.. warning::
   **The per-symbol form is capped at 20 rows *only when no date range is supplied***, and the
   market-wide form appears to be capped per symbol too. Measured:

   * ``symbol=ITC`` with no dates -> 20 rows (2015-2026 window only).
   * ``symbol=ITC&from_date=01-01-1990&to_date=27-09-2026`` -> **31** rows (2013-2026).
   * ``symbol=RELIANCE&from_date=...&to_date=...`` -> **28** rows (2006-2026).
   * 26,244 rows / 2,770 symbols over 2015-2026 is ~9.5 per symbol, not ~11 dividends.

   So :meth:`CorporateActionsClient.corporate_actions` **always sends an explicit date range** and
   defaults one in when the caller does not, because a silently truncated dividend history produces
   a confidently wrong ``Adj Close``.

Coverage: what this feed does and does not contain
-------------------------------------------------
Everything below was measured against yfinance 1.7.0's own ``dividends``/``splits`` (an independent
publisher) on 2026-09-27.

**Ex-dates are exact.** Every ex-date checked matched yfinance to the day: RELIANCE 19-Aug-2024 /
14-Aug-2025 / 05-Jun-2026; LICI 21-Feb-2024 / 19-Jul-2024 / 25-Jul-2025 / 25-Jun-2026; SHRIRAMFIN
06-Feb-2024 / 23-Jul-2024 / 07-Nov-2024 / 31-Jan-2025 / 11-Jul-2025 / 07-Nov-2025 / 03-Jul-2026;
BAJAJ-AUTO 14-Jun-2024 / 20-Jun-2025 / 29-May-2026; HDFCBANK 10-May-2024 / 27-Jun-2025 /
25-Jul-2025 / 19-Jun-2026. **Zero date disagreements.** A full-history RELIANCE comparison against
yfinance 1.7.0 agrees on 33 of yfinance's 35 ex-dates; the 2 outside the feed's coverage are
pre-2005, and the 3 NSE dates yfinance does not list are a rights issue and a demerger, which this
module records as events but correctly declines to convert into a factor.

**Amounts are the rupees actually paid, unadjusted.** yfinance normalises every past dividend by
*all* subsequent splits; NSE does not. RELIANCE's 19-Aug-2024 dividend is ``Rs 10`` here and
``5.0`` in yfinance — exactly a factor of 2, the 1:1 bonus of 28-Oct-2024. SHRIRAMFIN's
06-Feb-2024 is ``Rs 10`` here and ``2.0`` in yfinance — exactly a factor of 5, the face-value
sub-division of 10-Jan-2025. **The unadjusted figure is the correct input** for the
``1 - D / prev_close`` back-adjustment, which is defined in *pre-action* rupees. Matching yfinance
here would be the bug, not the fix.

**Splits arrive as bonus issues and face-value sub-divisions, never as a bare "Split".** There is
no ``Split 1:2`` subject in 43,789 rows. The two forms are ``Bonus a:b`` (ratio ``(a+b)/b``) and
``Face Value Split (Sub-Division) - From Rs X/- Per Share To Rs Y/- Per Share`` (ratio ``X/Y``),
and the ratio is derivable in every case checked — 18 of 18 symbols agreed with yfinance exactly:
``Bonus 1:1``->2.0, ``Bonus 3:1``->4.0, ``Bonus 4:1``->5.0, ``Bonus 1:2``->1.5, ``Rs 10 -> Rs 2``->5.0,
``Rs 10 -> Rs 5``->2.0, ``Rs 10 -> Re 1``->10.0, ``Rs 5 -> Re 1``->5.0.

**NSE is sometimes right where yfinance is wrong.** RSSOFTWARE, ex-date 21-Jan-2015, subject
``Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share To Rs 5/- Per
Share``. The face-value sub-division is 2x. yfinance records split 2.0 — correct — but a dividend
of ``0.375``, which is 1.50/4, inconsistent with its own 2.0 split. NSE's Rs 1.50 pre-split, i.e.
0.75 post-split, is the self-consistent figure. The legacy bhavcopy agrees: ``ClsPric(2015-01-20) =
600.6`` against ``ClsPric(2015-01-21) = 290.65``, a 2.07x fall on the day, of which 2.0 is the split.

**Recall is imperfect for a few names.** AARON's 2020-09-03 1.909 event and AAKASH's 2020-03-26
1.5 event are absent from the feed, and AAKASH has a single row in 30 years. This is stated rather
than papered over; see :func:`unexplained_price_jumps` for how a consumer detects a gap rather than
trusting silence.

**Rows this module deliberately does not turn into a factor**, each recorded in the ``Action``
column instead of being dropped: ``Buy Back`` (does not change the historical share count),
``Rights a:b @ Premium Rs X`` (yfinance records no ``actions`` entry), ``Demerger``,
``Interest Payment`` (debenture coupons), AGM-only notices, and every ``... Per Unit`` row — 359 of
them across 28 symbols in 2015-2026, being REIT and SML distributions whose "dividend" is not a
per-share equity cash flow. Folding those into ``Dividends`` would corrupt the back-adjustment.

Parse coverage, measured on the full 1995-2026 pull
---------------------------------------------------
43,789 rows / 3,930 symbols, classified as **25,573 dividends, 682 sub-divisions, 671 bonus issues
and 16,863 unconverted (38.5%)**. The unconverted bucket is dominated by things that are not events
at all: 8,649 ``Annual General Meeting`` notices, 1,303 ``Agm``, 843 ``Interest Payment``, 177
``Rights``, 159+108 ``Buy Back``, 90 ``Demerger``. Of the 631 unconverted rows that still *look*
price-relevant, all are either a **nil** dividend (``Dividend - Nil``, ``Agm/Dividend-Nil``, bare
``Div`` — a real declaration of zero), a REIT/SML ``Per Unit`` distribution, a bonus of preference
shares or debentures, or a filing with no amount in it (``Dividend - Rs Per Share``,
``Agm/Div Fin-10% + Spl-10%``). Those stay unconverted on purpose. There is no rule that invents a
number for them, and their count is in ``attrs["bfinance_unconverted_action_rows"]``.

Widening the dividend grammar is worth measuring rather than guessing: three passes over the full
pull moved the converted count 25,177 -> 25,407 -> 25,573, and every widening was re-audited for
false positives against all 43,789 rows. That audit is what caught the spellings listed beside
:data:`_DIVIDEND_RE`; it also caught a real regression, when a loosened separator class stopped
consuming the compound ``/-`` token and silently halved the dividend count.

The ``PrvsClsgPric`` oracle does **not** work, and this module records why
----------------------------------------------------------------------
The bhavcopy's own ``PrvsClsgPric`` (UDiFF) / ``PREVCLOSE`` (legacy) column was evaluated as an
action detector and as a verification oracle for the derived factor. **It is neither.** Measured:

* RELIANCE, ex-date 19-Aug-2024, Rs 10 dividend: ``ClsPric(2024-08-16) = 2956.4`` and
  ``PrvsClsgPric(2024-08-19) = 2956.4``. **Equal.** The dividend is not deducted.
* SHRIRAMFIN, ex-date 10-Jan-2025, 5x sub-division: ``ClsPric(2025-01-09) = 2809.85`` and
  ``PrvsClsgPric(2025-01-10) = 2809.85``. **Equal.** The split is not reflected.
* RELIANCE, ex-date 28-Oct-2024, 1:1 bonus: ``ClsPric(2024-10-25) = 2655.7`` and
  ``PrvsClsgPric(2024-10-28) = 2655.7``. **Equal.**
* Control, an ordinary session: ``ClsPric(t-1) / PREVCLOSE(t) = 1.0000``.

NSE publishes the previous close **in the rupees that were actually traded on the previous day**,
unadjusted for any intervening action, exactly like every other vendor. So the column is a
tautology with ``ClsPric(t-1)``: it carries zero information about whether a corporate action
occurred, and it cannot verify a factor this module computes. That rules it out, and it is why
:func:`unexplained_price_jumps` exists to do the consistency check locally instead.
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
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from bfinance.nse.marketlens import validate_nse_symbol
from bfinance.utils.exceptions import (
    ParsingError,
    RateLimitExceededError,
    UpstreamServiceError,
)

logger = logging.getLogger("bfinance")

IST = ZoneInfo("Asia/Kolkata")

#: Provenance tag recorded on every frame this module produces.
PROVENANCE = "nse_corporate_actions"

#: Landing page whose cookie primes the retry path. Fetched at most once per client.
LANDING_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"

BASE_URL = "https://www.nseindia.com"
API_PATH = "/api/corporates-corporateActions"

#: NSE segment keys that answer with the row-array shape. ``debt`` does not: it answers
#: ``{"data": [], "msg": "no data found"}`` and is therefore rejected up front rather than
#: producing a confusing error later.
INDICES: Tuple[str, ...] = ("equities", "sme", "mf")

#: Upstream date format. The absent-date sentinel is the string ``"-"`` (not null, not empty).
DATE_FORMAT = "%d-%b-%Y"
ABSENT = "-"

#: Output columns. The ex-date is the index, not a column, so it is not listed here. ``Value`` and
#: ``SplitRatio`` are separate because a single subject can be both a dividend and a split, and one
#: column cannot hold a rupee amount and a ratio at once. The rest is the raw upstream row, carried
#: so a consumer can see exactly what a value was derived from.
COLUMNS: Tuple[str, ...] = (
    "Symbol", "Action", "Value", "SplitRatio", "Basis", "Components", "Subject",
    "RecordDate", "FaceValue", "Series", "Company", "ISIN",
)

#: The yfinance-shaped action columns this module fills.
ACTION_COLUMNS: Tuple[str, ...] = ("Dividends", "Stock Splits")

#: ``Action`` values. The first three carry a ``Value``; :data:`OTHER_ACTION` never does.
DIVIDEND = "dividend"
BONUS = "bonus"
SUBDIVISION = "subdivision"
OTHER_ACTION = "other"

#: Action kinds that a yfinance ``Stock Splits`` ratio is derived from. A row can be both, in
#: which case the components multiply (see the module docstring).
PRICE_AFFECTING_ACTIONS = (BONUS, SUBDIVISION)

#: How far back the feed reaches. Measured: the market-wide form for 1990-2026 returns ex-dates
#: from 1995 onward, and the per-symbol form for an old listing goes back to 2006. 1990 is a
#: conservative floor, deliberately earlier than anything observed, so a caller asking for the
#: maximum available history is not silently clipped by this module.
FEED_FIRST_DATE = "1990-01-01"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Global pacing, deliberately the same process-global scheme as ``nse.bhavcopy`` and
# ``nse.marketlens``: a market-wide pull is a large payload and NSE blocks bursty clients.
_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

# ------------------------------------------------------------------ subject grammar
#
# ``subject`` is free text written by ~2,800 listed companies and their filing agents, so every
# pattern below was taken from the 43,789-row market-wide pull, not invented. The awkward parts
# (double spaces, "Re" for paise, a leading slash, "Per Sh") are all observed spellings.

#: ``Dividend - Rs 6 Per Share`` / ``Interim Dividend - Rs 10 Per Share`` /
#: ``Annual General Meeting/Dividend - Rs  15 Per Share`` (double space is a real upstream value) /
#: ``Special Dividend - Rs 5 Per Share`` / ``Dividend - Rs 1.50/- Per Share``.
#:
#: Four legacy spellings are live on RELIANCE's own record: ``Int Div-Rs.11/- Per Sh``,
#: ``Int Div-Rs.13/- Per Share``, ``Dividend Rs. 9.50 Per Equity Share`` and
#: ``Annual General Meeting/ Dividend Rs. 9 Per Equity Share``. A full 43,788-row pull added three
#: more that a strict pattern missed, and each is a real dividend being dropped:
#:
#: * ``Pr Share`` / ``Pr Sh`` — a typo for ``Per Share``, ~120 rows (``Dividend-Rs.5 Pr Share``).
#: * ``Annual General Meetingdividend - Rs 9 Per Share`` — "dividend" run into the preceding word,
#:   16 rows. A ``\b`` or lookbehind before "div" cannot match it, so there is no guard on the
#:   prefix at all: what keeps this from over-matching is the ``Rs <digits> per share`` body, which
#:   a full 43,788-row pull was re-audited against for false positives.
#: * ``Dividend Rs- 5 Per Share``, ``Dividend Of Rs.5 Per Share``, ``Div.Rs.5 Per Share``,
#:   ``Dividend-Rs.5/ Per Share`` — punctuation between the tokens varies (4+4+3+3 rows).
#:
#: A trailing ``Per Share`` is still **required**: it is what separates an equity cash dividend from
#: a REIT/SML "Total Dividend ... Per Unit" row, and from a ``Dividend - Nil`` declaration.
_DIVIDEND_RE = re.compile(
    r"div(?:idend|s)?\s*[-:.=/]*\s*(?:of\s*)?(?:rs|re)\.?\s*[-:.=/]*\s*"
    r"([\d,]+(?:\.\d+)?)\s*(?:/-|[-/])?\s*(?:per|pr)\s*(?:equity\s*)?sh",
    re.IGNORECASE,
)
#: Legacy filings quote a percentage of paid-up capital rather than a rupee amount:
#: ``Agm/Dividend-10%`` (877 rows) and ``Agm/Dividend - 12.5 %`` (644 rows) over 2005-2026.
_DIVIDEND_PCT_RE = re.compile(
    r"div(?:idend|s)?\s*[-:.=/]*\s*([\d,]+(?:\.\d+)?)\s*%", re.IGNORECASE
)
#: ``Bonus 1:1`` / ``Bonus 3:1`` / ``Bonus 1:2`` / ``Bonus 4:1``. A ``-`` or ``:`` may follow
#: "Bonus" (observed: ``Bonus- 1:2``, ``Bonus - 1:2``), but a *word* may not: ``Bonus Ncrps 1:10``,
#: ``Bonus Debentures 1:1`` and ``Bonus - 1 Debenture For 1 Equity Share Held`` are preference
#: shares and debentures, which leave the equity share count untouched and therefore produce no
#: price factor. The word-class check below rejects them explicitly rather than relying on the
#: regex to fail, so the exclusion is visible in the code instead of implicit in a pattern.
_BONUS_RE = re.compile(r"bonus\s*[-:]*\s*(\d+)\s*[:/]\s*(\d+)", re.IGNORECASE)
#: Instruments that are not the common equity. A bonus of these changes the capital structure but
#: not the number of equity shares, so it is not a split for a price series.
_NON_EQUITY_BONUS_RE = re.compile(r"ncrps|debenture|warrant", re.IGNORECASE)
#: A bonus denominator beyond this is not a share bonus. Measured across the 26,244-row
#: 2015-2026 pull: every real bonus denominator is <= 26 (``Bonus 1:25`` occurs twice, for KTIL).
#: Two rows carry ``Bonus 1 : 1250`` and ``Bonus 25:202``, which would parse to a 1.0008 "split"
#: and silently rescale every prior bar by a factor indistinguishable from 1 — a factor that looks
#: real and is noise. Rejecting them leaves the event visible as an unconverted row instead.
MAX_BONUS_DENOMINATOR = 100
#: ``Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share`` and the older
#: ``Fv Split Rs.10/- To Rs.5/`` / ``Fv Splt Frm Rs 10 To Rs 2`` /
#: ``Face Valus Split (Sub-Division) - From Rs 10/- Per To Rs 2/- Per Share`` (upstream typo, real).
#:
#: Matching a whole template is what failed on the first four of those, so this anchors on the
#: *currency amounts* instead: find the split keyword, take the amount after "from"/"frm" (or the
#: first amount after the keyword when neither word is present), and the amount after "to".
#: Anchoring matters for a composite subject — ``Interim Dividend - Rs 1.50/- Per Share / Face Value
#: Split - From Rs 10/- Per Share To Rs 5/- Per Share`` carries three amounts, and a naive
#: first/last pair would compute 1.50/5 = 0.3 instead of 10/5 = 2.0.
_SPLIT_KEYWORD_RE = re.compile(
    r"(?:face\s*val(?:ue|us)?\s*split|fv\s*s?plt|fv\s*split|stock\s*split|split|sub-?division)",
    re.IGNORECASE,
)
_FROM_AMT_RE = re.compile(
    r"\b(?:from|frm)\s*(?:rs\.?|re\.?)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE
)
_TO_AMT_RE = re.compile(r"\bto\s*(?:rs\.?|re\.?)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"(?:rs\.?|re\.?)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


# ------------------------------------------------------------------ pure helpers


def validate_index(index: str) -> str:
    """Uppercase and validate the segment before any request.

    ``debt`` is rejected deliberately: it answers ``{"data": [], "msg": "no data found"}`` — a dict,
    not the row array every other index returns — and accepting it would push a shape mismatch into
    the parser instead of surfacing it as an argument error.
    """
    if not isinstance(index, str):
        raise ValueError(f"index must be a string, got {type(index).__name__}")
    clean = index.strip().lower()
    if clean not in INDICES:
        raise ValueError(
            f"index={index!r} is not supported. Must be one of {', '.join(INDICES)}. "
            f"'debt' is excluded because that segment answers with a different JSON shape."
        )
    return clean


def _fmt_day(value: Union[str, pd.Timestamp, None]) -> Optional[str]:
    """Format a date as NSE's ``DD-MM-YYYY`` query format, or ``None`` to omit the parameter."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    stamp = pd.to_datetime(value)
    if stamp is pd.NaT or pd.isna(stamp):
        raise ValueError(f"date={value!r} is not parseable")
    return stamp.strftime("%d-%m-%Y")


def corporate_actions_url(
    symbol: Optional[str] = None,
    start: Union[str, pd.Timestamp, None] = None,
    end: Union[str, pd.Timestamp, None] = None,
    index: str = "equities",
) -> str:
    """Build the request URL. Pure and offline — no request is issued.

    Exposed so a caller can see exactly what will be requested, and so the always-send-a-date-range
    rule (see the module docstring on the 20-row cap) is visible without reading the client.
    """
    clean_index = validate_index(index)
    parts = [f"index={clean_index}"]
    if symbol is not None:
        parts.append(f"symbol={validate_nse_symbol(symbol)}")
    first = _fmt_day(start)
    last = _fmt_day(end)
    if first:
        parts.append(f"from_date={first}")
    if last:
        parts.append(f"to_date={last}")
    return f"{BASE_URL}{API_PATH}?" + "&".join(parts)


def _to_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    cleaned = str(text).replace(",", "").replace("/-", "").replace("-", "", 0).strip()
    if not cleaned or cleaned == ABSENT:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _ex_date(text: Optional[str]) -> Optional[pd.Timestamp]:
    """Parse NSE's ``DD-MMM-YYYY``, mapping the ``"-"`` sentinel to ``None``."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or raw == ABSENT:
        return None
    parsed = pd.to_datetime(raw, format=DATE_FORMAT, errors="coerce")
    if pd.isna(parsed):
        # Tolerate a locale/format drift rather than dropping the row silently. An unparseable
        # ex-date has no place in a time series, so it becomes NaT and is counted in attrs.
        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def parse_dividend_amount(subject: str, face_value: Optional[float]) -> Tuple[Optional[float], str]:
    """Extract the cash dividend per share from a free-text ``subject``.

    Returns ``(amount, basis)``; ``amount`` is ``None`` when the subject declares no rupee
    dividend. ``basis`` records *how* the number was obtained, because the two routes mean
    different things:

    * ``"per_share"`` — a published rupee amount. Used as-is. This is the unadjusted figure
      actually payable on the ex-date, which is what the back-adjustment needs.
    * ``"pct_of_face_value"`` — a legacy ``Dividend-10%`` filing. ``10%`` of a published
      ``faceVal`` of 10 is Rs 1.00/share. Deterministic arithmetic on two published fields, not an
      inference about a missing fact, so it is used — and labelled, so a consumer can exclude it.
    * ``""`` — no dividend declared.

    A ``... Per Unit`` subject never reaches here as a dividend: :func:`parse_action_subject`
    rejects it first, because a REIT/SML distribution is not a per-share equity cash flow.
    """
    if not subject or "per unit" in subject.lower():
        return None, ""
    amounts = [float(a.replace(",", "")) for a in _DIVIDEND_RE.findall(subject)]
    if amounts:
        # A subject may declare a dividend and a special dividend on one ex-date
        # ("Dividend - Rs 22 Per Share/Special Dividend - Rs 5 Per Share"). A holder receives both,
        # and yfinance has exactly one slot per date, so the components are summed.
        return round(sum(amounts), 4), "per_share"
    pct = _DIVIDEND_PCT_RE.search(subject)
    if pct and face_value:
        try:
            rate = float(pct.group(1).replace(",", ""))
        except ValueError:
            return None, ""
        return round(rate / 100.0 * face_value, 4), "pct_of_face_value"
    return None, ""


def parse_bonus_ratio(subject: str) -> Optional[float]:
    """``Bonus a:b`` -> ``(a + b) / b``, the yfinance split ratio for a bonus issue.

    ``Bonus 1:1`` -> 2.0, ``Bonus 3:1`` -> 4.0, ``Bonus 1:2`` -> 1.5, ``Bonus 4:1`` -> 5.0. All four
    were confirmed against yfinance on 2026-09-27.

    Returns ``None`` — never a near-1.0 guess — when the subject is a bonus of preference shares or
    debentures (:data:`_NON_EQUITY_BONUS_RE`) or when the denominator exceeds
    :data:`MAX_BONUS_DENOMINATOR`.
    """
    if not subject or _NON_EQUITY_BONUS_RE.search(subject):
        return None
    match = _BONUS_RE.search(subject)
    if not match:
        return None
    try:
        a, b = int(match.group(1)), int(match.group(2))
    except ValueError:
        return None
    if b <= 0 or a < 0 or b > MAX_BONUS_DENOMINATOR:
        return None
    return round((a + b) / b, 6)


def parse_subdivision_ratio(subject: str) -> Optional[float]:
    """``Face Value Split (Sub-Division) - From Rs X/- ... To Rs Y/-`` -> ``X / Y``.

    An Indian listed company effects a stock split by changing its face value, so the face-value
    sub-division **is** the price split: Rs 10 -> Rs 2 is a 5-for-1, Rs 10 -> Re 1 is 10-for-1. All
    eight observed spellings are handled; see the module docstring for the real strings.
    """
    if not subject:
        return None
    keyword = _SPLIT_KEYWORD_RE.search(subject)
    if keyword is None:
        return None

    target_match = _TO_AMT_RE.search(subject)
    if target_match is None:
        return None
    # The source amount is the one the subject explicitly attributes to "from"/"frm". When neither
    # word is present (the older "Fv Split Rs.10/- To Rs.5/" filings) it is the first currency
    # amount that follows the split keyword -- not the first amount in the string, which on a
    # composite subject is the dividend.
    source_match = _FROM_AMT_RE.search(subject)
    if source_match is None:
        tail = subject[keyword.end():]
        first_amount = _AMOUNT_RE.search(tail)
        if first_amount is None:
            return None
        source_text = first_amount.group(1)
    else:
        source_text = source_match.group(1)
    try:
        source = float(source_text.replace(",", ""))
        target = float(target_match.group(1).replace(",", ""))
    except ValueError:
        return None
    if target <= 0 or source <= 0:
        return None
    ratio = round(source / target, 6)
    if ratio == 1.0:
        # A face value that does not change is not a corporate action. Returning 1.0 would write a
        # "split" of 1.0 into the frame, which reads as a real ratio and contributes nothing --
        # and it is the exact shape a mis-parse produces (an absent "from" amount falling through
        # to the "to" amount, so 2/2 = 1.0). No factor, no row value.
        return None
    return ratio


def parse_action_subject(
    subject: Optional[str], face_value: Optional[float] = None
) -> Tuple[str, Optional[float], str]:
    """Classify one ``subject`` into ``(Action, DividendAmount, Basis)``.

    ``Action`` is one of :data:`DIVIDEND`, :data:`BONUS`, :data:`SUBDIVISION`, :data:`OTHER_ACTION`.
    The returned float is the **cash dividend per share**, or ``None`` — the split ratio is *not*
    returned here, because it is a different quantity and belongs in its own column
    (``SplitRatio``); :func:`split_ratio` is its only reader. ``Basis`` records how the cash amount
    was obtained; see :func:`parse_dividend_amount`.

    A single subject can be both at once — ``Interim Dividend - Rs 1.50/- Per Share / Face Value
    Split - From Rs 10/- Per Share To Rs 5/- Per Share`` — in which case ``Action`` is
    :data:`DIVIDEND` (the cash amount is what this column carries) and ``SplitRatio`` is populated
    independently, so nothing is lost to the overlap.

    :data:`OTHER_ACTION` is a real outcome with ``None`` throughout: an event this module refuses to
    convert into a factor is recorded, never guessed at and never dropped.
    """
    text = (subject or "").strip()
    if not text or text == ABSENT:
        return OTHER_ACTION, None, ""

    # "Per Unit" rows are REIT/SML distributions -- a 359-row slice of the 2015-2026 pull across 28
    # symbols. Their "dividend" is a distribution on a unit, not a per-share equity cash flow, so
    # folding it into Dividends would corrupt the back-adjustment.
    if "per unit" in text.lower():
        return OTHER_ACTION, None, ""

    amount, basis = parse_dividend_amount(text, face_value)
    if amount is not None:
        return DIVIDEND, amount, basis
    if parse_bonus_ratio(text) is not None:
        return BONUS, None, "ratio"
    if parse_subdivision_ratio(text) is not None:
        return SUBDIVISION, None, "ratio"
    return OTHER_ACTION, None, ""


def split_ratio(subject: Optional[str]) -> Tuple[Optional[float], str]:
    """Total price-affecting split ratio for one ``subject``.

    Returns ``(ratio, components)``; ``ratio`` is ``None`` when the subject declares no split. A
    subject that declares both a bonus and a sub-division has its factors **multiplied** — a
    ``Bonus 1:1`` alongside a ``Rs 10 -> Rs 2`` sub-division leaves a holder with ten shares where
    there was one, so the combined factor is 2 x 5. This is the one inference in the module and it
    is stated rather than hidden; the components string records it was composite.
    """
    text = (subject or "").strip()
    if not text or text == ABSENT or "per unit" in text.lower():
        return None, ""
    total = 1.0
    parts: List[str] = []
    bonus = parse_bonus_ratio(text)
    if bonus is not None:
        total *= bonus
        parts.append(f"bonus={bonus}")
    subdivision = parse_subdivision_ratio(text)
    if subdivision is not None:
        total *= subdivision
        parts.append(f"subdivision={subdivision}")
    if not parts:
        return None, ""
    return round(total, 6), "+".join(parts)


def _rows_from_payload(payload: Any, url: str = "") -> List[Dict[str, Any]]:
    """Normalise the two response shapes into a list of row dicts.

    ``[]`` is the empty feed and ``{"data": [], "msg": "no data found"}`` is the same thing said in
    the other shape (measured on ``index=debt``). Anything else is a schema change and raises.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if data is None or data == []:
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        raise ParsingError(
            f"NSE corporate-actions payload has an unexpected 'data' type "
            f"({type(data).__name__}) for {url}. Upstream schema changed."
        )
    raise ParsingError(
        f"NSE corporate-actions payload is a {type(payload).__name__}, expected a JSON array of "
        f"rows for {url}. Upstream schema changed."
    )


def parse_corporate_actions(
    payload: Any,
    *,
    symbol: Optional[str] = None,
    url: str = "",
) -> pd.DataFrame:
    """Turn a raw payload into the parsed actions frame. Pure — no network, no clock.

    Returns columns :data:`COLUMNS` indexed by a naive ``DatetimeIndex`` named ``Date`` (one row
    per feed row, so an unparsed event is still visible), plus ``attrs`` recording the source, the
    requested symbol, the action-kind histogram, and the count of rows deliberately *not* converted
    into a factor.

    Rows are never dropped for being unparseable. An event with a missing or malformed ex-date has
    no place in a time series, so it is kept with a ``NaT`` index, counted in
    ``attrs["bfinance_undated_action_rows"]``, and excluded from every downstream join by
    construction — invisible to the arithmetic, visible to the reader.
    """
    rows = _rows_from_payload(payload, url=url)
    records: List[Dict[str, Any]] = []
    undated = 0
    for row in rows:
        raw_subject = row.get("subject")
        subject = "" if raw_subject is None else str(raw_subject).strip()
        face_value = _to_float(row.get("faceVal"))
        action, value, basis = parse_action_subject(subject, face_value)
        ratio, ratio_parts = split_ratio(subject)
        ex_date = _ex_date(row.get("exDate"))
        if ex_date is None:
            undated += 1
        records.append(
            {
                "Date": ex_date,
                "Symbol": (str(row.get("symbol")) or "").strip().upper() or None,
                "Action": action,
                # Cash dividend per share, unadjusted. The split factor lives in its own column
                # because a subject can be both at once and one column cannot hold both.
                "Value": value,
                "SplitRatio": ratio,
                "Basis": basis,
                # Every price-affecting factor found, so a composite subject ("Bonus 1:1 / Face
                # Value Split From Rs 10 To Rs 2") is auditable instead of being a bare number.
                "Components": ratio_parts or "-",
                "Subject": subject,
                "RecordDate": _ex_date(row.get("recDate")),
                "FaceValue": face_value,
                "Series": (str(row.get("series")) or "").strip() or None,
                "Company": (str(row.get("comp")) or "").strip() or None,
                "ISIN": (str(row.get("isin")) or "").strip() or None,
            }
        )

    if records:
        frame = pd.DataFrame(records)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("Date"), errors="coerce"))
        frame.index.name = "Date"
        frame = frame.sort_index(kind="stable", na_position="last")
    else:
        frame = pd.DataFrame(
            {c: pd.Series(dtype=object) for c in COLUMNS},
            index=pd.DatetimeIndex([], name="Date"),
        )

    counts = frame["Action"].value_counts().to_dict() if not frame.empty else {}
    frame.attrs["bfinance_provenance"] = PROVENANCE
    frame.attrs["bfinance_symbol"] = (symbol or "").upper() or None
    frame.attrs["bfinance_source_url"] = url or None
    frame.attrs["bfinance_source_row_count"] = len(rows)
    frame.attrs["bfinance_undated_action_rows"] = undated
    frame.attrs["bfinance_action_counts"] = {k: int(v) for k, v in counts.items()}
    frame.attrs["bfinance_unconverted_action_rows"] = int(counts.get(OTHER_ACTION, 0))
    frame.attrs["bfinance_undated_action_subjects"] = (
        [str(s) for s in frame.loc[frame.index.isna(), "Subject"]] if undated else []
    )
    return frame


def actions_frame(
    events: pd.DataFrame,
    *,
    symbol: Optional[str] = None,
    url: str = "",
) -> pd.DataFrame:
    """Collapse parsed events into the yfinance ``actions`` shape.

    Returns a ``DataFrame`` indexed by an ``Asia/Kolkata``-aware ``DatetimeIndex`` named ``Date``
    with exactly :data:`ACTION_COLUMNS` as float columns, union of event dates, ascending. Divisors
    dated ``NaT`` are excluded (they have no place in a time series and are counted in attrs) and
    a duplicate event on one date is **summed** rather than silently last-wins, because two
    dividends on one ex-date are two cash payments and a dividend plus a split on one ex-date is
    one payment and one ratio.

    The dividend column holds the **unadjusted** rupee amount. It is deliberately not normalised by
    subsequent splits: ``1 - D / prev_close`` is defined in pre-action rupees, and normalising here
    would double-discount. See the module docstring for the measured RELIANCE and SHRIRAMFIN cases.
    """
    index = pd.DatetimeIndex([], name="Date", tz=IST)
    records: Dict[pd.Timestamp, Dict[str, float]] = {}
    if events is not None and not events.empty:
        usable = events[events.index.notna()]
        for stamp, action, value, ratio in zip(
            usable.index, usable["Action"], usable["Value"], usable["SplitRatio"]
        ):
            day = pd.Timestamp(stamp).tz_localize(IST)
            slot = records.setdefault(day, {"Dividends": 0.0, "Stock Splits": 0.0})
            if action == DIVIDEND and value is not None and not pd.isna(value):
                # Two dividends on one ex-date are two cash payments; the frame has one slot per
                # date, so they sum.
                slot["Dividends"] += float(value)
            if action in PRICE_AFFECTING_ACTIONS and ratio is not None and not pd.isna(ratio):
                # split_ratio() has already multiplied a composite subject's bonus and
                # sub-division factors into one number, so this assigns rather than accumulates.
                # Two *separate* feed rows on one date do multiply, which is the only correct
                # reading: two bonus issues on one ex-date are two issuances.
                factor = float(ratio)
                slot["Stock Splits"] = (
                    factor if slot["Stock Splits"] == 0.0 else slot["Stock Splits"] * factor
                )

    if records:
        frame = pd.DataFrame.from_dict(records, orient="index")[list(ACTION_COLUMNS)].astype(float)
        frame.index = pd.DatetimeIndex(list(records), name="Date")
        frame.sort_index(inplace=True)
    else:
        frame = pd.DataFrame(
            {c: pd.Series(dtype=float) for c in ACTION_COLUMNS}, index=index
        )

    frame.attrs["bfinance_provenance"] = PROVENANCE
    frame.attrs["bfinance_symbol"] = (symbol or "").upper() or None
    frame.attrs["bfinance_source_url"] = url or None
    frame.attrs["bfinance_action_counts"] = dict(events.attrs.get("bfinance_action_counts", {})) if (
        events is not None and events.attrs
    ) else {}
    frame.attrs["bfinance_unconverted_action_rows"] = int(
        events.attrs.get("bfinance_unconverted_action_rows", 0)
    ) if (events is not None and events.attrs) else 0
    frame.attrs["bfinance_dividend_basis"] = "unadjusted_rupees_at_ex_date"
    return frame


def empty_actions_frame(symbol: Optional[str] = None, url: str = "") -> pd.DataFrame:
    """The honest shape of "the feed has nothing for this symbol": zero rows, real columns.

    Never a synthesised row, and still stamped with provenance so a consumer can tell "NSE has no
    corporate action on record" from "we did not look".
    """
    frame = actions_frame(None, symbol=symbol, url=url)
    frame.attrs["bfinance_empty_feed"] = True
    return frame


def unexplained_price_jumps(
    close: pd.Series, actions: pd.DataFrame, tolerance: float = 0.25
) -> pd.DataFrame:
    """Report day-over-day price falls that no event in ``actions`` explains.

    This is the local replacement for the bhavcopy ``PrvsClsgPric`` column, which was measured to be
    byte-identical to ``Close.shift(1)`` and therefore useless for this (see the module docstring).
    A fall larger than ``tolerance`` on a date with no dividend and no split is either a corporate
    action this feed did not record, or an unadjusted price series — either way the consumer should
    see it rather than trust a silent ``Adj Close``.

    Returns a frame with ``Date``, ``Close``, ``PriorClose``, ``Ratio``, ``ExplainedBy``.
    ``ExplainedBy`` is ``"dividend"``, ``"split"``, ``"dividend+split"`` or ``None``.
    """
    if close is None or len(close) < 2:
        return pd.DataFrame(columns=["Date", "Close", "PriorClose", "Ratio", "ExplainedBy"])
    prior = close.astype(float).shift(1)
    ratio = prior / close.astype(float)
    moves = ratio[ratio > 1.0 + tolerance]

    # The actions index is Asia/Kolkata-aware and the price index is naive, so both sides are
    # reduced to a bare calendar date before they are compared. Comparing them as-is would never
    # match, and every real event would be reported as unexplained -- the exact false alarm this
    # function exists to prevent.
    explained: Dict[Any, str] = {}
    if actions is not None and not actions.empty and actions.index.notna().any():
        for stamp, row in actions[actions.index.notna()].iterrows():
            kinds = []
            if float(row.get("Dividends", 0.0) or 0.0) != 0.0:
                kinds.append("dividend")
            if float(row.get("Stock Splits", 0.0) or 0.0) not in (0.0, 1.0):
                kinds.append("split")
            if kinds:
                day = pd.Timestamp(stamp)
                if day.tzinfo is not None:
                    day = day.tz_localize(None)
                explained[day.normalize()] = "+".join(kinds)

    out = pd.DataFrame(
        {
            "Date": moves.index,
            "Close": close.reindex(moves.index).to_numpy(),
            "PriorClose": prior.reindex(moves.index).to_numpy(),
            "Ratio": moves.to_numpy(),
            "ExplainedBy": [
                explained.get(pd.Timestamp(d).normalize()) for d in moves.index
            ],
        }
    )
    return out[out["ExplainedBy"].isna()].reset_index(drop=True)


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


# ------------------------------------------------------------------ client


class CorporateActionsClient:
    """Client for NSE's corporate-filings corporate-actions feed.

    Stateless per call and safe to reuse. One request per symbol — the market-wide form
    (``symbol=None``) is available for a cross-section, but the per-symbol form is what a single
    instrument needs and it is never capped once a date range is sent.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 300.0,
        prime_cookie_on_block: bool = True,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self.prime_cookie_on_block = prime_cookie_on_block
        self._last_request_time = 0.0
        self._cookie_primed = False
        #: Set when a blocked response forced a landing-page prime, so a caller can see the retry
        #: path actually ran rather than assuming it was needed.
        self.primed_cookie = False

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
        """Pace requests against a process-global clock.

        Same 300 ms floor plus jitter as the sibling NSE clients. A 30-year market-wide pull is a
        multi-megabyte payload; bursting N of those is what gets a client blocked.
        """
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

        Measured as **unnecessary** for this route (4/4 HTTP 200 with no cookie primed). It exists
        because the generic ``/api/quote-equity`` on the same host does 403 even primed, so which
        routes are blocked is a property of the edge, not of the client.
        """
        if not self.prime_cookie_on_block or self._cookie_primed:
            return
        try:
            await client.get(LANDING_URL, headers=self._headers(ajax=False))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.debug("corporate actions: cookie priming failed (%s); continuing.", exc)
        self._cookie_primed = True
        self.primed_cookie = True

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        """GET with retry. Raises typed errors; never returns ``None`` for a failure.

        A 403/503 is treated as "possibly needs a session" rather than as a hard failure: the cookie
        is primed **once** and the request retried. That single retry is the whole
        ``jugaad-data`` pattern, and it is bounded — a second block raises rather than looping.
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
                    "corporate actions: HTTP %d for %s; primed a session cookie and retrying once.",
                    response.status_code, url,
                )
                continue

            if response.status_code == 429:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RateLimitExceededError(
                    f"NSE corporate-actions feed rate limited (HTTP 429) for {url}"
                )

            if response.status_code >= 500:
                last_error = UpstreamServiceError(
                    f"NSE corporate-actions feed returned HTTP {response.status_code} for {url}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code != 200:
                raise UpstreamServiceError(
                    f"NSE corporate-actions feed returned unexpected HTTP {response.status_code} for "
                    f"{url}. The route is {API_PATH} (camelCase); the kebab-case spellings return "
                    f"404 and mean the route is gone, not that the client is blocked."
                )

            try:
                return response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ParsingError(
                    f"NSE corporate-actions feed returned a non-JSON body for {url} "
                    f"(first 160 chars: {response.text[:160]!r}). An HTML page served with a 200 "
                    f"is the usual cause."
                ) from exc

        raise UpstreamServiceError(
            f"NSE corporate-actions feed failed after {self.max_retries} attempts: {url}"
        ) from last_error

    async def _fetch(
        self,
        symbol: Optional[str],
        start: Optional[Union[str, pd.Timestamp]],
        end: Optional[Union[str, pd.Timestamp]],
        index: str,
    ) -> Tuple[pd.DataFrame, str]:
        clean_index = validate_index(index)
        clean_symbol = validate_nse_symbol(symbol) if symbol is not None else None
        # Always send a range. Omitting it silently caps a per-symbol query at 20 rows (measured),
        # which truncates the dividend history and produces a confidently wrong Adj Close.
        first = start if start is not None else FEED_FIRST_DATE
        last = end if end is not None else pd.Timestamp.now(tz=IST)
        url = corporate_actions_url(clean_symbol, first, last, clean_index)

        async with httpx.AsyncClient(
            timeout=self.timeout, proxy=self.proxy, follow_redirects=True
        ) as client:
            payload = await self._get_json(client, url)
        return parse_corporate_actions(payload, symbol=clean_symbol, url=url), url

    # ------------------------------------------------------------------ public

    async def corporate_actions_async(
        self,
        symbol: Optional[str] = None,
        start: Union[str, pd.Timestamp, None] = None,
        end: Union[str, pd.Timestamp, None] = None,
        index: str = "equities",
    ) -> pd.DataFrame:
        """Real, ex-dated corporate actions for one symbol (or the whole market when omitted).

        Args:
            symbol: NSE symbol. A ``.NS`` suffix is accepted and stripped. ``None`` returns the
                market-wide feed in a single request.
            start: Inclusive ex-date lower bound. Defaults to :data:`FEED_FIRST_DATE`.
            end: Inclusive ex-date upper bound. Defaults to today.
            index: One of :data:`INDICES`.

        Returns:
            A parsed frame with columns :data:`COLUMNS` and a naive ``DatetimeIndex`` named
            ``Date``, plus ``attrs['bfinance_provenance'] = 'nse_corporate_actions'``.

        Raises:
            ValueError: ``index`` is not in :data:`INDICES`, or a date is unparseable.
            NotAnNseSymbolError: ``symbol`` is BSE-shaped, an index, or a scrip code.
            RateLimitExceededError: HTTP 429 after retries.
            UpstreamServiceError: transport failure, 5xx, or an unexpected status after retries.
            ParsingError: a non-JSON body, or a payload shape that is neither row array nor the
                ``{"data": [], "msg": ...}`` empty form.
        """
        frame, _ = await self._fetch(symbol, start, end, index)
        return frame

    def corporate_actions(
        self,
        symbol: Optional[str] = None,
        start: Union[str, pd.Timestamp, None] = None,
        end: Union[str, pd.Timestamp, None] = None,
        index: str = "equities",
    ) -> pd.DataFrame:
        """Sync variant of :meth:`corporate_actions_async`."""
        return _run_async(self.corporate_actions_async(symbol, start, end, index))

    async def actions_async(
        self,
        symbol: str,
        start: Union[str, pd.Timestamp, None] = None,
        end: Union[str, pd.Timestamp, None] = None,
        index: str = "equities",
    ) -> pd.DataFrame:
        """:meth:`corporate_actions_async` collapsed to the yfinance ``actions`` shape.

        Returns exactly the columns :data:`ACTION_COLUMNS` on an ``Asia/Kolkata`` index, ready to
        left-join onto a history frame. An empty feed yields zero rows, not a guess.
        """
        events, url = await self._fetch(symbol, start, end, index)
        return actions_frame(events, symbol=symbol, url=url)

    def actions(
        self,
        symbol: str,
        start: Union[str, pd.Timestamp, None] = None,
        end: Union[str, pd.Timestamp, None] = None,
        index: str = "equities",
    ) -> pd.DataFrame:
        """Sync variant of :meth:`actions_async`."""
        return _run_async(self.actions_async(symbol, start, end, index))


_DEFAULT_CLIENT: Optional[CorporateActionsClient] = None


def _default_client() -> CorporateActionsClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = CorporateActionsClient()
    return _DEFAULT_CLIENT


# ------------------------------------------------------------------ module shortcuts


def corporate_actions(
    symbol: Optional[str] = None,
    start: Union[str, pd.Timestamp, None] = None,
    end: Union[str, pd.Timestamp, None] = None,
    index: str = "equities",
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`CorporateActionsClient.corporate_actions`.

    >>> from bfinance.nse.corporate_actions import corporate_actions      # doctest: +SKIP
    >>> corporate_actions("RELIANCE", "2024-01-01", "2026-09-27")           # doctest: +SKIP
    """
    return _default_client().corporate_actions(symbol, start, end, index)


def corporate_actions_async(
    symbol: Optional[str] = None,
    start: Union[str, pd.Timestamp, None] = None,
    end: Union[str, pd.Timestamp, None] = None,
    index: str = "equities",
) -> pd.DataFrame:
    """Async variant of :func:`corporate_actions`."""
    return _default_client().corporate_actions_async(symbol, start, end, index)


def actions(
    symbol: str,
    start: Union[str, pd.Timestamp, None] = None,
    end: Union[str, pd.Timestamp, None] = None,
    index: str = "equities",
) -> pd.DataFrame:
    """Module-level shortcut for :meth:`CorporateActionsClient.actions`.

    Returns the ``['Dividends', 'Stock Splits']`` frame with true ex-dates.
    """
    return _default_client().actions(symbol, start, end, index)


def actions_async(
    symbol: str,
    start: Union[str, pd.Timestamp, None] = None,
    end: Union[str, pd.Timestamp, None] = None,
    index: str = "equities",
) -> pd.DataFrame:
    """Async variant of :func:`actions`."""
    return _default_client().actions_async(symbol, start, end, index)
