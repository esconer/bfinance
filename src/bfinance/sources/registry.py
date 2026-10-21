"""
The registry: which source can answer which capability, on which basis, and in what order.

Read this before changing anything about routing. It holds one table, :data:`DEFAULT`, and the
whole point of the table is that it is **data, not imports** — every source is a row of strings,
so importing :mod:`bfinance.sources` imports none of the six modules it describes.

Why a second registry, when :mod:`bfinance.statements` already has one
--------------------------------------------------------------------
Because the two answer different questions, and conflating them is how a layer grows a bug it
cannot see.

:mod:`bfinance.statements` asks **"which payload route answers this (kind, frequency)?"** Its
rows are routes on two vendors, its cascade is the whole of its job, and it knows nothing about
prices, flows, holdings or cost schedules. This module asks **"which capability can this source
serve, at all, and on what basis?"** — a question that has no answer inside any one of the six
modules, because each of them is the only one that knows about its own data.

So this registry does **not** re-implement the statements cascade. :data:`STATEMENTS_PROVIDER`
is a single row pointing at :mod:`bfinance.statements`, and the statements package's own
try-order, its four refusal cells and its four refusal messages all keep working underneath. What
this layer adds is the part statements cannot know: that ``india_vix`` has no second source, that
an as-traded request must never be served from an adjusted one, and that a caller who wants
"whichever of these six works" should not have to know there are six.

The basis column, and why it is a column
----------------------------------------
Every row declares the basis it publishes. This is the load-bearing part of the table, and it is
here rather than in the provider implementations because **the provider is not in a position to
know what was asked for**. A bhavcopy reader returns whatever the tape said; it has no idea the
caller wanted a split-adjusted series, and asking it to decide would be asking the wrong layer.

The consequences, all of them enforced by :mod:`bfinance.sources.cascade`:

* a row whose basis is not the requested one is **never called** — not called and rejected;
* a hop between rows with different bases is **flagged** on the result, and never blended;
* a series spanning two bases is a **container** that refuses to collapse
  (:mod:`bfinance.sources.series`), because the number does not exist.

The measured disagreements, quoted here so a future row cannot quietly contradict one
-------------------------------------------------------------------------------------------
* screener.in is **consolidated-only** on the statements route.
* The second vendor ships **both** bases in one response, and its rendered page prints
  **standalone** while the XHR behind it reports ``is_consolidated: true``. MOTHERSON at Mar 2026:
  ``38,290.1`` standalone and ``40,979.6`` consolidated, same row, same period, same vendor.
* screener.in's **quote** is as-traded (269.0 == bhavcopy ``ClsPric`` 269.0) but its **chart
  history** is dividend-adjusted. The effect is measured; the *cause* is not, and the row says so
  rather than inventing a mechanism.

The rows about which nothing can be done
-----------------------------------------
Three capabilities are registered with **no provider**, and a measured reason:
``tri_index_return``, ``rbi_reference_rate``, ``pledge_shares`` and ``fpi_ownership``. Registering
them as refusals rather than omitting them is deliberate: a caller who asks gets told the limit is
upstream and will not be fixed by a later bfinance, instead of a ``KeyError`` that reads like a
typo. That is the same standard :class:`~bfinance.statements.errors.StatementCapabilityError`
sets, and matching it is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .bindings import PATH as _BINDINGS
from .model import Basis

__all__ = [
    "SourceSpec",
    "Registry",
    "DEFAULT",
    "default_registry",
    "STATEMENTS_PROVIDER",
    "COSTS_PROVIDER",
    "OWNERSHIP_PROVIDER",
    "TRENDLYNE_OHLC_PROVIDER",
    "UNWIRED_NOTE",
    "build_default",
]

#: The provider name for the statements cascade. Registered as **one** provider because
#: :mod:`bfinance.statements` is already a cascade, with its own try-order and its own refusals.
#: Wrapping its two vendors as two rows here would duplicate that cascade and give the caller two
#: places to configure one thing.
STATEMENTS_PROVIDER = "statements-cascade"

#: The second vendor's daily OHLC, read by this package's own parser rather than by the leaf
#: package. Named as a constant because it is an identifier that appears in the try-order, in
#: every ``attempts`` entry and in the health ledger, and it is spelled once here for the same
#: reason every other source name is.
TRENDLYNE_OHLC_PROVIDER = "trendlyne-ohlc"

#: The two capabilities whose modules are still landing. They are registered **by name** with a
#: lazy module string and nothing else, so this layer is complete and importable before either
#: module exists — and so a caller gets a precise "that module is not importable" rather than an
#: ``ImportError`` at import time, which would take down every other capability along with it.
COSTS_PROVIDER = "statutory-costs"
OWNERSHIP_PROVIDER = "ownership-bulk"


@dataclass(frozen=True)
class SourceSpec:
    """One source, and what it can be asked for.

    Attributes:
        name: The stable identifier. Appears in :attr:`~bfinance.sources.model.Resolved.source`,
            in every ``attempts`` entry, in the health ledger, and in refusal messages, so it is
            spelled once here and never composed at a call site.
        capabilities: What this source can serve. A row that serves nothing is a registry bug and
            is rejected in :meth:`Registry.register`.
        bases: The bases this source publishes. **Never inferred from the source's own output** —
            that is the whole failure mode. See the module docstring for the measurements.
        provides: The **columns** this source's price rows actually carry, spelled in the
            vocabulary :mod:`bfinance.market.ohlcv` builds frames in. This is the second
            eligibility rule, and it exists because a basis is not the only way a source can fail
            to answer: MarketLens publishes ``Close`` and ``Volume`` and no Open/High/Low, and a
            close-only frame is a *non-empty* answer, so a caller asking for bars would be served a
            frame with none of them and nothing would say so. Declared here rather than left to be
            discovered at call time, for the same reason :attr:`bases` is: **the provider is not in
            a position to know what was asked for**.

            Empty — the default, and the right value for every row that is not a price row — means
            "this source publishes no named price column", and such a row is **not** column-filtered.
            That is the conservative reading on purpose: skipping a source for a column it might
            publish under another name is how a working failover gets turned into a refusal, and an
            unregistered column is not a measurement.
        note: Why the row says what it says. Mandatory and non-empty, because a basis column with
            no evidence next to it is a claim, and this table's whole purpose is to replace claims
            with measurements.
        module: The module to import at call time, or ``""`` for a row that is declared but not
            wired. A **string**, never an import: that is what keeps
            :mod:`bfinance.sources` importable without the six modules.
        entry: The attribute to read from ``module``.
        adapter: Dotted path to a function in *this* package that turns ``(entry, request)`` into a
            value, also a string, for the rows whose entry point does not already take a request
            mapping — a class that must be constructed, a function with named parameters. Empty
            means "call ``entry(request)``", which is the right default and the only thing a row
            should use unless it has a specific reason not to. Naming the adapter as a string
            rather than holding a reference to it is the same rule as :attr:`module`: the table
            stays data, so importing it imports nothing.
        upstream_only: This source is the **only publisher anywhere**, not merely the only one this
            layer registered. A stronger claim, and the one that tells a caller their capability
            has no failover at all before the day it matters. Every row asserting it is either
            backed by a measurement in its :attr:`note` or does not assert it.
    """

    name: str
    capabilities: Tuple[str, ...]
    bases: Tuple[Basis, ...]
    note: str
    provides: Tuple[str, ...] = ()
    module: str = ""
    entry: str = ""
    adapter: str = ""
    upstream_only: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a SourceSpec needs a name; it is how every attempt is recorded")
        if not self.capabilities:
            raise ValueError(
                f"source {self.name!r} serves no capability, so it can never be selected and will "
                f"only ever appear in a report as a mystery row"
            )
        if not self.bases:
            raise ValueError(
                f"source {self.name!r} declares no basis. Every resolved value carries a basis, so "
                f"a row without one cannot be asked whether it is eligible — which is precisely "
                f"the check that prevents a silent basis change."
            )
        if not self.note:
            raise ValueError(
                f"source {self.name!r} has no note. The basis column here is a measurement; a row "
                f"that cannot say how it was measured is a guess with a table around it."
            )
        if (not self.module) != (not self.entry):
            raise ValueError(
                f"source {self.name!r} must declare both module and entry, or neither "
                f"(got module={self.module!r}, entry={self.entry!r})"
            )
        if self.adapter and not self.module:
            raise ValueError(
                f"source {self.name!r} declares adapter {self.adapter!r} but no module, so the "
                f"adapter has nothing to adapt"
            )

    @property
    def wired(self) -> bool:
        """Whether this row can actually be called, as opposed to only declared.

        A declared-but-unwired row is a real, load-bearing thing: it holds a basis claim so that
        a request on that basis is routed correctly, and it makes an eligible request fail with a
        stated reason instead of being silently served from a different basis. Asking an unwired
        row to load raises rather than returning ``None``, because ``None`` here would be
        indistinguishable from a source that answered with nothing — the exact confusion this
        package exists to prevent.
        """
        return bool(self.module)

    def serves(self, basis: Basis) -> bool:
        """Whether this row publishes ``basis`` and so is eligible for a request on it."""
        return basis in self.bases

    def missing_columns(self, required: Sequence[str]) -> Tuple[str, ...]:
        """The requested columns this row does **not** publish, in the order asked for.

        Empty means "eligible", and empty is also what an empty :attr:`provides` returns for any
        request — a row that publishes no named price column is not column-filtered, for the reason
        :attr:`provides` documents. Order is preserved rather than sorted so the refusal sentence
        quotes the caller back the columns it named, in its own order.
        """
        if not self.provides:
            return ()
        published = set(self.provides)
        return tuple(column for column in required if column not in published)

    def unavailability(self) -> str:
        """The refusal text for a row that cannot be loaded, naming the reason.

        For an unwired row the reason is a deliberate architectural decision, and it is stated
        here so a caller debugging a missing capability finds a sentence rather than an
        ``AttributeError`` on ``None``.
        """
        if self.wired:
            return f"source {self.name!r} is wired to {self.module}:{self.entry}"
        return (
            f"source {self.name!r} is registered but not wired into this layer. {self.note} "
            f"Reach it through its own module; do not route around this, because the basis it "
            f"publishes ({', '.join(b.value for b in self.bases)}) is the reason it is here."
        )

    def describe(self) -> str:
        """One line for a report."""
        bases = ", ".join(b.value for b in self.bases)
        target = f"{self.module}:{self.entry}" if self.wired else "not wired"
        if self.adapter:
            target += f" via {self.adapter}"
        solo = " [only publisher upstream]" if self.upstream_only else ""
        columns = f" columns: {', '.join(self.provides)}" if self.provides else ""
        return (
            f"{self.name} -> {', '.join(self.capabilities)} on {bases} via {target}{columns}{solo}"
        )


#: Why the second vendor's adjusted price history is registered but not wired. Kept as a constant
#: because it is the one place in this package where a source is *known* and deliberately
#: unreachable, and that is a sentence someone will otherwise "fix" by adding the import.
#:
#: Rewritten when the wired row landed beside it. The previous version was wrong in a way a claim
#: test caught: it presented the AST import scan as the rule that keeps the leaf package out of
#: reach, and a scan of import statements would not have stopped a dotted string -- which is how
#: every other row in this table defers its import to call time. The two rules are both real and
#: both are named below; only one of them binds.
UNWIRED_NOTE = (
    "This row reserves the split_adjusted basis without naming a module this layer can load, and "
    "it is registered but not wired on purpose. The vendor's adjusted price history is reachable "
    "only through bfinance.trendlyne, which is a leaf package, and two separate rules keep it "
    "that way. The one that BINDS is tests/test_sources_cascade.py::TestRegistrationWithoutImport, "
    "which requires bfinance.trendlyne to be absent from Registry.by_module(): a resolution must "
    "not be able to reach the leaf package at all, lazily or otherwise. The other is "
    "tests/test_trendlyne_core.py::TestIsolation, which scans the AST of every module outside the "
    "package and fails on any import naming it. That one is still enforced and still true -- it "
    "simply is not what holds this row closed, and an earlier version of this note claimed it was. "
    "Read the leaf directly if you need the vendor's own client.\n\n"
    "A wired row now sits beside this one and is ordered ahead of it: "
    "bfinance.sources.price_provider reads the same three routes with its own parser rather than "
    "importing the leaf, which is why it can be wired where this row cannot. So this row is a "
    "second entry on the same basis, not the only one, and a split_adjusted request is answered "
    "by that row whenever it can be. This one is reached only when it cannot, and reaching it "
    "raises SourceUnavailableError naming this note -- loudly, never a None that a caller could "
    "not distinguish from 'the source has no data'. Nothing about the basis has changed: the "
    "series is as_traded_split_bonus_adjusted and DEMERGER_APPLIED is False, so it must not be "
    "spliced with as-traded bars across a split or bonus date, and it must not be spliced with a "
    "source that rewrites demerger history, where the disagreement runs the other way."
)


class Registry:
    """Capability -> ordered providers, plus the measured reasons for the gaps.

    Mutable, and cheap to copy. A caller that wants a different try-order (to pin a vendor for a
    comparison, say) should build their own with :meth:`copy` and :meth:`register` rather than
    mutating :data:`DEFAULT`, so that one caller's experiment cannot re-order the process.

    Insertion order is the try-order, and it is the *declared* order. Health re-ordering happens
    in :class:`~bfinance.sources.health.HealthLedger` and is recorded on the result, so the
    registry always shows the cold-start order and never a transient one.
    """

    def __init__(self) -> None:
        self._specs: Dict[str, SourceSpec] = {}
        self._capabilities: Dict[str, List[str]] = {}
        self._unavailable: Dict[str, str] = {}

    # -- construction ----------------------------------------------------- #

    def register(self, spec: SourceSpec) -> SourceSpec:
        """Add one source. A duplicate name is a registry bug and raises.

        Overwriting would be worse than raising: two rows for one name means the health ledger,
        the ``attempts`` list and the refusal message can all refer to different things under one
        identifier, and the resulting bug is silent.
        """
        if spec.name in self._specs:
            raise ValueError(
                f"source {spec.name!r} is already registered as "
                f"{self._specs[spec.name].describe()!r}. A second row with the same name would "
                f"make the health ledger and the attempts list lie about which one ran."
            )
        self._specs[spec.name] = spec
        for capability in spec.capabilities:
            self._capabilities.setdefault(capability, []).append(spec.name)
        return spec

    def declare_unavailable(self, capability: str, reason: str) -> None:
        """Record a capability nobody can serve, and the measured reason.

        Not an omission. A registered refusal gives the caller the same treatment
        :mod:`bfinance.statements` gives its four unbuildable cells: named, measured, and
        explicitly not fixable by upgrading.
        """
        if not reason:
            raise ValueError(
                f"capability {capability!r} is being declared unavailable with no reason. That is "
                f"strictly worse than leaving it unregistered: the caller is told the answer is no "
                f"and given nothing to check that against."
            )
        if capability in self._capabilities:
            raise ValueError(
                f"capability {capability!r} already has provider(s) "
                f"{self._capabilities[capability]}, so it cannot also be declared unavailable"
            )
        self._unavailable[capability] = reason

    def copy(self) -> "Registry":
        """A shallow copy, for a caller who wants their own try-order without touching the default."""
        clone = Registry()
        clone._specs = dict(self._specs)
        clone._capabilities = {k: list(v) for k, v in self._capabilities.items()}
        clone._unavailable = dict(self._unavailable)
        return clone

    # -- reading ---------------------------------------------------------- #

    def specs_for(self, capability: str) -> Tuple[SourceSpec, ...]:
        """The sources for one capability, in the registry's declared order.

        Empty for a capability nobody registered and for a capability registered as unavailable —
        the two are told apart by :meth:`unavailable_reason`, which is why that is a separate
        method rather than a return value here: a caller that treats "empty list" as "no sources"
        is exactly the caller this package is trying to stop.
        """
        return tuple(self._specs[name] for name in self._capabilities.get(capability, ()))

    def names_for(self, capability: str) -> Tuple[str, ...]:
        """Provider names for one capability, in order, without building the specs."""
        return tuple(self._capabilities.get(capability, ()))

    def capabilities(self) -> Tuple[str, ...]:
        """Every capability with a provider, in first-registration order."""
        return tuple(self._capabilities)

    def unavailable_capabilities(self) -> Tuple[str, ...]:
        """Every capability nobody can serve, with a measured reason."""
        return tuple(self._unavailable)

    def is_registered(self, capability: str) -> bool:
        """Whether anything at all is registered for this capability, refusals included."""
        return capability in self._capabilities or capability in self._unavailable

    def is_solo(self, capability: str) -> bool:
        """Whether exactly one registered source can serve this capability.

        The question a caller cannot otherwise answer: **is this capability redundant at all?**
        India VIX and participant flows each have one source, so a failover for them is not a
        shorter wait, it is the same wait. A report that says ``1 source`` is the difference
        between learning that now and learning it during an outage.
        """
        return len(self._capabilities.get(capability, ())) == 1

    def is_upstream_exclusive(self, capability: str) -> bool:
        """Whether the sole source is also the only publisher anywhere.

        Stronger than :meth:`is_solo`, and rarely true. It means the capability cannot gain
        redundancy by registering a second row either — there is nothing to register.
        """
        names = self._capabilities.get(capability, ())
        return len(names) == 1 and self._specs[names[0]].upstream_only

    def unavailable_reason(self, capability: str) -> Optional[str]:
        """The measured reason this capability has no provider, or ``None`` if it has one."""
        return self._unavailable.get(capability)

    def source(self, name: str) -> Optional[SourceSpec]:
        """One spec by name, for a caller who wants to read a note."""
        return self._specs.get(name)

    def all_specs(self) -> Tuple[SourceSpec, ...]:
        """Every registered source, in registration order."""
        return tuple(self._specs.values())

    def by_module(self) -> Dict[str, Tuple[str, ...]]:
        """Module name -> the sources that import it, for the isolation tests to read.

        Exposed because a caller (and the isolation test that guards this layer) needs to know
        *which* modules a resolution would reach without performing one. Every value is a string;
        nothing here imports anything.
        """
        grouped: Dict[str, List[str]] = {}
        for spec in self._specs.values():
            if spec.module:
                grouped.setdefault(spec.module, []).append(spec.name)
        return {module: tuple(names) for module, names in sorted(grouped.items())}

    # -- refusal text ----------------------------------------------------- #

    def capability_error(self, capability: str) -> str:
        """The message for a capability that is not registered at all.

        Distinguishes "you asked for something nobody registered" from "we registered it as
        impossible", because the two want different things from the reader: the first is a typo,
        the second is a decision.
        """
        reason = self._unavailable.get(capability)
        if reason is not None:
            return (
                f"bfinance has no source for the {capability!r} capability, and that is an "
                f"upstream limit rather than a gap in this layer.\n\n{reason}\n\n"
                f"Upgrading bfinance will not produce it, and retrying will not help. What is "
                f"available:\n{self.capability_report()}"
            )
        return (
            f"the {capability!r} capability is not registered in this layer, which knows about: "
            f"{sorted(self._capabilities)}. That is a registry gap rather than an upstream limit "
            f"— if it is a capability you expected, please report it rather than reaching around "
            f"this layer, because a source used directly gets none of the basis checking that "
            f"exists to stop a silent basis change.\n\n"
            f"What is available:\n{self.capability_report()}"
        )

    def capability_report(self) -> str:
        """Every capability, its sources, its bases, and whether it has any redundancy.

        The artefact a caller reads *before* trusting a capability, and the one that makes
        single-source capabilities visible. Deliberately terse: a report nobody reads is not a
        report.
        """
        lines: List[str] = []
        for capability in self._capabilities:
            specs = self.specs_for(capability)
            bases = sorted({b.value for spec in specs for b in spec.bases})
            if len(specs) == 1:
                redundancy = "1 source, no redundancy"
                if specs[0].upstream_only:
                    redundancy += " (and no other publisher exists upstream)"
            else:
                redundancy = f"{len(specs)} sources"
            lines.append(
                f"  {capability}: {', '.join(s.name for s in specs)} "
                f"[basis: {'/'.join(bases)}] - {redundancy}"
            )
        for capability, reason in self._unavailable.items():
            lines.append(f"  {capability}: unavailable - {' '.join(reason.split())[:200]}")
        return "\n".join(lines) if lines else "(no capability is registered)"

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[SourceSpec]:
        return iter(self._specs.values())

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __repr__(self) -> str:  # pragma: no cover - display only
        return (
            f"<Registry {len(self._specs)} sources, {len(self._capabilities)} capabilities, "
            f"{len(self._unavailable)} refusals>"
        )


# --------------------------------------------------------------------------- #
# the default table
# --------------------------------------------------------------------------- #

#: Capabilities that no reachable source publishes, with the measurement. Registered as refusals
#: rather than omitted: a caller who asks is told the limit is upstream and permanent, instead of
#: getting a ``KeyError`` that reads as a typo in their own code.
#:
#: Each of the four is refused *by the module that owns the data* with a typed error, so this table
#: is recording a decision those modules already made, not inventing one. Where the refusal is
#: enforced upstream, the capability has no provider here and the cascade never runs.
UNAVAILABLE: Tuple[Tuple[str, str], ...] = (
    (
        "tri_index_return",
        "NSE publishes total-return index levels for a small set of indices and not for the "
        "rest, and no reachable source publishes a TRI series for an arbitrary NIFTY constituent. "
        "bfinance.nse.macro raises a typed error rather than approximating a TRI by compounding a "
        "price index with a dividend yield, because that approximation is wrong by construction: it "
        "cannot know the reinvestment date.",
    ),
    (
        "rbi_reference_rate",
        "The RBI reference rate is published as a circular plus a rate-masthead page, neither of "
        "which is a machine-readable series, and bfinance.nse.macro raises a typed error rather "
        "than scraping a rate that a bank would then apply to a trade. It is published in a form "
        "suitable for human reading, and reading it programmatically is how you get a rate that is "
        "a day stale and labelled as today.",
    ),
    (
        "pledge_shares",
        "Shareholding patterns on the bulk-deal and shareholding routes carry no pledge column, "
        "and bfinance.ownership refuses it explicitly. A pledge figure read from a page that does "
        "not publish one is necessarily a fabrication, and a fabricated pledge ratio is a number "
        "that gets quoted.",
    ),
    (
        "fpi_ownership",
        "FPI ownership is not published as a percentage on the routes bfinance.ownership reads; "
        "the page exposes a status flag and no holdings. bfinance.ownership raises "
        "FpiOwnershipUnavailableError with the reason. The alternative — dividing an FII/DII "
        "market-flow figure by market capitalisation to manufacture an ownership percentage — "
        "would be a number in the right units and with no relationship to ownership at all.",
    ),
)


def build_default() -> Registry:
    """The default table. Pure data: this function imports nothing.

    Every row names its module as a **string** and its entry as a string. Nothing here performs
    an import, so :func:`build_default` is callable at any time, and importing
    :mod:`bfinance.sources` costs one dataclass and a dict. That is the whole mechanism behind
    the isolation tests: the table can be read, printed, asserted on and reported on in a process
    where none of the six modules has ever been imported.

    The price rows carry the measurement that matters most, so read the notes before reordering
    them. Reordering ``price_history`` is safe in the sense that a request is only ever routed to
    a row that publishes the requested basis, so a reorder cannot make an as-traded call land on
    an adjusted row -- but it *is* what a report prints, and a report is read as a claim. The
    ``trendlyne-ohlc`` row is deliberately not first for exactly that reason, and its note carries
    the 14-symbol side-by-side that decided it. Changing a row's basis is not safe at all, and the
    reason each is what it is sits next to it.
    """
    registry = Registry()

    # -- statements: one row, the sub-cascade underneath -------------------- #
    registry.register(
        SourceSpec(
            name=STATEMENTS_PROVIDER,
            capabilities=("statements",),
            bases=(Basis.CONSOLIDATED, Basis.STANDALONE),
            note=(
                "bfinance.statements is already a cascade with its own try-order, its own "
                "per-basis routing and four measured refusal cells (quarterly balance sheet, "
                "quarterly cash flow, TTM balance sheet, TTM cash flow). It is registered here as "
                "a single provider so that this layer does not duplicate it. Its per-(kind, "
                "frequency) capability matrix lives in bfinance.statements.registry; this layer "
                "only knows that 'statements' exists and that it can serve either accounting "
                "basis, never both at once for one value."
            ),
            module="bfinance.statements",
            entry="resolve_from_payloads",
            adapter=_BINDINGS["statements"],
        )
    )

    # -- price history: five sources, three different bases ------------------- #
    registry.register(
        SourceSpec(
            name=TRENDLYNE_OHLC_PROVIDER,
            capabilities=("price_history",),
            bases=(Basis.SPLIT_ADJUSTED,),
            note=(
                "Per-symbol daily OHLCV over the second vendor's three robots-ALLOWED routes: the "
                "equity sitemap, the symbol's own page for its per-symbol token, and the "
                "/mapp/v1/ OHLC JSON. It is read by bfinance.sources.price_provider, which carries "
                "its own parser rather than importing the leaf package the isolation rules forbid; "
                "tests/test_sources_price_provider.py pins that parser against the same recorded "
                "payloads the leaf's own tests use, so the duplication cannot rot silently. This "
                "row is NOT one of the six modules this table describes -- it is a reader that "
                "lives in this package, so the isolation probes are unaffected by its existence.\n\n"
                "MEASURED BEFORE RANKED, 2026-09-30, 14 symbols x 2 windows against the bhavcopy "
                "on the common sessions. Window A 2026-09-01..2026-09-25 (18 sessions, past every "
                "event in the set): 11/11 symbols byte-identical, max |dClose| 0.00. Window B "
                "2024-12-16..2025-03-13 (62 sessions, BEFORE the HDFCBANK 1:1 bonus of 2025-08-26 "
                "and the MOTHERSON 1:2 bonus of 2025-07-18, and straddling the ITC demerger of "
                "2025-01-06): 11/14 byte-identical, and 3/14 disagree on 62 of 62 sessions -- "
                "HDFCBANK max|dClose| 932.60, MOTHERSON 56.27, NESTLEIND 1158.55, with the "
                "trendlyne/bhavcopy close ratio constant at 0.500000, 0.666667 and 0.500000 "
                "respectively, i.e. each scaled by its own corporate-action factor. TATAMOTORS did "
                "not resolve at all: it is absent from the 5,925-entry sitemap (reported as a "
                "failure, not averaged in).\n\n"
                "VERDICT: this is NOT the as_traded basis, and Basis.comparable is right to refuse "
                "it. The 11 that match have no event inside the span, so 'matches the bhavcopy' is "
                "what as-traded looks like when no corporate action falls in the window -- a "
                "per-symbol match is not evidence of a shared basis. CONSEQUENCE FOR THIS TABLE: "
                "the row is registered split_adjusted, and BASIS -- not position -- is what routes "
                "a call, so an as_traded request skips it on basis and spends no request on it. "
                "It is nevertheless declared FIRST (operator decision, 2026-10-13): the default "
                "vendor, with the two NSE as-traded rows as the cascade behind it. That ordering is "
                "safe precisely because of the measurement above -- moving the row up cannot make "
                "an as-traded call land on a split-adjusted series, because the basis column "
                "refuses it before the request is built. It also buys the only wired source on the "
                "split_adjusted basis, which was already first for every request it can serve. What "
                "it does not buy is speed on the default path: DEFAULT_PRICE_BASIS is as_traded, "
                "so Ticker.info() still reaches the bhavcopy, which downloads one archive file per "
                "trading day (measured 2026-10-13: 184s for info(), against 3.5s for this row's "
                "single-request path) -- a cost of the as-traded basis, not of this ordering. "
                "One coverage limit is part of the row, not a caveat: ETFs are "
                "absent from the sitemap entirely and the route that resolves them is "
                "robots-disallowed, so that class of instrument is refused rather than answered "
                "thin.\n\n"
                "COLUMNS: the full OHLCV five, read off bfinance.sources.price_provider.COLUMNS "
                "(parse_ohlc_payload returns ``frame[list(COLUMNS)]``) and off the leaf reader this "
                "row deliberately does not import, whose own COLUMNS tuple is the same five. So a "
                "Close-only request and an OHLC request can both reach this row."
            ),
            provides=("Open", "High", "Low", "Close", "Volume"),
            module="bfinance.sources.price_provider",
            entry="price_history",
            adapter=_BINDINGS["price_provider"],
        )
    )
    registry.register(
        SourceSpec(
            name="marketlens-close",
            capabilities=("price_history",),
            bases=(Basis.AS_TRADED,),
            note=(
                "MarketLens publishes a raw close and volume, unadjusted, for one symbol at a "
                "time. Comparable with the bhavcopy, so this row is a genuine second source for an "
                "as-traded request — which makes as-traded the only price basis in this package "
                "with real redundancy. The redundancy is worth naming precisely: it is per-symbol "
                "and whole-market is not, so a request for one symbol has two answers and a "
                "request for a market-wide screen has one.\n\n"
                "COLUMNS: Close and Volume ONLY. This is the row's limitation and it is a "
                "measurement, not a guess: bfinance.nse.marketlens builds its frame as "
                "``{'Close': close, 'Volume': volume}`` and its docstring says twice that the "
                "endpoint has no Open/High/Low. So this row is skipped -- never called -- for an "
                "OHLC request, and it is the reason :attr:`SourceSpec.provides` exists at all: "
                "a close-only frame is a NON-EMPTY answer, so without a declared column set the "
                "cascade would hand one back for a request that needs bars and the caller would "
                "lose its Open/High/Low with nothing raised. What it IS good for is the measured "
                "2.5s against the bhavcopy's 530s over two years, because it is per-symbol and "
                "answers one request; that is the reason a Close-only request wants it, and it is "
                "worth 200x rather than a rounding difference."
            ),
            provides=("Close", "Volume"),
            module="bfinance.nse.marketlens",
            entry="price_history",
            adapter=_BINDINGS["marketlens"],
        )
    )
    registry.register(
        SourceSpec(
            name="nse-bhavcopy",
            capabilities=("price_history",),
            bases=(Basis.AS_TRADED,),
            note=(
                "The NSE bhavcopy is the tape: ClsPric as printed, with no corporate-action "
                "adjustment of any kind. This is the reference basis, and the measurement that "
                "establishes it is that screener.in's own quote agrees with it to the decimal — "
                "269.0 on both for the same symbol and day. Note what this row is NOT: it is a "
                "whole-market daily file read over a date range, with no per-symbol argument. "
                "Narrowing it to one symbol is the caller's job, and a caller who assumes this "
                "source takes a ticker will either pass a key it ignores or conclude the source is "
                "broken.\n\n"
                "COLUMNS: the OHLCV five, read off bfinance.nse.bhavcopy.OHLCV_COLUMNS -- the half "
                "of its output contract that OHLCVEngine looks up BY NAME. Its COLUMNS tuple also "
                "carries TurnoverValue, which is a traded-value figure rather than a price and is "
                "additive here; it is not in this row because no caller of this layer can ask for "
                "it by name. Being the one row that can serve every price column, this is the row "
                "an OHLC request is routed to -- and, on the shipped try-order, the row it is "
                "routed to for a CLOSE-ONLY request too, which is a fact about the ordering rather "
                "than about this row's capabilities.\n\n"
                "THE COST OF THIS ROW, measured 2026-10-13 on TCS, because a reader deciding whether "
                "to ask this layer for a price series needs it before paying for it: "
                "bhavcopy_range iterates one archive file PER TRADING DAY over the window, so one "
                "month is 18.3s and 60,736 rows, two years is 530s and 1,211,192 rows, and the "
                "1-year window Ticker.info used to ask for took 184s. The one as-traded alternative "
                "answers the same window in 2.5s over 1,237 rows. Column eligibility does not change "
                "that -- it decides what MAY answer, never who answers first."
            ),
            provides=("Open", "High", "Low", "Close", "Volume"),
            module="bfinance.nse.bhavcopy",
            entry="bhavcopy_range",
            adapter=_BINDINGS["bhavcopy"],
        )
    )
    registry.register(
        SourceSpec(
            name="screener-chart",
            capabilities=("price_history",),
            bases=(Basis.DIVIDEND_ADJUSTED,),
            note=(
                "screener.in's QUOTE is as-traded and its CHART HISTORY is not: the same vendor, "
                "the same symbol, two different bases, one page. The effect is measured and the "
                "cause is not, and this row says so rather than inventing one. That the quote and "
                "the chart disagree is the whole reason a same-vendor failover is not automatically "
                "basis-preserving: this source is the concrete case where it is not. The entry is "
                "ScreenerClient, not bfinance.screener.charts.ScreenerChartEngine — the latter "
                "builds a query parameter and parses JSON and cannot fetch anything, so a row "
                "pointing at it would look wired and never answer.\n\n"
                "COLUMNS: NONE in this layer's vocabulary, and that is measured rather than "
                "forgotten. bfinance.screener.charts builds its frame from "
                "``datasets[].metric`` -- the column NAMES are whatever the vendor called the "
                "metric, so the price chart's single column is called ``price``, not ``Close`` -- "
                "and it publishes no Open/High/Low either. So ``provides`` is empty here, which by "
                ":meth:`SourceSpec.missing_columns` means the row is NOT column-filtered rather "
                "than being skipped for every column: refusing it for a name it might use is a "
                "guess, and the engine's own required-column check still refuses this row's frame "
                "loudly rather than accepting it, which is the shape the defect takes either way."
            ),
            module="bfinance.screener.client",
            entry="ScreenerClient",
            adapter=_BINDINGS["screener_chart"],
        )
    )
    registry.register(
        SourceSpec(
            name="vendor-b-adjustments",
            capabilities=("price_history",),
            bases=(Basis.SPLIT_ADJUSTED,),
            note=(
                UNWIRED_NOTE
                + "\n\nCOLUMNS: the OHLCV five, declared from the reader this row names -- "
                "bfinance.trendlyne.client.COLUMNS, which is documented there as identical to the "
                "bhavcopy's so a frame drops into the same consumers. It is read rather than "
                "imported, for the two isolation reasons the note above gives; the row itself is "
                "unwired and so is never called, which is why declaring its columns costs nothing "
                "and omitting them would cost a future row its eligibility check."
            ),
            provides=("Open", "High", "Low", "Close", "Volume"),
        )
    )

    # -- macro: two capabilities, one source, and that is the point ----------- #
    registry.register(
        SourceSpec(
            name="nse-macro",
            capabilities=("india_vix", "index_valuation"),
            bases=(Basis.REPORTED,),
            note=(
                "Index levels, P/E, P/B and dividend yield by date, published by NSE. One source "
                "for both capabilities, and there is no second publisher of the VIX series — so a "
                "caller asking for india_vix is asking for something with no redundancy anywhere, "
                "not merely something this layer has not found a second route to. A rate-limited or "
                "down NSE is a total outage for this capability and no failover will help."
            ),
            module="bfinance.nse.macro",
            entry="india_vix",
            adapter=_BINDINGS["macro"],
            upstream_only=True,
        )
    )

    # -- flows: the dated CSV archive and AMFI -------------------------------- #
    registry.register(
        SourceSpec(
            name="nse-participant-archive",
            capabilities=("participant_flows",),
            bases=(Basis.REPORTED,),
            note=(
                "FII/DII/Pro/Client daily net flows, read from NSE's dated CSV archive. Real "
                "history back to 2020 and exactly one source, so this capability is a single point "
                "of failure in exactly the same way india_vix is. A gap in the archive is reported "
                "as a missing date with a reason, not as a zero flow: a zero flow is a claim about "
                "the market and a missing file is a claim about the server."
            ),
            module="bfinance.flows",
            entry="participant_flows_range",
            adapter=_BINDINGS["participant_flows"],
            upstream_only=True,
        )
    )
    registry.register(
        SourceSpec(
            name="amfi-workbook",
            capabilities=("amfi_aum",),
            bases=(Basis.REPORTED,),
            note=(
                "Monthly AMFI workbook. Also one source, also a single point of failure. The "
                "workbook is a spreadsheet, so the month it covers is a property of the file's "
                "contents rather than its name, and coverage is computed from the data."
            ),
            module="bfinance.flows",
            entry="amfi_monthly_report",
            adapter=_BINDINGS["amfi"],
            upstream_only=True,
        )
    )

    # -- universe: the live pages and the local recorder ------------------------ #
    registry.register(
        SourceSpec(
            name="nse-index-pages",
            capabilities=("index_membership",),
            bases=(Basis.REPORTED,),
            note=(
                "Index membership read from NSE's own constituent pages. This row is a PARSER, "
                "not a fetcher: parse_constituents takes the page body as text, and the fetching "
                "is the caller's job. A caller who expected a client here will not find one, which "
                "is why the row says so rather than implying otherwise. One source, so a "
                "membership question has no failover. The trap it has instead is compositional: "
                "an index's membership is a function of its rebalance calendar, so 'membership on "
                "date X' is answerable only where X is a date somebody has actually observed, and "
                "answered as-of the nearest observation otherwise - never interpolated."
            ),
            module="bfinance.universe",
            entry="parse_constituents",
            adapter=_BINDINGS["constituents"],
            upstream_only=True,
        )
    )
    registry.register(
        SourceSpec(
            name="universe-recorder",
            capabilities=("membership_history",),
            bases=(Basis.REPORTED,),
            note=(
                "The local recorder's run-length-encoded snapshot archive. This is a *local* "
                "source and it is a real one: it is the only capability here that can answer "
                "'what did this index contain on that date' for a date nobody published, because it "
                "is the only source that remembers. It is registered separately from the live "
                "pages so that a membership-history request does not fall through to a page that "
                "can only describe the present tense."
            ),
            module="bfinance.universe",
            entry="UniverseArchive",
            adapter=_BINDINGS["archive"],
        )
    )

    # -- the two modules still landing ----------------------------------------- #
    registry.register(
        SourceSpec(
            name=COSTS_PROVIDER,
            capabilities=("transaction_costs",),
            bases=(Basis.STATUTORY,),
            note=(
                "The date-keyed Indian statutory cost schedule: brokerage slabs, STT, exchange "
                "charge, SEBI turnover, stamp duty, GST, IPFT, with each rate keyed to the date it "
                "took effect, and a measured 22.248 bps round trip. STATUTORY is its own basis "
                "rather than REPORTED because a rate schedule has an *effective date* that the "
                "value is meaningless without: quoting today's stamp duty for a trade in 2019 is a "
                "number that looks right. A request on any other basis skips this row rather than "
                "being handed a rate with no effective date."
            ),
            module="bfinance.costs",
            entry="quote",
            adapter=_BINDINGS["costs"],
            upstream_only=True,
        )
    )
    registry.register(
        SourceSpec(
            name=OWNERSHIP_PROVIDER,
            capabilities=(
                "shareholding_pattern",
                "bulk_deals",
                "block_deals",
                "short_deals",
            ),
            bases=(Basis.REPORTED,),
            note=(
                "Shareholding patterns and the bulk/short/block deal tapes. Two shapes on one row "
                "because they come from one module with one failure mode: when the deal route "
                "changes shape, the shareholding route usually still works, and splitting them into "
                "two rows would let a caller get a clean 'unavailable' for the half that is broken "
                "instead of the accurate 'the whole source is degraded'. Reported is the basis "
                "because a shareholding percentage is published as it stands and is restated "
                "retroactively by the vendor when it corrects a prior quarter — which is a fact "
                "about the series that belongs in as_of, not silently smoothed here."
            ),
            module="bfinance.ownership",
            entry="OwnershipClient",
            adapter=_BINDINGS["ownership"],
            upstream_only=True,
        )
    )

    for capability, reason in UNAVAILABLE:
        registry.declare_unavailable(capability, reason)
    return registry


#: The process-wide default. Built at import time, from strings.
DEFAULT: Registry = build_default()


def default_registry() -> Registry:
    """:data:`DEFAULT`, for callers who prefer a function to a global.

    Returns the shared instance, not a copy. A caller who wants their own try-order calls
    :meth:`Registry.copy` — handing out a fresh copy by default would mean each caller's edits
    vanished, which is a worse surprise than a documented shared object.
    """
    return DEFAULT
