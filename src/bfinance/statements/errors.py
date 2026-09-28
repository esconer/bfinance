"""
Typed failures for :mod:`bfinance.statements`.

Every public call in this package fails with a subclass of :class:`StatementError`, and each one
also inherits the closest match from :mod:`bfinance.utils.exceptions` — the same mixin rule
:mod:`bfinance.trendlyne.errors` documents, applied here so that::

    except StatementError:         # everything from this package
    except UpstreamServiceError:   # anything upstream, including both vendors

both hold at once. A caller already writing ``except UpstreamServiceError`` around a statement
fetch does not have to learn a new hierarchy to benefit from the cascade.

The taxonomy is three questions, and the class name says which one was asked
----------------------------------------------------------------------------
1. **Can this capability exist at all?** :class:`StatementCapabilityError` — no reachable source
   publishes it. Measured, cited by vendor, and terminal. Not a bug and not going to be fixed in
   a later version. This is the only error here that subclasses :class:`NotImplementedError`,
   because it replaces a bare ``NotImplementedError`` raise that told its caller nothing.
2. **Did the right bytes arrive?** :class:`StatementNotADataPageError` — a page that is not a
   data page. trendlyne serves five such pages at HTTP 200, so this is a route-selection mistake
   and saying so is the difference between a fixable URL and a mysterious empty frame.
3. **Did the bytes parse into the expected shape?** :class:`StatementTableError` and its
   subclass :class:`StatementEmptyError` — the table is there but the answer is not.
   :class:`StatementEmptyError` is separated because "the vendor has no data" and "nobody asked
   the right question" look identical in a frame and must not be reported as the same thing.
"""

from __future__ import annotations

from bfinance.utils.exceptions import (
    BFinanceError,
    ParsingError,
    UpstreamServiceError,
)

__all__ = [
    "StatementError",
    "StatementCapabilityError",
    "StatementSourceError",
    "StatementNotADataPageError",
    "StatementTableError",
    "StatementSchemaError",
    "StatementEmptyError",
    "PeriodLabelError",
]


class StatementError(BFinanceError):
    """Base class for every failure raised by :mod:`bfinance.statements`."""


class StatementCapabilityError(StatementError, NotImplementedError):
    """No reachable source publishes this statement at this frequency.

    The one error in this module that is **not** a bug report, and the message says so. Both
    vendors were probed: quarterly balance sheet, quarterly cash flow, TTM balance sheet and TTM
    cash flow exist nowhere among the compliant routes. The message names the measurement and
    both vendors so the reader does not file this as a missing feature or wait for a version that
    adds it.

    Subclasses :class:`NotImplementedError` so the bare raises it replaces are still caught by
    existing ``except NotImplementedError`` handlers.
    """


class StatementSourceError(StatementError, UpstreamServiceError):
    """A provider could not be reached, or the caller supplied no payload for it.

    This is the error the cascade catches and falls through from, so a provider that is merely
    unavailable does not take the whole call down when a second provider can answer.
    """


class StatementNotADataPageError(StatementSourceError, ParsingError):
    """The bytes arrived but they are not a data page — the wrong route.

    trendlyne answers **HTTP 200** for ``/fundamentals/balance-sheet/``,
    ``/fundamentals/cash-flow/``, ``/fundamentals/quarterly-results/``,
    ``/fundamentals/annual-results/`` and ``/fundamentals/financial-ratios/`` with an ~85 KB shell
    holding **zero** ``<table>``: five pages that differ only in ``<title>`` and are populated by
    a single XHR. A parser pointed at one of them gets an empty result and reports it as missing
    data, which is a false negative about the vendor. This error names the two routes that do
    carry data.
    """


class StatementTableError(StatementError, ParsingError):
    """The page is a data page, but the table asked for is not on it, or did not match.

    Carries enough of the page in the message — which tables were found, and how each was
    classified — to diagnose a markup change without re-fetching anything.
    """


class StatementSchemaError(StatementError, ParsingError):
    """A payload parsed as JSON but did not have the shape this module requires.

    A missing envelope key, a renamed dump, a basis sub-object that is not an object. trendlyne
    answers **HTTP 200** for a failed request, so ``head.status != "0"`` is checked here rather
    than left to the status code: a client that checks only the status sees success and then an
    empty statement.
    """


class StatementEmptyError(StatementTableError):
    """A statement was located and read, and it came back with **no numbers in any cell**.

    This is deliberately not "zero-valued data". A balance sheet whose every cell is null across
    13 advertised periods is a question that was not asked correctly, not a company with no
    balance sheet, and reporting it as the latter is the exact failure
    :class:`StatementTableError`'s siblings exist to prevent.
    """


class PeriodLabelError(StatementError, ValueError):
    """A period label is not in a form this module can canonicalise.

    Refused rather than guessed: ``"Q2 FY27"``, ``"Jun-26"`` and ``"2026-06-30"`` are all things
    a vendor could plausibly print, and inventing a period from an unrecognised one puts a wrong
    column in a financial statement.
    """
