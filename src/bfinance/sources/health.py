"""
Health: the record of which sources are working, so the cascade can prefer one that is.

Why this is not a nicety
------------------------
A cascade whose provider order is fixed has a single point of failure at the front of the list.
The one source that is up is the one that gets asked, every time, until it stops being up — and
at that moment every caller pays the full timeout before falling through. Inverting the order on
every call (last success first) is not much better: it is a coin flip with no memory, and it
re-learns a bad afternoon once per caller.

:class:`HealthLedger` is the middle option, and it exists because of one specific question the
caller otherwise cannot answer: **how many sources does this capability actually have?** India VIX
has exactly one. FII/DII participant flows have exactly one. Those capabilities have no failover
at all, and a caller learns that only when the one source is down — which is the worst time to
learn it. :meth:`HealthLedger.solo_capabilities` answers it before the bad afternoon, from
:data:`~bfinance.sources.registry.Registry`, so ``capability_report()`` can say ``1 source`` in a
way that is checkable.

What a failure buys
------------------
A recorded failure **reorders, it does not ban**. A source that failed this morning is asked
again tomorrow, and after enough successes it returns to the front. The alternative — permanent
ejection — is a self-healing outage that never heals and a source that is never found to be
fixed. So the ledger has no memory of its own beyond the counters, and :meth:`HealthLedger.reset`
is a first-class operation rather than a restart.

A recorded failure also **costs that source one position**, not the whole capability. Ordering is
by consecutive failures with a stable tie-break, so a source that failed once sits behind a clean
one but still ahead of a source that has been failing all afternoon, and the order is
deterministic — two calls in the same state ask in the same order, which is what makes a failover
test reproducible.

The counters are not a substitute for the log. They carry no timestamps beyond the last success
and last error, they are per-process and not shared, and they reset when the process does. They
are a *prior for the next call*, and a prior is exactly what they are for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

__all__ = ["SourceHealth", "HealthLedger", "default_ledger", "HEALTH_ATTR"]

#: Key under which a health summary travels in a :class:`~bfinance.sources.model.Resolved`.
HEALTH_ATTR = "bfinance_sources_health"


@dataclass
class SourceHealth:
    """What one source has done, in this process.

    Mutable and owned by the ledger. Attributes:
        successes: Calls that returned a value.
        failures: Calls that were asked and did not answer.
        consecutive_failures: The counter that orders the cascade. Reset to zero by a single
            success, which is what makes the source a prior rather than a verdict.
        last_error: One line, from the most recent failure. ``None`` until one has happened, and
            never cleared by a success — a source that is working again should still show why it
            was not, or the operator has no way to tell a blip from an unreported change.
        last_success: Monotonic timestamp of the most recent success, for a human reading a
            report. Never used for ordering: a wall clock makes the order of two calls
            irreproducible, and reproducible order is worth more here than a readable timestamp.
        observed_ms: Most recent observed duration, or ``None`` if never measured. Feeds the
            "do not serialise the rest behind a known-slow source" check.
        calls: Total attempts, success or failure.
    """

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_success: Optional[float] = None
    observed_ms: Optional[float] = None
    calls: int = 0

    @property
    def ok(self) -> bool:
        """Whether the last call this source was asked for succeeded."""
        return self.consecutive_failures == 0

    def as_dict(self) -> Dict[str, Any]:
        """A JSON-safe summary, for a status endpoint or a log line."""
        return {
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_success": self.last_success,
            "observed_ms": self.observed_ms,
        }

    def describe(self) -> str:
        """One line for a report: successes/failures, the trailing state, and the last error."""
        state = "ok" if self.ok else f"{self.consecutive_failures} consecutive failure(s)"
        tail = f", last error: {self.last_error}" if self.last_error else ""
        return f"{self.successes} ok / {self.failures} failed, {state}{tail}"


class HealthLedger:
    """Per-source counters, and the ordering they imply.

    One ledger per registry in practice. The default is process-global and memoised, because a
    caller who wants the cascade to be stateless can pass ``HealthLedger()`` and a caller who
    wants it to learn can have the default; the two behaviours differ only in a constructor
    argument, and choosing "stateless" should not mean editing five call sites.
    """

    def __init__(self, sources: Optional[Sequence[str]] = None) -> None:
        """Args:
        sources: Names to pre-register at zero, so a report lists a source that has not been
            called yet instead of omitting it. Omitting an uncalled source is how a
            single-source capability reads as "no sources" and hides the very thing the caller
            came to find out.
        """
        self._health: Dict[str, SourceHealth] = {}
        for name in sources or ():
            self._health[name] = SourceHealth()

    # -- recording -------------------------------------------------------- #

    def record_success(self, source: str, observed_ms: Optional[float] = None) -> SourceHealth:
        """Record a served call, clearing the consecutive-failure counter.

        ``observed_ms`` is passed in rather than measured here, so a test can pin it and so a
        caller with its own timing (a cache hit, a warm connection) can report the duration it
        actually experienced.
        """
        health = self._health.setdefault(source, SourceHealth())
        health.successes += 1
        health.calls += 1
        health.consecutive_failures = 0
        health.last_success = time.monotonic()
        if observed_ms is not None:
            health.observed_ms = observed_ms
        return health

    def record_failure(
        self, source: str, error: str, observed_ms: Optional[float] = None
    ) -> SourceHealth:
        """Record a call that was asked and did not answer.

        ``error`` is expected to be one line. The ledger is read by a status endpoint, and a
        traceback there is a wall of noise in front of the two numbers that matter.
        """
        health = self._health.setdefault(source, SourceHealth())
        health.failures += 1
        health.calls += 1
        health.consecutive_failures += 1
        health.last_error = " ".join(str(error).split())
        if observed_ms is not None:
            health.observed_ms = observed_ms
        return health

    # -- reading ---------------------------------------------------------- #

    def get(self, source: str) -> SourceHealth:
        """Health for one source, creating a zeroed record on first ask.

        Returns the stored object rather than a copy, so ``ledger.get(x).successes`` reads the
        live count. Callers that want a snapshot use :meth:`snapshot`.
        """
        return self._health.setdefault(source, SourceHealth())

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Every source's counters as plain dicts, JSON-safe."""
        return {name: health.as_dict() for name, health in sorted(self._health.items())}

    def reset(self, source: Optional[str] = None) -> None:
        """Forget one source's history, or all of it.

        A first-class operation because a ledger that can only be cleared by restarting the
        process is a ledger that gets ignored: after an upstream incident is resolved the obvious
        thing a caller wants is "ask that source first again", and the alternative is editing
        module state from a test.
        """
        if source is None:
            self._health.clear()
        else:
            self._health.pop(source, None)

    # -- ordering --------------------------------------------------------- #

    def order(self, specs: Sequence[Any]) -> Tuple[Any, ...]:
        """The specs in the order they should be tried.

        Sorted by :attr:`SourceHealth.consecutive_failures`, ascending, with the **incoming
        position as the tie-break**. Two consequences, both intended:

        * Registry order is the default and survives whenever the ledger has nothing to say, so
          the try-order a caller reads in the registry is the try-order they get on a cold start.
        * Ties break on position rather than on dict order, so the result is deterministic. An
          unstable sort here would make a failover test pass or fail on the order of two equally
          healthy sources, which is a coin flip dressed as a test.

        Returns the same objects, reordered; does not mutate ``specs``.
        """
        indexed = list(enumerate(specs))
        indexed.sort(key=lambda pair: (self.get(pair[1].name).consecutive_failures, pair[0]))
        return tuple(spec for _, spec in indexed)

    def slow(self, source: str, budget_ms: float) -> bool:
        """Whether this source is known to cost more than ``budget_ms``.

        Conservative in both directions, and the direction matters: **unknown is not slow.** A
        source with no recorded duration is asked, because refusing to ask a source for not having
        been measured yet would make a cold ledger refuse everything, and a cascade that refuses
        everything is a cascade that always raises.

        This is the mechanism behind the rule that one source does not serialise the rest. An
        ordered cascade is inherently serial — the first provider is asked before the second, so
        the first provider's latency is the second provider's wait. The only way out without
        threads is to know in advance who is slow and pass them over, which is what this
        predicate and the recorded :attr:`SourceHealth.observed_ms` are for.
        """
        observed = self.get(source).observed_ms
        return observed is not None and observed > budget_ms

    def register(self, specs: Sequence[Any]) -> None:
        """Pre-register a set of source names at zero, so a report lists uncalled sources.

        Called by the cascade with the specs for the capability being resolved, which is why
        ``capability_report()`` can state a source count that includes sources nobody has called
        yet.
        """
        for spec in specs:
            self._health.setdefault(spec.name, SourceHealth())

    def describe(self) -> str:
        """A human-readable report: one line per source, alphabetically."""
        if not self._health:
            return "(no source has been called yet)"
        return "\n".join(
            f"  {name}: {health.describe()}" for name, health in sorted(self._health.items())
        )


#: Memoised process-wide ledger. Deliberately *not* created at import: constructing it is cheap
#: but the point being made is that a module-level mutable is a thing a reader has to reason
#: about, so it is behind a factory and the factory is trivial.
_DEFAULT: Optional[HealthLedger] = None


def default_ledger() -> HealthLedger:
    """The process-wide ledger, created on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = HealthLedger()
    return _DEFAULT
