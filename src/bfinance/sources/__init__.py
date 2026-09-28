"""
bfinance.sources — one resilient entry point over six independent scraper modules.

**Opt-in, like :mod:`bfinance.trendlyne` and :mod:`bfinance.statements`.** ``bfinance/__init__.py``
does not import this package, nothing in the main path imports it, and importing it opens no
socket and imports none of the six modules it describes. It is reached only by an explicit::

    from bfinance.sources import resolve, Basis, DEFAULT

What this layer is
------------------
Six modules each own one kind of data and know nothing about each other:

=========================  =========================================  ===========================
module                     capability                                failover?
=========================  =========================================  ===========================
:mod:`bfinance.statements`  income / balance sheet / cash flow         yes, two vendors
:mod:`bfinance.nse.macro`   India VIX, index P/E P/B yield            **no** — one publisher
:mod:`bfinance.flows`       FII/DII/Pro/Client flows, AMFI AUM        **no** — one archive
:mod:`bfinance.universe`    index membership and its history          partially
:mod:`bfinance.costs`       statutory cost schedule, date-keyed       **no** — the statutes
:mod:`bfinance.ownership`   shareholding, bulk/block/short deals      **no** — NSE only
=========================  =========================================  ===========================

"Failover?" is the question, and this package exists to answer it. Three of the six capabilities
have **one** source, so for them a failover is not a shorter wait, it is the same wait — and a
caller who does not know that learns it during an outage. :meth:`Registry.capability_report` says
so up front, and :meth:`Registry.is_upstream_exclusive` distinguishes "one source registered here"
from "no other publisher exists anywhere", which is the claim that actually matters.

The reason the basis is a first-class concern
----------------------------------------------
The four price sources are on **different bases**, and the difference is not a rounding question:

* the NSE bhavcopy is **as-traded** — what printed on the tape;
* MarketLens is **as-traded** — raw close and volume;
* screener.in's **quote** is as-traded and its **chart history** is not. Same vendor, same symbol,
  same page, two bases. The effect is measured (269.0 == bhavcopy ``ClsPric`` 269.0 on the quote);
  the cause is not, and this package says so rather than inventing one;
* the second vendor's history is **split/bonus adjusted**.

A naive failover walks down that list, takes the first vendor that answers, and splices its
history onto the previous vendor's. The seam is invisible — both are "price", the join is clean on
the index, and the series now contains a move that never happened. A 1:2 split reads as
-50.00%.

The second seam is the accounting basis, and it is measured rather than hypothetical: the second
vendor publishes **both** bases in one response, and its rendered page prints **standalone** while
the XHR behind it reports ``is_consolidated: true`` — 38,290.1 vs 40,979.6 for MOTHERSON at Mar
2026, same row, same period, same vendor.

So every resolved value carries the basis it is **on** — read off the provider, never copied from
the request, because copying it from the request is the lie this package exists to prevent — the
basis that was **requested**, and a ``basis_mismatch`` flag the caller can branch on. A source
that cannot serve the requested basis is **never called**. And a series that spans two bases is a
container that refuses to collapse (:mod:`~bfinance.sources.series`), because the number does not
exist.

The four properties, and where each is enforced
-----------------------------------------------
=========================  ==============================================================
failover is real            :func:`~bfinance.sources.cascade.resolve` — an exception falls
                            through, the skip is in ``Resolved.attempts``, and ``degraded`` says
                            the caller did not get first choice
unavailability is typed     :class:`~bfinance.sources.errors.SourceUnavailableError`, naming the
                            capability and every provider that declined, and never an empty frame
health is observable        :class:`~bfinance.sources.health.HealthLedger` — per-source counts,
                            last error, and a reordering that prefers a working source
no source serialises        a source whose recorded duration exceeds ``max_ms`` is never called,
                            and a failing source is asked later rather than first
=========================  ==============================================================

Two structural decisions worth knowing about
---------------------------------------------
**Nothing is imported at module level.** Every source is a row of *strings* in
:data:`~bfinance.sources.registry.DEFAULT` — a module path, an attribute, an adapter path — and
:func:`~bfinance.sources.cascade.load_provider` turns a row into a callable at call time. That is
what lets ``import bfinance.sources`` work in a process where none of the six modules has ever
been loaded, which is the only way the isolation tests can ask the question at all. It is also how
:mod:`bfinance.costs` and :mod:`bfinance.ownership` are registered **by capability name** before
their modules exist: a row naming a module that is not importable is a *source failure at call
time*, not an ``ImportError`` at startup that takes down every other capability with it.

**No filename in this package names a vendor package.**
``tests/test_trendlyne_core.py::TestIsolation`` matches the substring ``trendlyne`` in any module
name an ``ImportFrom`` references, and it has already caught a module that merely *named* itself
``trendlyne_provider`` and imported nothing. The files here are named for what they do —
``bindings``, ``registry``, ``cascade``, ``series``, ``model``, ``errors``, ``health`` — and
:mod:`bfinance.sources.bindings` is the only one that would want to say the vendor's name, and
does not need to: the second vendor's adjusted history is declared-but-unwired in the registry
with the reason recorded, rather than imported past the test. See
:data:`~bfinance.sources.registry.UNWIRED_NOTE`. **Do not rename these files.**

What is here
------------
- :mod:`~bfinance.sources.registry` — **read this first.** The table of which source serves which
  capability, on which basis, and the measured reasons for the four capabilities nobody can serve.
- :mod:`~bfinance.sources.model` — :class:`~bfinance.sources.model.Basis`,
  :class:`~bfinance.sources.model.Resolved`, and the comparability rule.
- :mod:`~bfinance.sources.cascade` — the try-order, the fall-through, and the basis decision.
- :mod:`~bfinance.sources.series` — the seam made structural: a container that will not collapse.
- :mod:`~bfinance.sources.health` — per-source counters and the ordering they imply.
- :mod:`~bfinance.sources.bindings` — the one place that knows a source's call shape, and the one
  that imports nothing until it is called.
- :mod:`~bfinance.sources.errors` — the typed failures.
"""

from __future__ import annotations

from .bindings import load_adapter
from .cascade import (
    Fetch,
    Plan,
    is_empty_payload,
    load_provider,
    plan,
    resolve,
    unavailable_message,
)
from .errors import (
    BasisMismatchError,
    SourceBasisError,
    SourceCapabilityError,
    SourceError,
    SourceUnavailableError,
)
from .health import HealthLedger, SourceHealth, default_ledger
from .model import (
    ATTEMPTS_ATTR,
    ATTEMPT_OUTCOMES,
    BASIS_AXIS,
    CALLED_OUTCOMES,
    FAILED,
    HEALTH_ATTR,
    MOTHERSON_CONSOLIDATED,
    MOTHERSON_STANDALONE,
    PROVENANCE,
    SERVED,
    SKIPPED_BASIS,
    SKIPPED_HEALTH,
    Attempt,
    Basis,
    BasisAxis,
    Resolved,
    basis_from,
    cross_basis_note,
    is_series,
    served,
)
from .registry import (
    COSTS_PROVIDER,
    DEFAULT,
    OWNERSHIP_PROVIDER,
    STATEMENTS_PROVIDER,
    UNAVAILABLE,
    UNWIRED_NOTE,
    Registry,
    SourceSpec,
    build_default,
    default_registry,
)
from .series import BasisHop, Segment, Series, segment_of

__all__ = [
    # entry points
    "resolve",
    "plan",
    "load_provider",
    "load_adapter",
    "is_empty_payload",
    "unavailable_message",
    "Fetch",
    "Plan",
    # model
    "Basis",
    "BasisAxis",
    "BASIS_AXIS",
    "Attempt",
    "Resolved",
    "PROVENANCE",
    "ATTEMPT_OUTCOMES",
    "ATTEMPTS_ATTR",
    "CALLED_OUTCOMES",
    "SERVED",
    "FAILED",
    "SKIPPED_BASIS",
    "SKIPPED_HEALTH",
    "basis_from",
    "cross_basis_note",
    "is_series",
    "served",
    "MOTHERSON_STANDALONE",
    "MOTHERSON_CONSOLIDATED",
    # series
    "Series",
    "Segment",
    "BasisHop",
    "segment_of",
    # registry
    "Registry",
    "SourceSpec",
    "DEFAULT",
    "UNAVAILABLE",
    "UNWIRED_NOTE",
    "STATEMENTS_PROVIDER",
    "COSTS_PROVIDER",
    "OWNERSHIP_PROVIDER",
    "build_default",
    "default_registry",
    # health
    "HealthLedger",
    "SourceHealth",
    "default_ledger",
    "HEALTH_ATTR",
    # errors
    "SourceError",
    "SourceCapabilityError",
    "SourceUnavailableError",
    "SourceBasisError",
    "BasisMismatchError",
]
