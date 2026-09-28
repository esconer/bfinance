"""
One canonical form for a period label, and an explicit conversion into it.

Why this module exists
----------------------
The two upstreams disagree on how to spell a period, and the disagreement is *in the same
vendor*:

==========================  ======================  ======================
source                      as printed              example
==========================  ======================  ======================
screener.in company page    ``"Jun 2023"``          ``section#quarters``
trendlyne ``*_DataDump``    ``"Jun 2026"``          ``quarterlyDataDump``
trendlyne ``/financials/``  ``"Jun '26"``           five SSR tables
both, trailing twelve       ``"TTM"``               not a calendar period
==========================  ======================  ======================

Left alone, a consumer joining a screener frame to a trendlyne frame gets a silent outer join
that drops every row, and the failure surfaces as a plausible-looking empty chart. So the
conversion is one function, :func:`canonical_period`, and the canonical form is **one** of:

* ``"<Mon> <YYYY>"`` — ``"Jun 2026"``, the fiscal period *end*, no day component. The day is
  deliberately not invented: trendlyne's own sparkline epochs are quarter-end midnights IST while
  its period labels carry no day, and a statement column labelled with a day the source never
  stated is a claim the source did not make.
* ``"TTM"`` — trailing twelve months, which is a *window*, not a calendar period, and is
  therefore sorted ahead of every calendar column rather than given a fake date. Its
  as-of date is not derivable from the label, so the module never derives one; callers that
  need it must read the frame's own columns.

The two-digit-year pivot, stated because it is a decision
--------------------------------------------------------
``"Jun '26"`` has no century. :func:`expand_short_year` applies a **2000s pivot** — ``26`` ->
``2026``, ``16`` -> ``2016`` — and this is a measured bound rather than a hopeful one: every
period either vendor currently publishes for these statements falls in 2016..2026, so the pivot
is correct for the whole observed range. A two-digit year outside it would need a vendor
statement to resolve, and this module raises rather than guessing — see
:func:`canonical_period`'s docstring for the one exception it makes, and why.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from .errors import PeriodLabelError

__all__ = [
    "TTM",
    "MONTHS",
    "CANONICAL_RE",
    "canonical_period",
    "expand_short_year",
    "is_canonical_period",
    "order_periods",
    "period_sort_key",
    "try_canonical_period",
]

#: The trailing-twelve-months marker. Not a calendar period; see the module docstring.
TTM = "TTM"

#: The twelve month abbreviations every one of these vendors prints, in calendar order.
MONTHS: Tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

_MONTHS_RE = "|".join(MONTHS)
_MONTH_INDEX = {name: index for index, name in enumerate(MONTHS, start=1)}

#: ``"Jun 2026"`` — the canonical form, and the only calendar form this module emits.
CANONICAL_RE = re.compile(rf"^({_MONTHS_RE})[ \t]+(\d{{4}})$")

# ``"Jun '26"`` (the apostrophe trendlyne's SSR tables print), ``"Jun'26"``, ``"Jun '2026"``,
# and the bare ``"Jun 26"``. All four are collapsed deliberately: the apostrophe is a rendering
# choice, not information, and a period that matches on the apostrophe and not on the bare two
# digits is a period that silently fails to join.
_SHORT_RE = re.compile(rf"^({_MONTHS_RE})[ \t]*'?[ \t]*(\d{{2}}|\d{{4}})$")

#: The two-digit-year window the 2000s pivot is measured over. See the module docstring.
SHORT_YEAR_PIVOT = 2000


def expand_short_year(year: int) -> int:
    """``26`` -> ``2026``, ``16`` -> ``2016``.

    The 2000s pivot, documented above. Not a general two-digit-year rule and not offered as one:
    a vendor that starts publishing ``"Jun '98"`` makes this wrong, and the way to find out is to
    read the new vendor's own four-digit periods, not to widen a regex.
    """
    if not 0 <= year <= 99:
        raise PeriodLabelError(
            f"two-digit year {year!r} is out of range; only 0..99 is a two-digit year."
        )
    return SHORT_YEAR_PIVOT + year


def canonical_period(label: str) -> str:
    """Return the canonical form of one period label.

    ``"Jun 2023"`` -> ``"Jun 2023"``; ``"Jun '26"`` -> ``"Jun 2026"``; ``"Jun '2016"`` ->
    ``"Jun 2016"``; ``"TTM"`` -> ``"TTM"``. Case is accepted on the month and on ``TTM`` and
    discarded, because a period is not a different period for being capitalised differently.

    Args:
        label: A period label as one of the upstreams printed it.

    Returns:
        ``"<Mon> <YYYY>"`` or ``"TTM"``.

    Raises:
        PeriodLabelError: The label is not a period this module can canonicalise. It is **not**
            passed through, and a lookup is never attempted: ``"Q2 FY27"``, ``"FY26"`` and
            ``"2026-06-30"`` are all things a vendor could print, and silently accepting them
            would put an unverified period into a financial statement.
    """
    if not isinstance(label, str):
        raise PeriodLabelError(
            f"period label must be a string, got {type(label).__name__}: {label!r}"
        )
    stripped = label.strip()
    if not stripped:
        raise PeriodLabelError("period label is empty")

    if stripped.upper() == TTM:
        return TTM

    canonical = CANONICAL_RE.match(stripped)
    if canonical is not None:
        return f"{canonical.group(1)} {canonical.group(2)}"

    short = _SHORT_RE.match(stripped)
    if short is not None:
        month, digits = short.group(1), short.group(2)
        year = expand_short_year(int(digits)) if len(digits) == 2 else int(digits)
        if not 1000 <= year <= 9999:
            raise PeriodLabelError(
                f"period label {label!r} resolves to year {year}, which is not a four-digit year."
            )
        return f"{month} {year}"

    raise PeriodLabelError(
        f"period label {label!r} is not a form this module canonicalises. Accepted: "
        f"'<Mon> <YYYY>' (e.g. 'Jun 2026'), \"<Mon> '<YY>\" (e.g. \"Jun '26\"), and 'TTM'. "
        f"Refused rather than guessed — a period invented from an unrecognised label is a wrong "
        f"column in a financial statement."
    )


def try_canonical_period(label: str) -> "str | None":
    """:func:`canonical_period` returning ``None`` instead of raising, for filtering a column list.

    Use only where a caller genuinely wants to skip what it cannot read — a report on which
    columns were skipped, for instance. Never to turn a bad label into a good one.
    """
    try:
        return canonical_period(label)
    except PeriodLabelError:
        return None


def is_canonical_period(label: str) -> bool:
    """``True`` when ``label`` is already exactly the canonical form (no case folding, no rewrite)."""
    return isinstance(label, str) and (label == TTM or CANONICAL_RE.match(label) is not None)


def period_sort_key(period: str) -> Tuple[int, int, int]:
    """Sort key placing ``TTM`` first, then calendar periods ascending by (year, month).

    ``TTM`` first because it is the most recent window any of these statements carries: both
    vendors print it as the trailing column of the annual table, and as a *row* of the quarterly
    one. Sorting it to the end would imply it is the oldest.
    """
    if period == TTM:
        return (0, 0, 0)
    match = CANONICAL_RE.match(period)
    if match is None:
        raise PeriodLabelError(
            f"period_sort_key expects a canonical period, got {period!r}. Call "
            f"canonical_period() first — sorting on an unconverted label orders the wrong "
            f"column."
        )
    return (1, int(match.group(2)), _MONTH_INDEX[match.group(1)])


def order_periods(periods: Iterable[str]) -> Tuple[str, ...]:
    """Canonicalise, de-duplicate and sort a period list.

    Args:
        periods: Labels in any form this module accepts, in any order — e.g. straight from a
            vendor's table header, which is newest-first on trendlyne and oldest-first on
            screener.in.

    Returns:
        The canonical labels, ascending, ``TTM`` first, each appearing once.

    Raises:
        PeriodLabelError: A label is unrecognisable, or the list canonicalises to duplicates.
            Two columns that both claim to be ``"Jun 2026"`` are a vendor inconsistency this
            module will not resolve by picking one.
    """
    canonical = [canonical_period(label) for label in periods]
    seen: List[str] = []
    for raw, period in zip(periods, canonical, strict=True):
        if period in seen:
            raise PeriodLabelError(
                f"period {period!r} appears more than once after canonicalisation "
                f"(raw labels: {sorted(r for r, c in zip(periods, canonical, strict=True) if c == period)}). "
                f"Two columns claiming the same period is a vendor inconsistency, not something "
                f"to resolve by picking one."
            )
        seen.append(period)
    return tuple(sorted(seen, key=period_sort_key))
