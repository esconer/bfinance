"""
Cache-aside for one symbol's price window: answer from the frame store, fetch only when it cannot.

**Why this module exists.** ``nse.bhavcopy.bhavcopy_range`` spends **one whole-market archive request
per calendar weekday of the span it is handed** -- it walks ``iter_trading_days`` and downloads one
file per day behind its 350 ms pacing floor -- and the price path remembered none of it. The plan this
module belongs to records ``Ticker("TCS").info`` at **184 s**, which is that walk over a 1y window:
261 candidate days on 2026-10-17 (``iter_trading_days("2025-10-17", "2026-10-16")`` -- a count that
drifts with the calendar, which is why nothing here hard-codes it), paid again in full on the very
next identical call. This module is the seam that makes the second call free: read
:class:`~bfinance.cache.frame_store.FrameStore` -- the one cache in this package that round-trips
``attrs`` -- and walk the archive only when the store cannot answer.

**What this module is not.** It owns no storage policy. Every rule about *what may be kept* -- never a
window that contains today, never a failure shape, never a null, never a 404 -- is
:mod:`bfinance.cache.frame_store`'s, already implemented and already tested there; this module relies
on it instead of restating it, and never caches anything on its own authority. It has no TTL constant
and no pacing constant of any kind. It never fabricates an ``Open``, ``High`` or ``Low``, never
resamples a bar and never back-fills a short window from another price basis: the frame that comes
out is the frame the fetcher produced (or a stored copy of one), labelled with where its data ends,
never extended.

**The decision table.** One call to :meth:`PriceCache.get_or_fetch`, and what comes back:

===========================  ==========================================================================
lookup answers               what the wrapper does, and what the returned frame publishes
===========================  ==========================================================================
``"hit"``, gap explained     returns a ``.copy()`` of the stored frame, status ``"hit"``, with
                             ``fetched_at``/``coverage_end`` from the row it was served from
``"hit"``, gap unexplained   **does not serve it.** Fetches live, status ``"miss"``, and the gaps are
                             named in :data:`CACHE_UNCOVERED_DAYS_ATTR` before the frame is returned
``"miss"``                   fetches live, offers the frame to the store, returns it with status
                             ``"miss"`` -- the data came over the wire, so that is what it says
``"null"``/``"expired"``     same as ``"miss"``: nothing usable was behind the key
``"corrupt"``                same as ``"miss"``; the store has already logged which row refused
``"disabled"``               the *store* is switched off: fetches live and publishes status
                             ``"disabled"``. No write is attempted against a store that has said it is
                             not storing
wrapper ``enabled=False``    the store is never opened, read or written at all: fetches live and
                             publishes status ``"disabled"``
a failed fetcher             the failure propagates. Nothing is cached, and no frame is invented
===========================  ==========================================================================

**The liveness rule, which is what the published attrs are for.** A cache hit is a *replay*, and a
replay of a frame fetched at 14:00 on a Tuesday that silently ends the previous Monday reads exactly
like "NSE is open and TCS has not traded" -- a liveness claim about the exchange that nothing in this
package has measured. Three published keys make that reading impossible:

* :data:`CACHE_STATUS_ATTR` says where the frame came from: ``"hit"`` from the store, ``"miss"`` from a
  live fetch, ``"disabled"`` when the caching layer was not in play. The names are
  :class:`~bfinance.cache.sqlite_cache.CacheLookup`'s own; a cached frame is never indistinguishable
  from a live one, and no path returns a frame with no status at all.
* :data:`CACHE_COVERAGE_END_ATTR` and :data:`CACHE_REQUESTED_END_ATTR` are published as a pair and are
  allowed to differ, so "the data ends here" is never confused with "I asked for it to end here".
* :data:`CACHE_UNCOVERED_DAYS_ATTR` lists the candidate trading days of the requested window that the
  frame does not contain -- present on every return, empty or not, so a consumer reads one key without
  branching. It is not ``bfinance_bhavcopy_missing_dates``: that key records days NSE published no
  archive for, which is a statement about the exchange, while this one records days this frame simply
  does not reach, which is a statement about the frame. The second is what a replay cannot fake.

**A request past the cached coverage is never answered from the cache silently.** The window is looked
up whole -- ``frame_key`` names the symbol, the exchange, the series and *both* resolved ends, so a
wider request is a different key and cannot be served by a narrower row -- and a hit is served only
when the days between the frame's coverage end and the requested end are accounted for: they are
either non-trading days (weekends are not gaps; treating them as gaps would make every window ending
on a Sunday look truncated) or they are named in the frame's own uncovered-days record. A row that
stops early with nothing saying so is *refetched*, never replayed, which is the one failure mode this
module exists to make impossible: a clean short frame is indistinguishable from a complete one, and
the caller has no second place to look.

The check is on the **end** only. A bar missing in the middle of the window is not a shortfall: the
archive covered that day and the security had no bar, which is an observation -- a suspension reads
exactly like this -- rather than an unknown. A day after the frame's last bar is the opposite case: it
is a day nobody has looked at, and reporting it as "the symbol did not trade" would be the invented
liveness claim above.

**Two callers never hold one frame.** A hit returns ``.copy()`` of what the store served, in the same
spirit as the store's own read boundary and for the measured reason it states: a shared frame leaks
the bars *and* the containers in its attrs (verified again here -- a mutation test appends to
``bfinance_price_source_attempts`` and to the uncovered-days list and requires the next read to be
untouched). A live frame is handed over as it came, because the fetcher created it for this call and
the write makes its own snapshot; copying it would only double the memory of the whole-market-sized
frame on the way past.

**The seam.** The fetcher is a parameter, not an import: a zero-argument callable already bound to the
symbol and the window, which is what makes this wrapper testable offline and keeps it from importing
the archive reader, the mirror or the cascade. The caller binds the window into it; the wrapper binds
it to the store key it looked up. ``series`` is passed through to the store verbatim, because
``frame_key`` is the one place the key is built and it does not fold a list of series into a tuple --
two spellings of one request would be two rows and two walks.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import pandas as pd

# The vocabulary, the formats, the store and the calendar are imported rather than restated.
# `frame_key` is where a key is built, so this module never builds one; the four attrs constants are
# what a consumer greps for, so a second spelling of them here would be the drift they exist to
# prevent; and "which days cost a request" has exactly one definition in this package
# (`bhavcopy.py`'s walk calls this function), so a second weekend rule here would drift from it the
# moment that one grows a holiday calendar.
from bfinance.cache.frame_store import (
    CACHE_COVERAGE_END_ATTR,
    CACHE_FETCHED_ATTR,
    CACHE_REQUESTED_END_ATTR,
    CACHE_STATUS_ATTR,
    DISABLED_STATUS,
    IST,
    FrameStore,
)
from bfinance.nse.bhavcopy import iter_trading_days

logger = logging.getLogger("bfinance")

#: The fifth published key, and the only attr this module names itself: the candidate trading days of
#: the requested window that the frame does not hold. Written on the frame *before* it is offered to
#: the store, so it is part of the stored payload -- which is what lets a later read tell an explained
#: short frame from a silent one (see the liveness rule above).
CACHE_UNCOVERED_DAYS_ATTR = "bfinance_cache_uncovered_days"


# ------------------------------------------------------------------------------ the small pieces


def _iso_date(value: Any) -> str:
    """One date as ``YYYY-MM-DD``, or ``ValueError``. ``NaT`` is refused rather than spelled.

    The same refusal, for the same reason, as the store's own key components: ``pd.Timestamp(None)``
    is ``NaT``, and a window end spelled ``"NaT"`` is a value that compares as a string and means
    nothing -- worse here than there, because this spelling lands in an attrs *value* a consumer
    branches on.
    """
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError(f"{value!r} is not a date: a window end cannot be spelled from NaT")
    return stamp.date().isoformat()


def _now_iso() -> str:
    """Now, as the IST instant it is -- the ``fetched_at`` a live frame publishes.

    IST and not UTC because the boundary every rule in this pair of modules is judged against is
    NSE's publish date, and a stamp that crossed midnight five and a half hours early would date a
    live fetch to the wrong day.
    """
    return pd.Timestamp.now(tz=IST).isoformat()


def _frame_coverage_end(frame: pd.DataFrame) -> Optional[str]:
    """How far the frame's own data reaches, or ``None`` when it holds no rows.

    ``index.max()`` rather than the requested end, for the reason the store states when it stores the
    same value: only the index answers "does the data end here, or did I merely ask it to". An empty
    frame has covered nothing at all, and ``None`` is that fact -- publishing the window's end for it
    would be the one thing this module must never do.

    A non-``DatetimeIndex`` raises rather than being coerced. ``FrameStore.write`` refuses a
    ``(Date, Symbol)`` whole-market cross-section under one symbol's key, and this function reads the
    same index to decide where the coverage ends; a tuple from ``MultiIndex.max()`` reaching
    ``pd.Timestamp`` would surface as a conversion error far from the frame that caused it.
    """
    if len(frame) == 0:
        return None
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError(
            f"a price frame's index must be a DatetimeIndex, not {type(frame.index).__name__}: this "
            f"caches one symbol's window, so a whole-market (Date, Symbol) cross-section has to be "
            f"reduced first -- FrameStore.write refuses it for the same reason."
        )
    return _iso_date(frame.index.max())


def _uncovered_days(*, start: Any, coverage_end: Optional[str], requested_end: str) -> list:
    """The window's candidate trading days that a frame covered only to ``coverage_end`` lacks.

    The candidate days are :func:`iter_trading_days`' Mon-Fri list -- the same list the archive walk
    spends requests on -- so weekends are not uncovered days: they are not trading days, and recording
    them would make every window that ends on a Sunday look short. Exchange holidays *are* in the list
    (the calendar is not bundled), which is why the fetch layer's own missing-dates record is a subset
    of this one and why this list is recomputed rather than read back.

    ``coverage_end=None`` means the frame holds no rows, so the whole window is uncovered.
    """
    if coverage_end is not None and _iso_date(coverage_end) >= _iso_date(requested_end):
        return []
    first = start if coverage_end is None else pd.Timestamp(coverage_end) + pd.Timedelta(days=1)
    return [day.isoformat() for day in iter_trading_days(first, requested_end)]


def _recorded_uncovered(frame: pd.DataFrame) -> set:
    """The uncovered days the frame itself records, as ISO strings.

    A missing, unreadable or hand-edited value counts as **no record at all**, which is the direction
    that costs a refetch rather than serving a truncated window: the caller of this function uses it to
    decide whether a short frame may be replayed, and "I could not read the record" must not read as
    "there was nothing missing".
    """
    recorded = frame.attrs.get(CACHE_UNCOVERED_DAYS_ATTR)
    if not recorded or isinstance(recorded, (str, bytes)):
        return set()
    try:
        return {_iso_date(value) for value in recorded}
    except (TypeError, ValueError):
        logger.warning(
            "Price cache: %s is not a list of dates on the frame being read; treating the frame as "
            "having no record of its shortfall and refetching",
            CACHE_UNCOVERED_DAYS_ATTR,
        )
        return set()


def _unexplained_days(
    frame: pd.DataFrame, *, start: Any, coverage_end: Optional[str], requested_end: str
) -> list:
    """The uncovered days the frame does *not* say anything about, in order.

    Empty means the frame's shortfall, if it has one, is fully accounted for and the frame may be
    replayed. Non-empty means the frame stops early with nothing on it that says why, which is the
    silent-truncation failure mode: the caller of this function must fetch instead of serving.
    """
    recorded = _recorded_uncovered(frame)
    return [
        day
        for day in _uncovered_days(
            start=start, coverage_end=coverage_end, requested_end=requested_end
        )
        if day not in recorded
    ]


def _fetched(fetch: Callable[[], Any]) -> pd.DataFrame:
    """Call the fetcher once and hand back a frame -- or raise, without swallowing anything.

    Nothing is caught here on purpose. A fetcher that raises is a fetch that failed, and the only
    honest answers to that are the exception itself (so the caller's typed error survives) or an
    explicit refusal; an empty frame invented here would be cached as "this window is empty" and would
    look, for the life of the row, exactly like a symbol that does not trade.

    The same is true of a fetcher that returns something that is not a frame at all -- ``None``, an
    HTTP status, a response body. Those are how "there is no frame" arrives from a caller, and this is
    the last place where refusing them is cheap: past here the coverage is read off the index and the
    frame is offered to the store.
    """
    frame = fetch()
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(
            f"the fetcher answered {type(frame).__name__!r}, which is not a DataFrame. A failure, a "
            f"null and a 404 have no frame; returning one for them would cache 'this symbol has "
            f"nothing' for the life of the row, so the answer is refused before anything is written."
        )
    return frame


def _isolated(frame: pd.DataFrame) -> pd.DataFrame:
    """The copy boundary of a replay, named so that it can be pointed at.

    The store already copies on its way out (``frame_store._isolated``), so today this call is the
    second lock on one door. It is kept, and it is the wrapper's own, because the guarantee belongs to
    whoever hands the frame out: a loader that memoised, or an in-process tier above the store, would
    put one object in two callers' hands without this line -- and the damage is silent in both
    directions. Measured on pandas 3.0.5: ``frame.iloc[0, 0] = -999.0`` and an append to a list inside
    ``frame.attrs`` are both visible through the second reference.
    """
    return frame.copy()


def _publish(
    frame: pd.DataFrame,
    status: str,
    *,
    fetched_at: str,
    coverage_end: Optional[str],
    requested_end: str,
    uncovered: list,
) -> pd.DataFrame:
    """Put the five observation attrs on the frame that is about to be returned.

    ``update``-shaped, never a replacement: the frame arrives carrying the fetch layer's provenance --
    ``bfinance_provenance``, ``price_basis``, the source-attempt ledger, the mirror record -- and every
    one of those has to survive a trip through a cache, which is the entire reason this package stores
    frames as pickles. A dict rebuilt from these five keys would erase all of it and leave a frame that
    looks complete and cannot say where its numbers came from.

    ``uncovered`` is copied into a fresh list, so that a caller mutating what it was handed is not
    mutating what the next read computes.
    """
    frame.attrs[CACHE_STATUS_ATTR] = status
    frame.attrs[CACHE_FETCHED_ATTR] = fetched_at
    frame.attrs[CACHE_COVERAGE_END_ATTR] = coverage_end
    frame.attrs[CACHE_REQUESTED_END_ATTR] = requested_end
    frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] = list(uncovered)
    return frame


def _served_frame(frame: pd.DataFrame, *, start: Any, end: str) -> pd.DataFrame:
    """A frame that came out of the store, isolated and labelled as the replay it is.

    ``status`` is ``"hit"`` -- and it is the store's own status rather than a constant typed here,
    because the store is what observed the row. ``fetched_at`` is read off the frame, where the store
    published the instant the *row* was written: a replay that stamped "now" would claim the data was
    just fetched, which is the same lie as a live frame claiming to be cached.

    A store-like object that answers a hit without saying when the row was fetched produces a frame
    that is **undated** -- ``None``, with the key still published -- rather than one dated "now". The
    shipped :class:`~bfinance.cache.frame_store.FrameStore` always publishes the instant it read off
    the row, so this is about the injection point: ``PriceCache(store=...)`` takes any object with a
    ``read``, and the one thing this function must never do on the way past is invent a fetch time for
    data it did not fetch. "I do not know when this was fetched" is honest and visible; "now" is
    neither.
    """
    served = _isolated(frame)
    coverage_end = served.attrs.get(CACHE_COVERAGE_END_ATTR)
    return _publish(
        served,
        "hit",
        fetched_at=served.attrs.get(CACHE_FETCHED_ATTR),
        coverage_end=coverage_end,
        requested_end=end,
        uncovered=_uncovered_days(start=start, coverage_end=coverage_end, requested_end=end),
    )


def _live_frame(
    fetch: Callable[[], Any],
    *,
    status: str,
    start: Any,
    end: str,
    observed: Any = None,
) -> pd.DataFrame:
    """One live fetch, labelled with the truth about it -- and about how far its data reaches.

    ``observed`` lets a fetch layer that already computed a watermark state it (the store takes the
    same argument for the same reason: the frames a fetch layer holds are not always the whole story
    of what it fetched). ``None`` reads the frame's own index.

    The status is the caller's, because only the caller knows whether the caching layer was in play:
    ``"miss"`` means the store was consulted and had nothing (or had something it must not serve), and
    ``"disabled"`` means there was no cache decision to report at all.
    """
    frame = _fetched(fetch)
    coverage_end = _iso_date(observed) if observed is not None else _frame_coverage_end(frame)
    return _publish(
        frame,
        status,
        fetched_at=_now_iso(),
        coverage_end=coverage_end,
        requested_end=end,
        uncovered=_uncovered_days(start=start, coverage_end=coverage_end, requested_end=end),
    )


# ------------------------------------------------------------------------------------ the wrapper


class PriceCache:
    """One symbol's window, answered from :class:`FrameStore` when the store can answer it.

    The module docstring carries the decision table, the liveness rule and the two boundaries; what
    belongs here is the two things a caller has to know before using it:

    * **the fetcher is the only thing this class does not own.** It is called at most once per
      :meth:`get_or_fetch`, only when the store cannot serve the window, and whatever it raises or
      answers is passed through rather than repaired.
    * **``enabled=False`` means the cache does not exist.** Not "it is read but not written": no
      lookup, no write, and no store is even built -- so a caller who has been told to take the live
      path cannot be served a stale frame by a defaulted store on the way past, and a cache that was
      switched off leaves no file behind (the store's own rule for its ``enabled`` flag, applied one
      layer up).
    """

    def __init__(
        self,
        store: Optional[FrameStore] = None,
        enabled: bool = True,
    ) -> None:
        """Open the wrapper around one store.

        Args:
            store: the frame store to read and write. ``None`` takes
                :class:`~bfinance.cache.frame_store.FrameStore`'s default file, created on first use
                rather than here -- so a wrapper that is switched off, or a caller who injects a store
                for a test, never touches ``~/.bfinance``.
            enabled: ``False`` makes every call a live fetch that publishes
                :data:`~bfinance.cache.frame_store.DISABLED_STATUS`.
        """
        self._store = store
        self.enabled = bool(enabled)

    @property
    def store(self) -> FrameStore:
        """The store, built on first use (see :meth:`__init__`)."""
        if self._store is None:
            self._store = FrameStore()
        return self._store

    def get_or_fetch(
        self,
        fetch: Callable[[], Any],
        *,
        symbol: str,
        exchange: str,
        series: Optional[str] = None,
        start: Any,
        end: Any,
        observed_coverage_end: Any = None,
    ) -> pd.DataFrame:
        """The window, from the store if it may be served from there, else live.

        Args:
            fetch: zero-argument callable that fetches this symbol's window, already bound to it.
            symbol, exchange, series, start, end: the **resolved** window, exactly as
                :meth:`FrameStore.read` takes it -- the same five components the key is built from.
            observed_coverage_end: how far the fetched data reaches, when the fetcher knows better
                than the frame's own index does. Ignored on a replay: a stored row already states the
                coverage it was stored with.

        Returns:
            A frame carrying :data:`CACHE_STATUS_ATTR`, :data:`CACHE_FETCHED_ATTR`,
            :data:`CACHE_COVERAGE_END_ATTR`, :data:`CACHE_REQUESTED_END_ATTR` and
            :data:`CACHE_UNCOVERED_DAYS_ATTR`, and the fetch layer's own attrs untouched. The bars are
            whatever the fetch layer produced -- this method adds labels and nothing else.

        Raises:
            Whatever the fetcher raises: a failed fetch is the caller's error to see, and caching it
            is not an option. ``TypeError`` when the fetcher answers with something that is not a
            frame, since a null, an HTTP status and a 404 have no frame to label.
        """
        requested_end = _iso_date(end)

        if not self.enabled:
            logger.debug(
                "Price cache: caching is switched off; %s is fetched live through %s",
                symbol,
                requested_end,
            )
            return _live_frame(
                fetch,
                status=DISABLED_STATUS,
                start=start,
                end=requested_end,
                observed=observed_coverage_end,
            )

        lookup = self.store.read(
            symbol=symbol, exchange=exchange, series=series, start=start, end=end
        )

        if lookup.status == "hit":
            stored = lookup.value
            coverage_end = stored.attrs.get(CACHE_COVERAGE_END_ATTR)
            unexplained = _unexplained_days(
                stored, start=start, coverage_end=coverage_end, requested_end=requested_end
            )
            if not unexplained:
                return _served_frame(stored, start=start, end=requested_end)
            logger.warning(
                "Price cache: not serving %s %s..%s from the cache -- %d candidate trading day(s) "
                "(%s..%s) the stored frame does not reach are not recorded as uncovered, so "
                "replaying it would return a short frame that looks complete. Fetching live.",
                symbol,
                _iso_date(start),
                requested_end,
                len(unexplained),
                unexplained[0],
                unexplained[-1],
            )
        elif lookup.status != DISABLED_STATUS:
            # "miss", and the two names a store may answer with that mean "nothing usable is behind
            # this key" -- never served, and never invented here. The store logs its own why.
            logger.debug(
                "Price cache: %s %s is a cache %s; fetching live",
                symbol,
                requested_end,
                lookup.status,
            )

        # The store is switched off, so the frame is live and the caching layer was not in play: a
        # "miss" would claim the store was asked and had nothing, which is not what happened.
        status = DISABLED_STATUS if lookup.status == DISABLED_STATUS else "miss"
        frame = _live_frame(
            fetch,
            status=status,
            start=start,
            end=requested_end,
            observed=observed_coverage_end,
        )
        self._remember(frame, symbol=symbol, exchange=exchange, series=series, start=start, end=end)
        return frame

    # ------------------------------------------------------------------------------- the write

    def _remember(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str,
        exchange: str,
        series: Optional[str],
        start: Any,
        end: Any,
    ) -> None:
        """Offer the frame to the store, and let the store's own answer be the record.

        The frame is stamped before it gets here, so the stored payload carries both the coverage it
        was stored with and the uncovered days it was stored against -- which is what a later read
        needs in order to tell an explained short window from a silent one. The coverage is passed as
        the store's ``observed_coverage_end`` so that the row's column and the frame's attr are the
        same string by construction rather than two computations that agree today.

        A store that declines is not an error and not this method's news to break: the store logs
        which of its refusals fired (an open window at DEBUG, a failure shape at WARNING), and the
        caller already has a correct frame. What is logged here is only the consequence a caller
        cannot see from the frame -- that the next identical call will fetch again.
        """
        written = self.store.write(
            frame,
            symbol=symbol,
            exchange=exchange,
            series=series,
            start=start,
            end=end,
            observed_coverage_end=frame.attrs.get(CACHE_COVERAGE_END_ATTR),
        )
        if written != "hit":
            logger.debug(
                "Price cache: %s %s was not stored (%s); the next identical call will fetch again",
                symbol,
                _iso_date(end),
                written,
            )
