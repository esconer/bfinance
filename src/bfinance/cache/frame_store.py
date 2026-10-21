"""
Pickle-backed storage for price frames: the one cache in this package that round-trips ``attrs``.

**Why this module exists.** :class:`~bfinance.cache.sqlite_cache.SQLiteCache` cannot hold a price
frame, and it does not say so out loud. Measured on this machine, 2026-10-17, against the shipped
schema::

    c.set("ohlcv_TCS_1y", pickle.dumps(df))  ->  WARNING "Skipping cache set: unserializable
                                                 payload for key ohlcv_TCS_1y"
    c.get("ohlcv_TCS_1y")                    ->  None
    SELECT COUNT(*) FROM bfinance_cache      ->  0

Two independent refusals, either of which is enough on its own: :meth:`SQLiteCache.set`
JSON-encodes its payload with ``allow_nan=False`` (``sqlite_cache.py:218``), and the column it would
write into is ``payload TEXT NOT NULL`` (``sqlite_cache.py:130``). The ``TypeError`` that comes back
is caught at ``sqlite_cache.py:229-231``, logged at WARNING and returned from -- no exception, no
row, no counter. A caller that asked the cache to remember a frame therefore has no way to learn
that it did not. That failure mode is why this module exists as a separate store rather than as a
change to ``set``: making ``set`` raise breaks every caller whose payload is not a frame, and making
it succeed silently hides the fact that the row cannot be read back.

**JSON is the wrong format for a price frame even where a row could hold it.** Measured:
``pd.read_json(df.to_json()).attrs == {}`` while ``pickle.loads(pickle.dumps(df)).attrs ==
df.attrs`` with the ``DatetimeIndex`` intact. The attrs are not decoration -- the price layer
publishes the winning vendor, every source considered (``bfinance_price_source_attempts``, a list),
the mirror record (``bfinance_mirror``, a dict) and the basis the numbers are stated in -- so a cache
that returns the bars with the provenance erased turns a labelled frame into an unlabelled one, and
"was this as traded or share-count adjusted" stops being answerable from the frame itself.

**This store owns TTL; the memo layer owns freshness** (plan R6 -- one rule, stated once, so the two
Wave-3 workers cannot ship incompatible policies). In practice the TTL policy is one *refusal*
rather than a duration:

* A window whose last day is **strictly before today** (IST) is immutable -- the NSE archive file for
  a day strictly before today is published and never rewritten -- so its row never expires. There is
  deliberately no ``expires_at`` column to tune, because this store's two cases are "never" and "not
  applicable", and a duration nobody has measured would be a third policy invented on the way past.
* A window that **contains today** is neither stored nor served; today's archive does not exist yet,
  and the same rule is re-applied on the read, against the row's *stored* fetch date rather than a
  fresh clock, so that a row written by an older build or edited in by hand cannot cross a publish
  boundary either -- not today, and not tomorrow either, when the window would look closed
  (:meth:`FrameStore._may_serve`).

Freshness -- "is this frame recent enough for what the caller is doing" -- is the memo layer's
question. What it needs from here is the window, the fetch time and the coverage end, and those are
published on every served frame (:data:`CACHE_FETCHED_ATTR`, :data:`CACHE_COVERAGE_END_ATTR`,
:data:`CACHE_REQUESTED_END_ATTR`).

**The threat model, stated because it cannot be stated anywhere else.** Reads go through
``pickle.loads``, so the file this store opens is a **deserialisation surface**: a row this process
did not write can execute code when it is read. That is the accepted cost of preserving ``attrs``
(no other format in the standard library does), and it fixes what the file must never be:

* **user-owned, and never world-writable.** It lives under ``~/.bfinance`` (or ``$BFINANCE_HOME``)
  for that reason, exactly as the other two SQLite artefacts this package writes do.
* never a place to put data that came from somewhere other than this process -- not a shared mount,
  not an exported-then-imported cache, not something a user was asked to download;
* never read with ``pickle.loads`` from anywhere except the ``payload`` column of this module's own
  table (:meth:`FrameStore.read` is the only call site).

**A row the fetch date cannot vouch for is checked against its key, not just against the clock.**
:meth:`FrameStore._may_serve` refuses a row whose *window* was still open when the row was written,
which is what the fetch date is compared for -- but the date says nothing about what the payload
*holds*, and a foreign row is exactly the case that must not be trusted on its word. So
:meth:`FrameStore.read` also refuses a row whose bars do not lie inside the window the key names
(:func:`_bars_lie_inside_the_window`), as ``"corrupt"``, which every consumer already answers by
refetching: a miss costs a fetch, a wrong hit costs correctness. The refusal is the whole row or
nothing: a stored frame is served exactly as it was stored, or not at all, and the part of a
mis-filed one that happens to fit the window is never the part that is handed over. That is this
module's own rule, enforced at :meth:`FrameStore.read` (:func:`_bars_lie_inside_the_window`), and it
is now the only copy of it -- see that function for where the guard lives and what became of the
second one.

A pickle is also version-coupled: a row written by a different pandas major may not load in this one
at all, or may load into an object of the wrong type. Both are reported as ``"corrupt"`` and
refetched, never guessed at (:func:`_frame_from_blob`).

**The vocabulary is the existing one.** :class:`~bfinance.cache.sqlite_cache.CacheLookup` names
every status this store reports: ``"hit"``, ``"miss"``, ``"corrupt"`` (any read that could not be
trusted, including I/O-level failures, which is what the class's own docstring says the name covers)
and ``"disabled"`` (documented at ``ownership.py:1288``, where "no request was made" has to be
visible on the result rather than inferred from a log line). Two of the five cannot occur here:
``"null"``, because a null is never stored (:func:`_refusal`), and ``"expired"``, because no window
this store accepts can expire (the TTL policy above).
"""

from __future__ import annotations

import logging
import os
import pickle
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

import pandas as pd

# The connect helper and the process-wide lock are imported rather than copied. The three SQLite
# artefacts in `~/.bfinance` (the screener cache, the mirror, this store) need the same WAL and
# busy_timeout settings on any filesystem that will take them, and a second copy of that reasoning
# (`sqlite_cache.py:51-77`) is exactly the drift the original's docstring warns about. The lock is
# shared for the same reason it exists: two connections to one database in one process serialise
# better under a lock than under busy_timeout alone.
from .sqlite_cache import _CACHE_LOCK, CacheLookup, _connect

logger = logging.getLogger("bfinance")

#: The exchange calendar every date in this module is judged against. NSE's publish boundary is
#: midnight IST, and a store that used UTC would decide "today" wrong for the five and a half hours
#: of every day in which the current session's bhavcopy is being published.
IST = "Asia/Kolkata"

#: The four attrs keys a served frame carries. Module constants, so a consumer, a test and this
#: module spell them identically -- the convention ``ohlcv.py:757-762`` states for the four
#: price-source keys, for the same reason: a rename becomes one edit the compiler then enforces,
#: instead of a search for a string nobody can grep for.
CACHE_STATUS_ATTR = "bfinance_cache_status"
CACHE_FETCHED_ATTR = "bfinance_cache_fetched_at"
CACHE_COVERAGE_END_ATTR = "bfinance_cache_coverage_end"
CACHE_REQUESTED_END_ATTR = "bfinance_cache_requested_end"

#: What a read answers when the store is switched off. ``ownership.py:1288`` documents this name
#: next to the :class:`CacheLookup` statuses: "no request was made" is a different fact from "the
#: lookup ran and found nothing", and a caller that cannot tell them apart cannot report either.
DISABLED_STATUS = "disabled"

#: The one table this module owns, in the one file :func:`default_frame_store_path` names. The key
#: carries all five components in the open (``SYMBOL|EXCHANGE|SERIES|START|END``); the rest of the
#: row is what the key cannot say -- how far the stored data actually reaches, how big it was, and
#: when it was fetched. ``payload`` is ``BLOB`` rather than ``TEXT`` for the measured reason at the
#: top of this file.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bfinance_price_frames (
    frame_key    TEXT PRIMARY KEY,
    coverage_end TEXT NOT NULL,
    n_rows       INTEGER NOT NULL,
    fetched_at   REAL NOT NULL,
    payload      BLOB NOT NULL
);
"""


def default_frame_store_path() -> Path:
    """``~/.bfinance/frames.db``, or ``$BFINANCE_HOME/frames.db`` when that is set.

    **Its own file, not a second table in ``cache.db``,** and that is a measurement rather than a
    preference. The payload is a pickle: as ``bytes`` it can never satisfy ``payload TEXT NOT NULL``,
    and as text it would be a latin-1 round trip of the same bytes through ``json.dumps`` -- which
    does work (measured: ``pickle.loads(blob.decode("latin-1").encode("latin-1")).attrs`` survives)
    and is still the wrong place for it, because the two stores then share a writer whose documented
    answer to an unrepresentable payload is to log and return. A file that one module owns end to
    end cannot inherit that silence.

    ``sqlite_cache.py:109-116`` places its file at ``~/.bfinance/cache.db`` and does not consult
    ``BFINANCE_HOME``; ``mirror.py:102-105`` is the precedent followed here for a *second* artefact
    in that home, including the ``$BFINANCE_HOME`` override, so a user who moved their bfinance data
    has this store moved with it.
    """
    home = Path(os.environ.get("BFINANCE_HOME") or (Path.home() / ".bfinance"))
    return home / "frames.db"


def _now() -> pd.Timestamp:
    """The real clock, in IST. The seam is :class:`FrameStore`'s ``clock`` argument."""
    return pd.Timestamp.now(tz=IST)


def _component(value: Any, name: str) -> str:
    """One identity component of the key: stripped, upper-cased, and non-empty.

    Case and surrounding whitespace are normalised because ``tcs`` and ``TCS`` are the same symbol,
    and two keys for them would be two downloads of 262 files. Nothing else is: ``TCS.NS`` is
    **not** folded into ``TCS``, because the suffix is a spelling yfinance uses for a symbol whose
    exchange this key already carries separately, and guessing at it can only turn a miss into a
    wrong hit. A miss costs a refetch; a wrong hit costs correctness.
    """
    text = str(value).strip().upper() if value is not None else ""
    if not text:
        raise ValueError(
            f"{name} is empty: a frame key names one symbol, one exchange and one series"
        )
    return text


def _date_iso(value: Any) -> str:
    """One window component as ``YYYY-MM-DD``, or ``ValueError``.

    The key is built from the **resolved** window -- the dates a ``period`` or a default resolved to
    -- while the requested spelling (``"1y"``, ``None``) never reaches this module. ``NaT`` is
    refused rather than serialised: ``pd.Timestamp(None)`` is ``NaT``, and a key component spelled
    ``"NaT"`` is a row nobody can ever find again, including the caller who just wrote it.
    """
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError(f"{value!r} is not a date: a window key cannot be built from NaT")
    return stamp.date().isoformat()


def frame_key(
    symbol: str,
    exchange: str,
    series: Optional[str],
    start: Any,
    end: Any,
) -> str:
    """The key, built in exactly one place: ``SYMBOL|EXCHANGE|SERIES|START|END``.

    Five components because five things make two frames different data: the security, the venue, the
    series, and the two ends of the window the caller asked for. ``|`` is the separator because no
    NSE symbol, series or exchange code contains one, and open spelling rather than a hash because a
    key that a human can read out of ``sqlite3`` output is a cache that can be debugged.

    ``series`` may be empty or ``None``, the one component a caller can honestly not have: a plain
    ``history()`` call has no series in it, and it is spelled ``"ALL"`` here so that two callers who
    both mean "whichever series served" do not write two rows for one frame. The symbol is taken as
    given otherwise -- see :func:`_component`.

    Passing the *resolved* window is the caller's contract, and it is what makes the key safe: a
    ``1y`` request resolved yesterday and the same request resolved today are different windows and
    must not share a row, because the second one contains a day the first cannot.
    """
    return "|".join(
        (
            _component(symbol, "symbol"),
            _component(exchange, "exchange"),
            _component(series, "series") if series else "ALL",
            _date_iso(start),
            _date_iso(end),
        )
    )


def _payload_blob(frame: pd.DataFrame) -> bytes:
    """The stored form of a frame: ``pickle``, at the interpreter's own highest protocol.

    The protocol is not the compatibility floor: a pickle is read back by the same interpreter
    family that wrote it, out of a file the user owns, and :data:`pickle.HIGHEST_PROTOCOL` is what
    keeps a 262-row range frame small enough to be worth writing.
    """
    return pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL)


def _frame_from_blob(blob: bytes) -> pd.DataFrame:
    """The inverse, with the payload's *shape* asserted rather than assumed.

    ``pickle.loads`` returns whatever was pickled. A row written by another pandas major, or edited
    by hand, can therefore load into something that is not a frame at all -- and ``isinstance`` is
    the whole check, because everything downstream (``.copy()``, ``.attrs``, the four published
    keys) needs a ``DataFrame`` and would otherwise fail one attribute at a time, in the middle of a
    caller's code, far from the row that caused it.
    """
    frame = pickle.loads(blob)
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"the stored payload is a {type(frame).__name__}, not a DataFrame")
    return frame


def _refusal(frame: pd.DataFrame) -> Optional[str]:
    """Why this frame must not be stored, or ``None`` when it may be.

    Three shapes are detectable here, and each one is a shape a *failed* fetch really takes rather
    than a hypothetical:

    * **no rows.** :func:`bfinance.sources.cascade.is_empty_payload` already defines an empty frame
      as a failure rather than as data, and ``ohlcv`` refuses such a frame again on its own terms.
      Storing one would cache "this symbol has nothing" for a window whose rows may simply not have
      been published yet.
    * **every value NaN.** ``OHLCVEngine.download`` answers a ticker that failed with an all-NaN
      column rather than dropping it -- deliberately, so a failed ticker still appears -- and that
      column, cached against a closed window, is indistinguishable for the life of the row from a
      security that never trades. It is the same latch as caching a 404.
    * **no columns at all.** Not a price frame under any reading.

    A 404 is *not* detectable here, and that is not a gap in the check: an HTTP status never arrives
    as a frame, so the contract on the caller is that a raise, a timeout or a non-200 response is
    never handed to :meth:`FrameStore.write`. What this function adds is that the failure shapes a
    caller can hand over by accident are refused here too, instead of being trusted.
    """
    if len(frame) == 0:
        return "it has no rows, and an empty frame is how a failed fetch is spelled"
    if len(frame.columns) == 0:
        return "it has no columns"
    if bool(frame.isna().all().all()):
        return (
            "every value is NaN, which is the shape a failed download takes: caching it would "
            "latch 'this symbol never trades' for the life of the row"
        )
    return None


def _coverage_end(frame: pd.DataFrame, observed: Any = None) -> str:
    """How far this frame's data actually reaches: ``observed`` when the caller knows better, else
    read off the frame's own index.

    ``index.max()`` rather than ``len(index)`` or the window's end: the question the stored value has
    to answer is "does the data end here, or did I merely ask for it to end here", and only the
    index answers that. The index must be a ``DatetimeIndex`` -- a price frame is indexed by dates,
    which is what makes this derivable at all -- and a ``MultiIndex`` from an un-reduced bhavcopy
    cross-section is refused by name rather than accepted under a symbol's key.

    The index check runs even when ``observed`` is given, because a caller who overrides the coverage
    has not thereby said that the frame is one symbol's time series: the bhavcopy range cache is
    exactly a caller that knows the watermark *and* holds a whole-market frame, and the two together
    must not become a row under one symbol's key.
    """
    if not isinstance(frame.index, pd.DatetimeIndex):
        index_name = type(frame.index).__name__
        hint = ""
        if isinstance(frame.index, pd.MultiIndex) and "Date" in (frame.index.names or ()):
            hint = (
                "; a bhavcopy cross-section is keyed by (Date, Symbol) and must be reduced with "
                "`frame.xs(symbol, level='Symbol')` before it is stored under one symbol's key"
            )
        raise TypeError(f"a price frame's index must be a DatetimeIndex, not {index_name}{hint}")
    if observed is not None:
        return _date_iso(observed)
    return _date_iso(frame.index.max())


def _bars_lie_inside_the_window(frame: pd.DataFrame, *, start_iso: str, end_iso: str) -> bool:
    """Do every bar of this stored frame lie inside the window it is filed under?

    The key names a window, so a row under it is an answer to that window's question -- and only the
    frame's own index can show that it is. :meth:`FrameStore.write` stores the frame it was handed for
    the window it was handed, so a row this process wrote holds bars inside its own window by
    construction; one that is not got here some other way. Those ways are the module docstring's own
    threat model: a ``frames.db`` that was copied, edited by hand or written by an older build, and a
    future writer that slices one day differently from the request it files the row under. Measured,
    the first of them is not hypothetical -- a frame whose bars are all in 2027-01, filed under a key
    ending 2026-10-16, writes as ``"hit"``, reads as ``"hit"``, serves ``2027-01-04..2027-01-06`` and
    publishes ``coverage_end=2027-01-06`` against ``requested_end=2026-10-16``, the row's own metadata
    confirming the lie -- so the caller is handed next year's bars for a window ending last year.

    Compared as ISO strings, for the reason :meth:`FrameStore._may_serve` compares its own dates that
    way: the day is the only part either side means, and ``str(...)[:10]`` cannot raise on a zone the
    payload happens to carry. Empty is inside
    -- a frame with no bars has none that could be outside the window, and an empty shape is refused on
    the way *in* by :func:`_refusal` rather than here. Nothing checks that the index is a
    ``DatetimeIndex``: :func:`_coverage_end` owns that question on the way in, and what is asked here
    is whether the bars are inside the window, not whether the frame is a price frame at all.

    **This is the only copy of the check in the package, and who calls it is the point.** ``ohlcv``'s
    split-window merge relies on it rather than keeping its own: it states that its stored prefix
    lies inside the boundary that prefix is filed under "because :meth:`FrameStore.read` refuses a
    row whose bars do not answer the key it is filed under" (``market/ohlcv.py``,
    ``_prefix_and_tail``). The copy that used to sit beside that path
    (``_prefix_lies_inside_its_window``) was measured unreachable once this one ran on every read,
    and was deleted rather than kept as a second rule to hold in step; it is not to be restored. The
    guard a reader can point at is therefore here, called from :meth:`FrameStore.read` -- a caller
    who needs to know which rows are refused reads this function, and not a peer's copy of it.
    """
    if len(frame) == 0:
        return True
    return start_iso <= str(frame.index.min())[:10] and str(frame.index.max())[:10] <= end_iso


def _ist_instant(epoch: float) -> pd.Timestamp:
    """Epoch seconds as the IST instant they name.

    ``time.time()`` is UTC-based and an epoch carries no zone, so the conversion has to be stated
    once and used by both readers of the column: the ISO spelling published on a frame, and the
    fetch *date* the publish rule compares a window against. Two spellings of one instant, produced
    from one expression -- which is the only way they cannot disagree about which day it was.
    """
    return pd.Timestamp(float(epoch), unit="s", tz="UTC").tz_convert(IST)


def _iso_timestamp(epoch: float) -> str:
    """The same instant as an ISO-8601 string, for the frame's attrs.

    The column keeps the raw epoch (the unit ``mirror_days.fetched_at`` uses, and the only one that
    compares cheaply), because an attrs value is read by people and survives a JSON export.
    """
    return _ist_instant(epoch).isoformat()


def _isolated(frame: pd.DataFrame) -> pd.DataFrame:
    """The isolation boundary of a read, named so that it can be pointed at.

    ``.copy()`` and not the object ``pickle.loads`` just produced. Unpickling already hands back a
    fresh frame, so today this call is defence in depth rather than the mechanism -- and it is kept
    for exactly that reason: the guarantee "two callers never hold the same frame" belongs to
    :meth:`FrameStore.read`, not to the storage format. A loader that memoised, an in-process tier
    above this store, or a future "cache the unpickled frame, unpickling is expensive" optimisation
    would all hand one object to two callers, and the damage is silent in both directions --
    measured: ``first.iloc[0, 0] = x`` and ``first.attrs["bfinance_price_source_attempts"].append``
    are both visible through the second reference.
    """
    return frame.copy()


def _concat_keeping_attrs(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """``pd.concat`` with the provenance put back on afterwards, because pandas silently drops it.

    Measured on pandas 3.0.5: ``pd.concat([a, b]).attrs == {}`` as soon as ``a`` and ``b`` disagree
    about *any* attr -- no exception, no warning, no other symptom. (Frames whose attrs agree keep
    theirs, which is an accident of this implementation rather than a guarantee, and it stops being
    true the moment one input's attrs differ at all.) A range assembled from per-day frames cannot
    rely on that, so the re-assertion below is not belt-and-braces: without it, the first concat of
    two days whose source attempts differ erases the provenance of the whole range.

    **The first frame's attrs are the range's attrs, and a later disagreement is logged rather than
    blended.** Blending is what the price layer forbids -- a frame is served whole, with the basis it
    is actually stated in -- and picking a winner silently is the thing a reader would never find
    out about. The comparison that produces the warning is best-effort on purpose: it feeds a log
    line, and an attrs value this module cannot compare (a numpy array, whose truth value is
    ambiguous) is not a reason to refuse frames that have already been concatenated.
    """
    frames = list(frames)
    base = dict(frames[0].attrs)
    combined = pd.concat(frames)
    combined.attrs = dict(base)
    for other in frames[1:]:
        try:
            differs = set(base) != set(other.attrs) or any(
                bool(base[name] != other.attrs[name]) for name in base
            )
        except Exception:  # noqa: BLE001 - this comparison feeds a log line, nothing else
            differs = False
        if differs:
            logger.warning(
                "Frame store: the frames being concatenated disagree about attrs; the first "
                "frame's are kept (%s)",
                sorted(set(base) ^ set(other.attrs)) or "same keys, different values",
            )
            break
    return combined


def _publish(
    frame: pd.DataFrame,
    status: str,
    *,
    fetched_at: str,
    coverage_end: str,
    requested_end: str,
) -> pd.DataFrame:
    """Put the four observation attrs on the frame that is about to be returned.

    Four keys because there are four different questions, and one packed dict would make a reader
    parse prose to find the one value they branch on:

    * :data:`CACHE_STATUS_ATTR` -- the real status, read off the lookup that produced this frame, not
      a second constant that can drift from it.
    * :data:`CACHE_FETCHED_ATTR` -- when the *data* was fetched, which is when the row was written:
      a frame enters this store immediately after it is fetched, so the write time is the fetch time.
    * :data:`CACHE_COVERAGE_END_ATTR` and :data:`CACHE_REQUESTED_END_ATTR` -- the observed end and
      the requested end, kept apart because a served frame may be shorter than the window that was
      asked for. Without both, "the data stops here" and "I asked for it to stop here" are the same
      date, and a caller cannot tell a complete answer from a truncated one.
    """
    frame.attrs[CACHE_STATUS_ATTR] = status
    frame.attrs[CACHE_FETCHED_ATTR] = fetched_at
    frame.attrs[CACHE_COVERAGE_END_ATTR] = coverage_end
    frame.attrs[CACHE_REQUESTED_END_ATTR] = requested_end
    return frame


def _frames_to_store(frame: Union[pd.DataFrame, Sequence[pd.DataFrame]]) -> list:
    """The argument as a list of frames -- one frame, or the frames to concatenate into one.

    A range worth caching is fetched as one frame per trading day, and the store takes the whole set
    rather than one frame per call so that the concatenation happens in the module that knows what a
    concatenation costs (see :func:`_concat_keeping_attrs`).

    Anything that is not a frame raises, and the raise is the point: ``None``, an HTTP status, an
    exception object and a raw response body are how "never cache a failure, a null or a 404" looks
    from this side, and answering ``"miss"`` for them -- which is what a lenient version of this
    function would do -- is the silent no-op this whole module exists to remove.
    """
    if isinstance(frame, pd.DataFrame):
        return [frame]
    if isinstance(frame, Sequence) and not isinstance(frame, (str, bytes)):
        frames = list(frame)
        if frames and all(isinstance(item, pd.DataFrame) for item in frames):
            return frames
    raise TypeError(
        f"frame must be a DataFrame or a non-empty sequence of them, not "
        f"{type(frame).__name__}: a failure, a null or a 404 has no frame, and nothing that does "
        f"is ever stored here"
    )


class FrameStore:
    """Price frames by ``(symbol, exchange, series, window)``, pickled so ``attrs`` survive.

    The module docstring carries the reasoning: why pickle, why its own file, the deserialisation
    threat model, and the split of responsibilities with the memo layer (TTL here, freshness there).

    Two obligations that are this class's rather than the caller's:

    * **A read never returns the stored object.** Every served frame is a ``.copy()`` of what was
      loaded, because a frame handed to two callers shares both its data and its attrs -- measured:
      appending to one caller's ``bfinance_price_source_attempts`` list was visible to the other, and
      so was ``frame.iloc[0, 0] = x``. ``write`` copies nothing, because the payload becomes bytes
      inside the call and the caller's object is never retained.
    * **A served frame may be shorter than the window it was asked for, and says so.** Refusing to
      serve a row whose coverage ends early would make a symbol with a real gap -- a suspension, a
      long halt -- a permanent cache miss, i.e. a full 262-file download on every call; serving it
      with :data:`CACHE_COVERAGE_END_ATTR` published is honest and leaves the refetch decision with
      the layer that can see the request. What is never done is returning a short frame with nothing
      on it that says so.
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        enabled: bool = True,
        clock: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Open the store, creating the table if it is not there.

        Args:
            db_path: the file to use. ``None`` takes :func:`default_frame_store_path`.
            enabled: ``False`` makes every read answer ``"disabled"`` and every write refuse, without
                creating, opening or touching the file at all -- a store that is switched off must
                not leave an artefact behind.
            clock: ``() -> timestamp``, the "now" the publish rule in :meth:`write` is judged
                against. ``None`` uses the real clock. The seam exists because that rule is the one
                thing here that depends on what day it is, and a rule that cannot be pointed at a
                fixed instant cannot be tested at all.

        Raises:
            Whatever ``sqlite3`` raises when the file cannot be created or written: the loud
            direction, and the same behaviour as :class:`~bfinance.cache.sqlite_cache.SQLiteCache`.
            A store that cannot open its file must not look like a store that has nothing cached.
        """
        self.db_path = str(db_path) if db_path is not None else str(default_frame_store_path())
        self.enabled = bool(enabled)
        self._clock = clock or _now
        if self.enabled:
            self._init_db()

    # ------------------------------------------------------------------ setup

    def _init_db(self) -> None:
        """Create the table, through the cache layer's connect helper (WAL + busy_timeout)."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    # ------------------------------------------------------------------ the publish rule

    def _today(self) -> str:
        """Today in IST as ``YYYY-MM-DD``, read through the injected clock.

        A naive clock value is localised to IST rather than refused: the IST date is the only date
        this rule can mean, and a caller who hands over ``datetime.now()`` on an IST machine has said
        what they meant.
        """
        stamp = pd.Timestamp(self._clock())
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST)
        return _date_iso(stamp.tz_convert(IST))

    def _may_serve(self, window_end: str, fetched_at: float) -> bool:
        """May a row for this window, fetched at this instant, be written or served?

        Two comparisons, because they are two different questions and only the pair covers the
        publish boundary:

        * ``window_end < today`` -- the window is closed, so every archive file it needs is already
          published and never rewritten. This is the rule stated in one place for both layers: a
          window that contains today is neither stored nor served, because today's file does not
          exist yet and a partial session must not be presented as a final one.
        * ``window_end < the row's own fetch date`` -- the row was written *after* its window closed.
          This store's :meth:`write` cannot produce any other kind of row, so a row that fails this
          test came from somewhere else: an older build, a copy, or an edit by hand. Without it the
          first clause alone would let such a row become servable the next day: the publish-boundary
          failure, arriving a day late and looking like an ordinary hit.

        At *write* time the two clauses are the same comparison -- the fetch date of a frame arriving
        now is today -- so the write path's refusal is exactly "this window contains today", and the
        second clause only ever decides anything about a row that is already on disk.

        ISO dates compare correctly as strings, so neither comparison needs parsing.
        """
        return window_end < self._today() and window_end < _date_iso(_ist_instant(fetched_at))

    # ------------------------------------------------------------------ read

    def read(
        self,
        *,
        symbol: str,
        exchange: str,
        series: Optional[str] = None,
        start: Any,
        end: Any,
    ) -> CacheLookup:
        """Look the window up and answer with a status, never with a bare ``None``.

        Args:
            symbol: the security, as the caller spells it (see :func:`_component`).
            exchange: ``NSE``/``BSE``; required, because a default here is how an NSE frame ends up
                filed under a BSE key.
            series: ``EQ``/``BE``/...; ``None`` means "all series" and is spelled ``"ALL"``.
            start, end: the **resolved** window, inclusive, as dates.

        Returns:
            :class:`~bfinance.cache.sqlite_cache.CacheLookup`. ``.value`` is a copy of the stored
            frame carrying :data:`CACHE_STATUS_ATTR`, :data:`CACHE_FETCHED_ATTR`,
            :data:`CACHE_COVERAGE_END_ATTR` and :data:`CACHE_REQUESTED_END_ATTR` when the status is
            ``"hit"``, and ``None`` for every other status. ``"disabled"`` when the store is
            switched off, ``"corrupt"`` when the row exists but could not be trusted (an unreadable
            payload, a read that failed, or a frame whose bars do not lie inside the window the key
            names -- a row is served only when it answers the question it was asked under),
            ``"miss"`` when there is no row -- including a window that contains today, which is never
            served from here.
        """
        if not self.enabled:
            return CacheLookup(DISABLED_STATUS)

        key = frame_key(symbol, exchange, series, start, end)
        start_iso = _date_iso(start)
        requested_end = _date_iso(end)

        try:
            with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT payload, coverage_end, fetched_at FROM bfinance_price_frames "
                    "WHERE frame_key = ?",
                    (key,),
                ).fetchone()
        except Exception:
            # Same reading as `CacheLookup`'s own docstring gives this name: the row could not be
            # read, so nothing behind that key may be used, and the traceback is the record.
            logger.exception("Frame store read failed for key %s", key)
            return CacheLookup("corrupt")

        if row is None:
            return CacheLookup("miss")

        payload, coverage_end, fetched_at = row
        if not self._may_serve(requested_end, fetched_at):
            # The row exists and is not servable: the window it covers was still open when it was
            # written (see `_may_serve`). A miss, not an error, because the caller's next move is the
            # one a miss produces anyway -- fetch it -- and the DEBUG line names which half refused.
            logger.debug(
                "Frame store: not serving %s -- the window ends %s and the row was fetched %s",
                key,
                requested_end,
                _iso_timestamp(fetched_at),
            )
            return CacheLookup("miss")

        try:
            frame = _frame_from_blob(payload)
        except Exception:
            logger.exception("Frame store row for %s could not be unpickled; refetching", key)
            return CacheLookup("corrupt")

        if not _bars_lie_inside_the_window(frame, start_iso=start_iso, end_iso=requested_end):
            # The row exists and does not answer the key it is filed under: its bars are not in the
            # window the key names (see `_bars_lie_inside_the_window`). "corrupt" and not a served row,
            # and never a row trimmed to the part that happens to fit -- serving a shortened version of
            # a stored frame is the doctrine this package refuses everywhere else, and a wrong hit is
            # the one failure a caller has no second place to see. Every consumer already refetches on
            # "corrupt", so refusing is also the cheap direction.
            logger.warning(
                "Frame store: refusing %s -- the stored frame's bars (%s..%s) do not lie inside the "
                "window %s..%s it is filed under; refetching rather than serving a frame that does "
                "not answer the key",
                key,
                str(frame.index.min())[:10],
                str(frame.index.max())[:10],
                start_iso,
                requested_end,
            )
            return CacheLookup("corrupt")

        frame = _publish(
            _isolated(frame),
            "hit",
            fetched_at=_iso_timestamp(fetched_at),
            coverage_end=coverage_end,
            requested_end=requested_end,
        )
        return CacheLookup("hit", frame)

    # ------------------------------------------------------------------ write

    def write(
        self,
        frame: Union[pd.DataFrame, Sequence[pd.DataFrame]],
        *,
        symbol: str,
        exchange: str,
        series: Optional[str] = None,
        start: Any,
        end: Any,
        observed_coverage_end: Any = None,
    ) -> str:
        """Store one window's frame, or refuse it and say which of the two happened.

        Args:
            frame: the fetched frame, or the sequence of frames to concatenate into one (per-day
                frames of a range). Anything else raises -- see :func:`_frames_to_store`.
            symbol, exchange, series, start, end: the key, exactly as :meth:`read` takes it.
            observed_coverage_end: how far the data actually reaches, when the caller knows better
                than the frame's own index does (a fetch layer that already computed the archive's
                watermark). ``None`` takes ``frame.index.max()``. The index must still be a
                ``DatetimeIndex`` either way: the override changes where the coverage ends, not
                whether the frame is one symbol's time series (see :func:`_coverage_end`).

        Returns:
            The status a :meth:`read` of the same key would now report: ``"hit"`` when a row is
            there, ``"miss"`` when nothing was written, ``"disabled"`` when the store is switched
            off. The one case where the two answers differ is a frame whose bars do not lie inside the
            window it is filed under: this method stores what it was handed for the window it was
            handed and never inspects the bars, so it answers ``"hit"`` for the row it just wrote,
            while the read of that key refuses it as ``"corrupt"`` -- a row is served only when it
            answers the key it is filed under (:func:`_bars_lie_inside_the_window`). Two refusals
            answer ``"miss"``, and both are logged:

            * **an open window** -- one that contains today -- at DEBUG, because declining to cache
              the current session is routine rather than an anomaly. The same predicate
              (:meth:`_may_serve`) is applied again on read, where it is the row's *stored* fetch
              date rather than a fresh clock that decides, so a row claiming to have been written
              while its window was still open is not served either;
            * **a frame that is a failure shape** -- no rows, no columns, or every value NaN -- at
              WARNING, because handing one to a cache means the caller believes it fetched
              something. See :func:`_refusal`.

            A refusal is not an error to raise: the caller has a frame either way, and what it must
            not do is assume the next call will be served from the store.
        """
        if not self.enabled:
            return DISABLED_STATUS

        key = frame_key(symbol, exchange, series, start, end)
        window_end = _date_iso(end)
        fetched_at = time.time()
        if not self._may_serve(window_end, fetched_at):
            logger.debug(
                "Frame store: %s not stored -- the window ends %s, which is today or later in %s",
                key,
                window_end,
                IST,
            )
            return "miss"

        frames = _frames_to_store(frame)
        stored = frames[0] if len(frames) == 1 else _concat_keeping_attrs(frames)

        refusal = _refusal(stored)
        if refusal:
            logger.warning("Frame store declined %s: %s", key, refusal)
            return "miss"

        coverage_end = _coverage_end(stored, observed_coverage_end)
        blob = _payload_blob(stored)
        try:
            with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO bfinance_price_frames
                        (frame_key, coverage_end, n_rows, fetched_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(frame_key) DO UPDATE SET
                        coverage_end = excluded.coverage_end,
                        n_rows = excluded.n_rows,
                        fetched_at = excluded.fetched_at,
                        payload = excluded.payload
                    """,
                    (key, coverage_end, len(stored), fetched_at, blob),
                )
                conn.commit()
        except Exception:
            logger.exception("Frame store write failed for key %s", key)
            return "miss"
        return "hit"
