"""
The cascade: try providers in registry order, record why each one did or did not answer.

What this layer is
------------------
:mod:`bfinance.statements` already cascades, over two vendors and three statement kinds. This
module cascades over **capabilities** — over the six modules that each own a different kind of
data and have no knowledge of each other. It is the layer a caller reaches when the question is
"give me India VIX, and if NSE is having a bad afternoon tell me" rather than "give me the
quarterly P&L, and if screener is down use the other vendor". The statements cascade is
registered here as a single provider and keeps doing that work itself; nothing below re-implements
it.

What it does that a loop over providers would not
------------------------------------------------
A naive cascade is nine lines and is wrong in four ways. Each of the four is a bug this module
exists to make impossible, and each has a test that would go red if it were reintroduced:

**It does not concatenate across a basis change.** Every value carries the basis it is *on*, read
off the provider rather than copied from the request. A hop that changes basis is flagged
(:attr:`~bfinance.sources.model.Resolved.basis_mismatch`) and never blended, because a blended
price series contains a move that never happened. See :mod:`bfinance.sources.series` for the
container that makes the seam visible instead of dissolving it.

**It never calls a source that cannot serve the requested basis.** Not "calls it and checks the
answer" — does not call it. A source that publishes only split-adjusted prices costs a request to
tell a caller that it cannot serve an as-traded request, and for a rate-limited vendor that
request is the expensive one. The skip is recorded with its reason, so "why is my data from that
vendor" is answerable without reading the registry.

**It never answers with nothing.** An unavailability is
:class:`~bfinance.sources.errors.SourceUnavailableError` naming the capability and every
provider that declined. :func:`is_empty_payload` is the definition of "answered with nothing",
and the one case where a source returning an empty frame is treated as a *failure* rather than as
data — because a vendor that serves five HTML shells at HTTP 200 with zero rows would otherwise
read as "this company has no price history".

**It learns which source is working.** :class:`~bfinance.sources.health.HealthLedger` reorders by
consecutive failures, so the source that is up is asked first and a source that failed this
morning is behind the one that is working now. And a source *known* to be slow is passed over
outright, which is the only way an ordered cascade avoids serialising the whole capability behind
its slowest member.

On the ordering, precisely
-------------------------
An ordered cascade is serial by construction: provider 1 is asked before provider 2, so provider
1's latency is provider 2's wait. There is no concurrency here, deliberately — threads would make
a failover test depend on a scheduler, and a test that depends on a scheduler is a test that
fails on a busy CI machine. The two things that would otherwise require concurrency are instead
done with information: a source whose recorded duration exceeds the caller's ``max_ms`` is
**never called**, and a source with a recorded failure streak is **asked later**. What is left
genuinely serial is a source that is neither known-slow nor known-broken, and no amount of local
plumbing can answer for that without going concurrent.

Nothing here opens a socket. Providers are callables — either loaded from a module string at call
time by :mod:`bfinance.sources.bindings`, or handed in by a caller as ``fetch``. The whole
cascade is therefore exercisable from recorded fixtures, and there is no code path in which a
routing decision decides to go and get a page.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bindings import call_entry, load_adapter
from .errors import SourceBasisError, SourceCapabilityError, SourceUnavailableError
from .health import HealthLedger, default_ledger
from .model import (
    FAILED,
    SERVED,
    SKIPPED_BASIS,
    SKIPPED_HEALTH,
    Attempt,
    Basis,
    Resolved,
    basis_from,
)
from .registry import DEFAULT, Registry, SourceSpec

logger = logging.getLogger("bfinance")

__all__ = [
    "Fetch",
    "Plan",
    "is_empty_payload",
    "load_provider",
    "plan",
    "resolve",
    "unavailable_message",
]

#: What the cascade calls to get one source's value. It may raise, and a raise is a fall-through
#: rather than a failure of the call. Supplied by the caller, which is what makes the whole
#: cascade testable offline: a stub is a dict lookup, and a stub that raises is the failover
#: case.
Fetch = Callable[[SourceSpec, Mapping[str, Any]], Any]


def _short(reason: BaseException) -> str:
    """One line naming what went wrong, for ``attempts`` and the final error.

    Class name and message, never a traceback: these strings travel into logs and CSV exports,
    and a traceback in a cell is a wall of noise in both. The class name is kept because
    ``"ConnectionError: 'NoneType' object is not iterable"`` is only diagnosable if you know it
    was not a connection error.
    """
    text = " ".join(str(reason).split())
    if len(text) > 300:
        text = text[:297] + "…"
    return f"{type(reason).__name__}: {text}"


# --------------------------------------------------------------------------- #
# "answered with nothing"
# --------------------------------------------------------------------------- #


def is_empty_payload(value: Any) -> bool:
    """Whether a provider's return counts as "did not answer" rather than as data.

    Deliberately narrow, because the rule cuts both ways. Treating *any* falsy value as a failure
    would be wrong in the other direction: an empty list is a real answer for a membership-diff
    capability, and turning it into an exception would make the layer refuse to report a genuine
    "nothing changed". So exactly three shapes count as a non-answer:

    * ``None`` — the provider had nothing and said so by returning nothing;
    * a **frame** with zero rows — the shape a vendor's HTML shell or a failed join produces, and
      the exact shape that reads as a real answer to a caller who is not looking;
    * an **empty mapping** — the same failure in dict form, for a provider that does not build a
      frame.

    A ``DataFrame`` with columns and no rows is not the same thing as ``None``, and the
    difference is worth stating: the first is a vendor that answered with a table; the second is
    a vendor that did not answer. Only the second is a failure here.
    """
    if value is None:
        return True
    if isinstance(value, Mapping) and len(value) == 0:
        return True
    # Duck-typed on the frame protocol rather than imported, so this module can make the decision
    # about a value from any module — including a stub — without knowing which framework built it.
    if hasattr(value, "empty") and hasattr(value, "shape") and getattr(value, "empty", False):
        return True
    return False


# --------------------------------------------------------------------------- #
# lazy provider resolution
# --------------------------------------------------------------------------- #


def load_provider(spec: SourceSpec) -> Callable[[Mapping[str, Any]], Any]:
    """Turn a registry row into a callable, importing what it names, right now.

    This is the lazy resolution, and it is the only place in the package where a module string
    becomes an import. The import happens on the call that needs the source, not at import time
    and not at registry-build time, which is why ``import bfinance.sources`` costs one dataclass
    and a dict no matter how many of the six modules it describes.

    Raises:
        SourceUnavailableError: The row is not wired. Refusing loudly matters here: returning
            ``None`` would be indistinguishable from a source that answered with nothing, and
            that is the one confusion the whole package is organised around avoiding.
    """
    if not spec.wired:
        raise SourceUnavailableError(spec.unavailability())
    try:
        module = importlib.import_module(spec.module)
    except ImportError as exc:
        raise SourceUnavailableError(
            f"source {spec.name!r} is registered against {spec.module!r}, which could not be "
            f"imported: {_short(exc)}. The row is data, so the module is only required when a "
            f"call actually reaches this source — which is the point of the design, and also why "
            f"this reads as a source failure rather than an import error at startup."
        ) from exc
    try:
        entry = getattr(module, spec.entry)
    except AttributeError as exc:
        raise SourceUnavailableError(
            f"source {spec.name!r} is registered against {spec.module}:{spec.entry}, and that "
            f"attribute does not exist. {spec.note}"
        ) from exc
    if not spec.adapter:
        return lambda request: call_entry(entry, request)
    adapter = load_adapter(spec.adapter)
    return lambda request: adapter(entry, request)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Plan:
    """The try-order and every exclusion, computed without calling anything.

    Attributes:
        basis: The basis the call would run on — the caller's, or the capability's default.
        basis_explicit: Whether the caller named it. ``False`` means the basis was defaulted, so a
            later ``basis_mismatch`` is worth less than one the caller asked for and the result
            records the difference.
        ordered: The eligible sources, in the order they would be tried, after health reordering.
        attempts: The non-calls, already recorded, so a caller can see which sources were passed
            over and why **before** paying for a call.
        excluded: Sources the caller removed by name, and the reason.
    """

    basis: Basis
    basis_explicit: bool
    ordered: Tuple[SourceSpec, ...]
    attempts: Tuple[Attempt, ...]
    excluded: Mapping[str, str]


def plan(
    capability: str,
    *,
    basis: Optional[Any] = None,
    registry: Optional[Registry] = None,
    health: Optional[HealthLedger] = None,
    only: Optional[Sequence[str]] = None,
    disable: Sequence[str] = (),
    max_ms: Optional[float] = None,
) -> Plan:
    """Work out the try-order and every exclusion, **without calling anything**.

    Exposed as a public entry point, not just an internal step, because "which sources could serve
    this, and which are being passed over" is a question a caller has a legitimate reason to ask
    before committing to a call — to check whether a capability has any redundancy, or to see that
    an as-traded request is being routed away from the split-adjusted source. It is the same
    computation :func:`resolve` performs, minus the calls, so the two cannot disagree.
    """
    reg = DEFAULT if registry is None else registry
    led = default_ledger() if health is None else health

    requested = _requested_basis(capability, basis, reg)
    specs = reg.specs_for(capability)

    excluded: Dict[str, str] = {}
    if only is not None:
        wanted = set(only)
        unknown = sorted(wanted - {spec.name for spec in specs})
        if unknown:
            raise SourceCapabilityError(
                f"source(s) {unknown} are not registered for {capability!r}. Registered: "
                f"{list(reg.names_for(capability))}.\n\n{reg.capability_report()}"
            )
        specs = tuple(spec for spec in specs if spec.name in wanted)
    disabled = set(disable)
    for spec in specs:
        if spec.name in disabled:
            excluded[spec.name] = "disabled by the caller for this call"

    eligible = [spec for spec in specs if spec.name not in excluded]
    led.register(eligible)

    ordered = led.order(eligible)
    attempts: List[Attempt] = []
    for spec in ordered:
        if not spec.serves(requested):
            published = " and ".join(b.value for b in spec.bases)
            attempts.append(
                Attempt(
                    provider=spec.name,
                    outcome=SKIPPED_BASIS,
                    reason=(
                        f"publishes only the {published} basis, and the {requested.value} basis was "
                        f"requested. {spec.note}"
                    ),
                    basis=requested,
                )
            )
        elif max_ms is not None and led.slow(spec.name, max_ms):
            observed = led.get(spec.name).observed_ms
            attempts.append(
                Attempt(
                    provider=spec.name,
                    outcome=SKIPPED_HEALTH,
                    reason=(
                        f"last observed call took {observed:.0f}ms, over the {max_ms:.0f}ms "
                        f"ceiling this call set, so it is not asked at all rather than making "
                        f"everything behind it wait"
                    ),
                    basis=requested,
                )
            )
    return Plan(
        basis=requested,
        basis_explicit=basis is not None,
        ordered=tuple(ordered),
        attempts=tuple(attempts),
        excluded=dict(excluded),
    )


def _requested_basis(capability: str, basis: Optional[Any], registry: Registry) -> Basis:
    """The basis this call is on: the caller's, or the capability's declared default.

    A capability whose sources disagree has no honest default unless one is declared, and the
    declaration is "the first registered source's basis" — the cold-start answer, since that is
    the source that will answer when everything is healthy. A caller who did not name a basis and
    got served by anything else is told so, because *they* have no expectation and the seam still
    happened; the fact that it was defaulted is recorded so they can tell the two apart.
    """
    if basis is not None:
        return basis_from(basis)
    specs = registry.specs_for(capability)
    if not specs:
        # Nothing to default from. ``REPORTED`` is the axis with no adjustment semantics, so a
        # request on it matches any source that publishes a non-price figure, and the capability
        # error raised by the caller reports the real problem rather than this fallback.
        return Basis.REPORTED
    return specs[0].bases[0]


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def resolve(
    capability: str,
    *,
    request: Optional[Mapping[str, Any]] = None,
    basis: Optional[Any] = None,
    registry: Optional[Registry] = None,
    health: Optional[HealthLedger] = None,
    fetch: Optional[Fetch] = None,
    only: Optional[Sequence[str]] = None,
    disable: Sequence[str] = (),
    max_ms: Optional[float] = None,
) -> Resolved:
    """Resolve one capability, trying providers in order and falling through on failure.

    Args:
        capability: What to read. Must be registered in the registry; an unknown name is a
            :class:`~bfinance.sources.errors.SourceCapabilityError` raised before anything is
            called.
        request: The request mapping handed to the provider. Never mutated, never logged, and
            deliberately the *only* channel — a source that needs a bespoke argument list is an
            :func:`~bfinance.sources.bindings.call_entry` problem, not a signature problem here.
        basis: The basis wanted. A source that does not publish it is **never called**. Omit it to
            take the capability's declared default; the result then records that the basis was
            defaulted rather than asked for.
        registry: Which routing table. Defaults to :data:`~bfinance.sources.registry.DEFAULT`.
        health: Which ledger. Defaults to the process-wide one, so a caller does not have to opt
            in to the layer learning from its own failures.
        fetch: Called once per **eligible** provider with ``(spec, request)``. It may raise; a
            raise is a fall-through. Supplying it bypasses module loading entirely, which is how
            the suite drives this offline.
        only: Restrict to these source names, in the registry's order.
        disable: Source names to pass over for this call, recorded as skipped.
        max_ms: A **per-source** ceiling. A source whose last observed call exceeded it is not
            called at all, which is how an ordered cascade avoids serialising everything behind a
            known-slow source. A source never observed is still asked — unknown is not slow.

    Returns:
        A :class:`~bfinance.sources.model.Resolved` from the first provider that answered, with
        every attempt recorded, ``degraded`` set when the caller did not get first choice, and the
        basis read off the provider rather than assumed.

    Raises:
        SourceCapabilityError: The capability is unknown, or registered as impossible upstream.
        SourceBasisError: Providers exist for this capability but **none** publishes the requested
            basis. Deliberately not a :class:`~bfinance.sources.errors.SourceUnavailableError`:
            nothing upstream is broken, and telling a caller to retry a request that can never
            succeed is how a refusal gets ignored.
        SourceUnavailableError: Every eligible provider was asked and none answered. The message
            names the capability and every provider considered, with the reason each gave.
    """
    reg = DEFAULT if registry is None else registry
    led = default_ledger() if health is None else health
    req = dict(request or {})

    if not reg.is_registered(capability):
        raise SourceCapabilityError(reg.capability_error(capability))
    if not reg.specs_for(capability):
        # Registered as a measured refusal, not as an oversight. The message says so, and the
        # report says what is available, so the caller has somewhere to go.
        raise SourceCapabilityError(reg.capability_error(capability))

    planned = plan(
        capability,
        basis=basis,
        registry=reg,
        health=led,
        only=only,
        disable=disable,
        max_ms=max_ms,
    )
    if not planned.ordered:
        raise SourceBasisError(
            f"no source registered for {capability!r} publishes the {planned.basis.value} basis, "
            f"so this request cannot be served by any of them and asking again will not change "
            f"that. Registered, and what each one publishes: "
            f"{ {s.name: [b.value for b in s.bases] for s in reg.specs_for(capability)} }.\n\n"
            f"The remedy is to relax the request, not to retry: a basis is a property of what a "
            f"vendor publishes, not of whether it is up today.\n\n{reg.capability_report()}"
        )

    attempts: List[Attempt] = []
    # Skips are computed by plan() but recorded here, in try-order, as the loop reaches each row.
    # Recording them up front would put every skip ahead of every call in ``attempts``, and the
    # order of that list is the order the cascade actually considered things in — which is the
    # whole reason a caller can read it.
    skips = {attempt.provider: attempt for attempt in planned.attempts}
    if planned.ordered and len(skips) == len(planned.ordered):
        # Every eligible source was passed over rather than asked. That is a *different* refusal
        # from every source having failed: nothing upstream is broken, retrying will not help, and
        # the fix is to ask for a basis somebody publishes. Reporting it as an upstream failure
        # would tell a caller to retry a request that can never succeed, and would also make the
        # health ledger record four failures for sources that were never asked.
        raise SourceBasisError(
            f"no source registered for {capability!r} publishes the {planned.basis.value} basis. "
            f"{len(skips)} of the {len(planned.ordered)} eligible source(s) were passed over "
            f"without being asked, and the request cannot be served by any of them:\n"
            + "\n".join(
                f"  {attempt.provider}: {attempt.reason}" for attempt in skips.values()
            )
            + f"\n\nThe remedy is to relax the request, not to retry: a basis is a property of "
            f"what a vendor publishes, not of whether it is up today.\n\n{reg.capability_report()}"
        )
    for spec in planned.ordered:
        preskip = skips.get(spec.name)
        if preskip is not None:
            attempts.append(preskip)
            logger.debug(
                "sources: skipping %s for %s — %s", spec.name, capability, preskip.reason
            )
            continue
        started = time.monotonic()
        try:
            if fetch is not None:
                value = fetch(spec, req)
            else:
                value = load_provider(spec)(req)
        except Exception as exc:
            # Deliberately broad, and deliberately only ``Exception``. To a cascade a fetch that
            # raises, a payload of the wrong shape, a transport failure and a KeyError from a
            # schema change all mean the same thing — this provider did not answer — and none of
            # them is a reason to lose a capability a second provider can serve.
            # ``BaseException`` is not caught, so KeyboardInterrupt and SystemExit still stop.
            elapsed = (time.monotonic() - started) * 1000.0
            led.record_failure(spec.name, _short(exc), observed_ms=elapsed)
            attempts.append(Attempt(spec.name, FAILED, _short(exc), planned.basis))
            logger.warning(
                "sources: %s could not serve %s (%s); falling through if there is another",
                spec.name, capability, _short(exc),
            )
            continue
        elapsed = (time.monotonic() - started) * 1000.0
        if is_empty_payload(value):
            reason = _empty_reason(spec, value)
            led.record_failure(spec.name, reason, observed_ms=elapsed)
            attempts.append(Attempt(spec.name, FAILED, reason, planned.basis))
            logger.warning("sources: %s answered %r for %s; falling through", spec.name, value, capability)
            continue
        led.record_success(spec.name, observed_ms=elapsed)
        attempts.append(Attempt(spec.name, SERVED, "", planned.basis))
        return _finish(
            spec=spec,
            value=value,
            capability=capability,
            planned=planned,
            attempts=attempts,
            led=led,
            reg=reg,
            request=req,
        )

    raise SourceUnavailableError(
        unavailable_message(
            capability,
            planned.basis,
            attempts,
            excluded=planned.excluded,
            registry=reg,
            health=led,
        )
    )


def _empty_reason(spec: SourceSpec, value: Any) -> str:
    """Why an empty payload counts as a failure rather than an answer.

    The reason is stated rather than implied, because "the source was passed over" and "the source
    was asked and returned nothing" are different diagnoses and the second is usually a vendor
    problem worth reporting.
    """
    shape = "frame" if hasattr(value, "shape") else "mapping"
    return (
        f"answered with an empty {shape} ({value!r}). Treated as a failure, not as data: a vendor "
        f"that serves a shell or an empty table at HTTP 200 would otherwise read as a real answer, "
        f"and 'this has no history' is a claim about the market that the source did not make. "
        f"{spec.note}"
    )


def _finish(
    *,
    spec: SourceSpec,
    value: Any,
    capability: str,
    planned: Plan,
    attempts: Sequence[Attempt],
    led: HealthLedger,
    reg: Registry,
    request: Mapping[str, Any],
) -> Resolved:
    """Attach the cascade's own history to a provider's value.

    Done in one place, so a result cannot claim ``degraded=False`` while its attempt list contains
    a failure, and so the basis verdict is computed once from the serving provider's own declared
    basis rather than from anything the caller passed in.

    The ``degraded`` rule is the subtle one and it is deliberate: a provider that was **skipped**
    because it cannot serve the basis is *not* degradation — the system did the only thing
    available and the caller should not be trained to ignore the flag. Only a provider that was
    **asked and failed** is degradation, because there the caller wanted that answer and did not
    get it. A basis mismatch is degradation too, for the same reason.
    """
    served_at = next(i for i, a in enumerate(attempts) if a.provider == spec.name and a.outcome == SERVED)
    failed_first = any(a.outcome == FAILED for a in attempts[:served_at])
    served_basis, basis_only_from_request = _served_basis(spec, planned.basis, value)
    mismatch = served_basis is not planned.basis
    attrs: Dict[str, Any] = {
        "bfinance_sources_basis_defaulted": not planned.basis_explicit,
        "bfinance_sources_order": [spec.name for spec in planned.ordered],
    }
    if basis_only_from_request:
        # A source that publishes several bases was asked for one of them and is answering with
        # the result of that request. That is a weaker statement than a single-basis row makes, and
        # it is recorded rather than presented as if it were the strong one.
        attrs["bfinance_sources_basis_multi"] = [b.value for b in spec.bases]
    if isinstance(value, Resolved):
        # A provider that declared its basis did so by returning a `served(...)` envelope. That
        # envelope is an assertion about provenance, not the payload, so it is consumed here and
        # the caller gets the value. Handing back the wrapper would mean every caller had to know
        # that ``.value`` might be a Resolved or might be their data, which is exactly the kind of
        # union this layer exists to remove.
        attrs.update(value.attrs)
        payload = value.value
        as_of = value.as_of if value.as_of is not None else _as_of_of(payload, request)
    else:
        payload = value
        as_of = _as_of_of(value, request)
    return Resolved(
        value=payload,
        source=spec.name,
        capability=capability,
        basis=served_basis,
        basis_requested=planned.basis,
        basis_mismatch=mismatch,
        degraded=failed_first or mismatch,
        attempts=tuple(attempts),
        as_of=as_of,
        attrs=attrs,
        health=led.snapshot(),
    )


def _served_basis(spec: SourceSpec, requested: Basis, value: Any) -> Tuple[Basis, bool]:
    """The basis this provider actually answered on, and whether that is only the request echoed.

    Resolution order, most authoritative first:

    1. **The value's own declaration.** A provider that knows it served something other than what
       its registry row promised says so, by returning
       :func:`~bfinance.sources.model.served` or a :class:`~bfinance.sources.model.Resolved`. This
       is the case the whole layer exists for: the second vendor's rendered page prints
       ``standalone`` while the XHR behind it reports ``is_consolidated: true``, and no registry
       row can know in advance which of the two representations a given fetch landed on. The row
       is the *expected* basis and decides routing; the payload is the *actual* basis and decides
       the flag.
    2. **A ``Basis`` returned bare**, which is the same statement in one fewer characters.
    3. **The row's single declared basis.** A row that publishes exactly one basis cannot be on
       any other, so this is a deduction rather than an assumption.
    4. **The requested basis**, for a multi-basis row that declared nothing. This is the weakest
       step and it is reported as such in ``attrs``: "this source publishes both and you asked for
       consolidated" is a weaker guarantee than "this source publishes only consolidated", and
       only the first can be leaned on when the two representations disagree.
    """
    if isinstance(value, Resolved):
        return value.basis, False
    if isinstance(value, Basis):
        return value, False
    attrs = getattr(value, "attrs", None)
    if isinstance(attrs, Mapping) and attrs.get("bfinance_statements_basis"):
        return basis_from(attrs["bfinance_statements_basis"]), False
    if len(spec.bases) == 1:
        return spec.bases[0], False
    return requested, True


def _as_of_of(value: Any, request: Mapping[str, Any]) -> Optional[Any]:
    """The currency stamp of a resolved value, or ``None``.

    In priority order: what the caller declared, what a nested :class:`Resolved` declared, what
    the value's own provenance says, and the newest period of a frame whose index is a date. The
    last one is a convenience and it is only reached for a value with the **frame** protocol,
    because a plain ``list`` also has an ``.index`` — and that method has no length, no dtype, and
    no notion of "newest". Reading it would raise, and a wrong ``as_of`` is worse than none.
    """
    if "as_of" in request:
        return request["as_of"]
    if isinstance(value, Resolved):
        return value.as_of
    attrs = getattr(value, "attrs", None)
    if isinstance(attrs, Mapping):
        for key in ("bfinance_sources_as_of", "bfinance_as_of", "bfinance_statements_as_of"):
            if key in attrs:
                return attrs[key]
    if not (hasattr(value, "shape") and hasattr(value, "index")):
        return None
    index = value.index
    if not len(index) or getattr(index, "dtype", None) is None:
        return None
    kind = str(index.dtype)
    if kind.startswith("datetime") or kind.startswith("period") or kind.startswith("date"):
        return max(index)
    return None


def unavailable_message(
    capability: str,
    basis: Basis,
    attempts: Sequence[Attempt],
    *,
    excluded: Optional[Mapping[str, str]] = None,
    registry: Optional[Registry] = None,
    health: Optional[HealthLedger] = None,
) -> str:
    """The refusal text for a capability nothing could serve, built from the attempts.

    Names the capability, the basis, and **every** provider considered — asked or not — with the
    reason each gave, then the health record. The health record is in the message on purpose: a
    caller reading a failure wants to know whether this is today's incident or a permanent state,
    and that is the one piece of context an exception otherwise cannot carry.
    """
    called = [a for a in attempts if a.called]
    passed_over = [a for a in attempts if not a.called]
    lines = [
        f"bfinance could not return {capability!r} on the {basis.value} basis. "
        f"{len(called)} of the {len(attempts)} source(s) considered were asked, and none "
        f"answered:",
    ]
    for attempt in called:
        lines.append(f"  {attempt.provider}: {attempt.reason}")
    if passed_over:
        lines.append(f"  {len(passed_over)} source(s) were not asked at all:")
        for attempt in passed_over:
            lines.append(f"  {attempt.provider}: {attempt.reason}")
    for name, reason in (excluded or {}).items():
        lines.append(f"  {name}: {reason}")
    if registry is not None:
        lines.append("")
        lines.append("What is registered for this capability:")
        lines.append(f"{registry.capability_report()}")
    if health is not None:
        lines.append("")
        lines.append("Source health so far:")
        lines.append(health.describe())
    return "\n".join(lines)
