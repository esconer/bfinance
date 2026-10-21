"""
A :class:`~bfinance.statements.model.StatementResult` as the frame ``Ticker.financialals`` returns.

Why a bridge, and why it is the only one
----------------------------------------
``bfinance.statements`` is provider-agnostic and ``bfinance.models.statements.FinancialStatement``
already owns the yfinance shape: ``DatetimeIndex`` columns **newest first**, absolute rupees,
generic row labels, non-date periods dropped and *named*. Two implementations of that shape would
drift, so this module builds a :class:`FinancialStatement` and calls ``to_yfinance()``. It adds
the two things that module cannot know, because they are properties of a *vendor* rather than of a
statement:

1. **the unit of each row**, so the Cr -> Rs scale is applied to money and to nothing else;
2. **whose labels these are**, recorded on ``attrs`` so a frame never looks like a yfinance frame
   whose index happens to spell a vendor's vocabulary.

The scale rule, and the trap it exists to avoid
-----------------------------------------------
:func:`bfinance.models.statements._rows_to_scale` decides which rows to scale by reading
screener.in's **labels** — ``"OPM %"`` carries a percent sign, ``"EPS in Rs"`` is on a per-share
list. That works for screener.in and is the right rule there: the label is where the unit lives
on that vendor.

trendlyne's JSON route states the unit **per row**, in ``body.parameterMetadata[id]['unit']``,
and that is a better source than any label heuristic because the vendor said so. Measured over
all 347 keys of the NTPC payload captured live on 2026-10-09:

=====  =====  ==========================================================  ===================
unit    rows  examples                                                  scale to absolute ₹?
=====  =====  ==========================================================  ===================
``Cr``    201  ``SR_A`` 189,798.56 · ``TA_A`` 558,643.56                 **yes, ×1e7**
``%``      55  ``OPMPCT_A`` 29.13 · ``ROE_A`` 13.31                      no
``Rs``      25  ``BVSH_A`` 209.53 · ``EPS_adj_A`` 27.9                   no (per share)
``abs``     15  ``CRATIO_A`` · ``DEBT_CE_A`` · ``EYield_A``              no (ratio/count)
``""``       6  **``PBV_A`` 1.49** + 5 document-link rows                 no
=====  =====  ==========================================================  ===================

**``PBV_A`` is a price-to-book ratio carrying no unit at all.** A rule of the form "scale unless
the unit says otherwise" multiplies it by ten million and reports nothing, which is the same
defect ``_rows_to_scale``'s own docstring records for screener.in (``OPM %`` came back as
``350000000.0``), arriving through the second vendor. So the rule here is the positive one —
**scale only what the vendor labels ``Cr``** — and a row whose unit is empty or unrecognised is
left alone **and named** in ``attrs["bfinance_statements_unscaled_rows"]``, so an unmeasured row
is visible rather than silently wrong in either direction.

The unit is read from the **row id** (``StatementResult.row_units`` is keyed by id), never from
the display label, because two ids can share one label — see below.

A label collision, recorded rather than swallowed
-------------------------------------------------
Rows are keyed by display label, so two row ids that publish the same ``sstr`` address **one**
row. Measured on the recorded NTPC payload: ``"OPM Ann. %"`` is both ``OPM_A`` and
``OPMPCT_A``'s, and ``"Cash Accruals Ann."`` is both ``ACCRUALS_A``'s and
``DepreciationOnLeasedAssets_A``'s — trendlyne's own metadata, where two indicators collide on
their short label.

One row has to win, and the **first** does, matching the rule
``fundamentals_provider._assemble`` already applies when one row *id* repeats. What may not
happen is the winner doing so quietly: ``StatementResult.duplicate_rows`` counts repeated ids
upstream and is blind to two ids sharing a label, so a lost row would leave no trace at all.
Every collision is therefore published on the frame under
:data:`COLLIDING_ROWS_ATTR` as ``{label: (ids…)}`` in result order — first id the survivor —
and logged, so the loss is something a consumer can find.

Because the scale decision is taken **per id** and only a row that survives its label is
scaled, a collision straddling ``Cr`` and ``%`` scales the surviving row on its own unit: the
old label-keyed decision would have scaled both partners or neither.

No label rename, and why not
-----------------------------
``INDIAN_TO_YFINANCE_LABELS`` is a measured screener.in -> yfinance map. There is **no** measured
trendlyne -> yfinance one, and ``statements/model.py`` refuses to invent one: a mapping asserted
from memory would rename one vendor's number into another's vocabulary with nothing behind it.
So the index carries the vendor's own ``sstr`` — ``"Total Rev. Ann."``, `"OPM Ann. %'``,
``"Total Assets Ann."`` — and ``attrs`` names the vendor, so a caller can see whose words they are
reading. A trendlyne-labelled row is *closer* to yfinance than a row spelled ``SR_A``, and it is
honest about what it is.

Basis, carried rather than assumed
----------------------------------
The result's basis attrs ride along on the frame (``bfinance_statements_basis``,
``…_basis_requested``, ``…_basis_mismatch``, ``…_basis_note``). A frame served on the standalone
basis when consolidated was asked for says so on the frame; it is never relabelled, and nothing
here blends two providers' rows into one result.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd

from bfinance.models.statements import FinancialStatement

from .model import StatementResult

__all__ = [
    "COLLIDING_ROWS_ATTR",
    "MONEY_UNIT",
    "UNSCALED_ROWS_ATTR",
    "result_to_financial_statement",
    "result_to_yfinance_frame",
]

logger = logging.getLogger("bfinance")

#: The one unit string that denominates money, verbatim as the vendor writes it. Everything else
#: -- ``%``, ``Rs`` (per share), ``abs`` (ratios and counts) and the empty string -- passes
#: through unscaled. A second value added here would need its own measurement, not an analogy.
MONEY_UNIT = "Cr"

#: Written onto every frame this module returns that carries a row whose unit the vendor did not
#: state. Published so the reader can see which rows were *not* scaled and why, instead of
#: inferring it from a magnitude that looks wrong.
UNSCALED_ROWS_ATTR = "bfinance_statements_unscaled_rows"

#: Written onto every frame this module returns, as ``{display label: (row id, …)}`` for every
#: label **more than one row id resolves to**, in result order — the first id being the one the
#: frame kept. Written even when empty, so "nothing collided" and "nothing looked" are
#: distinguishable. Published because a collision costs a row the frame cannot show: one row has
#: to win, and the ids behind it are invisible in a label-keyed frame.
COLLIDING_ROWS_ATTR = "bfinance_statements_colliding_rows"


def _labelled_statement(
    result: StatementResult,
) -> Tuple[FinancialStatement, Dict[str, Tuple[str, ...]]]:
    """The label-keyed statement, plus every label two or more row ids resolve to.

    Rows are keyed by display label, so two ids sharing an ``sstr`` address one row. The **first**
    occurrence wins — the same rule :func:`~bfinance.statements.fundamentals_provider._assemble`
    applies when a row *id* repeats — and every loser is named, because a row dropped between the
    result and the frame is otherwise untraceable: ``StatementResult.duplicate_rows`` counts
    repeated ids upstream and cannot see two ids that share a label.

    Args:
        result: A parsed statement from :mod:`bfinance.statements`.

    Returns:
        The :class:`~bfinance.models.statements.FinancialStatement`, and the collisions as
        ``{label: (ids…)}`` in result order, the first of which is the surviving row. Empty when
        every label is unique, which is the normal case.
    """
    rows: Dict[str, List[Optional[float]]] = {}
    owner_of: Dict[str, str] = {}
    colliding: Dict[str, List[str]] = {}
    for metric, values in result.rows.items():
        label = result.row_labels.get(metric, metric)
        if label in rows:
            ids = colliding.setdefault(label, [owner_of[label]])
            ids.append(metric)
            logger.warning(
                "row ids %s all resolve to the display label %r in the %s/%s table; keeping the "
                "first (%s) and naming the loss on attrs[%s].",
                ", ".join(ids), label, result.kind.value, result.frequency.value, ids[0],
                COLLIDING_ROWS_ATTR,
            )
            continue
        rows[label] = list(values)
        owner_of[label] = metric
    return (
        FinancialStatement(headers=list(result.periods), rows=rows),
        {label: tuple(ids) for label, ids in colliding.items()},
    )


def result_to_financial_statement(result: StatementResult) -> FinancialStatement:
    """One statement as the model :meth:`FinancialStatement.to_yfinance` reads.

    Args:
        result: A parsed statement from :mod:`bfinance.statements`, on any basis, from either
            trendlyne route or from screener.in.

    Returns:
        A :class:`FinancialStatement` whose ``headers`` are the result's canonical period labels
        in **chronological** order and whose ``rows`` are keyed by the provider's own label.
        ``FinancialStatement.to_yfinance()`` reverses the columns to newest-first itself, so the
        chronological order here is deliberate rather than incidental.

    Note:
        Rows are keyed by ``result.row_labels`` when present and by the row key otherwise, which
        is what makes the JSON route's ``sstr`` labels (``"Total Rev. Ann."``) reach the frame
        while an id-keyed row still arrives under its own id. Nothing is renamed across vendors:
        this is the same vocabulary the result already carries.

        Two row ids that publish the same label address **one** row, and the first occurrence
        wins — logged here, and published as :data:`COLLIDING_ROWS_ATTR` on the frame from
        :func:`result_to_yfinance_frame`, which is where ``attrs`` live: a
        :class:`FinancialStatement` carries none of its own. The native shape therefore reports
        the collision in the log and the yfinance shape reports it on the frame, but neither
        drops a row without saying so.
    """
    statement, _colliding = _labelled_statement(result)
    return statement


def result_to_yfinance_frame(result: StatementResult) -> pd.DataFrame:
    """The yfinance-shaped copy of one statement: newest-first columns, absolute rupees.

    Args:
        result: A parsed statement from :mod:`bfinance.statements`.

    Returns:
        A frame with the same column order, index-name and attrs-key set
        ``Ticker.financialals`` returns — ``DatetimeIndex`` columns newest first, absolute rupees
        — plus the result's own basis attrs, :data:`UNSCALED_ROWS_ATTR` and
        :data:`COLLIDING_ROWS_ATTR`. Returns an empty ``DataFrame`` for an empty result rather
        than raising, matching how :class:`~bfinance.ticker.Ticker` degrades.

    Note:
        Money rows are multiplied by :data:`~bfinance.models.statements.CR_TO_RS`. Percentages,
        per-share quantities, ratios and every row whose unit the vendor did not state are **not**
        scaled — see the module docstring for the measured census and for ``PBV_A``, the ratio
        with no unit that a negative rule would corrupt.

        The decision is taken on the **row id** and mapped to labels afterwards, so a label two
        ids share is scaled on the unit of the id that actually survived it.
    """
    if not result.periods or not result.rows:
        return pd.DataFrame()

    statement, colliding = _labelled_statement(result)

    # Which rows the vendor says are money, read from the RESULT's row keys because that is what
    # row_units is keyed on. Passed to to_yfinance() as the scale decision, replacing its
    # label-derived one: trendlyne's labels carry no unit marker at all ("BVSH Ann." is per share,
    # "Price To Book Value Annual Adjusted" is a ratio), so the label rule would scale both.
    #
    # The ids that lost their label are left out of both sets: their numbers are not in the frame
    # at all, so the row the frame does hold under that label is decided by the survivor's unit
    # alone. Deciding on the label instead would scale both partners of a collision straddling
    # "Cr" and "%", or neither.
    label_for: Dict[str, str] = {
        metric: result.row_labels.get(metric, metric) for metric in result.rows
    }
    shadowed = {metric for ids in colliding.values() for metric in ids[1:]}
    units = result.row_units or {}
    carried = [
        (label_for[metric], units.get(metric, ""))
        for metric in result.rows
        if metric not in shadowed
    ]
    money_labels = {label for label, unit in carried if unit == MONEY_UNIT}
    unscaled = tuple(label for label, unit in carried if unit != MONEY_UNIT)

    frame = statement.to_yfinance(scalable_rows=money_labels)
    if frame.empty:
        return frame

    frame.attrs[UNSCALED_ROWS_ATTR] = unscaled
    # The collisions themselves: {label: ids}, first id the one this frame kept. Empty on a result
    # whose labels are unique, which is why this is written unconditionally -- absent would mean
    # "nobody looked".
    frame.attrs[COLLIDING_ROWS_ATTR] = colliding
    # The result's own provenance and basis ride along with to_yfinance()'s attrs rather than
    # replacing them: the frame should say both what shape it is in and where its numbers came
    # from, and losing either set would make one of the two unanswerable from the frame alone.
    for key, value in result.attrs().items():
        frame.attrs.setdefault(key, value)
    return frame
