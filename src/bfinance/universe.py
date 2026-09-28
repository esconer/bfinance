"""
NSE index membership — a **recorder**, because point-in-time membership exists nowhere else.

Why this module is a recorder and not a reader
----------------------------------------------
NSE publishes **only the current index snapshot**, and it **overwrites membership in place**. There
is no history route. Four dated/history variants were probed and all returned **404**::

    ind_nifty50list_20240101.csv
    ind_nifty50list_01012024.csv
    ind_nifty50_2024.csv
    .../2024/ind_nifty50list.csv

The published rebalance calendar lives on ``nseindices.com``, which **does not resolve at all**
(DNS timeout, TCP/443 refused), and no rebalancing CSV exists on ``nsearchives`` either.

The consequence is not a bug to route around, it is a fact about the data:

    **Survivorship bias cannot be removed retroactively from any source.**

A backtest that uses today's Nifty 50 list to pick 2020 constituents is wrong in a way no fixture
can repair, because the 2020 list was never published. The *only* way any caller can ever obtain
point-in-time membership is if some process accumulates snapshots locally **starting now**.

So every successful read here appends to a dated local archive, and the archive is the feature.

The rule this module exists to enforce
--------------------------------------
A membership query for a date the archive does not cover **must fail loudly**. It must never be
answered with the current snapshot wearing a historical label. That single substitution is how a
survivorship-biased backtest gets to look clean, so :meth:`UniverseArchive.membership_asof`
raises :class:`HistoryUnavailableError` and names the earliest date that *is* available. There is
no code path in this module that resolves an as-of miss to the newest snapshot.

Endpoints — verified live 2026-09-27
------------------------------------
::

    https://nsearchives.nseindia.com/content/indices/ind_nifty<name>list.csv

``nsearchives`` is preferred over ``www.nseindia.com``: it absorbed 50 requests including 23 that
404'd with no friction, and needs no cookie and no Akamai prime. The data hosts serve no
``robots.txt`` at all — per RFC 9309 a missing ``robots.txt`` means unrestricted, and the 404s are
themselves the evidence (``tests/fixtures/nse/ROBOTS_EVIDENCE.json``).

Verbatim columns, in this order::

    Company Name, Industry, Symbol, Series, ISIN Code

``Series`` and ``ISIN Code`` are **carried, not discarded**: ISIN is the join key that lets a caller
line this membership up against any other source without going through a ticker string.

The filename pattern, and the 404 trap
--------------------------------------
The pattern is ``ind_nifty<name>list.csv``. Guessing wrong **404s**, and a 404 is indistinguishable
from "no such index", which is how a whole index silently goes missing:

===============================  ========
Filename                         Result
===============================  ========
``ind_niftymidcap150list.csv``   200
``ind_niftymidsmallcap400list.csv``  200
``ind_midcap150list.csv``        **404**
``ind_midsmallcap400list.csv``   **404**
===============================  ========

The catalogue below is therefore authoritative rather than derived from a template, and
:func:`index_filename` builds names from it. Note ``ind_niftytotalmarket_list.csv`` breaks the
pattern with an extra underscore before ``list``.

``ind_niftysmallcap250list.csv`` is mislabelled and is **fenced off**
--------------------------------------------------------------------
It is not a small-cap list. Measured over the complete recorded symbol sets
(``tests/fixtures/nse/universe/_partition_proof.json``):

* **251** data rows against Nifty 500's **501** — not 250, and not a subset of the small caps.
* A **strict subset of Nifty 500**: intersection 251, i.e. the whole file.
* **Exactly** ``midsmallcap400 - midcap150`` — both difference directions empty.
* Therefore **zero actual small caps**.

It is in :data:`BLOCKED_INDICES` with that reason, and :func:`resolve_index` refuses it. It is
still *listed* by :func:`list_indices` (marked unusable, with the reason) so the trap stays visible
instead of being quietly forgotten.

Never trust a row count as a membership size
---------------------------------------------
NSE's own **"Nifty 500"** file has **501** rows. So ``500`` is not the size of anything, and this
module contains no hardcoded ``500``, no ``head(500)``, and no truncation of any kind.
:attr:`IndexSpec.nominal_size` exists only for display and is never applied to the data — a test
pins the 500-vs-501 disagreement so the nominal number can never quietly become a cap.

``ind_niftytotalmarket_list.csv`` (755 symbols, a superset of the 500) is the only genuine
small/mid-cap source on this host.

Rebalance events
----------------
**No rebalance calendar is invented and none is shipped.** The calendar's only home,
``nseindices.com``, does not resolve, so any schedule this module produced would be a guess dressed
as data. Fabricating one is exactly the failure class this repo has been deleting.

What *is* offered is :meth:`UniverseArchive.observed_changes`, derived from **observed** differences
between snapshots the archive actually recorded. It is labelled ``observed_snapshot_diff`` and
``is_scheduled=False`` throughout. An observed change is a fact about two real retrievals; it is not
a promise about a future effective date, and callers must not treat it as one.

Archive format
--------------
A directory (default ``~/.bfinance/universe``) holding::

    archive_meta.json          format + version marker
    snapshots/<sha256_16>.json one file per DISTINCT membership set, content-addressed
    indices/<index_key>.json   the run-length-encoded date -> snapshot map

**Run-length encoding is the growth bound.** NSE changes membership only at rebalances, roughly
twice a year, so a daily read returns the *same* set almost every time. A naive archive stores a
full copy per index per day: 365 x 500 rows per index per year, forever. Here, an unchanged
membership collapses into the existing open run, so storage is ``O(distinct membership changes)``
— about two blobs and two runs per index per year — **independent of how often it is read**.

A repeat read of unchanged membership performs **zero** filesystem writes. A read that observes a
genuine change writes exactly one new snapshot blob and rewrites exactly one small run index; the
earlier blobs are never touched. Snapshots are content-addressed over the *symbol-sorted* member
set, so a re-ordering of NSE's file that does not change membership is correctly recognised as the
same snapshot instead of forking a new one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import httpx
import pandas as pd

from bfinance.utils.exceptions import BFinanceError, ParsingError, UpstreamServiceError

#: Every public name in this module, and nothing else. Without it, ``from bfinance.universe
#: import *`` binds this module's own ``pd``, ``httpx``, ``csv``, ``json``, ``date`` and
#: ``Path`` into the caller's namespace, which is a way to shadow a caller's ``json`` with
#: NSE's parser. Only names **defined here** are listed; every imported name is excluded, so a
#: star import carries the API and not the dependencies. Matches the convention
#: :mod:`bfinance.flows` and :mod:`bfinance.costs` use.
__all__ = [
    # ---- the archive's own coordinates ----
    "ARCHIVE_HOST",
    "CONTENT_ROOT",
    "FILENAME_PATTERN",
    "KNOWN_404_FILENAMES",
    "MIN_REQUEST_INTERVAL",
    # ---- the published column contracts ----
    "SOURCE_COLUMNS",
    "MEMBERSHIP_COLUMNS",
    # ---- errors ----
    "UniverseError",
    "UnsupportedIndexError",
    "HistoryUnavailableError",
    # ---- the catalogue and name resolution ----
    "IndexSpec",
    "CATALOGUE",
    "BLOCKED_INDICES",
    "resolve_index",
    "index_filename",
    "index_url",
    "index_for_filename",
    "list_indices",
    # ---- parsing ----
    "parse_constituents",
    # ---- the local archive ----
    "Coverage",
    "RecordResult",
    "MembershipDiff",
    "UniverseArchive",
    "default_archive",
]

#: Retrieval timestamps are recorded in UTC. Index membership is a date-stamped observation, not a
#: market session, so a local trading-day conversion here would invent a boundary NSE never had.

#: Host that serves the constituent CSVs. Preferred over ``www.nseindia.com``: no cookie, no
#: Akamai prime, and 23 consecutive 404s produced no friction when it was probed.
ARCHIVE_HOST = "nsearchives.nseindia.com"
CONTENT_ROOT = f"https://{ARCHIVE_HOST}/content/indices/"

#: The documented shape of the filename. The catalogue below is authoritative, but this is the
#: pattern that makes a wrong guess 404 rather than merely wrong.
FILENAME_PATTERN = "ind_nifty<name>list.csv"

#: Confirmed 404s. Present so the trap is documented in code and pinned by a test rather than
#: rediscovered by a future contributor who "tidies" a name.
KNOWN_404_FILENAMES: frozenset[str] = frozenset(
    {
        "ind_midcap150list.csv",
        "ind_midsmallcap400list.csv",
    }
)

#: Minimum seconds between requests to :data:`ARCHIVE_HOST`. Other clients share this host.
MIN_REQUEST_INTERVAL = 1.2

#: Verbatim NSE column order.
SOURCE_COLUMNS: tuple[str, ...] = (
    "Company Name",
    "Industry",
    "Symbol",
    "Series",
    "ISIN Code",
)

#: Output frame columns. ``series`` and ``isin`` are carried because ISIN is the cross-source
#: join key; a frame that dropped them would force callers back onto ticker strings.
MEMBERSHIP_COLUMNS: tuple[str, ...] = (
    "symbol",
    "company_name",
    "industry",
    "series",
    "isin",
)

# ------------------------------------------------------------------ exceptions


class UniverseError(BFinanceError):
    """Base for index-membership failures."""


class UnsupportedIndexError(UniverseError):
    """The requested index is unknown, or is known to be unusable.

    Raised for both an unknown name and a fenced-off one, because both mean the caller asked for
    membership this module will not hand over.
    """


class HistoryUnavailableError(UniverseError):
    """The archive cannot answer an as-of question for the requested date.

    This is the honest answer and the whole point of the module. It carries the requested date and
    the window the archive *can* speak to, so a caller can say something truthful instead of
    silently receiving today's list.
    """

    def __init__(
        self,
        index_key: str,
        requested: str,
        earliest: Optional[str],
        latest: Optional[str],
        reason: str,
    ) -> None:
        self.index_key = index_key
        self.requested = requested
        self.earliest_available = earliest
        self.latest_available = latest
        self.reason = reason
        if earliest and requested < earliest:
            detail = f"history unavailable from {earliest}"
        elif latest:
            detail = f"nothing recorded for {requested} (history runs {earliest} to {latest})"
        else:
            detail = f"no membership has been recorded for {requested}"
        super().__init__(
            f"Index {index_key!r}: {detail}. {reason} The current snapshot is deliberately NOT "
            f"returned in its place - it is not the membership on {requested}."
        )


# ------------------------------------------------------------------ catalogue


@dataclass(frozen=True)
class IndexSpec:
    """One NSE index and the exact file that holds its constituents."""

    key: str
    filename: str
    label: str
    nominal_size: Optional[int]
    blocked_reason: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.blocked_reason is None

    @property
    def url(self) -> str:
        return f"{CONTENT_ROOT}{self.filename}"


#: The authoritative index -> filename mapping. Names here are the ones the host actually serves.
#:
#: ``nominal_size`` is the number in the index's *name* and is display metadata only. It is never
#: applied to the data: NSE's "Nifty 500" file holds 501 rows.
CATALOGUE: dict[str, IndexSpec] = {
    "nifty50": IndexSpec("nifty50", "ind_nifty50list.csv", "Nifty 50", 50),
    "nifty500": IndexSpec("nifty500", "ind_nifty500list.csv", "Nifty 500", 500),
    "midcap100": IndexSpec("midcap100", "ind_niftymidcap100list.csv", "Nifty Midcap 100", 100),
    "midcap150": IndexSpec("midcap150", "ind_niftymidcap150list.csv", "Nifty Midcap 150", 150),
    "midsmall400": IndexSpec(
        "midsmall400", "ind_niftymidsmallcap400list.csv", "Nifty MidSmallcap 400", 400
    ),
    "totalmarket": IndexSpec(
        "totalmarket",
        # Breaks the pattern with an underscore before `list`; recorded working.
        "ind_niftytotalmarket_list.csv",
        "Nifty Total Market",
        None,
    ),
    "smallcap250": IndexSpec(
        "smallcap250",
        "ind_niftysmallcap250list.csv",
        "Nifty Smallcap 250 (MISLABELLED - unusable)",
        250,
        blocked_reason=(
            "NSE's ind_niftysmallcap250list.csv is MISLABELLED and contains no small caps. Measured "
            "over the complete recorded symbol sets: 251 data rows against Nifty 500's 501; a "
            "STRICT SUBSET of Nifty 500 (intersection 251 = the whole file); and exactly equal to "
            "midsmallcap400 - midcap150, with both difference directions empty. It is therefore "
            "zero actual small caps. Use 'totalmarket' (ind_niftytotalmarket_list.csv, 755 "
            "symbols, a superset of the 500) for small/mid-cap work, or 'midsmall400'. Evidence: "
            "tests/fixtures/nse/universe/_partition_proof.json"
        ),
    ),
}

#: Fenced-off indices mapped to the reason. Kept as a name in its own right so a reader (and a
#: test) can ask "what is blocked and why" without diffing the catalogue.
BLOCKED_INDICES: dict[str, str] = {
    key: spec.blocked_reason for key, spec in CATALOGUE.items() if spec.blocked_reason
}

_FILENAME_TO_KEY: dict[str, str] = {spec.filename: key for key, spec in CATALOGUE.items()}

#: Spellings that look right and 404. Each is answered with the file that does work.
_ALIASES: dict[str, str] = {
    "midcap150list": "midcap150",
    "midsmallcap400list": "midsmall400",
    "smallcap250list": "smallcap250",
    "nifty50list": "nifty50",
    "nifty500list": "nifty500",
    "niftymidcap100list": "midcap100",
    "niftymidcap150list": "midcap150",
    "niftymidsmallcap400list": "midsmall400",
    "niftysmallcap250list": "smallcap250",
    "nifty_total_market": "totalmarket",
    "total_market": "totalmarket",
    "niftytotalmarket": "totalmarket",
}


def resolve_index(name: str) -> IndexSpec:
    """Resolve an index name to its spec, refusing unknown and fenced-off indices.

    Raises :class:`UnsupportedIndexError` for an unknown name, and for any index in
    :data:`BLOCKED_INDICES` — carrying the measured reason, so the refusal is actionable.
    """
    if not isinstance(name, str) or not name.strip():
        raise UnsupportedIndexError(f"index name must be a non-empty string, got {name!r}")

    key = name.strip().lower().replace("-", "").replace("_", "").replace(" ", "")

    if key in CATALOGUE:
        spec = CATALOGUE[key]
        if spec.blocked_reason:
            raise UnsupportedIndexError(
                f"Index {key!r} is not available as a universe. {spec.blocked_reason}"
            )
        return spec

    if key in _ALIASES and _ALIASES[key] in CATALOGUE:
        target = _ALIASES[key]
        spec = CATALOGUE[target]
        if spec.blocked_reason:
            raise UnsupportedIndexError(
                f"Index {name!r} resolves to {target!r}, which is not available as a universe. "
                f"{spec.blocked_reason}"
            )
        return spec

    known = ", ".join(sorted(k for k, s in CATALOGUE.items() if s.usable))
    blocked = ", ".join(sorted(BLOCKED_INDICES)) or "none"
    hint = ""
    for wrong in sorted(KNOWN_404_FILENAMES):
        hint += f" Note {wrong!r} returns 404 - the pattern is {FILENAME_PATTERN}."
    raise UnsupportedIndexError(
        f"Unknown NSE index {name!r}. Filenames follow {FILENAME_PATTERN}, so a name that drops "
        f"the 'nifty' prefix 404s and looks like 'no such index'.{hint} "
        f"Usable indices: {known}. Blocked as unusable: {blocked}."
    )


def index_filename(name: str) -> str:
    """The exact CSV filename for an index. Raises rather than guessing a 404."""
    return resolve_index(name).filename


def index_url(name: str) -> str:
    """The full URL for an index's constituent CSV."""
    return resolve_index(name).url


def index_for_filename(filename: str) -> IndexSpec:
    """Map a constituent CSV filename back to its index.

    This is the fence around the 404 trap. ``ind_midcap150list.csv`` and
    ``ind_midsmallcap400list.csv`` are absent from :data:`KNOWN_404_FILENAMES` precisely because
    they do not exist on the host: a filename can only be turned into a request by naming something
    the catalogue has actually resolved before.
    """
    spec_key = _FILENAME_TO_KEY.get(filename)
    if spec_key is None:
        known = ", ".join(sorted(_FILENAME_TO_KEY))
        raise UnsupportedIndexError(
            f"{filename!r} is not a constituent CSV this module has resolved. The pattern is "
            f"{FILENAME_PATTERN}; a name that drops the 'nifty' prefix returns 404 and is "
            f"indistinguishable from a non-existent index. Confirmed 404s: "
            f"{sorted(KNOWN_404_FILENAMES)}. Filenames known to work: {known}."
        )
    return resolve_index(spec_key)


def list_indices() -> pd.DataFrame:
    """The catalogue as a frame, blocked entries included so the trap stays visible."""
    rows = [
        {
            "key": spec.key,
            "label": spec.label,
            "filename": spec.filename,
            "nominal_size": spec.nominal_size,
            "usable": spec.usable,
            "blocked_reason": spec.blocked_reason,
        }
        for spec in CATALOGUE.values()
    ]
    return pd.DataFrame(rows, columns=[
        "key", "label", "filename", "nominal_size", "usable", "blocked_reason",
    ])


# ------------------------------------------------------------------ parsing


def _blank_to_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_constituents(
    text: str,
    *,
    index: str = "",
    url: str = "",
    retrieved_utc: Optional[str] = None,
) -> pd.DataFrame:
    """Parse an NSE constituent CSV into a membership frame.

    Every row in the file becomes exactly one row out. Nothing is filtered, sampled, capped or
    de-duplicated away: if NSE publishes 501 rows for an index called "Nifty 500", the frame has
    501 rows. That is the only way the size can come from the data.

    Absent optional fields become ``None`` rather than an invented value. ``series`` and ``isin``
    are preserved verbatim when present — ISIN is the join key across sources.
    """
    if not text or not text.strip():
        raise ParsingError("constituent payload is empty")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ParsingError("constituent payload has no header row")

    symbol_column = _find_column(reader.fieldnames, ("symbol", "nse symbol", "scrip"))
    if symbol_column is None:
        raise ParsingError(
            f"constituent payload has no Symbol column; header was {list(reader.fieldnames)!r}. "
            f"Expected NSE's {list(SOURCE_COLUMNS)!r}."
        )

    name_column = _find_column(reader.fieldnames, ("company name", "company", "compsci"))
    industry_column = _find_column(reader.fieldnames, ("industry", "ind"))
    series_column = _find_column(reader.fieldnames, ("series",))
    isin_column = _find_column(reader.fieldnames, ("isin code", "isin"))

    rows: list[dict[str, Optional[str]]] = []
    for record in reader:
        symbol = _blank_to_none(record.get(symbol_column))
        if not symbol:
            # A blank symbol cannot be a member of anything; skipping it keeps `symbol` usable as
            # a key. The row is a formatting artefact, not a constituent.
            continue
        rows.append({
            "symbol": symbol,
            "company_name": _blank_to_none(record.get(name_column)) if name_column else None,
            "industry": _blank_to_none(record.get(industry_column)) if industry_column else None,
            "series": _blank_to_none(record.get(series_column)) if series_column else None,
            "isin": _blank_to_none(record.get(isin_column)) if isin_column else None,
        })

    frame = pd.DataFrame(rows, columns=list(MEMBERSHIP_COLUMNS))
    # Sorted so the frame is deterministic and the content hash is order-independent: NSE
    # re-ordering its own file is not a membership change and must not fork a new snapshot.
    frame = frame.sort_values("symbol", kind="mergesort").reset_index(drop=True)
    frame.attrs.update(_frame_attrs(index=index, url=url, retrieved_utc=retrieved_utc))
    return frame


def _find_column(fieldnames: Sequence[str], wanted: Iterable[str]) -> Optional[str]:
    lowered = {str(name).strip().lower(): str(name) for name in fieldnames}
    for candidate in wanted:
        if candidate in lowered:
            return lowered[candidate]
    return None


# ------------------------------------------------------------------ attrs


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _frame_attrs(
    *,
    index: str,
    url: str = "",
    retrieved_utc: Optional[str] = None,
    basis: str = "current_snapshot",
    as_of: Optional[str] = None,
    coverage: Optional["Coverage"] = None,
    archive_root: Optional[str] = None,
) -> dict[str, Any]:
    """Provenance for every frame this module hands back.

    ``basis`` is the load-bearing field: ``current_snapshot`` vs ``archived_snapshot`` vs
    ``membership_diff``. A consumer that cannot tell those apart is exactly how a present-day list
    gets used as history.
    """
    attrs: dict[str, Any] = {
        "bfinance_universe_index": index,
        "bfinance_universe_url": url,
        "bfinance_universe_retrieved_utc": retrieved_utc or _now_utc(),
        "bfinance_universe_basis": basis,
        "bfinance_universe_is_current_snapshot": basis == "current_snapshot",
        "bfinance_universe_as_of": as_of,
        "bfinance_universe_membership_observed": basis != "scheduled",
        # The truth about *when* the returned membership was actually observed. A consumer that
        # checks `snapshot_observed_from <= as_of` catches a substituted current snapshot even if
        # a caller never inspects the exception.
        "bfinance_universe_snapshot_observed_from": None,
        # The archive window is always present as a key, so a consumer never has to guess whether
        # the attribute was forgotten. `None` means no archive was consulted.
        "bfinance_universe_archive_earliest": None,
        "bfinance_universe_archive_latest": None,
        "bfinance_universe_archive_snapshots": None,
        "bfinance_universe_archive_runs": None,
        "bfinance_universe_history_starts_at": None,
    }
    if coverage is not None:
        attrs.update({
            "bfinance_universe_archive_earliest": coverage.earliest,
            "bfinance_universe_archive_latest": coverage.latest,
            "bfinance_universe_archive_snapshots": coverage.snapshot_count,
            "bfinance_universe_archive_runs": coverage.run_count,
            "bfinance_universe_history_starts_at": coverage.earliest,
        })
    if archive_root is not None:
        attrs["bfinance_universe_archive_root"] = archive_root
    return attrs


def _stamp_archived(
    frame: pd.DataFrame,
    *,
    index: str,
    coverage: "Coverage",
    archive_root: str,
    as_of: str,
    observed_from: str,
    retrieved_utc: Optional[str] = None,
) -> pd.DataFrame:
    """Relabel a frame as an archived (not current) membership set."""
    frame.attrs.update(_frame_attrs(
        index=index,
        url=index_url(index) if index in CATALOGUE else "",
        retrieved_utc=retrieved_utc,
        basis="archived_snapshot",
        as_of=as_of,
        coverage=coverage,
        archive_root=archive_root,
    ))
    frame.attrs["bfinance_universe_snapshot_observed_from"] = observed_from
    return frame


# ------------------------------------------------------------------ archive model


@dataclass(frozen=True)
class Coverage:
    """The window an archive can actually speak to, per index."""

    index_key: str
    earliest: Optional[str]
    latest: Optional[str]
    snapshot_count: int
    run_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_key": self.index_key,
            "earliest": self.earliest,
            "latest": self.latest,
            "snapshot_count": self.snapshot_count,
            "run_count": self.run_count,
        }


@dataclass(frozen=True)
class RecordResult:
    """What a single read did to the archive."""

    index_key: str
    snapshot_id: str
    rows: int
    new_snapshot: bool
    run_opened: bool
    written: bool
    files_written: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_key": self.index_key,
            "snapshot_id": self.snapshot_id,
            "rows": self.rows,
            "new_snapshot": self.new_snapshot,
            "run_opened": self.run_opened,
            "written": self.written,
            "files_written": list(self.files_written),
        }


@dataclass(frozen=True)
class MembershipDiff:
    """Difference between two archived snapshots.

    ``removed`` is the half that matters. A consumer that only ever counts additions is consuming a
    survivorship-biased universe: every name that left the index is invisible to it.
    """

    index_key: str
    older_date: str
    newer_date: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged_symbols: tuple[str, ...]
    older_rows: int
    newer_rows: int
    coverage: Optional["Coverage"] = None

    @property
    def unchanged(self) -> int:
        return len(self.unchanged_symbols)

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.removed)

    def to_frame(self) -> pd.DataFrame:
        """One row per symbol, labelled ``added``/``removed``/``unchanged``."""
        rows = [{"symbol": s, "change": "added"} for s in self.added]
        rows += [{"symbol": s, "change": "removed"} for s in self.removed]
        rows += [{"symbol": s, "change": "unchanged"} for s in self.unchanged_symbols]
        frame = pd.DataFrame(rows, columns=["symbol", "change"])
        frame.attrs.update(_frame_attrs(
            index=self.index_key,
            basis="membership_diff",
            as_of=self.newer_date,
            coverage=self.coverage,
        ))
        frame.attrs["bfinance_universe_diff_added"] = len(self.added)
        frame.attrs["bfinance_universe_diff_removed"] = len(self.removed)
        frame.attrs["bfinance_universe_diff_unchanged"] = self.unchanged
        frame.attrs["bfinance_universe_diff_basis"] = "observed_snapshot_diff"
        frame.attrs["bfinance_universe_is_scheduled"] = False
        return frame


def _content_hash(members: list[dict[str, Optional[str]]]) -> str:
    """Order-independent hash of a membership set.

    Content-addressed so an unchanged re-read is recognised as the same snapshot and a re-ordered
    file does not fork one.
    """
    payload = sorted(
        (tuple(row.get(column) for column in MEMBERSHIP_COLUMNS) for row in members),
        key=lambda item: (item[0] or ""),
    )
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _members_from_frame(frame: pd.DataFrame) -> list[dict[str, Optional[str]]]:
    missing = [c for c in MEMBERSHIP_COLUMNS if c not in frame.columns]
    if missing:
        raise UniverseError(
            f"membership frame is missing required columns {missing}; expected {list(MEMBERSHIP_COLUMNS)}"
        )
    members: list[dict[str, Optional[str]]] = []
    for record in frame[list(MEMBERSHIP_COLUMNS)].to_dict("records"):
        members.append({column: _blank_to_none(record.get(column)) for column in MEMBERSHIP_COLUMNS})
    return sorted(members, key=lambda row: row["symbol"] or "")


# ------------------------------------------------------------------ the archive


_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_NEXT_REQUEST_AT = 0.0


def _pace(min_interval: float) -> None:
    """Global inter-request spacing for :data:`ARCHIVE_HOST`."""
    global _GLOBAL_NEXT_REQUEST_AT
    if min_interval <= 0:
        return
    with _GLOBAL_PACING_LOCK:
        now = time.monotonic()
        wait = _GLOBAL_NEXT_REQUEST_AT - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _GLOBAL_NEXT_REQUEST_AT = now + min_interval


class UniverseArchive:
    """Dated, content-addressed, run-length-encoded archive of index membership.

    Layout::

        <root>/archive_meta.json
        <root>/snapshots/<sha256_16>.json
        <root>/indices/<index_key>.json

    Growth is ``O(distinct membership changes)`` rather than ``O(reads)`` because a read whose
    membership matches the open run performs no write at all and adds no run entry.
    """

    FORMAT = "bfinance-universe-archive"
    VERSION = 1

    def __init__(self, root: Optional[os.PathLike | str] = None) -> None:
        if root is None:
            base = os.environ.get("BFINANCE_HOME") or os.path.join(
                os.path.expanduser("~"), ".bfinance"
            )
            root = Path(base) / "universe"
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.indices_dir = self.root / "indices"
        self.meta_path = self.root / "archive_meta.json"
        self._ensure_root()

    # -- filesystem -------------------------------------------------------

    def _ensure_root(self) -> None:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.indices_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self._write_json(self.meta_path, {
                "format": self.FORMAT,
                "version": self.VERSION,
                "created_utc": _now_utc(),
                "note": (
                    "Point-in-time NSE index membership, accumulated from first use. NSE publishes "
                    "only the current snapshot and overwrites membership in place, so this archive "
                    "is the ONLY way historical membership will ever exist. Do not hand-edit."
                ),
                "growth": "run-length encoded: one blob and one run per membership CHANGE, "
                          "independent of how many reads happen.",
            })

    def _write_json(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")

    def _index_path(self, index_key: str) -> Path:
        return self.indices_dir / f"{index_key}.json"

    def _snapshot_path(self, snapshot_id: str) -> Path:
        return self.snapshots_dir / f"{snapshot_id}.json"

    def _load_index(self, index_key: str) -> dict[str, Any]:
        path = self._index_path(index_key)
        if not path.exists():
            return {"index_key": index_key, "runs": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        path = self._snapshot_path(snapshot_id)
        if not path.exists():
            raise UniverseError(f"archive is missing snapshot {snapshot_id!r} at {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- recording --------------------------------------------------------

    def record(
        self,
        index: str,
        frame: pd.DataFrame,
        *,
        retrieved_utc: Optional[str] = None,
        recorded_on: Optional[Any] = None,
    ) -> RecordResult:
        """Append a successful read to the archive. Idempotent and cheap when nothing changed.

        ``recorded_on`` exists so history can be replayed deterministically in tests and when
        back-filling from a recording. It defaults to today, because a read is evidence about the
        day it happened.
        """
        spec = resolve_index(index)
        key = spec.key
        members = _members_from_frame(frame)
        snapshot_id = _content_hash(members)[:16]
        stamp = retrieved_utc or _now_utc()
        if recorded_on is not None:
            day = _coerce_date(recorded_on).isoformat()
        else:
            day = date.today().isoformat()

        state = self._load_index(key)
        runs: list[dict[str, Any]] = [
            dict(r) for r in state.get("runs", []) if r.get("from")
        ]
        runs.sort(key=lambda r: r["from"])
        written: list[str] = []
        new_snapshot = False
        previous_observed = state.get("last_observed")

        snapshot_file = self._snapshot_path(snapshot_id)
        if not snapshot_file.exists():
            self._write_json(snapshot_file, {
                "snapshot_id": snapshot_id,
                "index_key": key,
                "rows": len(members),
                "columns": list(MEMBERSHIP_COLUMNS),
                "source_columns": list(SOURCE_COLUMNS),
                "content_sha256_16": snapshot_id,
                "first_retrieved_utc": stamp,
                "members": members,
            })
            written.append(snapshot_file.name)
            new_snapshot = True

        # --- run-length encoding: the growth bound -----------------------
        # NSE overwrites membership in place and changes it only at rebalances, so almost every
        # read returns the membership the newest run already holds. Recognising that is the whole
        # reason storage is O(membership changes) rather than O(reads).
        newest = _newest_effective_run(runs)
        membership_unchanged = newest is not None and newest["snapshot"] == snapshot_id
        same_day = previous_observed == day

        if membership_unchanged and same_day:
            # Nothing changed and the observation window has not moved: write nothing at all.
            return RecordResult(
                index_key=key,
                snapshot_id=snapshot_id,
                rows=len(members),
                new_snapshot=new_snapshot,
                run_opened=False,
                written=bool(written),
                files_written=tuple(written),
            )

        if not membership_unchanged:
            target_date = _coerce_date(day)

            # Close the run that currently covers this day, so runs never overlap.
            covered = _run_covering(runs, day)
            if covered is not None and covered["snapshot"] != snapshot_id:
                if covered["from"] >= day:
                    # The change was observed on the run's own first day, so there is no earlier
                    # day to close it on. Retiring it is the honest option; inventing a
                    # negative-length range would corrupt lookups on that date.
                    covered["superseded"] = True
                else:
                    covered["to"] = (target_date - timedelta(days=1)).isoformat()

            # Open the new run. It stays open-ended only if nothing is recorded after it, which
            # also keeps back-filled (out-of-order) reads from swallowing later history.
            later = [r for r in runs if r["from"] > day]
            new_run = {
                "from": day,
                "to": None,
                "snapshot": snapshot_id,
                "rows": len(members),
                "first_seen_utc": stamp,
            }
            if later:
                successor = min(later, key=lambda r: r["from"])
                new_run["to"] = (
                    _coerce_date(successor["from"]) - timedelta(days=1)
                ).isoformat()
            runs.append(new_run)
            runs = [r for r in runs if r.get("from")]
            runs.sort(key=lambda r: r["from"])

        # `last_observed` is what stops an open-ended run from answering for the future: a run may
        # only speak for dates at or before the last date a retrieval actually observed. It is a
        # single scalar rewritten in place, so it adds no files and no runs.
        self._write_json(self._index_path(key), {
            "index_key": key,
            "filename": spec.filename,
            "nominal_size": spec.nominal_size,
            "last_observed": day,
            "runs": runs,
        })
        written.append(self._index_path(key).name)

        return RecordResult(
            index_key=key,
            snapshot_id=snapshot_id,
            rows=len(members),
            new_snapshot=new_snapshot,
            run_opened=not membership_unchanged,
            written=True,
            files_written=tuple(written),
        )

    # -- coverage ---------------------------------------------------------

    def coverage(self, index: str) -> Coverage:
        """The date window recorded for an index, per the archive."""
        key = resolve_index(index).key
        state = self._load_index(key)
        runs = [r for r in state.get("runs", []) if r.get("from")]
        dates = [r["from"] for r in runs]
        ends = [r["to"] for r in runs if r.get("to")]
        # An open run is evidence only as far as the last retrieval, never to infinity.
        bounded = ends + ([str(state["last_observed"])] if state.get("last_observed") else [])
        latest = max(bounded + dates) if (bounded or dates) else None
        return Coverage(
            index_key=key,
            earliest=min(dates) if dates else None,
            latest=latest,
            snapshot_count=len({r["snapshot"] for r in runs if r.get("snapshot")}),
            run_count=len(runs),
        )

    def recorded_dates(self, index: str) -> list[str]:
        """Every date with a recorded snapshot for an index."""
        key = resolve_index(index).key
        runs = self._load_index(key).get("runs", [])
        return sorted({r["from"] for r in runs if r.get("from")})

    # -- reading ----------------------------------------------------------

    def _resolve_run(self, index_key: str, as_of: str) -> dict[str, Any]:
        """Find the run covering ``as_of``.

        **There is deliberately no fallback.** A miss raises. Returning the open run — the current
        membership — for a date it does not cover is precisely the survivorship substitution this
        module exists to prevent, so no such branch exists to be taken.
        """
        key = resolve_index(index_key).key
        state = self._load_index(key)
        runs = [r for r in state.get("runs", []) if r.get("from") and not r.get("superseded")]
        if not runs:
            raise HistoryUnavailableError(
                key,
                as_of,
                None,
                None,
                reason=(
                    "The archive has no recorded membership for this index. NSE publishes only the "
                    "current snapshot and overwrites membership in place, so nothing before the "
                    "first read can ever be recovered."
                ),
            )

        run = _run_covering(runs, as_of)
        last_observed = state.get("last_observed")

        if run is not None:
            # An open-ended run is only evidence up to the last date a retrieval actually
            # observed. Beyond that, nobody has looked, so there is no membership to report.
            if run.get("to") is None and last_observed and as_of > str(last_observed):
                coverage = self.coverage(key)
                raise HistoryUnavailableError(
                    key, as_of, coverage.earliest, coverage.latest,
                    reason=(
                        f"The newest membership observed for this index is from {last_observed}. "
                        f"{as_of} has not been observed, so reporting a membership for it would "
                        "be reporting the most recent known list as if it were a later one."
                    ),
                )
            return run

        coverage = self.coverage(key)
        if as_of < str(coverage.earliest):
            reason = (
                f"Accumulation began {coverage.earliest}; this archive cannot reach further back. "
                "NSE publishes only the current snapshot and overwrites membership in place, so "
                "membership for any earlier date was never published and cannot be reconstructed."
            )
        else:
            reason = (
                f"The newest membership recorded for this index is {coverage.latest}. A date after "
                "that has not been observed, so it has no membership to return."
            )
        raise HistoryUnavailableError(
            key, as_of, coverage.earliest, coverage.latest, reason=reason
        )

    def membership_asof(
        self,
        index: str,
        as_of: Any,
        *,
        retrieved_utc: Optional[str] = None,
    ) -> pd.DataFrame:
        """Membership of an index **as of** a recorded date.

        Raises :class:`HistoryUnavailableError` when the date is outside the archive's window. It
        never falls back to the current snapshot: the point of the module is that today's list
        cannot be laundered into a historical one.
        """
        key = resolve_index(index).key
        day = _coerce_date(as_of).isoformat()
        run = self._resolve_run(key, day)
        snapshot = self._load_snapshot(run["snapshot"])
        members = snapshot["members"]
        frame = pd.DataFrame(members, columns=list(MEMBERSHIP_COLUMNS))
        return _stamp_archived(
            frame,
            index=key,
            coverage=self.coverage(key),
            archive_root=str(self.root),
            as_of=day,
            observed_from=run["from"],
            retrieved_utc=retrieved_utc or run.get("first_seen_utc"),
        )

    def diff(self, index: str, older: Any, newer: Any) -> MembershipDiff:
        """Membership difference between two recorded dates, additions **and** removals.

        ``removed`` is the half a survivorship-blind consumer drops. A name that leaves the index
        is exactly the name a backtest must stop trading, and it is invisible if you only count
        what arrived.
        """
        key = resolve_index(index).key
        older_day = _coerce_date(older).isoformat()
        newer_day = _coerce_date(newer).isoformat()
        if newer_day < older_day:
            raise UniverseError(
                f"diff dates are reversed for {key!r}: newer={newer_day!r} precedes older={older_day!r}"
            )
        older_run = self._resolve_run(key, older_day)
        newer_run = self._resolve_run(key, newer_day)
        older_symbols = {m["symbol"] for m in self._load_snapshot(older_run["snapshot"])["members"]}
        newer_symbols = {m["symbol"] for m in self._load_snapshot(newer_run["snapshot"])["members"]}

        added = tuple(sorted(newer_symbols - older_symbols))
        removed = tuple(sorted(older_symbols - newer_symbols))
        unchanged = tuple(sorted(older_symbols & newer_symbols))
        return MembershipDiff(
            index_key=key,
            older_date=older_day,
            newer_date=newer_day,
            added=added,
            removed=removed,
            unchanged_symbols=unchanged,
            older_rows=len(older_symbols),
            newer_rows=len(newer_symbols),
            coverage=self.coverage(key),
        )

    def observed_changes(self, index: str) -> pd.DataFrame:
        """Changes **observed** between recorded snapshots.

        Labelled ``basis="observed_snapshot_diff"`` and ``is_scheduled=False`` on every row. NSE
        publishes no rebalance calendar on any reachable host, so this is the only honest form of
        the question: what changed between two retrievals this archive actually made. It is not a
        schedule, and callers must not use it as one.
        """
        key = resolve_index(index).key
        state = self._load_index(key)
        runs = [r for r in state.get("runs", []) if r.get("from") and not r.get("superseded")]
        runs.sort(key=lambda r: r["from"])
        rows: list[dict[str, Any]] = []
        for previous, current in zip(runs, runs[1:]):
            if previous["snapshot"] == current["snapshot"]:
                continue
            before = {m["symbol"] for m in self._load_snapshot(previous["snapshot"])["members"]}
            after = {m["symbol"] for m in self._load_snapshot(current["snapshot"])["members"]}
            added = sorted(after - before)
            removed = sorted(before - after)
            if not added and not removed:
                continue
            rows.append({
                "observed_on": current["from"],
                "since": previous["from"],
                "added": ", ".join(added),
                "removed": ", ".join(removed),
                "added_count": len(added),
                "removed_count": len(removed),
                "rows_before": len(before),
                "rows_after": len(after),
                "basis": "observed_snapshot_diff",
                "is_scheduled": False,
            })
        frame = pd.DataFrame(rows, columns=[
            "observed_on", "since", "added", "removed", "added_count", "removed_count",
            "rows_before", "rows_after", "basis", "is_scheduled",
        ])
        frame.attrs.update(_frame_attrs(
            index=key,
            basis="observed_snapshot_diff",
            coverage=self.coverage(key),
            archive_root=str(self.root),
        ))
        frame.attrs["bfinance_universe_is_scheduled"] = False
        frame.attrs["bfinance_universe_calendar_source"] = (
            "none: NSE publishes no rebalance calendar on any reachable host "
            "(nseindices.com does not resolve; no rebalancing CSV exists on nsearchives)"
        )
        return frame

    # -- live read --------------------------------------------------------

    def fetch(
        self,
        index: str,
        *,
        client: Optional[httpx.Client] = None,
        min_interval: float = MIN_REQUEST_INTERVAL,
        record: bool = True,
        retrieved_utc: Optional[str] = None,
        recorded_on: Optional[Any] = None,
    ) -> tuple[pd.DataFrame, Optional[RecordResult]]:
        """Read the current membership from NSE and record it.

        Returns ``(frame, record_result)``. ``record_result`` is ``None`` when ``record=False``.
        """
        spec = resolve_index(index)
        stamp = retrieved_utc or _now_utc()
        owned = client is None
        http = client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "bfinance/0.1 (+index membership recorder)"},
        )
        try:
            _pace(min_interval)
            response = http.get(spec.url)
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(f"GET {spec.url} failed: {exc}") from exc
        finally:
            if owned:
                http.close()

        if response.status_code == 404:
            raise UnsupportedIndexError(
                f"{spec.filename} returned 404. NSE's pattern is {FILENAME_PATTERN}; a name that "
                "does not match it 404s and is indistinguishable from a non-existent index. "
                f"Known-bad names: {sorted(KNOWN_404_FILENAMES)}."
            )
        if response.status_code >= 400:
            raise UpstreamServiceError(
                f"GET {spec.url} returned HTTP {response.status_code}"
            )

        frame = parse_constituents(
            response.text,
            index=spec.key,
            url=spec.url,
            retrieved_utc=stamp,
        )
        frame.attrs["bfinance_universe_filename"] = spec.filename
        frame.attrs["bfinance_universe_nominal_size"] = spec.nominal_size
        frame.attrs["bfinance_universe_size_from_data"] = len(frame)
        # Current membership is exactly the thing that must get archived, so a live read always
        # accumulates unless the caller explicitly opts out.
        result = (
            self.record(spec.key, frame, retrieved_utc=stamp, recorded_on=recorded_on)
            if record
            else None
        )
        coverage = self.coverage(spec.key)
        frame.attrs.update({
            "bfinance_universe_archive_earliest": coverage.earliest,
            "bfinance_universe_archive_latest": coverage.latest,
            "bfinance_universe_archive_snapshots": coverage.snapshot_count,
            "bfinance_universe_archive_runs": coverage.run_count,
            "bfinance_universe_archive_root": str(self.root),
            "bfinance_universe_recorded": result is not None,
        })
        return frame, result


# ------------------------------------------------------------------ helpers


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise UniverseError(
                f"expected an ISO date (YYYY-MM-DD), got {value!r}"
            ) from exc
    raise UniverseError(f"cannot interpret {value!r} as a date")


def _newest_effective_run(runs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The newest run still in effect, skipping retired ones."""
    for run in reversed(runs):
        if run.get("superseded"):
            continue
        if not run.get("to"):
            return run
    return None


def _run_covering(runs: list[dict[str, Any]], day: str) -> Optional[dict[str, Any]]:
    """The run whose ``[from, to]`` range contains ``day``. ``to=None`` means open-ended."""
    for run in runs:
        if run.get("superseded"):
            continue
        start = run.get("from")
        if not start or day < start:
            continue
        end = run.get("to")
        if end is None or day <= end:
            return run
    return None


def default_archive() -> UniverseArchive:
    """The archive at the default location."""
    return UniverseArchive()
