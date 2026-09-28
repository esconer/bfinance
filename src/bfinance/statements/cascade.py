"""
The cascade: try the providers in registry order, normalise to one shape, fall through on
failure — and record that you did.

The whole value proposition, stated precisely
---------------------------------------------
Two upstreams publish the same two capabilities, independently. That is what makes a failover
**redundancy** rather than ceremony: if screener.in is down, throttling, or has renamed a
``<section id=…>``, the quarterly P&L and the TTM P&L survive, and the caller gets a frame with
``attrs["bfinance_statements_fallback"] = True`` and the reason recorded next to it. A cascade
that works but leaves no trace is indistinguishable from a cascade that was never exercised, so
every skip and every failure is written to ``attrs`` — including the reason, including for a
provider that was skipped rather than tried.

What the cascade never does
---------------------------
**Blend.** There is no code path anywhere in this package that merges two providers' rows into
one :class:`~bfinance.statements.model.StatementResult`, and closing that path is not on the
backlog. The reason is measured, not stylistic: screener.in is consolidated-only on this route,
while trendlyne ships **both** bases in one response and its SSR page renders **standalone** while
its own XHR reports ``is_consolidated: true`` (MOTHERSON at Mar 2026 — the page prints
``TotalShareHoldersFunds_A`` as 38,290.1 and the consolidated dump says 40,979.6 for the same row
and the same period). Joining a consolidated figure to a standalone one, or relabelling one as the
other, produces a number that is wrong, looks right, and is wrong by exactly the difference
between the two bases. So a result is served on the basis it was served on, **flagged** when that
is not the basis requested, and never adjusted.

**Invent a capability.** When the registry has no provider for a (kind, frequency) the cascade
raises :class:`~bfinance.statements.errors.StatementCapabilityError` *before* any payload is
read, quoting the measured reason and naming both vendors. Quarterly balance sheet, quarterly
cash flow, TTM balance sheet and TTM cash flow are the four such cells, and refusing them is a
success: a package that fabricates a quarterly balance sheet is worse than one that says it
cannot get one.

Basis is a routing decision, not a post-hoc label
-------------------------------------------------
A provider that cannot serve the requested basis is **skipped with a reason**, not asked and
mis-served. So ``basis="standalone"`` skips screener.in (consolidated-only) with its note
attached and lands on trendlyne's JSON provider, and ``basis="consolidated"`` on a payload set
with only the SSR page available skips trendlyne-html and ends with a flagged, standalone-served
result rather than a lie. Either way the caller can read one boolean,
``attrs["bfinance_statements_basis_mismatch"]``, instead of parsing prose.

Offline by construction
-----------------------
The cascade never opens a socket. It is handed a ``fetch`` callable and calls it once per
provider, so the try-order, the fall-through and the recording are all testable from recorded
fixtures — and there is no code path in which a statement read decides to go and get a page.
:func:`resolve_from_payloads` wires the same cascade to payloads a caller already has in hand,
which is what the whole test suite uses.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .errors import StatementCapabilityError, StatementError, StatementSourceError
from .model import Basis, Frequency, StatementKind, StatementRequest, StatementResult
from .registry import (
    CAPABILITIES,
    VENDORS,
    ProviderSpec,
    capability_report,
    providers_for,
    unavailable_reason,
)
from .fundamentals_provider import (
    HTML_PAYLOAD,
    JSON_PAYLOAD,
    parse_financials_html,
    parse_fundamental_results,
)
from .screener_provider import PAYLOAD as SCREENER_PAYLOAD
from .screener_provider import parse_company_page

logger = logging.getLogger("bfinance")

__all__ = [
    "Basis",
    "CAPABILITIES",
    "Fetch",
    "Frequency",
    "PAYLOAD_PARSE",
    "StatementKind",
    "capability_error_message",
    "parse_payload",
    "providers_for",
    "resolve",
    "resolve_from_payloads",
]

#: What the cascade calls to get one provider's raw bytes. It may raise, and a raise is a
#: fall-through, not a failure of the call.
Fetch = Callable[[ProviderSpec], Any]

#: payload discriminator -> the function that reads it.
PAYLOAD_PARSE: Dict[str, Callable[[Any, StatementRequest, Dict[str, Any]], StatementResult]] = {
    SCREENER_PAYLOAD: lambda payload, request, opts: parse_company_page(
        payload, request, spec_name=opts.get("screener_table")
    ),
    HTML_PAYLOAD: lambda payload, request, opts: parse_financials_html(
        payload, request, spec_name=opts.get("trendlyne_table")
    ),
    JSON_PAYLOAD: lambda payload, request, opts: parse_fundamental_results(payload, request),
}


def _short(reason: BaseException) -> str:
    """One line naming what went wrong, for ``attrs`` and the final error.

    The class name and the message, never a traceback: a frame's ``attrs`` travels into logs and
    CSV exports, and a traceback in a cell is a wall of noise in both.
    """
    text = " ".join(str(reason).split())
    if len(text) > 300:
        text = text[:297] + "…"
    return f"{type(reason).__name__}: {text}"


def _skip_reason_for_basis(spec: ProviderSpec, request: StatementRequest) -> str:
    """Why this provider was not asked, when the reason is the basis rather than a failure."""
    published = " and ".join(basis.value for basis in spec.bases)
    return (
        f"publishes only the {published} basis, and the {request.basis.value} basis was "
        f"requested. {spec.note}"
    )


def capability_error_message(request: StatementRequest) -> str:
    """The refusal text, built from the registry so the two cannot drift.

    Names both vendors, states that this is measured rather than assumed, and says plainly that
    upgrading bfinance will not produce the data.
    """
    reason = unavailable_reason(request.kind, request.frequency)
    vendor_list = " and ".join(VENDORS)
    if reason is None:  # pragma: no cover - only when the cell is registered and unbuildable
        reason = (
            f"no provider is registered for {request.kind.value}/{request.frequency.value} and "
            f"no reason is recorded. That is a registry gap, not an upstream limit — report it."
        )
    return (
        f"bfinance cannot return a {request.kind.value} statement at "
        f"{request.frequency.value} frequency for {request.symbol}, and no reachable source "
        f"can either. {reason}\n\n"
        f"Checked against: {vendor_list}. "
        f"This is a measured upstream limit on both, not a bfinance bug, and it will not be "
        f"fixed by a later version of bfinance. What is available, and from where:\n"
        f"{capability_report()}"
    )


def parse_payload(
    spec: ProviderSpec,
    payload: Any,
    request: StatementRequest,
    options: Optional[Mapping[str, Any]] = None,
) -> StatementResult:
    """Read one provider's raw payload into a result, applying any spec override."""
    opts = dict(options or {})
    parser = PAYLOAD_PARSE.get(spec.payload)
    if parser is None:  # pragma: no cover - PAYLOAD_PARSE covers every registered payload
        raise StatementSourceError(
            f"provider {spec.name!r} declares payload {spec.payload!r} and this package has no "
            f"parser for it. Registered: {sorted(PAYLOAD_PARSE)}."
        )
    return parser(payload, request, opts)


def resolve(
    request: StatementRequest,
    fetch: Fetch,
    *,
    only: Optional[Sequence[str]] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> StatementResult:
    """Resolve one statement, trying each registered provider in order and falling through.

    Args:
        request: What to read.
        fetch: Called once per provider with its :class:`~bfinance.statements.registry.
            ProviderSpec`, returning that provider's raw payload — a page for the HTML providers,
            a decoded dict for the JSON one. **It may raise**; a raise is a fall-through. It is
            never called for a provider that cannot serve ``request.basis``, so a caller does not
            have to guard its own fetcher against a request it knows will be mis-served.
        only: Restrict to these provider names, in the registry's order. For answering "what does
            this vendor alone give me", and for the negative control that proves the fallback
            works.
        options: Per-provider parser options — ``screener_table`` and ``trendlyne_table`` to
            override the selected :class:`~bfinance.statements.registry.TableSpec`.

    Returns:
        The first :class:`~bfinance.statements.model.StatementResult` a provider produced, with
        ``providers_tried``, ``fallback``, ``fallback_from`` and ``skip_reasons`` filled in.

    Raises:
        StatementCapabilityError: The registry has no provider for this (kind, frequency), or
            ``only`` excluded every registered one. The message names both vendors and states
            that the limit is upstream.
        StatementError: Every provider that could serve the requested basis failed. The message
            names each one and why, so a caller can tell "both vendors are down" from "this
            symbol has no statements".
    """
    opts = dict(options or {})
    all_specs = providers_for(request.kind, request.frequency)
    # The capability check comes first, before `only` is even looked at. If no source publishes
    # this cell then the caller's provider preference is beside the point, and the refusal text —
    # which names both vendors and says a later bfinance will not help — is the answer they need,
    # not "screener is not registered here".
    if not all_specs:
        raise StatementCapabilityError(capability_error_message(request))

    if only is not None:
        wanted = tuple(only)
        unknown = [name for name in wanted if name not in {spec.name for spec in all_specs}]
        if unknown:
            raise StatementCapabilityError(
                f"provider(s) {unknown} are not registered for "
                f"{request.kind.value}/{request.frequency.value}. Registered: "
                f"{[spec.name for spec in all_specs]}. \n{capability_report()}"
            )
        specs = tuple(spec for spec in all_specs if spec.name in wanted)
    else:
        specs = all_specs

    if not specs:
        raise StatementCapabilityError(
            f"only={list(only or ())} excluded every provider registered for "
            f"{request.kind.value}/{request.frequency.value}, which has "
            f"{[spec.name for spec in all_specs]}. \n{capability_error_message(request)}"
        )

    skip_reasons: Dict[str, str] = {}
    attempted: list = []
    order = tuple(spec.name for spec in specs)
    for spec in specs:
        if request.basis not in spec.bases:
            skip_reasons[spec.name] = _skip_reason_for_basis(spec, request)
            logger.debug(
                "statements: skipping %s for %s — %s",
                spec.name, request.symbol, skip_reasons[spec.name],
            )
            continue
        attempted.append(spec.name)
        try:
            payload = fetch(spec)
            result = parse_payload(spec, payload, request, opts)
        except Exception as exc:
            # Deliberately broad, and deliberately only ``Exception``: a fetch that raises, a
            # payload that is the wrong shape, a page that is a decoy, a transport failure, a
            # KeyError from a schema change — all of them mean the same thing to a cascade, which
            # is "this provider did not answer", and none of them is a reason to lose a
            # capability a second provider can serve. ``BaseException`` is not caught, so
            # KeyboardInterrupt and SystemExit still stop the process.
            skip_reasons[spec.name] = _short(exc)
            logger.warning(
                "statements: %s could not serve %s/%s for %s (%s); falling through to the next "
                "provider if there is one.",
                spec.name, request.kind.value, request.frequency.value, request.symbol,
                _short(exc),
            )
            continue
        return _finish(result, order, attempted, skip_reasons)

    raise StatementError(
        f"no provider could return a {request.kind.value}/{request.frequency.value} statement "
        f"for {request.symbol} on the {request.basis.value} basis. "
        f"{len(attempted)} of the {len(order)} registered provider(s) were asked, and none "
        f"answered:\n"
        + "\n".join(f"  {name}: {reason}" for name, reason in skip_reasons.items())
        + f"\n\nCapabilities:\n{capability_report()}"
    )


def _finish(
    result: StatementResult,
    order: Sequence[str],
    attempted: Sequence[str],
    skip_reasons: Mapping[str, str],
) -> StatementResult:
    """Attach the cascade's own history to a provider's result.

    Done in one place, so a result can never claim ``fallback=False`` while ``registry_order``
    names two providers, and so the basis verdict is computed once from the serving provider's
    own basis rather than from anything the caller passed in.
    """
    return dataclasses.replace(
        result,
        registry_order=tuple(order),
        providers_tried=tuple(attempted),
        skip_reasons=dict(skip_reasons),
    )


def resolve_from_payloads(
    request: StatementRequest,
    *,
    screener_html: Optional[str] = None,
    trendlyne_html: Optional[str] = None,
    trendlyne_json: Optional[Any] = None,
    only: Optional[Sequence[str]] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> StatementResult:
    """Run :func:`resolve` against payloads the caller already has. No socket is opened.

    This is the entry point the offline suite uses and the one a caller with a cache should use.
    A provider with no payload is treated exactly as a failed fetch, with the reason naming which
    payload is missing — so "I only have the trendlyne page" and "trendlyne is down" produce the
    same fall-through and the same record, and neither silently returns an empty frame.

    Args:
        request: What to read.
        screener_html: A screener.in company page, or ``None`` for "not available".
        trendlyne_html: The ``/fundamentals/financials/`` SSR page, or ``None``.
        trendlyne_json: The decoded ``get-fundamental_results-v2`` payload, or ``None``.
        only: Restrict to these provider names. See :func:`resolve`.
        options: Per-provider parser options. See :func:`resolve`.
    """
    available: Dict[str, Any] = {
        SCREENER_PAYLOAD: screener_html,
        HTML_PAYLOAD: trendlyne_html,
        JSON_PAYLOAD: trendlyne_json,
    }
    names = {
        SCREENER_PAYLOAD: "screener_html",
        HTML_PAYLOAD: "trendlyne_html",
        JSON_PAYLOAD: "trendlyne_json",
    }

    def fetch(spec: ProviderSpec) -> Any:
        payload = available.get(spec.payload)
        if payload is None:
            raise StatementSourceError(
                f"no {names[spec.payload]} was supplied for provider {spec.name!r}; the cascade "
                f"was given nothing to read from it."
            )
        return payload

    return resolve(request, fetch, only=only, options=options)

