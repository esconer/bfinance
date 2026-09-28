"""
The seam made structural: a series that spans two bases is a container, never a number.

What this module is for
-----------------------
The failover bug this whole package exists to prevent is not a wrong number. It is a **right-looking
number in the middle of a wrong series**: a naive cascade concatenates vendor A's history onto
vendor B's when B is the one that answered, and because both are "price" the join is clean on the
index and nobody looks again. A 1:2 split arrives as a -50.00% move.

So the cross-source hop is represented as an explicit, inspectable object, and the operation a
caller actually wants — *give me the series as one thing* — is the one that raises. That inversion
is the design: :class:`Series` can hold a seam, and every method that would dissolve it refuses.

What it will not do
-------------------
There is no ``merge``, no ``blend``, no ``average``, and no ``to_frame`` on a mismatched series.
Not "deprecated", not "discouraged" — absent. A caller who wants one number from two bases has to
build it, in their own code, where the decision is visible and a reviewer can see it. Adding a
convenience here would move the decision somewhere nobody reads, and the whole point is that
this decision is never automatic.

The refusal, with the arithmetic in it
--------------------------------------
:meth:`Series.frame` raises :class:`~bfinance.sources.errors.BasisMismatchError` quoting both
sides of the seam. The concrete pair is not hypothetical: the second vendor publishes **both**
accounting bases in one response, and its own rendered page prints
:data:`~bfinance.sources.model.MOTHERSON_STANDALONE` while the XHR behind that page reports
``is_consolidated: true`` and says :data:`~bfinance.sources.model.MOTHERSON_CONSOLIDATED` for the
same row and the same period. One company, one date, one vendor, 2,689.5 apart.

A refusal that cites no numbers is a refusal the reader has to take on trust, and this one exists
because both numbers look right. So the error message carries the two figures, their difference,
and the reason no average of them is a number that means anything.

What *is* allowed
-----------------
A series on one basis, from any number of sources, reduces normally. Redundancy is the point of a
cascade, and a series stitched from two sources that agree on basis is perfectly comparable — it
just needs the provenance of each segment kept, which it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import BasisMismatchError
from .model import Basis, cross_basis_note

__all__ = ["Segment", "BasisHop", "Series", "segment_of"]


@dataclass(frozen=True)
class Segment:
    """A run of periods from **one** source on **one** basis.

    The single-provider restriction is not a limitation to work around; it is the invariant. A
    segment that merged two sources could not say which basis each point is on, and a
    segment that merged two bases could not say anything true at all.

    Attributes:
        source: Provider name.
        basis: The basis every point in this segment is on. A segment is homogeneous **by
            construction** — the constructor refuses a ``values`` mapping whose keys disagree about
            nothing, and the check that matters is upstream: whoever built this segment knew the
            source's basis from its registry row.
        values: Period -> value. Period keys are compared as strings, because the four modules
            disagree about period labels (that is the other seam, and
            :mod:`bfinance.statements.periods` is where it is canonicalised) and a container that
            demanded a ``date`` type would push that decision onto a caller at the worst moment.
        as_of: The source's currency stamp, carried per segment because two segments from two
            sources are as-of two different moments and a caller needs to know that.
    """

    source: str
    basis: Basis
    values: Mapping[str, Any]
    as_of: Optional[Any] = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a Segment needs a source name; unattributed values are unreviewable")
        if not self.values:
            raise ValueError(
                f"segment {self.source!r} has no values. An empty segment is unavailability "
                f"wearing a segment's clothes: the cascade raises for that, and a segment that "
                f"quietly accepted an empty mapping would turn a raise into a zero-length series "
                f"that looks like a real answer."
            )
        if not isinstance(self.basis, Basis):
            raise TypeError(
                f"Segment.basis must be a Basis, got {type(self.basis).__name__}. A basis outside "
                f"the vocabulary cannot be compared, and an uncomparable basis in a segment is how "
                f"a seam becomes invisible."
            )

    def get(self, period: str) -> Any:
        """The value for one period, or ``None`` if this segment does not cover it."""
        return self.values.get(period)


def segment_of(
    resolved: Any,
    values: Optional[Mapping[str, Any]] = None,
    *,
    periods: Optional[Sequence[str]] = None,
) -> Segment:
    """Build one :class:`Segment` from a resolved value, refusing anything that is not a series.

    The bridge between the two halves of this package. :func:`~bfinance.sources.cascade.resolve`
    hands back a value from **one** source on **one** basis, and this is how a caller collects
    several of those into a :class:`Series` and gets the seam check for free — which is the point,
    because the alternative is hand-writing segments, and a hand-written segment is where a caller
    quietly picks the wrong ``basis`` and the check never fires.

    The refusal is on :func:`~bfinance.sources.model.is_series`, deliberately. A scalar — one price,
    one shareholding percentage — has no period axis, and a segment built from one would carry a
    single-element ``values`` mapping that compares equal to nothing and hides a seam rather than
    showing it. A scalar belongs in a :class:`~bfinance.sources.model.Resolved`, which is what it
    already is.

    Args:
        resolved: A :class:`~bfinance.sources.model.Resolved`, or a raw value together with the
            basis and source to attribute it to. A raw value is accepted because a caller reading
            from their own cache has a basis and a source and no cascade result.
        values: The period -> value mapping, when it cannot be read off ``resolved`` itself.
        periods: Explicit period labels, for a frame whose columns are periods rather than whose
            index is. Overrides whatever the value's index says.

    Raises:
        ValueError: ``resolved`` carries a scalar and no explicit ``values`` were supplied, or the
            value has no readable period axis at all.
    """
    from .model import is_series

    basis = getattr(resolved, "basis", None)
    source = getattr(resolved, "source", None)
    as_of = getattr(resolved, "as_of", None)
    value = getattr(resolved, "value", resolved)

    if values is None:
        if not is_series(value):
            raise ValueError(
                f"cannot build a segment from a scalar ({value!r}). A Segment holds a run of "
                f"periods from one source on one basis; a single number is a Resolved, which "
                f"already carries its basis. If this really is a series, pass its period->value "
                f"mapping as `values`."
            )
        values = _read_periods(value, periods)
    if basis is None:
        raise ValueError(
            "cannot build a segment without a basis. That is the one field this package exists to "
            "keep track of, and inferring it here would be the exact substitution the seam check "
            "is meant to catch."
        )
    return Segment(
        source=source or "unknown",
        basis=basis,
        values=dict(values),
        as_of=as_of,
    )


def _read_periods(value: Any, periods: Optional[Sequence[str]]) -> Dict[str, Any]:
    """The period -> value mapping of a frame, from its columns or its index.

    Both shapes because both are real: a screener chart frame is period-major (columns are dates),
    and a bhavcopy frame is date-major (the index is dates). A caller who has one of the other
    passes ``periods`` explicitly.
    """
    if periods is not None:
        missing = [p for p in periods if p not in value]
        if missing:
            raise ValueError(
                f"the named period(s) {missing} are not in the value's columns; refusing to build "
                f"a segment with a hole in it, because a missing period is a gap in the data and "
                f"filling it would invent a point on the series"
            )
        return {str(period): value[period] for period in periods}
    if hasattr(value, "columns") and hasattr(value, "index"):
        if len(value.columns) == 1 and len(value.index) > 1:
            column = value.columns[0]
            return {str(index): row[column] for index, row in value.iterrows()}
        if len(value.index) == 1 and len(value.columns) > 1:
            row = value.iloc[0]
            return {str(column): row[column] for column in value.columns}
    index = getattr(value, "index", None)
    if index is None or not hasattr(value, "loc"):
        raise ValueError(
            f"cannot read periods from a {type(value).__name__}: it has no index and no columns. "
            f"Pass `periods=` if it is a mapping, or `values=` with the period->value mapping."
        )
    return {str(index_point): value.loc[index_point] for index_point in index}


@dataclass(frozen=True)
class BasisHop:
    """One change of basis between two adjacent segments, with both numbers attached.

    Attributes:
        period: The period at which the seam sits — the last period the earlier segment covers and
            the first the later one does.
        before / after: The two bases.
        before_source / after_source: Who published each side, because "the basis changed" is
            actionable and "the vendor changed" is what tells you whether to expect it again.
        before_value / after_value: The two figures either side of the seam, when the segment
            overlap is non-empty. These exist so a reviewer can see the size of the jump without
            loading anything.
    """

    period: str
    before: Basis
    after: Basis
    before_source: str
    after_source: str
    before_value: Any = None
    after_value: Any = None

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe, for an ``attrs`` cell or a log line."""
        return {
            "period": self.period,
            "before": self.before.value,
            "after": self.after.value,
            "before_source": self.before_source,
            "after_source": self.after_source,
            "before_value": self.before_value,
            "after_value": self.after_value,
        }


@dataclass(frozen=True)
class Series:
    """Several single-provider segments, in period order, and the seams between them.

    Build it by handing it segments; it sorts them by first period. Sorting here rather than
    trusting the caller's order is deliberate: the segments arrive from a cascade that cares about
    *provider* order, which is not period order, and a seam found in the wrong place is a seam
    nobody sees.

    Attributes:
        segments: In period order, guaranteed non-empty.
        hops: Every basis change between adjacent segments, in order. Empty for a series on one
            basis.
        basis_mismatch: ``bool(hops)``. Kept as a field rather than only a property so that a
            caller writing it into a frame's ``attrs`` reads one attribute and cannot be
            surprised by a recomputed value.
    """

    segments: Tuple[Segment, ...]
    hops: Tuple[BasisHop, ...] = ()
    basis_mismatch: bool = field(default=False)

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError(
                "a Series needs at least one segment. An empty series is the unavailability this "
                "package refuses to represent, so there is no reason to build one here."
            )
        if self.hops and not self.basis_mismatch:
            raise ValueError(
                f"a Series reports {len(self.hops)} basis hop(s) but basis_mismatch=False; the "
                f"flag is derived from the hops, so the two cannot be set independently"
            )

    @classmethod
    def build(cls, segments: Iterable[Segment]) -> "Series":
        """Order the segments by period and compute the seams between them.

        The only constructor. Building a :class:`Series` with a hand-written ``hops`` tuple would
        let a caller assert "no seam" over segments that plainly have one, which is the exact
        assertion this type exists to make impossible.
        """
        ordered = tuple(sorted(segments, key=_first_period))
        if not ordered:
            raise ValueError("a Series needs at least one segment")
        hops: List[BasisHop] = []
        for earlier, later in zip(ordered, ordered[1:]):
            # Seam detection goes through Basis.comparable, the one policy in the package, rather
            # than an identity test written here. Two places deciding comparability is how a
            # refusal and a merge end up disagreeing about the same pair of bases, and the failure
            # is silent in the direction that matters.
            if earlier.basis.comparable(later.basis):
                continue
            period = _seam_period(earlier, later)
            hops.append(
                BasisHop(
                    period=period,
                    before=earlier.basis,
                    after=later.basis,
                    before_source=earlier.source,
                    after_source=later.source,
                    before_value=earlier.get(period),
                    after_value=later.get(period),
                )
            )
        return cls(segments=ordered, hops=tuple(hops), basis_mismatch=bool(hops))

    # -- the whole point --------------------------------------------------- #

    @property
    def comparable(self) -> bool:
        """Whether this series is on one basis throughout and so can be reduced to a series."""
        return not self.basis_mismatch

    def frame(self) -> Dict[str, Any]:
        """The periods -> values mapping, or a refusal.

        This is the method a caller reaches for when they want one thing, so it is the method
        that has to check. It is also the *only* way to get a single mapping out of a
        :class:`Series`, which is why adding a ``to_frame`` alias later would be a way to route
        around the check rather than a convenience.

        Raises:
            BasisMismatchError: The series spans two bases. The message names every hop and
                quotes the two figures at each one.
        """
        if self.basis_mismatch:
            raise BasisMismatchError(self._mismatch_message())
        merged: Dict[str, Any] = {}
        for segment in self.segments:
            for period, value in segment.values.items():
                if period in merged:
                    # Two segments on the same basis may legitimately overlap; the later one wins
                    # and the overlap is recorded, because silently dropping one of two sources'
                    # readings of the same period is a blend in everything but name.
                    continue
                merged[period] = value
        return merged

    def value_at(self, period: str) -> Any:
        """One period's value, or a refusal if the seam runs through it.

        Allows a period on the far side of a seam to be read when the caller explicitly asks for
        that period, because refusing to read a single named period would be refusing to answer a
        question that *was* well posed. What it never does is average across, or pick a side
        silently: a period covered by two segments on **different** bases raises with both values
        in the message, because that is the only case where the caller's intent is ambiguous.
        """
        covering = [s for s in self.segments if period in s.values]
        if not covering:
            raise KeyError(
                f"no segment covers {period!r}. Covered: {sorted(self.periods())}"
            )
        first = covering[0]
        clashing = [s for s in covering[1:] if not first.basis.comparable(s.basis)]
        if clashing:
            figures = ", ".join(
                f"{s.source} says {s.values[period]!r} on {s.basis.value}" for s in covering
            )
            raise BasisMismatchError(
                f"{period!r} is covered by {len(covering)} segments on bases that are not "
                f"comparable, so there is no single value to return. The figures are: {figures}. "
                f"They are not reconciled here and must not be averaged - "
                f"{cross_basis_note(first.basis, clashing[0].basis)}"
            )
        return first.values[period]

    # -- provenance --------------------------------------------------------- #

    def periods(self) -> Tuple[str, ...]:
        """Every period, sorted, across all segments."""
        found = {period for segment in self.segments for period in segment.values}
        return tuple(sorted(found))

    def sources(self) -> Tuple[str, ...]:
        """The sources in this series, in segment order, duplicates collapsed."""
        seen: List[str] = []
        for segment in self.segments:
            if segment.source not in seen:
                seen.append(segment.source)
        return tuple(seen)

    def bases(self) -> Tuple[Basis, ...]:
        """Every basis present, in segment order."""
        seen: List[Basis] = []
        for segment in self.segments:
            if segment.basis not in seen:
                seen.append(segment.basis)
        return tuple(seen)

    def as_of(self) -> Optional[Any]:
        """The newest currency stamp across the segments, or ``None`` if none carries one.

        The newest, because that is what a caller wants on a stitched series: how stale is the
        worst part. Segments that do not publish a stamp do not lower it — a source with no
        as-of is an absence of information, not evidence of staleness.
        """
        stamps = [s.as_of for s in self.segments if s.as_of is not None]
        if not stamps:
            return None
        try:
            return max(stamps)
        except TypeError:
            return stamps[-1]

    def describe(self) -> str:
        """A one-screen summary: segments, sources, bases, and every seam."""
        lines = [
            f"{len(self.segments)} segment(s) from {len(self.sources())} source(s) "
            f"over {len(self.periods())} period(s), on basis "
            f"{', '.join(b.value for b in self.bases())}"
        ]
        for segment in self.segments:
            stamp = f", as of {segment.as_of}" if segment.as_of is not None else ""
            lines.append(
                f"  {segment.source} [{segment.basis.value}] {len(segment.values)} period(s){stamp}"
            )
        for hop in self.hops:
            lines.append(f"  ! seam at {hop.period}: {hop.before.value} -> {hop.after.value}")
        if self.basis_mismatch:
            lines.append("  this series spans two bases and must not be reduced to one number")
        return "\n".join(lines)

    def _mismatch_message(self) -> str:
        """The refusal, built from the hops so it cannot describe a seam that is not there."""
        paragraphs = [
            f"this series spans {len(self.bases())} bases across {len(self.segments)} segments "
            f"({', '.join(self.sources())}), and there is no single value or single series it can "
            f"be reduced to. The seams, in period order:",
        ]
        for hop in self.hops:
            detail = f"{hop.period!r}"
            if hop.before_value is not None or hop.after_value is not None:
                detail += (
                    f" — {hop.before_source} reports {hop.before_value!r} on {hop.before.value}, "
                    f"{hop.after_source} reports {hop.after_value!r} on {hop.after.value}"
                )
            paragraphs.append(f"  {detail}")
        paragraphs.append(
            "  " + cross_basis_note(self.segments[0].basis, self.segments[-1].basis)
        )
        paragraphs.append(
            "Both figures are reported. Neither is adjusted, relabelled, or averaged into the "
            "other. If you have a reason to prefer one side — a filing, a hedge, a backtest that "
            "says so — apply it in your own code where the decision is visible, because this layer "
            "cannot tell a legitimate preference from a silent substitution."
        )
        return "\n".join(paragraphs)


def _first_period(segment: Segment) -> str:
    """Sort key: the earliest period the segment covers.

    ``min`` rather than insertion order, so two segments that start on the same period keep a
    stable, declared order and a seam between them is reported in the same sequence every run.
    A seam that moves around between runs is a seam nobody can write a test for.
    """
    return min(segment.values)


def _seam_period(earlier: Segment, later: Segment) -> str:
    """Where the seam between two segments sits, in period order.

    The later segment's first period, or the earlier segment's last when the later one starts
    earlier — which happens when segments are handed over out of order, and picking the wrong end
    of the boundary would report the jump at a period where nothing changed.
    """
    later_start = min(later.values)
    earlier_end = max(earlier.values)
    return later_start if later_start >= earlier_end else earlier_end
