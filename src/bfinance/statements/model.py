"""
The provider-agnostic shapes: what is asked for, and what comes back.

One shape, one orientation, one basis label
-------------------------------------------
:class:`StatementResult` is what every provider in this package must produce and what the cascade
hands back, so a caller never has to know which vendor answered. Its contract:

* **orientation** — index is the **metric**, columns are the **periods**, ascending, with
  ``"TTM"`` first when present. Metric rows x period-string columns, matching the orientation
  :mod:`bfinance.models.statements` documents as native. (That module's own
  ``to_dataframe(orient="columns")`` transposes it; this package does not, and the orientation is
  pinned by a test rather than inferred.)
* **periods** — canonical, per :mod:`bfinance.statements.periods`. ``"Jun '26"`` and
  ``"Jun 2026"`` are the same column.
* **metric labels** — **the provider's own**. screener.in labels a row ``"Net Profit+"``;
  trendlyne labels the same idea ``NP_A``. No cross-vendor label map is provided, because none
  was measured: a mapping asserted from memory would be a rename of one vendor's number into
  another's vocabulary with no verification behind it, which is the fabrication this package
  exists to avoid. The provider is on ``attrs`` for exactly this reason.
* **basis** — recorded on every result, always, whether or not it matches what was asked for.
  See :attr:`StatementResult.basis_mismatch`.

Every measurement that shaped a frame is on ``attrs`` under a ``bfinance_statements_`` key
declared in this module as a module-level constant, so a consumer can branch on the fact instead
of parsing a string. :data:`ATTR_KEYS` is the complete set, and a test asserts no frame leaves
this package carrying a key that is not in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

import pandas as pd

__all__ = [
    "Basis",
    "Frequency",
    "StatementKind",
    "StatementRequest",
    "StatementResult",
    "PROVENANCE",
    "ATTR_KEYS",
    "ATTR_BASIS",
    "ATTR_BASIS_MISMATCH",
    "ATTR_BASIS_NOTE",
    "ATTR_BASIS_REQUESTED",
    "ATTR_DROPPED_ROWS",
    "ATTR_DUPLICATE_ROWS",
    "ATTR_FALLBACK",
    "ATTR_FALLBACK_FROM",
    "ATTR_FREQUENCY",
    "ATTR_IDENTIFIED_BY",
    "ATTR_IDENTIFY_NOTE",
    "ATTR_KIND",
    "ATTR_OMITTED_ROWS",
    "ATTR_PAYLOAD",
    "ATTR_PERIODS",
    "ATTR_PROVIDER",
    "ATTR_PROVIDERS_TRIED",
    "ATTR_SKIP_REASONS",
    "ATTR_SYMBOL",
    "ATTR_UNITS_NOTE",
]

#: Provenance tag on every frame this package returns. Matches the convention already used by
#: ``bfinance.nse.marketlens`` (``nse_marketlens``), ``bfinance.nse.bhavcopy``
#: (``nse_bhavcopy``) and ``bfinance.trendlyne.tables`` (``trendlyne``).
PROVENANCE = "bfinance.statements"


class StatementKind(str, Enum):
    """Which statement. Deliberately three: ratios are not a statement, and a fourth kind would
    need a fourth set of measured facts before it could be honest about what it does not have."""

    INCOME = "income"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Frequency(str, Enum):
    """``ANNUAL`` and ``QUARTERLY`` are calendar frequencies; ``TTM`` is a trailing-twelve-month
    *window*, which is why it is not folded into either of them and gets its own period label."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    TTM = "ttm"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Basis(str, Enum):
    """Consolidated (group, with subsidiaries) or standalone (the company alone).

    Recorded, never assumed, and never blended. See :mod:`bfinance.statements.cascade` for the
    measured reason: screener.in is consolidated-only on this route while trendlyne ships **both**
    bases in one request and renders **standalone** on the page that says ``is_consolidated:
    true``. A frame that quietly carried one basis under the other's label is the worst outcome
    available, and it is a number that looks right.
    """

    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# --------------------------------------------------------------------------- #
# attrs contract
# --------------------------------------------------------------------------- #

ATTR_SYMBOL = "bfinance_symbol"
ATTR_PROVIDER = "bfinance_statements_provider"
ATTR_PAYLOAD = "bfinance_statements_payload"
ATTR_KIND = "bfinance_statements_kind"
ATTR_FREQUENCY = "bfinance_statements_frequency"
ATTR_PERIODS = "bfinance_statements_periods"
ATTR_BASIS = "bfinance_statements_basis"
ATTR_BASIS_REQUESTED = "bfinance_statements_basis_requested"
ATTR_BASIS_MISMATCH = "bfinance_statements_basis_mismatch"
ATTR_BASIS_NOTE = "bfinance_statements_basis_note"
ATTR_IDENTIFIED_BY = "bfinance_statements_identified_by"
ATTR_IDENTIFY_NOTE = "bfinance_statements_identify_note"
ATTR_PROVIDERS_TRIED = "bfinance_statements_providers_tried"
ATTR_FALLBACK = "bfinance_statements_fallback"
ATTR_FALLBACK_FROM = "bfinance_statements_fallback_from"
ATTR_SKIP_REASONS = "bfinance_statements_skip_reasons"
ATTR_DUPLICATE_ROWS = "bfinance_statements_duplicate_rows"
ATTR_DROPPED_ROWS = "bfinance_statements_dropped_rows"
ATTR_OMITTED_ROWS = "bfinance_statements_omitted_numeric_rows"
ATTR_UNITS_NOTE = "bfinance_statements_units_note"

#: Every key this package writes. Asserted to be the complete set by
#: ``tests/test_statements_cascade.py``.
ATTR_KEYS: Tuple[str, ...] = (
    "bfinance_provenance",
    ATTR_SYMBOL,
    ATTR_PROVIDER,
    ATTR_PAYLOAD,
    ATTR_KIND,
    ATTR_FREQUENCY,
    ATTR_PERIODS,
    ATTR_BASIS,
    ATTR_BASIS_REQUESTED,
    ATTR_BASIS_MISMATCH,
    ATTR_BASIS_NOTE,
    ATTR_IDENTIFIED_BY,
    ATTR_IDENTIFY_NOTE,
    ATTR_PROVIDERS_TRIED,
    ATTR_FALLBACK,
    ATTR_FALLBACK_FROM,
    ATTR_SKIP_REASONS,
    ATTR_DUPLICATE_ROWS,
    ATTR_DROPPED_ROWS,
    ATTR_OMITTED_ROWS,
    ATTR_UNITS_NOTE,
)


# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StatementRequest:
    """What the caller asked for. Carries no payload, so it is safe to log, cache-key and compare.

    Attributes:
        symbol: The symbol being asked about, for ``attrs`` and error messages. Not
            cross-checked against the payload — a vendor page carries its own symbol and a
            mismatch is a caller error the cascade reports, not something to paper over.
        kind: Which statement.
        frequency: Which frequency. Note that asking for a frequency no provider publishes is a
            :class:`~bfinance.statements.errors.StatementCapabilityError`, raised by the cascade
            before a provider runs.
        basis: Which basis the caller wants. A provider that cannot serve it is **skipped with a
            recorded reason**, not asked and mis-served.
    """

    symbol: str
    kind: StatementKind
    frequency: Frequency
    basis: Basis = Basis.CONSOLIDATED

    def __post_init__(self) -> None:
        for name, value in (("kind", self.kind), ("frequency", self.frequency), ("basis", self.basis)):
            if not isinstance(value, Enum):
                raise TypeError(
                    f"StatementRequest.{name} must be a StatementKind/Frequency/Basis, got "
                    f"{type(value).__name__}: {value!r}"
                )


# --------------------------------------------------------------------------- #
# result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StatementResult:
    """One statement, from one provider, on one basis, with every measurement that shaped it.

    Deliberately a single-provider object. There is no code path that merges two providers' rows
    into one result, and that is not an oversight to be fixed later — see
    :mod:`bfinance.statements.cascade` on why a basis difference between providers is the one
    thing a cascade must not smooth over. ``providers_tried`` and ``skip_reasons`` record what
    was *not* used, so the choice stays visible.
    """

    request: StatementRequest
    provider: str
    payload: str
    basis: Basis
    periods: Tuple[str, ...]
    rows: Mapping[str, Tuple[Optional[float], ...]]
    row_ids: Mapping[str, str] = field(default_factory=dict)
    row_labels: Mapping[str, str] = field(default_factory=dict)
    identified_by: str = ""
    identify_note: Optional[str] = None
    #: Every provider the registry lists for this (kind, frequency), in order — the basis on which
    #: :attr:`served` and :attr:`fallback_from` are decided. Empty for a result built by calling
    #: a provider directly rather than through the cascade.
    registry_order: Tuple[str, ...] = ()
    #: The providers that were actually **asked** for this result, in order. A basis-skipped
    #: provider is absent from this tuple and present in :attr:`skip_reasons`.
    providers_tried: Tuple[str, ...] = ()
    skip_reasons: Mapping[str, str] = field(default_factory=dict)
    duplicate_rows: int = 0
    dropped_rows: Tuple[str, ...] = ()
    omitted_rows: int = 0
    units_note: Optional[str] = None

    def __post_init__(self) -> None:
        if len(set(self.periods)) != len(self.periods):
            raise ValueError(
                f"periods are not unique: {list(self.periods)}. A statement with two columns "
                f"claiming the same period cannot be built."
            )
        if not self.periods:
            raise ValueError("a statement result must carry at least one period")
        for metric, values in self.rows.items():
            if len(values) != len(self.periods):
                raise ValueError(
                    f"row {metric!r} has {len(values)} value(s) for {len(self.periods)} "
                    f"period(s) ({', '.join(self.periods)}). Refusing a ragged statement."
                )
        if not self.rows:
            raise ValueError(
                "a statement result must carry at least one row; an empty statement is "
                "StatementEmptyError, not a result"
            )

    # -- provenance -------------------------------------------------------- #

    @property
    def kind(self) -> StatementKind:
        return self.request.kind

    @property
    def frequency(self) -> Frequency:
        return self.request.frequency

    @property
    def metrics(self) -> Tuple[str, ...]:
        """Row labels, in the provider's own order."""
        return tuple(self.rows)

    @property
    def served(self) -> bool:
        """``True`` when the first provider the registry ordered for this cell answered.

        "First" is the *registry* order, not the order things happened to be tried in, so a
        provider that was skipped for basis and a provider that was asked and failed are both
        counted the same way: the primary source did not answer.
        """
        return bool(self.registry_order) and self.provider == self.registry_order[0]

    @property
    def fallback(self) -> bool:
        """``True`` when a provider ahead of the one that answered did not serve.

        Either because it was skipped (wrong basis) or because it failed. ``skip_reasons`` says
        which, per provider. A result built by calling a provider directly, without the cascade,
        has no registry order and therefore no fallback: there was no failover to record.
        """
        return bool(self.fallback_from)

    @property
    def fallback_from(self) -> Tuple[str, ...]:
        """The registered providers ahead of the one that answered, in registry order."""
        if self.provider not in self.registry_order:
            return tuple(self.registry_order)
        return tuple(self.registry_order[: self.registry_order.index(self.provider)])

    @property
    def basis_mismatch(self) -> bool:
        """``True`` when the basis served is not the basis requested.

        The frame is **not** adjusted, converted or discarded. It is returned on the basis it was
        actually served on, labelled, with the flag set — a consumer can check one boolean.
    """
        return self.basis is not self.request.basis

    @property
    def basis_note(self) -> str:
        """One sentence on the basis, always present, and the mismatch spelled out when there is
        one. Never empty, so a consumer that reads only the note cannot miss it."""
        served = f"served on the {self.basis} basis"
        if not self.basis_mismatch:
            return f"{served}, which is the basis requested."
        skipped = ", ".join(
            f"{name} publishes only {reason}"
            for name, reason in sorted(self.skip_reasons.items())
        ) or "no provider was skipped for basis"
        return (
            f"{served}, but the {self.request.basis} basis was requested. The numbers are NOT "
            f"adjusted for this: they are one provider's {self.basis} figures. A consolidated "
            f"and a standalone figure are different numbers for the same company and the same "
            f"period, and blending or substituting one for the other is a silent corruption. "
            f"Check the flag on attrs, or ask for a provider that publishes "
            f"{self.request.basis} ({skipped})."
        )

    # -- access ------------------------------------------------------------ #

    def metric(self, name: str) -> Dict[str, Optional[float]]:
        """One row as ``{period: value}``.

        The lookup is case-insensitive on the provider's own label, matching
        :meth:`bfinance.models.statements.FinancialStatement.get_metric`. It is *not* fuzzy and
        does not fall back to a second name: a miss returns ``{}``, which is distinguishable from
        a row of nulls.
        """
        if name in self.rows:
            return dict(zip(self.periods, self.rows[name], strict=True))
        lowered = name.strip().lower()
        for key, values in self.rows.items():
            if key.strip().lower() == lowered:
                return dict(zip(self.periods, values, strict=True))
        return {}

    def non_null_cells(self) -> int:
        """How many cells in the whole statement carry a number. Zero is a refusal, not a result."""
        return sum(
            1 for values in self.rows.values() for value in values if value is not None
        )

    def cell_count(self) -> int:
        return len(self.rows) * len(self.periods)

    # -- serialisation ----------------------------------------------------- #

    def attrs(self) -> Dict[str, Any]:
        """The full ``attrs`` mapping, using only keys in :data:`ATTR_KEYS`."""
        payload: Dict[str, Any] = {
            "bfinance_provenance": PROVENANCE,
            ATTR_SYMBOL: self.request.symbol,
            ATTR_PROVIDER: self.provider,
            ATTR_PAYLOAD: self.payload,
            ATTR_KIND: self.kind.value,
            ATTR_FREQUENCY: self.frequency.value,
            ATTR_PERIODS: len(self.periods),
            ATTR_BASIS: self.basis.value,
            ATTR_BASIS_REQUESTED: self.request.basis.value,
            ATTR_BASIS_MISMATCH: self.basis_mismatch,
            ATTR_BASIS_NOTE: self.basis_note,
            ATTR_IDENTIFIED_BY: self.identified_by,
            ATTR_IDENTIFY_NOTE: self.identify_note,
            ATTR_PROVIDERS_TRIED: tuple(self.providers_tried),
            ATTR_FALLBACK: self.fallback,
            ATTR_FALLBACK_FROM: self.fallback_from,
            ATTR_SKIP_REASONS: dict(self.skip_reasons),
            ATTR_DUPLICATE_ROWS: int(self.duplicate_rows),
            ATTR_DROPPED_ROWS: tuple(self.dropped_rows),
            ATTR_OMITTED_ROWS: int(self.omitted_rows),
            ATTR_UNITS_NOTE: self.units_note,
        }
        unknown = set(payload) - set(ATTR_KEYS)
        if unknown:  # pragma: no cover - guarded by a test over ATTR_KEYS
            raise AssertionError(f"attrs keys not declared in ATTR_KEYS: {sorted(unknown)}")
        return payload

    def to_dataframe(self) -> pd.DataFrame:
        """The statement as a frame: **index = metric, columns = period**, ascending, ``TTM`` first.

        Every cell is a real vendor number or ``NaN``; nothing is filled, interpolated, rounded or
        aligned against a period the source did not publish. ``attrs`` carries the full
        :meth:`attrs` mapping, so the frame and the result object can never disagree about where
        the numbers came from.
        """
        frame = pd.DataFrame(
            {metric: list(values) for metric, values in self.rows.items()},
            index=list(self.periods),
        ).T
        frame.index.name = "metric"
        frame.columns.name = "period"
        frame.attrs.update(self.attrs())
        return frame

    def __repr__(self) -> str:  # pragma: no cover - display only
        return (
            f"<StatementResult {self.kind}/{self.frequency} from {self.provider} "
            f"({self.basis}) periods={len(self.periods)} metrics={len(self.rows)}"
            f"{' FALLBACK' if self.fallback else ''}>"
        )
