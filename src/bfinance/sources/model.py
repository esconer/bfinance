"""
The vocabulary of the seam: what a basis **is**, and why two of them cannot be put in one series.

The trap this module exists for
-------------------------------
Six independent scrapers, and the four price sources among them do not agree on what a number
means. This is not noise; it is the difference between a real move and an accounting artefact:

=========================  =====================  ==========================================
source                     basis                   why it matters
=========================  =====================  ==========================================
NSE bhavcopy               as-traded               the tape. No adjustment of any kind.
screener.in quote          as-traded               verified: 269.0 == bhavcopy ``ClsPric`` 269.0
screener.in chart history  **dividend-adjusted**   same vendor, different basis, same page
MarketLens                 raw close + volume      as-traded, no adjustment
second vendor's quotes      as-traded **+ split/bonus adjusted**
=========================  =====================  ==========================================

A naive failover walks down that list, takes the first vendor that answers, and concatenates its
history onto whatever the previous vendor supplied. When the failover lands on a different basis,
the seam is **invisible**: both series are "price", the join is clean on the index, and the
result contains a move that never happened. A 1:2 split reads as -50.00%. That is the bug this
layer is built to make impossible, and the reason :class:`Basis` is a closed vocabulary rather
than a free-text string is that a free-text basis is one typo away from saying the same thing
twice.

Two axes, and they are not interchangeable
------------------------------------------
:class:`BasisAxis` keeps the two independent meanings apart, because collapsing them is the
second-order version of the same mistake:

* :attr:`BasisAxis.PRICE` — corporate-action adjustment. ``as_traded`` vs ``split_adjusted`` vs
  ``dividend_adjusted``. Getting this wrong fabricates a price move.
* :attr:`BasisAxis.ACCOUNTING` — consolidated (group, with subsidiaries) vs standalone (the
  company alone). Getting this wrong does not move a price; it puts the wrong shareholders'
  funds in a balance sheet. The measured size of that error: MOTHERSON at Mar 2026, the same row
  and the same period, prints ``38290.1`` standalone on the vendor's own rendered page and
  ``40979.6`` consolidated in the XHR the page is populated from. Same company, same period,
  **2,689.5 different** — and the vendor's own two representations of it disagree.

:class:`Basis.comparable` is the whole policy, and it is one rule: **only an identical basis is
comparable.** Not "close enough", not "both are prices". An as-traded series and a split-adjusted
series are not comparable, a consolidated figure and a standalone figure are not comparable, and
two things on different axes are not comparable at all. Every refusal in this package comes from
this one function, so there is no per-call-site judgement to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "Basis",
    "BasisAxis",
    "BASIS_AXIS",
    "Attempt",
    "Resolved",
    "served",
    "HEALTH_ATTR",
    "ATTEMPTS_ATTR",
    "ATTEMPT_OUTCOMES",
    "CALLED_OUTCOMES",
    "PROVENANCE",
    "SERVED",
    "FAILED",
    "SKIPPED_BASIS",
    "SKIPPED_HEALTH",
    "SKIPPED_COLUMNS",
    "is_series",
    "basis_from",
    "cross_basis_note",
    "MOTHERSON_STANDALONE",
    "MOTHERSON_CONSOLIDATED",
]

#: MOTHERSON, Mar 2026, ``TotalShareHoldersFunds_A``. The same row, on the same period, from the
#: same vendor's two representations of it. Spelled out as module constants because the whole
#: package refuses to reconcile them, and a refusal nobody can check the arithmetic of is a
#: refusal nobody will believe.
MOTHERSON_STANDALONE = 38290.1
MOTHERSON_CONSOLIDATED = 40979.6


class BasisAxis(str, Enum):
    """Which kind of "same number" a basis is asserting.

    Attributes:
        PRICE: Corporate-action adjustment. Compare like with like or not at all.
        ACCOUNTING: Group vs company-alone. A different axis, same hazard.
    """

    PRICE = "price"
    ACCOUNTING = "accounting"
    #: Not a price and not an accounting-basis pair — a flow, a holding percentage, an index
    #: level, a statutory rate. Published as-is, with no adjustment axis at all.
    REPORTED = "reported"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Basis(str, Enum):
    """Every basis any of the six modules can be on, as a closed vocabulary.

    Deliberately an :class:`~enum.Enum` and not a ``str`` field. A basis is the one thing in this
    layer that must never be spelled two ways, because the entire safety argument is
    :meth:`comparable` doing an equality test — and ``"split_adjusted"`` and
    ``"split-adjusted"`` do not compare equal, so a typo here would be a silently disabled
    mismatch check rather than a visible error. :class:`Basis` inherits ``str`` so the values
    still travel into a frame's ``attrs`` and a CSV without conversion.

    An unknown basis is refused by :func:`basis_from` rather than accepted as a string, so a
    caller who invents a basis gets told, at the call, that this layer will not compare against
    it.
    """

    #: No corporate-action adjustment. What printed on the tape.
    AS_TRADED = "as_traded"
    #: Restated for splits and bonus issues across the window.
    SPLIT_ADJUSTED = "split_adjusted"
    #: Restated for dividends. Distinct from :attr:`SPLIT_ADJUSTED`: a dividend adjustment moves
    #: every historical price and a split adjustment does not move the last one, so these are two
    #: different numbers and are not interchangeable.
    DIVIDEND_ADJUSTED = "dividend_adjusted"
    #: Group accounts, subsidiaries included.
    CONSOLIDATED = "consolidated"
    #: The company alone.
    STANDALONE = "standalone"
    #: A published figure that is not a price and has **no adjustment axis** — a flow in crore, a
    #: shareholding percentage, an index level. Exists so a non-price capability still carries a
    #: basis rather than ``None``, which is what makes "every resolved value carries a basis"
    #: true without an exception clause.
    REPORTED = "reported"
    #: A rate schedule published by a regulator and keyed to the date it took effect. Its own
    #: member rather than :attr:`REPORTED` because a statutory rate is the one published figure
    #: whose meaning is **date-dependent**: today's stamp duty applied to a 2019 trade is a number
    #: with the right unit and no relationship to what was charged. Keeping it distinct means a
    #: request that cannot be given a date is refused by the basis check rather than answered with
    #: a rate that has none.
    STATUTORY = "statutory"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def axis(self) -> BasisAxis:
        """Which of the two independent axes this basis belongs to."""
        return BASIS_AXIS[self]

    def comparable(self, other: "Basis") -> bool:
        """Whether a value on this basis and one on ``other`` may sit in the same series.

        One rule, and it is the conservative one: **only an identical basis is comparable.**
        "Close enough" is not a property these six vendors share, and the cost of guessing wrong
        is a fabricated price move, so the check refuses anything it cannot prove.

        Two cross-axis cases are spelled out rather than folded into a generic ``False``,
        because they fail for measurably different reasons and a reader debugging a series needs
        to know which one they hit:

        * ``as_traded`` vs any adjusted basis — a split inserts a move that never happened.
        * ``consolidated`` vs ``standalone`` — 38,290.1 vs 40,979.6, same row, same period.
        """
        other = basis_from(other)
        if self is other:
            return True
        if self.axis is not other.axis:
            return False
        if BasisAxis.PRICE in (self.axis, other.axis):
            # Within the price axis the only safe pairing is identical, which returned above. Any
            # other pairing means an adjustment is being compared against an unadjusted series.
            return False
        return False

    def mismatch_note(self, other: "Basis") -> str:
        """Why a cross-basis join is refused, in one sentence, with the arithmetic attached.

        Returned rather than raised so the caller can put the reason in a frame's ``attrs`` next
        to the numbers, which is where a note is useful. The refusal itself happens at
        :meth:`bfinance.sources.series.Series.frame`.
        """
        other = basis_from(other)
        return cross_basis_note(self, other)


#: The axis table. Kept as data rather than as a property per member so that a new basis is a
#: *data* change and every consumer that needs to learn about it is found rather than missed.
#:
#: :attr:`Basis.REPORTED` is on :attr:`BasisAxis.REPORTED` and **not** folded into
#: :attr:`BasisAxis.PRICE`, on purpose. A flow in crore is not an unadjusted price, and calling it
#: one would make ``reported.comparable(as_traded)`` a question with a plausible-looking answer
#: instead of an obvious mistake.
BASIS_AXIS: Dict[Basis, BasisAxis] = {
    Basis.AS_TRADED: BasisAxis.PRICE,
    Basis.SPLIT_ADJUSTED: BasisAxis.PRICE,
    Basis.DIVIDEND_ADJUSTED: BasisAxis.PRICE,
    Basis.CONSOLIDATED: BasisAxis.ACCOUNTING,
    Basis.STANDALONE: BasisAxis.ACCOUNTING,
    Basis.REPORTED: BasisAxis.REPORTED,
    Basis.STATUTORY: BasisAxis.REPORTED,
}


def basis_from(value: Any) -> Basis:
    """Coerce a basis, refusing anything not in the vocabulary.

    Accepts a :class:`Basis` or its string value. Raises :class:`ValueError` for anything else
    rather than passing it through as a string, because the one thing worse than an unknown basis
    is an unknown basis that compares unequal to itself.
    """
    if isinstance(value, Basis):
        return value
    if isinstance(value, str):
        try:
            return Basis(value)
        except ValueError:
            raise ValueError(
                f"{value!r} is not a basis this layer knows. Known bases: "
                f"{sorted(b.value for b in Basis)}. A basis outside this vocabulary cannot be "
                f"checked for comparability, and an unchecked basis is the failure this package "
                f"exists to prevent."
            ) from None
    raise ValueError(f"expected a Basis or its string value, got {type(value).__name__}: {value!r}")


def cross_basis_note(one: Basis, other: Basis) -> str:
    """The refusal sentence for a cross-basis join, with the measured arithmetic.

    Factored out of :meth:`Basis.mismatch_note` so that the accounting case quotes real figures
    rather than asserting a hazard in the abstract. A refusal that cites no numbers is a refusal
    the reader has to take on trust, and this one exists precisely because two numbers that both
    look right are not both right.

    Returns ``""`` for a **comparable** pair, and that short-circuit is the whole contract of this
    function: it describes a refusal, so a pair that is not refused has no note. Without it, asking
    for the note on two identical bases produced a sentence claiming an as-traded series is not
    comparable to an as-traded series — which is how a "just explain the difference" convenience
    turns into something that lies.
    """
    one, other = basis_from(one), basis_from(other)
    if one.comparable(other):
        return ""
    if BasisAxis.ACCOUNTING not in (one.axis, other.axis):
        return (
            f"a {one.value} series and a {other.value} series are not comparable and must not be "
            f"concatenated. One is as-traded and the other is adjusted for corporate actions, so "
            f"their levels are not on a common scale: a 1:2 split appears in the as-traded series "
            f"as a -50.00% move that never happened, while the adjusted series restates every "
            f"point before it. Neither level is wrong and the pair is not a series."
        )
    delta = MOTHERSON_CONSOLIDATED - MOTHERSON_STANDALONE
    return (
        f"a {one.value} series and a {other.value} series are not comparable and must not be "
        f"concatenated, nor averaged, nor relabelled. The second vendor publishes both bases for "
        f"the same row and the same period, which is the measurement: MOTHERSON shareholders' "
        f"funds at Mar 2026 is {MOTHERSON_STANDALONE:,.1f} standalone and "
        f"{MOTHERSON_CONSOLIDATED:,.1f} consolidated — a difference of {delta:,.1f} for one company "
        f"on one date, from the same vendor, in the same response. Reconciling them would require "
        f"choosing one, and that choice is the caller's to make with the filings in hand, not this "
        f"layer's to make silently."
    )


# --------------------------------------------------------------------------- #
# attempt outcomes
# --------------------------------------------------------------------------- #

#: A provider returned a value and it is the one being returned to the caller.
SERVED = "served"
#: A provider was asked and did not answer: it raised, or it returned nothing usable.
FAILED = "failed"
#: A provider was **never asked** because it cannot serve the requested basis. Recorded so the
#: caller can see what was passed over, which is the information that makes a "why is my data from
#: that vendor" question answerable without reading the registry.
SKIPPED_BASIS = "skipped_basis"
#: A provider was never asked because its recorded health says it is currently failing. Also a
#: skip rather than a failure: the ledger is a *prior*, not a verdict, and the next call may well
#: ask it.
SKIPPED_HEALTH = "skipped_health"
#: A provider was never asked because it does not publish every column the request named. Also a
#: skip rather than a failure, and the reason is a property of the source rather than of today.
#: Measured: MarketLens publishes ``Close`` and ``Volume`` and has **no** Open/High/Low (its module
#: docstring says so and the parser builds exactly those two columns), so it cannot serve an OHLC
#: request — and :func:`~bfinance.sources.cascade.is_empty_payload` calls its close-only frame a
#: non-empty answer, which means a request that needs bars would otherwise be satisfied by a frame
#: that has none of them. Recorded separately from :data:`SKIPPED_BASIS` so "why is my data from
#: that vendor" has two distinguishable answers rather than one vague one.
SKIPPED_COLUMNS = "skipped_columns"

#: Every outcome a :class:`Attempt` may carry. Asserted to be exactly this set by
#: ``tests/test_sources_cascade.py``, so a new outcome cannot be added without deciding whether it
#: is a call or a non-call.
ATTEMPT_OUTCOMES: Tuple[str, ...] = (
    SERVED,
    FAILED,
    SKIPPED_BASIS,
    SKIPPED_HEALTH,
    SKIPPED_COLUMNS,
)

#: Outcomes in which the provider was actually invoked. Everything else is a non-call, and the
#: difference is load-bearing: "never called" is a stronger claim than "called and rejected", and
#: only the first one means a source with a costly request was spared the request.
CALLED_OUTCOMES: Tuple[str, ...] = (SERVED, FAILED)


@dataclass(frozen=True)
class Attempt:
    """What happened to one provider, in one resolution.

    Attributes:
        provider: The :class:`~bfinance.sources.registry.SourceSpec` name.
        outcome: One of :data:`ATTEMPT_OUTCOMES`.
        reason: One line. For :data:`FAILED`, the exception's class and message. For a skip, why
            the provider was passed over without being called. Empty on :data:`SERVED`, because a
            success needs no explanation and a spurious one reads as a hedge.
        basis: The basis this provider could have served, whether or not it was asked. Carried so
            a caller reading only :attr:`attempts` can reconstruct *why* the order looked the way
            it did.

    The reason is truncated to one line on purpose. It travels into logs and into ``attrs``, and a
    traceback in a frame is a wall of noise in both.
    """

    provider: str
    outcome: str
    reason: str = ""
    basis: Optional[Basis] = None

    @property
    def called(self) -> bool:
        """Whether the provider was actually invoked, as opposed to passed over.

        ``True`` for exactly the outcomes in :data:`CALLED_OUTCOMES`. Tests assert on this to
        prove a basis-ineligible source was *never called* rather than called and rejected — the
        distinction that keeps a rate-limited or slow vendor from paying a request it cannot
        honour.
        """
        return self.outcome in CALLED_OUTCOMES

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.provider}: {self.outcome}" + (f" ({self.reason})" if self.reason else "")


# --------------------------------------------------------------------------- #
# Resolved
# --------------------------------------------------------------------------- #

#: Provenance tag, following the convention in :mod:`bfinance.statements.model` and
#: :mod:`bfinance.nse.macro`.
PROVENANCE = "bfinance.sources"

#: Key holding the tuple of :class:`Attempt` on a :class:`Resolved` in its ``attrs`` rendering.
ATTEMPTS_ATTR = "bfinance_sources_attempts"
#: Key holding the per-source health summary. Mirrored from
#: :data:`~bfinance.sources.health.HEALTH_ATTR` rather than redefined, so a caller reading either
#: one gets the same cell. The literal is repeated because importing ``health`` from ``model`` would
#: be a circular import: ``health`` builds the ordering that ``cascade`` applies, and ``cascade``
#: returns the counters that ``health`` records.
HEALTH_ATTR = "bfinance_sources_health"


def is_series(value: Any) -> bool:
    """Whether a value is a multi-point series, and so needs period alignment before comparison.

    A distinction the cascade genuinely has to make. A **scalar** is a number on a basis, and the
    only question is which basis. A **series** is a set of numbers each on some basis, and the
    question is whether the basis is constant across the points — which is a stronger claim, and
    the one a naive concatenation of two vendors' histories silently fails.

    Duck-typed on pandas and on a plain ``dict``, deliberately: a source that returns a
    :class:`~pandas.DataFrame` and one that returns ``{period: value}`` are both series, and
    making either of them declare itself is asking the wrong module to know about this one.
    """
    if hasattr(value, "index") and hasattr(value, "columns") and hasattr(value, "shape"):
        return True
    return isinstance(value, Mapping)


@dataclass(frozen=True)
class Resolved:
    """One value, from one provider, on one basis — and every measurement that shaped it.

    Deliberately a **single-provider** object. There is no code path in this package that merges
    two providers into one :class:`Resolved`, and that is not an oversight to be fixed later: a
    cascade that blends is a cascade that will one day blend two bases and produce a number that
    looks like a price. When a caller wants a series across sources, they build a
    :class:`~bfinance.sources.series.Series`, which is a *container* of single-provider segments
    and knows how to refuse to collapse them.

    Attributes:
        value: Whatever the provider returned. Passed through untouched — this layer normalises
            *provenance*, not payloads, and a caller that wants the frame gets the frame.
        source: Name of the provider that served it.
        capability: What was asked for.
        basis: **The basis the value is actually on.** This is the load-bearing field. It is read
            off the provider, never copied from the request, because copying it from the request
            is the lie the whole package exists to prevent.
        basis_requested: What the caller asked for. Equal to :attr:`basis` in the healthy case.
        basis_mismatch: ``basis is not basis_requested``. The single boolean a caller needs in
            order to know the seam moved underneath them.
        degraded: Whether the caller got first choice. ``True`` if a provider ahead of the one
            that served was **asked and failed** — not if it was skipped for being unable to
            serve the basis, which is the system working. Also ``True`` on a basis mismatch. This
            distinction is the difference between "we fell back and you should know" and "we
            did the only thing available", and conflating them trains callers to ignore the flag.
        attempts: Every provider considered, in the order considered, whether called or not.
        as_of: The provider's own currency stamp — a date, a period, whatever the source means by
            it. ``None`` only when the source publishes none, which is recorded rather than
            invented.
        attrs: Provenance for the value, ready to be merged into a frame's ``attrs``. Rendered by
            :meth:`render_attrs`; the field is public so a provider can carry extras.
    """

    value: Any
    source: str
    capability: str
    basis: Basis
    basis_requested: Basis
    basis_mismatch: bool
    degraded: bool
    attempts: Tuple[Attempt, ...]
    as_of: Optional[Any] = None
    attrs: Mapping[str, Any] = field(default_factory=dict)
    #: Health snapshot for the sources consulted, so a caller can see the state of the sources it
    #: did not use as well as the one it did.
    health: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Both are checked here rather than trusted from the cascade, so a :class:`Resolved` built
        # by hand — in a test, or by a caller — cannot carry a mismatch flag that disagrees with
        # the two basis fields. The flag is the one thing downstream code branches on, so it has
        # to be derivable rather than asserted.
        if self.basis_mismatch != (self.basis is not self.basis_requested):
            raise ValueError(
                f"basis_mismatch={self.basis_mismatch} disagrees with the basis fields "
                f"(basis={self.basis.value}, basis_requested={self.basis_requested.value}); the "
                f"flag is computed from them so it cannot be set independently."
            )

    @property
    def failed_providers(self) -> Tuple[str, ...]:
        """Names of the providers that were asked and did not answer, in order."""
        return tuple(a.provider for a in self.attempts if a.outcome == FAILED)

    @property
    def skipped_providers(self) -> Tuple[str, ...]:
        """Names of the providers that were never asked, with the reasons in :attr:`attempts`."""
        return tuple(a.provider for a in self.attempts if not a.called)

    def render_attrs(self) -> Dict[str, Any]:
        """This result as ``attrs``, for a caller that wants to hand provenance onward.

        The keys are the same vocabulary :mod:`bfinance.statements` already writes — same names,
        same values — rather than a parallel ``bfinance_sources_basis_*`` set. A caller who
        already knows how to read ``basis_mismatch`` off a statement frame can read it off this
        without learning anything, and one vocabulary cannot drift from the other because there
        is only one.
        """
        return {
            "bfinance_provenance": PROVENANCE,
            "bfinance_statements_provider": self.source,
            "bfinance_statements_basis": self.basis.value,
            "bfinance_statements_basis_requested": self.basis_requested.value,
            "bfinance_statements_basis_mismatch": self.basis_mismatch,
            "bfinance_statements_basis_note": self.basis_note,
            "bfinance_statements_fallback": self.degraded,
            "bfinance_statements_providers_tried": [a.provider for a in self.attempts if a.called],
            "bfinance_statements_skip_reasons": {
                a.provider: a.reason for a in self.attempts if not a.called
            },
            ATTEMPTS_ATTR: tuple(self.attempts),
            "bfinance_sources_capability": self.capability,
            "bfinance_sources_as_of": self.as_of,
            "bfinance_sources_degraded": self.degraded,
            HEALTH_ATTR: dict(self.health),
        }

    @property
    def basis_note(self) -> Optional[str]:
        """Why the basis moved, or ``None`` when it did not.

        Present exactly when :attr:`basis_mismatch` is, and absent otherwise — never an empty
        string, because a note that is present and says nothing is noise in a log and a blank
        cell in an export.
        """
        if not self.basis_mismatch:
            return None
        return cross_basis_note(self.basis_requested, self.basis)


def served(
    value: Any,
    basis: Any,
    *,
    source: str = "",
    capability: str = "",
    as_of: Optional[Any] = None,
    attrs: Optional[Mapping[str, Any]] = None,
) -> Resolved:
    """Wrap a value with **the basis the source actually served it on**.

    This is how a provider tells the cascade that the payload does not match the basis its
    registry row promised, and it is the only way a :attr:`Resolved.basis_mismatch` can arise on a
    source that passes its eligibility check. It exists because of a specific measured case: the
    second vendor's rendered page prints **standalone** while the XHR behind that page reports
    ``is_consolidated: true``, so for one fetch of one symbol the same route can land on either
    representation and no static row can predict which.

    Note the direction of the trust. A registry row is the *expected* basis and it is what routes;
    a payload's declaration is the *actual* basis and it is what sets the flag. When they disagree
    the disagreement is the finding, and the value is returned **flagged rather than refused** —
    a caller who asked for consolidated data and got standalone data should see it with a loud
    warning, not lose it, because dropping it would be a second, quieter lie.

    Args:
        value: Whatever the provider produced.
        basis: The basis it is on, and therefore different from what the row declared.
        source / capability / as_of: Provenance, carried through to the result.
        attrs: Extra provenance to merge in.
    """
    return Resolved(
        value=value,
        source=source,
        capability=capability,
        basis=basis_from(basis),
        basis_requested=basis_from(basis),
        basis_mismatch=False,
        degraded=False,
        attempts=(),
        as_of=as_of,
        attrs=dict(attrs or {}),
    )
