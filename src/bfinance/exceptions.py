"""
yfinance's exception names, for code being ported to bfinance.

What this module is, and the limit of it
----------------------------------------
yfinance is a **dev-only** dependency (``pyproject.toml``: ``dependencies`` does not name it,
``[project.optional-dependencies].dev`` does). So :class:`BFinanceError` cannot derive from
``yfinance.exceptions.YFException``: a drop-in replacement that cannot be imported without the
thing it replaces is not a drop-in replacement. Only one direction is achievable, and it is the
reading direction.

**The limit, stated plainly: this module makes ``bfinance``-side code read like ``yfinance`` code,
and nothing more.** ``from bfinance.exceptions import YFRateLimitError`` resolves, and the object
it binds IS :class:`~bfinance.utils.exceptions.RateLimitExceededError`, so ``except
YFRateLimitError`` catches a bfinance raise. It does **not** make ``except
yfinance.exceptions.YFRateLimitError`` catch a bfinance raise, because that ``except`` clause binds
**yfinance's** class object, and two objects are not one object however alike their names. Any
``except`` clause written against ``yf.exceptions.*`` must be rewritten to
``except bfinance.exceptions.*`` when the import is ported. A module that implied otherwise would
be a lie, which is worse than the absence, so the sentence is here rather than inferred.

Why not import the names from ``__init__.py``
---------------------------------------------
Deliberately: ``bfinance/__init__.py`` is on every caller's import path, and this module buys a
port nothing but a shorter ``except``. ``from bfinance.exceptions import X`` works as a plain
submodule import with nothing in ``__init__.py``, and ``import bfinance`` keeps paying for neither.

The eight names, and which are aliases and which are stubs
---------------------------------------------------------
An **alias** is a name bound to a class bfinance already raises, so the ``except`` clause a
port already had keeps working against the real behaviour:

============================  ====================================================
``YFException``               alias of :class:`~bfinance.utils.exceptions.BFinanceError`
``YFTickerMissingError``      alias of :class:`~bfinance.utils.exceptions.TickerNotFoundError`
``YFDataException``           alias of :class:`~bfinance.utils.exceptions.UpstreamServiceError`
``YFRateLimitError``          alias of :class:`~bfinance.utils.exceptions.RateLimitExceededError`
============================  ====================================================

A **stub** is a real, raisable class with no yfinance peer in bfinance's behaviour. It exists so
the ``except`` clause parses and the name is importable, and its docstring says which failure
bfinance raises **instead** -- because a caller who catches a stub that never fires has been told
the wrong thing by silence. Every stub derives from :class:`BFinanceError`, never from
``Exception`` directly, so the package keeps its one root and ``except BFinanceError`` still
catches the whole of ``bfinance`` (see ``tests/test_public_surface.py``).

:class:`~bfinance.utils.exceptions.ParsingError` and
:class:`~bfinance.utils.exceptions.NotAnNseSymbolError` have **no** yfinance peer and are
deliberately absent: inventing a ``YF`` name for either would put a class in the namespace that
no yfinance caller has ever written.

Aliases, not subclasses, and why it matters here too
----------------------------------------------------
The four aliases are bindings, not definitions, so ``value.__module__`` still reads
``bfinance.utils.exceptions``. That is what keeps them out of the single-root count: they cannot
add a second class deriving from ``Exception``, because they are not classes at all, they are the
one class under a second name.
"""

from .utils.exceptions import (
    BFinanceError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

# ---------------------------------------------------------------- the aliases

#: yfinance's root, bound to bfinance's root. Same object, so a ported ``except`` clause works.
YFException = BFinanceError

#: yfinance's "no such ticker", bound to the class bfinance actually raises for it.
YFTickerMissingError = TickerNotFoundError

#: yfinance's "the upstream data source failed", bound to bfinance's equivalent.
YFDataException = UpstreamServiceError

#: yfinance's "the upstream rate limited us", bound to bfinance's HTTP 429 refusal.
YFRateLimitError = RateLimitExceededError


# ------------------------------------------------------------------- the stubs


class YFTzMissingError(BFinanceError):
    """Raised when a timezone cannot be determined for the request.

    **bfinance never raises this**, and that is not a gap: every instrument bfinance serves is an
    Indian equity or index on NSE/BSE time, which is ``Asia/Kolkata`` with no DST and no
    per-ticker lookup to fail. yfinance raises its equivalent when a ticker carries no timezone
    metadata; there is no such ticker here. bfinance's frames come back tz-naive IST wall-clock,
    so the question this class asks has no failure mode.

    It is defined rather than omitted so ``except YFTzMissingError`` in ported code still
    resolves and still binds a catchable class. Nothing in ``bfinance`` raises it, so the clause
    is inert -- it will never fire, which is stated here rather than left to be discovered.
    """


class YFPricesMissingError(BFinanceError):
    """Raised when a symbol has no price history to return.

    **bfinance raises a different class for the nearest case, on purpose.**
    :class:`~bfinance.market.ohlcv.RealOHLCUnavailableError` means "the vendor was reached and
    could not serve this", which is an upstream failure; this class means "this instrument has no
    prices", which is a fact about the instrument. Those are different diagnoses with different
    fixes -- retry the request versus stop asking -- and binding one name to the other would hand
    a caller a confidently-wrong answer to "why is this empty?".

    So this stub is not an alias of :class:`~bfinance.market.ohlcv.RealOHLCUnavailableError`, and
    it is not raised anywhere. It exists to keep a ported ``except`` clause resolvable and
    catchable; a handler that catches it will never fire, and the honest fix is to catch
    ``RealOHLCUnavailableError`` instead.
    """


class YFInvalidPeriodError(BFinanceError):
    """Raised when a ``period`` argument is not one this package supports.

    **bfinance raises ``ValueError`` for this, not this class.** yfinance wraps its period check
    in a typed exception; bfinance does not. The two entry points that validate a period --
    :func:`bfinance.nse.marketlens.validate_period` and the Trendlyne client -- both raise a plain
    ``ValueError`` naming the offending value and the accepted set, e.g.::

        ValueError: period='7q' is not supported. Must be one of ...

    Nothing here raises this class. A ported ``except YFInvalidPeriodError`` clause will not catch
    the refusal bfinance actually makes, so a port has to change that clause to ``except
    ValueError`` -- the single change this module cannot make for the caller.
    """


class YFNotImplementedError(BFinanceError, NotImplementedError):
    """Raised where bfinance has a typed refusal instead of an implementation.

    yfinance's MRO is ``YFNotImplementedError -> NotImplementedError -> RuntimeError ->
    Exception`` and notably **not** ``YFException``, so in yfinance this class is *not* catchable
    as ``YFException``. bfinance's deliberately diverges, and the trade is stated rather than
    silently taken:

    * **What is kept from yfinance:** ``NotImplementedError`` in the bases, so a ported
      ``except NotImplementedError`` still catches it -- and so do the existing raise sites that
      raise bare ``NotImplementedError`` (see
      ``bfinance.market.derivatives.DerivativesEngine.generate_option_chain``).
    * **What is given up:** fidelity of the MRO. In bfinance this class *is* catchable as
      ``BFinanceError``/``YFException``, which yfinance's is not. The cost is that
      ``except bfinance.exceptions.YFException`` is slightly broader than
      ``except yfinance.exceptions.YFException``: bfinance's also catches this one.
    * **Why that direction:** the single-root rule is the package's load-bearing promise -- one
      ``except BFinanceError`` catches all of ``bfinance``. Deriving from ``NotImplementedError``
      alone would make this the one class in ``src/`` a caller's single ``except`` misses, and
      "the class a package's own umbrella does not cover" is a worse surprise than a slightly
      wider umbrella.

    Nothing here raises this class either; the stubs beside it are the same kind of courtesy. What
    bfinance actually raises for an unimplemented path is the bare ``NotImplementedError`` the
    method documents.
    """


#: Exactly the eight names this module exists to provide, and nothing else -- the four underlying
#: ``bfinance`` classes above are imported plumbing, not this module's public surface, so a star
#: import hands a caller the yfinance vocabulary and not the internals behind it.
__all__ = [
    "YFException",
    "YFTickerMissingError",
    "YFDataException",
    "YFRateLimitError",
    "YFTzMissingError",
    "YFPricesMissingError",
    "YFInvalidPeriodError",
    "YFNotImplementedError",
]