"""
Typed exceptions for :mod:`bfinance.trendlyne`.

Every public call in this package fails with a subclass of :class:`TrendlyneError`. There is no
third failure mode — no ``None``, no partially-built frame, no silently empty frame standing in
for an error. That is the whole contract: a number from trendlyne.com must be traceable to a
response that actually arrived.

Why every class also inherits a shared ``BFinanceError`` subclass
----------------------------------------------------------------
Each type below deliberately mixes in the closest match from
:mod:`bfinance.utils.exceptions` (which this module does **not** edit — it belongs to another
part of the package). The reason is caller compatibility: existing bfinance code already writes
``except UpstreamServiceError`` or ``except TickerNotFoundError`` around upstream calls, and a
new optional source that raised something outside those hierarchies would slip past those
handlers and abort a workflow that could otherwise have degraded.

So both hold at once::

    except TrendlyneError:            # everything from this package
    except UpstreamServiceError:      # anything upstream, including trendlyne

The mixins are advisory, not load-bearing: nothing in :mod:`bfinance.trendlyne` catches
:class:`BFinanceError`, and no class here is raised by any other bfinance module.

The failure taxonomy, and why 444 is not what it looks like
-----------------------------------------------------------
Measured live on 2026-09-27, one request at a time:

============================  ==========================================================
condition                     result
============================  ==========================================================
``/mapp/v1/...`` with a       **HTTP 444, empty body** — for a *valid* token on a
``Referer``                   healthy symbol
``/mapp/v1/...`` with a       HTTP 200, 92-byte envelope,
``Referer``, wrong token      ``head.status == "2"``, ``statusDescription == "Not Authorized !"``
``/mapp/v1/...`` with a       HTTP 200, the same 92-byte "Not Authorized !" envelope
``Referer``, wrong stock id
unknown route                 HTTP 404
============================  ==========================================================

444 is therefore **not** "bad token" — it is trendlyne's bot gate for the whole ``/mapp/v1/``
family, and it arrives with a zero-length body, so a client that treats it as anything but a
terminal status will crash inside ``response.json()`` with an empty-body decode error. It means
"this request looked like it did not come from a page on this site". The two conditions are
different problems with different fixes, so they get different types:
:class:`TrendlyneAccessDeniedError` for the gate, :class:`TrendlyneNotAuthorizedError` for the
envelope's own ``status != "0"``. See the module docstring of :mod:`bfinance.trendlyne.client`.
"""

from __future__ import annotations

from typing import Optional

from bfinance.utils.exceptions import (
    BFinanceError,
    ParsingError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

__all__ = [
    "TrendlyneError",
    "TrendlyneHTTPError",
    "TrendlyneAccessDeniedError",
    "TrendlyneNotAuthorizedError",
    "TrendlyneNotFoundError",
    "TrendlyneRateLimitedError",
    "TrendlyneServerError",
    "TrendlyneTransportError",
    "TrendlyneSymbolNotFoundError",
    "TrendlyneTokenError",
    "TrendlyneSchemaError",
    "TrendlyneTableNotFoundError",
    "TrendlyneSnapshotOnlyError",
]


class TrendlyneError(BFinanceError):
    """Base class for every failure raised by :mod:`bfinance.trendlyne`."""


class TrendlyneHTTPError(TrendlyneError):
    """An HTTP response arrived but was not usable.

    Carries the status code and URL so a caller can log or branch on them without parsing the
    message. ``url`` is redacted by the client before it gets here — a per-symbol OHLC token
    never appears in an exception.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class TrendlyneTransportError(TrendlyneError, UpstreamServiceError):
    """The request never completed: DNS failure, connection reset, or timeout.

    Retried up to ``max_retries`` with exponential backoff before being raised. A transport
    failure says nothing about whether trendlyne is up, so the message never claims it does.
    """


class TrendlyneRateLimitedError(TrendlyneHTTPError, RateLimitExceededError):
    """HTTP 429 that survived every retry. Terminal — a later call may still succeed."""


class TrendlyneServerError(TrendlyneHTTPError, UpstreamServiceError):
    """HTTP 5xx that survived every retry.

    Note that trendlyne returns **500 for a well-formed request it does not have data for**: the
    share-holding route answers 500 for any period segment other than ``latest``. A 500 is
    therefore not always a vendor outage, and this message does not assert that it is.
    """


class TrendlyneAccessDeniedError(TrendlyneHTTPError, UpstreamServiceError):
    """HTTP 444 (also 401/403) from trendlyne's ``/mapp/v1/`` gate. Not retried.

    Verified 2026-09-27: the *same* valid URL returns 444 with an empty body when the request
    omits ``Referer``, and 200 with the header. A 444 is a client-side request-shape problem, so
    retrying it unchanged would only spend the vendor's patience.
    """


class TrendlyneNotAuthorizedError(TrendlyneHTTPError, UpstreamServiceError):
    """HTTP 200 whose envelope reports failure: ``head.status != "0"``.

    Measured: a wrong (or rotated) OHLC token returns 200 with a 92-byte body,
    ``{"head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}}``. A client
    that only inspects the status code sees a success and then an empty frame.
    """


class TrendlyneNotFoundError(TrendlyneHTTPError, TickerNotFoundError):
    """HTTP 404 — no such route or resource. Terminal, not retried."""


class TrendlyneSymbolNotFoundError(TrendlyneError, TickerNotFoundError):
    """The symbol is absent from the equity sitemap.

    trendlyne has no symbol-search API (every guessed search route 404s), so the sitemap is the
    only resolver. A symbol missing from it is unresolvable *by this package* — the message says
    so instead of implying the scrip does not exist. Note this is raised **before** any request:
    the nearest thing the site itself does with a bad id is answer 200 with a failed envelope,
    which says nothing a caller could act on.
    """


class TrendlyneTokenError(TrendlyneError, UpstreamServiceError):
    """The per-symbol OHLC token could not be read out of the symbol's page.

    The token only exists inside a ``data-eodurl`` attribute, so a markup change makes it
    disappear silently. This is a loud failure by design.
    """


class TrendlyneSchemaError(TrendlyneError, ParsingError):
    """A response parsed as JSON/HTML but did not have the shape this package requires.

    Raised for a missing envelope key, a renamed column, a row of the wrong width, or an
    unparseable date. Never recovered by filling, coercing or dropping — an upstream change is
    reported, not papered over.
    """


class TrendlyneTableNotFoundError(TrendlyneError, ParsingError):
    """No table on the page matched the requested header signature.

    The message lists the signatures that *were* found, which is the first thing to check when
    trendlyne restyles a page.
    """


class TrendlyneSnapshotOnlyError(TrendlyneError):
    """A period was requested from a route that only serves the latest snapshot.

    Raised locally, before any request, so a caller cannot spend a request to learn what a URL
    shape already told us.
    """
