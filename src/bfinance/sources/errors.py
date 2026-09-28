"""
Typed failures for :mod:`bfinance.sources`.

Every public call in this package fails with a subclass of :class:`SourceError`, and each one
also inherits the closest match from :mod:`bfinance.utils.exceptions` — the same mixin rule
:mod:`bfinance.statements.errors` documents, so that::

    except SourceError:         # everything from this layer
    except UpstreamServiceError:   # anything upstream, including all six modules

both hold at once. A caller already writing ``except UpstreamServiceError`` around a data fetch
does not have to learn a new hierarchy to get the cascade.

The taxonomy is two questions, and the class name says which one was asked
----------------------------------------------------------------------------
1. **Can this capability exist at all?** :class:`SourceCapabilityError` — the registry has no
   provider for it. Measured, cited, and terminal. This is the only error here that subclasses
   :class:`NotImplementedError`, because it replaces a bare ``NotImplementedError`` raise that
   told its caller nothing. :mod:`bfinance.statements.errors` splits this taxonomy three ways;
   this layer needs two, because it does not fetch pages itself — it is handed a callable, so
   "the bytes were not a data page" is the provider's error to raise, not this layer's to
   re-classify, and re-classifying it here would *lose* the distinction the provider made.
2. **Did anything answer?** :class:`SourceUnavailableError` — providers exist, every one of them
   was asked or declined, and none produced a value. The message names the capability and every
   provider that declined, with the reason each gave. It is emphatically **not** an empty frame:
   see :func:`bfinance.sources.cascade.is_empty_payload` for what counts as "did not answer".

And one error that is not about the sources at all:

:class:`BasisMismatchError` — the caller asked this layer to reduce a series that spans two
bases to one number. That number does not exist. This is raised rather than answered because the
alternative — a value that looks like a price and is not — is the specific failure this whole
package was built to prevent.
"""

from __future__ import annotations

from bfinance.utils.exceptions import BFinanceError, UpstreamServiceError

__all__ = [
    "SourceError",
    "SourceCapabilityError",
    "SourceUnavailableError",
    "SourceBasisError",
    "BasisMismatchError",
]


class SourceError(BFinanceError):
    """Base class for every failure raised by :mod:`bfinance.sources`."""


class SourceCapabilityError(SourceError, NotImplementedError):
    """No provider is registered for this capability.

    The one error in this module that is **not** a bug report. Either nobody publishes it — and
    then the registry carries the measurement, the way ``bfinance.statements`` records its four
    unbuildable (kind, frequency) cells — or a capability name was misspelled. Both are worth the
    same refusal, because a caller who gets the former is right to give up and a caller who gets
    the latter is right to check their spelling, and the message is written to let them tell.

    Subclasses :class:`NotImplementedError` so the bare raises it replaces are still caught by
    existing ``except NotImplementedError`` handlers.
    """


class SourceUnavailableError(SourceError, UpstreamServiceError):
    """Every provider that could serve this capability either declined or failed.

    The message names the capability, lists **every** provider that was considered — not just the
    ones that were asked — with the reason each gave, and appends the health record so a caller
    can see whether this is today's bad afternoon or a permanent state. "A raise falls through"
    only means something when every fall-through was written down; a cascade that fails silently
    is indistinguishable from a cascade that was never tried.
    """


class SourceBasisError(SourceError, ValueError):
    """A basis was requested that no provider for this capability can serve.

    Distinct from :class:`SourceUnavailableError` because the remedy differs: here nothing went
    wrong, and asking again will not help — the sources publish a different basis. Distinct from
    :class:`SourceCapabilityError` because the capability *is* registered and will usually serve
    on its own basis if the caller relaxes the request.
    """


class BasisMismatchError(SourceError, ValueError):
    """A series spans two bases and was asked to yield a single value.

    Raised by :meth:`bfinance.sources.series.Series.frame` and
    :meth:`bfinance.sources.series.Series.value_at`. The message quotes the two figures on either
    side of the seam and the arithmetic between them, so the reader can see exactly how far apart
    they are and why no average of the two is a number that means anything.

    Inherits :class:`ValueError` because a caller who wrote ``series["2026-03-31"]`` in a
    spreadsheet-shaped expectation and got a basis problem is being told they asked the wrong
    question, which is a :class:`ValueError` in every other library they have used.
    """
