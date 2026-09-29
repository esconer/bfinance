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

The 34 UDiFF columns include ``TtlTrfVal`` — total traded **value** — alongside
``TtlTradgVol``. It is exposed as :data:`TURNOVER_COLUMN`; see "Turnover value and its unit"
below for what the unit is and is not.

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
   ``(Date, Symbol)``, and a migrated listing stitches into one continuous price series. Re-measured
   on 2026-09-24 and 2026-09-25: still **0**. It also explains why the migration is not a rare edge
   case: it is a routine, both-ways, monthly-scale event on a ~2,300-symbol board.
8. **``SctySrs`` holds 57 codes on a given day, not 2 — and the roster is not stable.** Measured
   over two consecutive sessions, 2026-09-24 and 2026-09-25, each fetched whole and recorded
   (:data:`SERIES_MEASURED`): **57** distinct values on 2026-09-25 (3,654 rows) and **60** on
   2026-09-24 (3,637 rows), a **union of 71** — 25 codes present on one day and absent on the
   other. There is therefore no correct hardcoded roster to publish, only a measured one, and a
   table circulated alongside this change whose own ten largest counts reproduce exactly while its
   stated total (59) matches **neither** day. :data:`SERIES_MEASURED` is a *dated observation* and
   :func:`series_in_file` re-derives the roster from whatever file was actually served. Ten of the
   codes carry listed securities that :data:`EQUITY_SERIES` does not cover
   (:data:`EQUITY_LIKE_SERIES`); a caller who wants a REIT or an InvIT must ask for it, and a
   request that matches no rows comes back as a **labelled** empty frame — carrying the roster the
   file did hold — never as a bare one.
9. **A symbol can appear under two series on one day, and ``BL`` always does.** ``BL`` is not an
   extra instrument but a *duplicate line*: 2026-09-25 ``BL`` = {ADANIENT, DALBHARAT}, 2026-09-24
   ``BL`` = {SANOFI, SEDEMAC}, and every one of those four is also an ``EQ`` row the same day. The
   ``BL`` row is a flat synthetic price (ADANIENT ``O=H=L=C=2905.00`` on 8,600,000 shares) under a
   *different* ``FinInstrmId`` (12184 vs 25). So adding ``BL`` cannot add an instrument, only a
   second ``(Date, Symbol)`` row. ``BL`` is not the only source: ``IMC1`` is filed under both
   ``N1`` and ``N3`` on 2026-09-24, and the **legacy** file collides far harder — 2023-05-15 has
   **153** duplicate ``(Date, Symbol)`` rows, with tickers like ``BRITANNIA`` and ``CHOLAFIN``
   listed under both ``EQ`` and a debt series. So a legacy ``series=None`` read *loses real equity
   rows* to the keep-last dedup. :func:`parse_bhavcopy` counts every such loss in
   ``attrs["bfinance_duplicate_date_rows"]`` rather than hiding it, and the default is immune
   because it filters to ``EQ``/``BE`` first.
10. **``TtlTrfVal`` is mapped; ``SttlmPric`` is not, and the difference is not cosmetic.** See below.

A note on ``TtlTrfVal`` and the turnover unit
---------------------------------------------
``TtlTrfVal`` (UDiFF) / ``TOTTRDVAL`` (legacy) is the session's traded **value**. It is exposed as
``TurnoverValue`` — a name that cannot be read as rupees — with the unit carried in
``attrs["bfinance_turnover_unit"]``.

What was **measured**, by execution, on whole recorded files:

* **CM (UDiFF and legacy).** ``TtlTrfVal / TtlTradgVol`` lies inside ``[LwPric, HghPric]`` for
  **every** testable row in all three recorded CM captures — 3,654 + 3,637 + 2,427 rows — with the
  largest violation **2.3e-13** in absolute terms. Every apparent failure under an exact ``<=``
  comparison is IEEE-754 division noise on a row whose ``Low == High``, not a real breach: e.g.
  ``2551516.80 / 988960`` evaluates to ``2.5800000000000002`` against a published low of ``2.58``.
  That identity is only possible if the value column is in the *same unit as price x share-count*,
  so for the cash market the unit is **established**, not assumed, and
  :func:`measure_turnover_unit` returns :data:`TURNOVER_UNIT_PRICE_X_VOLUME`.
* **F&O.** The identity **fails 0/18072**. This is not noise and not a units mistake in the
  fixture: ``TtlTradgVol`` counts *lots* while ``TtlTrfVal`` is in units of the **underlying**.
  Measured row: ``ABCAPITAL`` 3100, lot size 3100, premium range 0.45-0.80, volume 85,
  ``TtlTrfVal`` 118,724,575.00 -> 1,396,759.71, which is ABCAPITAL's *share* price, not its
  premium. So the ratio is the underlying price and can never fall inside the premium range. The
  F&O file also leaves ``SctySrs`` **blank on all 37,165 rows**.

Therefore the unit is **segment-dependent**, and this is measured rather than quoted. An
unverified third-party (GPL-3.0) project additionally claims an **era** dependency for F&O
(lakhs before 2024-07-08) and crores for index files. That claim is recorded here as an
**unverified hypothesis** — it is not measured, no pre-2024-07-08 F&O file was fetched, and this
module reads no index bhavcopy at all, so neither half of it can be checked from here. It is the
reason the unit lives in ``attrs`` and the column carries no currency, and the reason
:data:`TURNOVER_UNIT_UNRESOLVED` exists: a caller that needs a monetary unit must compare the
attr and refuse, rather than assume.
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
import weakref
import zipfile
from contextlib import asynccontextmanager
from datetime import date as _date, datetime, timedelta
from typing import Any, AsyncIterator, Dict, Iterable, List, Literal, Optional, Tuple, Union

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

#: The OHLCV half of the output contract — the columns ``OHLCVEngine`` produces, which is what
#: :mod:`bfinance.market.ohlcv` requires and checks for by name. :data:`COLUMNS` is the full set.
OHLCV_COLUMNS: Tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

#: The traded-value column. **Named to be unreadable as a currency.** ``Turnover`` would invite
#: ``frame["Turnover"].sum()`` into a cost calculation; see the "turnover unit" section of the
#: module docstring for why the unit is segment-dependent and lives in ``attrs`` instead.
TURNOVER_COLUMN = "TurnoverValue"

#: Exact output contract of every frame this module returns. The OHLCV five, plus
#: :data:`TURNOVER_COLUMN`. :mod:`bfinance.market.ohlcv` looks its five up **by name**, so the
#: extra column is additive there and changes no existing value.
COLUMNS: Tuple[str, ...] = OHLCV_COLUMNS + (TURNOVER_COLUMN,)

#: Attr carrying the unit of :data:`TURNOVER_COLUMN` for the file that was actually parsed.
#: Present on every frame, including empty ones, so a consumer reads one key without branching.
TURNOVER_UNIT_ATTR = "bfinance_turnover_unit"

#: The only unit value this module will *establish*, and it establishes it by measurement: the
#: VWAP identity holds for every testable row, which can only happen if the value column is in the
#: same unit as price x share-count. Measured on the CM captures (see the docstring).
TURNOVER_UNIT_PRICE_X_VOLUME = "price_x_volume"

#: What the attr says when the file's unit could not be established. Measured: every F&O capture.
#: A caller needing a monetary amount must treat this as "unknown", not as rupees — that refusal is
#: the entire point of naming the column without a currency and carrying the unit separately.
TURNOVER_UNIT_UNRESOLVED = "unresolved"

#: Relative tolerance for the VWAP identity, on the *price* scale. Justified rather than chosen:
#: the published columns are rounded to 2 dp, so the quotient of two rounded numbers can leave the
#: rounded range by at most a rounding error. The largest violation measured across the recorded CM
#: and legacy captures is **2.3e-13** in absolute terms — float noise, e.g. a row whose
#: ``Low == High == 2.58`` where ``2551516.80 / 988960`` evaluates to ``2.5800000000000002``. A
#: tolerance of 1e-9 is 4e3 times the worst observed and still far tighter than one 2-dp tick, so
#: it cannot mask a real unit error (which would miss by a factor of 100, 10,000 or 1e7).
VWAP_TOLERANCE = 1e-9

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

#: The series codes measured in whole, untrimmed CM archives — a **dated observation, not a
#: contract**. Two consecutive sessions, each fetched whole and recorded under
#: ``tests/fixtures/bhavcopy/``:
#:
#: * 2026-09-25 (3,654 rows): **57** distinct ``SctySrs``
#: * 2026-09-24 (3,637 rows): **60** distinct ``SctySrs``
#: * union over the two: **71** — 25 codes are present on one day and absent on the other
#:
#: The roster is therefore *not* stable, and publishing any single list as "the" set of series
#: would be a claim upstream does not support. This constant exists to document the measurement and
#: to let a caller *ask* for a code this module has not heard of; :func:`series_in_file` reports
#: what a given file actually held. Note the disagreement with a hand-counted table circulated
#: alongside this change: that table's own ten largest counts reproduce exactly (EQ 2660, SM 380,
#: BE 239, ST 84, GS 53, GB 40, BZ 27, IV 15, TB 14, RR 6) but it totals **59** distinct, where
#: the file has **57**. A total that contradicts its own components is a transcription error, and
#: is the reason this list is machine-derived.
SERIES_MEASURED: Dict[str, Tuple[str, ...]] = {
    "2026-09-25": (
        "AL", "BE", "BL", "BZ", "E1", "EQ", "GB", "GS", "IV", "N0", "N1", "N2", "N3", "N4",
        "N5", "N6", "N7", "N8", "N9", "NA", "NC", "ND", "NE", "NF", "NG", "NH", "NJ", "NK",
        "NL", "NM", "NN", "NO", "NR", "NS", "NT", "NX", "NZ", "RR", "SG", "SM", "ST", "SZ",
        "TB", "Y4", "Y6", "YA", "YC", "YI", "YM", "YT", "YW", "YY", "YZ", "Z6", "Z9", "ZH",
        "ZY",
    ),
    "2026-09-24": (
        "AL", "AZ", "BE", "BL", "BZ", "E1", "EQ", "GB", "GS", "IV", "N0", "N1", "N2", "N3",
        "N4", "N5", "N6", "N7", "N8", "N9", "NA", "NC", "ND", "NE", "NF", "NG", "NH", "NI",
        "NJ", "NK", "NL", "NM", "NN", "NO", "NP", "NS", "NU", "NW", "NX", "NZ", "RR", "SG",
        "SM", "ST", "SZ", "TB", "Y0", "Y5", "Y8", "YA", "YB", "YC", "YI", "YL", "YM", "YP",
        "YS", "YY", "Z8", "ZT",
    ),
}

#: Row counts per code, for the two days in :data:`SERIES_MEASURED`, counted from the same
#: recorded files. Kept because the *proportions* are the argument for keeping the default narrow:
#: on 2026-09-25, ``EQ``+``BE`` is 2,899 of 3,654 rows and everything the default rejects is 755.
SERIES_ROW_COUNTS: Dict[str, Dict[str, int]] = {
    "2026-09-25": {
        "EQ": 2660, "SM": 380, "BE": 239, "ST": 84,
        "GS": 53, "GB": 40, "BZ": 27, "N0": 21,
        "IV": 15, "TB": 14, "SG": 8, "N1": 7,
        "N5": 7, "N2": 6, "N6": 6, "RR": 6,
        "N3": 5, "NA": 5, "NE": 5, "ND": 4,
        "N4": 3, "N7": 3, "N8": 3, "NC": 3,
        "NF": 3, "NJ": 3, "NN": 3, "BL": 2,
        "E1": 2, "N9": 2, "NH": 2, "NK": 2,
        "NM": 2, "NO": 2, "NS": 2, "NX": 2,
        "NZ": 2, "YC": 2, "AL": 1, "NG": 1,
        "NL": 1, "NR": 1, "NT": 1, "SZ": 1,
        "Y4": 1, "Y6": 1, "YA": 1, "YI": 1,
        "YM": 1, "YT": 1, "YW": 1, "YY": 1,
        "YZ": 1, "Z6": 1, "Z9": 1, "ZH": 1,
        "ZY": 1,
    },
    "2026-09-24": {
        "EQ": 2664, "SM": 370, "BE": 236, "ST": 82,
        "GS": 53, "GB": 40, "BZ": 27, "N0": 24,
        "IV": 12, "N1": 11, "N2": 7, "N6": 7,
        "SG": 7, "N5": 6, "NC": 6, "RR": 6,
        "TB": 6, "N4": 5, "NA": 5, "N3": 4,
        "NE": 4, "NJ": 4, "N8": 3, "ND": 3,
        "NF": 3, "BL": 2, "N9": 2, "NH": 2,
        "NK": 2, "NL": 2, "NM": 2, "NS": 2,
        "AL": 1, "AZ": 1, "E1": 1, "N7": 1,
        "NG": 1, "NI": 1, "NN": 1, "NO": 1,
        "NP": 1, "NU": 1, "NW": 1, "NX": 1,
        "NZ": 1, "SZ": 1, "Y0": 1, "Y5": 1,
        "Y8": 1, "YA": 1, "YB": 1, "YC": 1,
        "YI": 1, "YL": 1, "YM": 1, "YP": 1,
        "YS": 1, "YY": 1, "Z8": 1, "ZT": 1,
    },
}

#: Which of the measured codes carry a **listed security** (a company, REIT, InvIT or fund) rather
#: than debt. :data:`EQUITY_SERIES` is deliberately *not* widened to include all of them.
#:
#: **Derived, not asserted.** :func:`is_debt_instrument` is the whole rule, and it reads the
#: published instrument *name*, not the ticker — because two symbol rules were tried against the
#: three recorded captures (9,718 rows) and both are refuted by real listed securities. See that
#: function for the two refutations and their counts. Over the recorded data the name rule
#: misclassifies **0** rows and leaves **0** series ambiguous, splitting the 71 observed codes into
#: exactly these 10 and :data:`DEBT_SERIES`. ``test_the_series_classification_is_derived_not_asserted``
#: recomputes this from the fixtures rather than reading it, so it cannot drift from the evidence.
#:
#: Per code, with the names that decide it:
#:
#: * ``EQ`` (2,660), ``BE`` (239) — ordinary cash equities. The default.
#: * ``SM`` (380), ``ST`` (84) — **are** equity (SME and start-up), excluded from the default
#:   because they are a different board, not because they are debt.
#: * ``BZ`` (27) — where companies **migrated onto** land: AGSTRA, ANKITMETAL, ASTRON, COMPINFO,
#:   IMPEXFERRO, SUULD.
#: * ``IV`` (15) — **InvITs**: CAPINVIT, INDIGRID, INDUSINVIT, PGINVIT, CITIUSINVT.
#: * ``RR`` (6) — **REITs**: EMBASSY, BAGMANE, BIRET, KRT, MINDSPACE, NXST.
#: * ``SZ`` (1) — PERFECT.
#: * ``E1`` (2) — ATLPP, ROCKPP. Published names are ``'ATL RE.0.50 PPD UP'`` and
#:   ``'ROCKINGDCE RS.5 PPD UP'``: participation certificates, not company shares. Classified
#:   equity-like because they are *not* debt, and flagged here because "is a listed security" is
#:   not the same claim as "is a company" — a caller wanting company fundamentals should not take
#:   ``E1`` for one.
#: * ``BL`` (2) — equity-like **and duplicative**: every ``BL`` symbol is also an ``EQ`` symbol the
#:   same day. See trap 9.
#:
#: Debt, i.e. everything else: every ``N*`` code, ``NA``-``NZ``, the ``Y*``/``Z*`` families, plus
#: ``TB``, ``SG``, ``GB``, ``GS`` and ``AL``. Their names carry the giveaway — ``'2.5%GOLDBONDS
#: 2028SR-III'`` (SGBJUN28 at 14,925.83), ``'GOI LOAN 6.67% 2050'`` (667GS2050 at 96.30),
#: ``'GOI TBILL 364D-06/03/25'``, ``'UNSE RE NCD 0% SR.III'`` — prices that look like equities and
#: would land in a portfolio as instruments, which is the reason the default filters at all.
EQUITY_LIKE_SERIES: Tuple[str, ...] = ("BE", "BL", "BZ", "E1", "EQ", "IV", "RR", "SM", "ST", "SZ")

#: The measured codes that are **debt** (or otherwise not a listed equity). The complement of
#: :data:`EQUITY_LIKE_SERIES` within the union of :data:`SERIES_MEASURED`.
DEBT_SERIES: Tuple[str, ...] = tuple(
    sorted(
        (set(SERIES_MEASURED["2026-09-25"]) | set(SERIES_MEASURED["2026-09-24"]))
        - set(EQUITY_LIKE_SERIES)
    )
)

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
        # Traded VALUE, not volume. The legacy equivalent is below; the unit is measured per file
        # and reported in attrs, because it is segment-dependent (see the module docstring).
        "turnover": ("TtlTrfVal",),
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
        # The legacy equivalent is NOT unavailable: the 14-column pre-2024 file carries
        # ``TOTTRDVAL`` (total traded value), and it satisfies the same VWAP identity. Measured on
        # the recorded 2023-05-15 archive: 2,417/2,427 testable rows, largest violation 2.3e-13.
        # So turnover works on both sides of the cutover and is not a post-2024 feature.
        "turnover": ("TOTTRDVAL",),
    },
}

#: Columns that are present in the file, are **deliberately not mapped**, and why. Kept as data so
#: the reason travels with the code and a future contributor sees it before "fixing" the omission.
#:
#: ``SttlmPric`` is the trap: it is present and **populated** on ordinary cash rows (ADANIENT
#: 2026-09-25 reads ``SttlmPric=3132.99`` while its ``Close`` is 2916.50), it looks like a
#: settlement/turnover figure, and a substring search for ``"stt"`` lands on it. It is the
#: *settlement price*, a different quantity from traded value, and mapping it as turnover would
#: produce ``3132.99 / 570504`` — a number ~1,000,000x too small, which no price-range assertion
#: would flag because the result is small rather than large.
UNMAPPED_COLUMNS: Dict[str, str] = {
    "SttlmPric": (
        "Settlement price, not traded value. Present and populated on cash rows; excluded "
        "deliberately."
    ),
    "UndrlygPric": "Underlying price for a derivative contract. Meaningless in the cash file.",
    "StrkPric": "Option/future strike. Blank for every cash equity row.",
    "TtlNbOfTxsExctd": "Trade count. Not requested by this module and not a price or notional.",
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


#: Wording in a published instrument name (``FinInstrmNm``) that marks a debt or money-market
#: instrument. Upper-cased before matching. Derived from the three recorded captures; every token
#: here appears verbatim in at least one of them, and the test re-derives
#: :data:`EQUITY_LIKE_SERIES` from these words rather than from the constant it checks.
DEBT_NAME_TOKENS: Tuple[str, ...] = (
    "NCD", "GOLDBOND", "GOI", "TBILL", "LOAN", "BOND", "ZCB", "STRPP",
    "SR.", "SR ", "SR-", "T-BILL", "DEPOSIT", "PCD",
)

#: Wording that marks a company, a fund or a trust. Checked **first**, because a name can contain
#: both (``'EDELAMC - BBETF0432'`` has no debt wording but a bare substring search for a short token
#: would otherwise hit it — which is how ``CPSE ETF``, ``HDFCAMC HDFCPSUBK`` and ``KCPSUGARINDUS``
#: were misread as debt during measurement).
COMPANY_NAME_TOKENS: Tuple[str, ...] = (
    "LIMITED", "LTD", "INC", "CORP", "PLC", "ETF", "FUND", "AMC", "REIT", "INVIT", "TRUST", "INDEX",
)

#: A coupon rate, e.g. ``'AFIL 12% 2028'``, ``'SEC RE NCGB 8.25% STRPP B'``. The percent sign is
#: load-bearing: matching a bare digit caught ``'21ST CENTURY MGMT SERVICE'`` and
#: ``'VALUE 360 COMMUNICATION L'``, both ordinary EQ companies.
_COUPON_RE = re.compile(r"\d+(?:\.\d+)?\s*%")


def is_debt_instrument(name: str) -> bool:
    """True when a published instrument **name** describes debt rather than a listed security.

    This is the rule that derives :data:`EQUITY_LIKE_SERIES`, and it reads the ``FinInstrmNm``
    column — not the ticker. That choice is forced by measurement, not preference.

    **Why not the ticker.** Two obvious symbol rules were tried against the three recorded
    captures (9,718 rows) and both are wrong:

    * *"A debt scrip code starts with a digit."* Refuted: **9** listed companies have digit-leading
      tickers — ``20MICRONS``, ``21STCENMGM``, ``360ONE``, ``3BBLACKBIO``, ``3IINFOLTD``,
      ``3MINDIA``, ``3PLAND``, ``5PAISA``, ``4THDIM`` — and every one is filed under ``EQ``/``BE``.
    * *"A debt scrip code ends in a maturity code (a digit, optionally a letter)."* Refuted: that
      matches **158** genuine equity rows, because NSE's ETFs and index funds end in digits too —
      ``BBETF0432``, ``GOLD360``, ``SILVER360``, ``MSCI360``, ``AONETMMQ50``, ``ENEXT50``,
      ``GROWWMC150``, ``ELM250``, ``EBANK10``, ``FAZE3Q``, ``C2C``, ``E2E``.

    A prefix rule fails for the same reason from the other side: ``IIFL`` and ``SFL`` and ``IMFA``
    are companies while ``IIFLZC28`` and ``SCL01825`` are bonds.

    The instrument name is the field that actually states what the thing is, and over the same
    9,718 rows this rule produces **0** rows in an equity-like series that it calls debt and
    **0** series whose rows it cannot separate — a clean split of the 71 observed codes into
    :data:`EQUITY_LIKE_SERIES` and :data:`DEBT_SERIES`.

    Company and fund wording is tested *before* debt wording, so a name that is both is not read
    as debt.
    """
    text = str(name).strip().upper()
    if not text:
        return False
    if any(token in text for token in COMPANY_NAME_TOKENS):
        return False
    if any(token in text for token in DEBT_NAME_TOKENS):
        return True
    return bool(_COUPON_RE.search(text))


def series_in_file(csv_text: str) -> Tuple[str, ...]:
    """Every series code present in one bhavcopy's CSV text, sorted.

    The measured answer to "what could I have asked for today", which is what makes an empty result
    explicable. :data:`SERIES_MEASURED` records two observations; this reads the file in hand, so
    it cannot be stale. Pure and offline — it reads the series column by name and does not parse the
    rest of the file.

    Used to label an empty result. An empty frame that says nothing is the failure mode this module
    exists to end, so a caller that got no rows can ask this and learn that the file held
    ``BZ`` but not, say, ``RR`` — a different answer from "that series does not exist".
    """
    frame = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False, na_values=[""])
    if "SctySrs" in frame.columns:
        column = "SctySrs"
    elif "SERIES" in frame.columns:
        column = "SERIES"
    else:
        raise ParsingError(
            "bhavcopy CSV has no series column. Looked for 'SctySrs' and 'SERIES'; got "
            f"{len(frame.columns)} columns starting {list(frame.columns)[:4]!r}."
        )
    codes = frame[column].astype(str).str.strip()
    # An F&O file carries the column but leaves it empty on all 37,165 rows, and `astype(str)` on
    # the NaN that `na_values=[""]` produced would otherwise report the literal "nan" as if it were
    # a series code. Blanks are not series.
    codes = codes[codes.ne("") & codes.ne("nan") & codes.notna()]
    return tuple(sorted(codes.unique()))


def measure_turnover_unit(frame: pd.DataFrame) -> str:
    """Establish — or refuse to establish — the unit of :data:`TURNOVER_COLUMN` on ``frame``.

    Returns :data:`TURNOVER_UNIT_PRICE_X_VOLUME` only when the VWAP identity
    ``Low <= TurnoverValue / Volume <= High`` holds for **every** row that can be tested, within
    :data:`VWAP_TOLERANCE`. That identity is not a heuristic: a session's traded value divided by
    its traded quantity *is* the volume-weighted average price, which cannot lie outside the
    session's own range. So if the ratio lands in the range, the value column must be denominated
    in the same unit as price x share-count; if it does not, this module says so instead of
    guessing.

    Returns :data:`TURNOVER_UNIT_UNRESOLVED` otherwise — which is the **measured** F&O outcome
    (0/18,072 rows), and the answer for any frame with too few testable rows to judge.

    The check runs on whatever frame it is given, so the answer is per file and per era rather than
    a constant that could go stale. A frame with no testable rows (all volumes zero, or empty)
    returns :data:`TURNOVER_UNIT_UNRESOLVED` — "no evidence" is not "same unit".
    """
    if TURNOVER_COLUMN not in frame.columns or "Volume" not in frame.columns:
        return TURNOVER_UNIT_UNRESOLVED
    volume = pd.to_numeric(frame["Volume"], errors="coerce")
    value = pd.to_numeric(frame[TURNOVER_COLUMN], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    high = pd.to_numeric(frame["High"], errors="coerce")
    testable = volume.gt(0) & value.notna() & low.notna() & high.notna()
    if not bool(testable.any()):
        return TURNOVER_UNIT_UNRESOLVED
    vwap = value[testable] / volume[testable]
    inside = (vwap >= low[testable] - VWAP_TOLERANCE) & (vwap <= high[testable] + VWAP_TOLERANCE)
    return TURNOVER_UNIT_PRICE_X_VOLUME if bool(inside.all()) else TURNOVER_UNIT_UNRESOLVED


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
    # Traded value. Deliberately absent from the OHLCV block above: it is a different quantity with
    # a different unit story, and _measure_turnover_unit below needs the assembled frame to decide
    # that unit rather than assert it.
    out[TURNOVER_COLUMN] = pd.to_numeric(kept[bound["turnover"]], errors="coerce").to_numpy()

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
        for label in OHLCV_COLUMNS
        if int(out[label].isna().sum()) > 0
    }
    # Traded value is counted on its own key rather than folded into `bfinance_missing_ohlcv`, whose
    # name promises OHLCV. A missing turnover is a different kind of gap: the bar is intact, only
    # the notional is absent.
    missing_turnover = int(out[TURNOVER_COLUMN].isna().sum())

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
    out.attrs["bfinance_missing_turnover"] = missing_turnover
    out.attrs["bfinance_unparsed_date_rows"] = unparsed_dates
    out.attrs["bfinance_nameless_rows"] = nameless
    out.attrs["bfinance_duplicate_date_rows"] = duplicates
    out.attrs["bfinance_symbol_count"] = int(out.index.get_level_values("Symbol").nunique())
    # The unit is decided by measuring this frame, never asserted, so it stays correct for an era
    # or a segment this module has never seen. See measure_turnover_unit.
    out.attrs[TURNOVER_UNIT_ATTR] = measure_turnover_unit(out)
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
    # An empty frame has no rows to measure, so the unit is not established. It is still present,
    # and says so, rather than being absent (which a caller could not tell from "never computed")
    # or optimistically claiming a unit no data supports. Same reason as the flag below.
    out.attrs.setdefault(TURNOVER_UNIT_ATTR, TURNOVER_UNIT_UNRESOLVED)
    # Set last, so a caller-supplied attrs dict can never suppress the flag. The whole point of
    # this module is that this key is trustworthy, so it is not overridable.
    out.attrs[SYNTHETIC_OHLC_ATTR] = False
    return out


# ------------------------------------------------------------------ client


class _LoopSession:
    """One ``httpx.AsyncClient`` plus the number of coroutines currently reading through it.

    A client is a per-loop object, so the session is too, and ``users`` is a reference count
    rather than a flag: ``asyncio.gather`` over several calls on one loop would otherwise let
    the first one to finish close a client the others are still reading from.
    """

    __slots__ = ("client", "users")

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.users = 0


class BhavcopyClient:
    """Client for NSE's published end-of-day bhavcopy archives.

    Stateless per call and safe to reuse: the archive route has no session, no cookie and no
    cache-staleness problem. A long-lived instance is fine, and sharing one is *preferred*
    because request pacing is coordinated globally, and because a shared instance reuses one
    keep-alive connection instead of paying a fresh TCP+TLS handshake per file.

    Measured over four trading-day archives, arms reversed between runs to cancel ordering:
    warm files took 1.02-1.14 s each sharing a client against 2.28-4.28 s each opening a
    client per request, and all sixteen responses were HTTP 200 with identical byte counts,
    so only the time moved. The first file in any run is a cold connect in both arms -- it
    cost 2.7-5.7 s either way -- so a single-file measurement shows no effect at all and
    would read as a regression.
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
        # One client per running event loop, never one per process. See _keepalive.
        self._sessions: Any = weakref.WeakKeyDictionary()
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------- keep-alive client

    def _acquire_client(self) -> httpx.AsyncClient:
        """The keep-alive client for the loop that is running right now, opening one if needed.

        An ``httpx.AsyncClient`` is not a process-level object. Its connection pool,
        its sockets and its transport are all created by, and bound to, the event loop
        that first performs I/O with it, and httpx does not support driving one from a
        second loop. This class is reached from ``market.ohlcv`` via
        ``asyncio.to_thread``, so every call can land on a different loop, and a client
        cached process-wide would be handed to each of them in turn. So the client is
        opened lazily, inside whichever loop is running, and is closed by
        :meth:`_keepalive` while that loop is still alive -- never by a later one, and
        never by a process-wide singleton.

        A note on the evidence, because it is thinner than it first looked. An earlier
        draft of this docstring claimed that cross-loop reuse and cross-loop ``aclose()``
        had been measured to raise ``RuntimeError('Event loop is closed')`` on this
        machine. That did not reproduce. Driving one client from two successive
        ``asyncio.run()`` calls against a local socket server succeeded at 1, 2, 4 and 8
        requests on the first loop, and dropping the client afterwards emitted no
        ``ResourceWarning``. Whatever the original trigger was, it is not reliably
        reproducible here, so this design is **defensive rather than a fix for a reliably
        reproducible crash**: a per-loop client is never wrong, whereas a process-global
        one is wrong whenever the trigger does fire. That asymmetry is the whole
        justification and it holds whether or not the failure is easy to provoke.

        The lock is a ``threading`` lock rather than an ``asyncio`` one because
        :func:`bhavcopy_range` reaches this class from a worker thread via
        ``asyncio.to_thread``: several threads, each on its own loop, share this dict. Nothing
        is awaited while it is held, so it cannot deadlock.
        """
        loop = asyncio.get_running_loop()
        with self._sessions_lock:
            session = self._sessions.get(loop)
            if session is None:
                # Keyed weakly on the loop, so a loop that dies takes its record with it
                # rather than leaving the client for this instance to accumulate.
                session = _LoopSession(
                    httpx.AsyncClient(
                        timeout=self.timeout, proxy=self.proxy, follow_redirects=True
                    )
                )
                self._sessions[loop] = session
            session.users += 1
            return session.client

    async def _release_client(self, client: httpx.AsyncClient) -> None:
        """Drop one user of ``client``; close it once the last user on this loop is done."""
        loop = asyncio.get_running_loop()
        stale: Optional[httpx.AsyncClient] = None
        with self._sessions_lock:
            session = self._sessions.get(loop)
            if session is not None and session.client is client:
                session.users -= 1
                if session.users <= 0:
                    del self._sessions[loop]
                    stale = session.client
        if stale is None or stale.is_closed:
            return
        # Deliberately broad, and deliberately swallowing. This runs on an unwind path,
        # including while ``asyncio.run`` is cancelling and during interpreter shutdown.
        # Raising here would replace whatever the caller was actually reporting, and leaving
        # the client open would surface as "unclosed socket" ResourceWarnings on stderr long
        # after the call that leaked it returned.
        try:
            await stale.aclose()
        except Exception as exc:  # pragma: no cover - loop already tearing down
            logger.debug("bhavcopy: keep-alive client close failed (%s); discarded", exc)

    @asynccontextmanager
    async def _keepalive(self) -> AsyncIterator[httpx.AsyncClient]:
        """Hold one client open across every request the enclosing scope makes.

        A single ``_get_archive`` call only ever makes one request, so the scope that pays is
        the range walk: N trading days, N requests, N TCP+TLS handshakes if each opens its own
        client. Opening the client here and closing it in the ``finally`` keeps those N
        requests on one connection *and* still guarantees the close happens on the loop that
        owns the connections, which is the only loop it can safely happen on.
        """
        client = self._acquire_client()
        try:
            yield client
        finally:
            await self._release_client(client)

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

        The client is held open for the duration of this call and closed on the way out, so
        the call is self-contained on any loop. A caller that issues several requests on one
        loop -- the range walk -- wraps them in :meth:`_keepalive` so all of them share a
        single client instead of one each.
        """
        async with self._keepalive() as client:
            return await self._fetch_archive(client, url)

    async def _fetch_archive(self, client: httpx.AsyncClient, url: str) -> Optional[bytes]:
        """The retry/pacing body of :meth:`_get_archive`, on a caller-supplied client."""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            try:
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
            # ...and *labelled with the reason*. A bare empty frame cannot distinguish "that series
            # does not exist" from "that series is absent today" from "my filter was wrong"; these
            # two keys do. `available` is the whole roster the file held, read back by name, so the
            # caller can see what it could have asked for instead.
            frame.attrs["bfinance_series_available"] = series_in_file(
                unzip_bhavcopy(payload, url=url)
            )
            logger.info(
                "bhavcopy %s %s: no rows for series=%s; the file held %d series: %s",
                clean_day, clean_exchange, clean_series,
                len(frame.attrs["bfinance_series_available"]),
                ", ".join(frame.attrs["bfinance_series_available"]),
            )
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
        # Days whose archive existed but held no row for the requested series. Recorded rather than
        # skipped silently: over a long span this is how a caller discovers that, say, an InvIT
        # listed mid-window, and without it a "complete" history silently starts later than asked.
        empty_for_series: List[str] = []
        available_across_days: set = set()
        # One keep-alive client for the whole walk, closed on this loop before it dies. Without
        # this scope each _get_archive would open and close its own, which is one TCP+TLS
        # handshake per trading day -- measured at 2.67 s of every 3.99 s per file.
        async with self._keepalive():
            for candidate in candidates:
                url = archive_url(candidate, clean_exchange)
                if progress:
                    logger.info("bhavcopy: %s", url)
                payload = await self._get_archive(url)
                if payload is None:
                    missing.append(candidate.isoformat())
                    if on_missing == "raise":
                        raise TickerNotFoundError(
                            f"NSE published no bhavcopy for {candidate:%Y-%m-%d} "
                            f"({clean_exchange}). Use on_missing='skip' to tolerate weekends "
                            f"and exchange holidays."
                        )
                    continue
                csv_text = unzip_bhavcopy(payload, url=url)
                day_frame = parse_bhavcopy(csv_text, series=series, url=url)
                if day_frame.empty:
                    # A file that arrived and matched nothing is a different event from a 404,
                    # and the two must stay distinguishable across a multi-day walk.
                    empty_for_series.append(candidate.isoformat())
                    available_across_days.update(series_in_file(csv_text))
                    continue
                # Accumulated before the attrs are dropped: the range answer to "which series
                # did I actually get" is the union over the days that returned rows, which is
                # a measurement no single day can make.
                observed_series.update(day_frame.attrs.get(SERIES_INCLUDED_ATTR, ()))
                # Drop per-day attrs: after a merge they would be a lie (they'd describe one
                # day, not the whole span). Provenance is re-asserted on the combined frame
                # below.
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
            # Both keys are present on every return, populated or not, so a consumer reads one key
            # without branching. An empty list here with a populated MISSING_DATES_ATTR means every
            # day that had a file also had rows.
            #
            # `bfinance_series_available` is the roster observed **on the days that returned no
            # rows**, not a union over the whole walk. That is narrower on purpose: it answers the
            # question the empty days raise ("what did the file offer instead?"), and computing it
            # for every day would mean a second full CSV parse per day across a multi-year range.
            # On a single-day read the key is the whole file, which is the common case.
            "bfinance_no_rows_for_series_days": empty_for_series,
            "bfinance_series_available": tuple(sorted(available_across_days)),
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
