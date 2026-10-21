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

The mixins are advisory, not load-bearing, and it is worth being precise about why, because
the precise version is weaker than the obvious one. No ``except BFinanceError`` clause exists
anywhere in :mod:`bfinance.trendlyne`, and no class here is raised by any other bfinance module
-- so a Trendlyne failure cannot be swallowed by a handler that did not mean to catch it.

There are exactly two broad handlers in the package, both around the sitemap cache
(``client.py``, one on read and one on write). They catch ``Exception``, which includes
:class:`BFinanceError`, and that is deliberate: a broken or unwritable cache must not be able
to fail a data call, and a cache backend that raised the package base would otherwise turn a
performance problem into a spurious refusal. The cost is that a genuine
:class:`BFinanceError` raised *by the cache backend itself* would be absorbed as a cache miss
rather than surfaced. That is a real, accepted trade rather than an oversight, and it is why
the two sites log at ``warning`` with ``exc_info`` rather than silently.

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

Above both of those sits a third status, the only one this package cannot retry past: trendlyne
sits behind AWS WAF on CloudFront, and a challenged session answers **HTTP 405** with an
``x-amzn-waf-action`` header (see :class:`TrendlyneWafChallengeError`). It is a per-session budget
rather than a per-request defect, so it is terminal and typed separately.
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
    "TrendlyneSmartOptionsError",
    "TrendlyneTransportError",
    "TrendlyneWafChallengeError",
    "TrendlyneSymbolNotFoundError",
    "TrendlyneTokenError",
    "TrendlyneSchemaError",
    "TrendlyneTableNotFoundError",
    "TrendlyneRobotsDisallowedError",
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


class TrendlyneRobotsDisallowedError(TrendlyneError, UpstreamServiceError):
    """A request was refused **locally** because trendlyne's ``robots.txt`` disallows the path.

    Raised before any socket is opened, so honouring robots costs this package nothing. The
    message names the path, the exact ``Disallow`` rule that covers it, and the flag that would
    permit the request — because a caller who hits this is usually looking for a symbol this
    package cannot reach, and the useful answer is *"here is why, and here is the switch"*.

    Why a type and not a silent empty result: the only route this applies to is trendlyne's own
    autocomplete, which is the sole resolver for instruments the equity sitemap omits — **all
    ETFs**. Returning ``[]`` would be indistinguishable from "trendlyne has no such symbol", and
    a caller would conclude the instrument does not exist. It does; this package just will not
    ask about it. See :data:`bfinance.trendlyne.client.ROBOTS_DISALLOWED_ROUTES`.

    The 10-second ``Crawl-delay`` that is widely quoted for this site is **not** the general one:
    it is scoped to ``GPTBot`` and ``ClaudeBot``. ``User-agent: *`` carries ``Crawl-delay: 1``.
    """


class TrendlyneTableNotFoundError(TrendlyneError, ParsingError):
    """No table on the page matched the requested header signature.

    The message lists the signatures that *were* found, which is the first thing to check when
    trendlyne restyles a page.
    """


class TrendlyneSmartOptionsError(TrendlyneError, UpstreamServiceError):
    """A SmartOptions request was refused **locally** because the opt-in is closed.

    ``smartoptions.trendlyne.com`` is a **second host** with its own
    ``robots.txt`` — read live on 2026-10-10, whose ``User-agent: *`` group disallows exactly
    ``*/my-alerts/*`` and ``*/strategy-builder/user/`` with ``Crawl-delay: 1``. Neither rule can
    match ``/phoenix/api/fno/option/chain/``, so **the chain route is robots-clean** and this class
    deliberately does **not** subclass :class:`TrendlyneRobotsDisallowedError`: claiming a robots
    conflict on a path the site permits would be a false statement about a site this package has
    read. The gate is off because SmartOptions ships as a *secondary* F&O source beside
    trendlyne.com — a different promise, a different provenance tag
    ``bfinance_provenance = "smartoptions"``), a different host with its own crawl-delay
    — and a caller should opt into that rather than inherit it. An operator decision recorded
    2026-10-10, not a policy conflict.

    Raised **before any socket is opened**, so the default costs the vendor nothing. The message
    names the flag that opens it (:data:`bfinance.trendlyne.smartoptions.SMART_OPTIONS_FLAG`) and
    says explicitly that this is *not* a robots refusal, so a reader who knows the robots verdict
    is not sent looking for a rule that does not exist.
    """


class TrendlyneWafChallengeError(TrendlyneHTTPError, UpstreamServiceError):
    """trendlyne's AWS WAF challenged this request: HTTP 405, or an ``x-amzn-waf-action`` header.

    **Terminal, and never retried.** A WAF challenge is the edge answering "stop", and the same
    request sent again earns the same answer plus another request against a budget that is already
    spent. It gets its own type rather than riding :class:`TrendlyneServerError` because the two
    send a caller to opposite places: a 5xx means wait and retry, this means the session is over.

    The two forms are the same event seen from different layers. ``x-amzn-waf-action: captcha`` is
    the header AWS WAF attaches when it interstitials a request, and it can accompany almost any
    status; **HTTP 405** is what trendlyne's CloudFront answers once the session is already
    challenged. Either one is terminal here, and neither reaches the retry loop.

    **On the request budget in the message.** The ~130-150 requests per session that the message
    quotes is a **third-party measurement** of anonymous access, recorded on 2026-10-10 by a
    reconnaissance pass that ran 22 requests without tripping the challenge, and it is stated as
    that -- an outside figure this package did not produce -- rather than as a bfinance guarantee.
    The site's own ``robots.txt`` publishes a ``Crawl-delay`` and 28 ``Disallow`` rules and says
    nothing about a per-session request count, so the only numbers this package can vouch for are
    the ones it publishes itself: :data:`bfinance.trendlyne.client.DEFAULT_MIN_REQUEST_INTERVAL_MS`
    (one request per second, the stated crawl-delay) and the serial pacing that enforces it.
    """

