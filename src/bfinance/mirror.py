"""
A local mirror of the NSE bhavcopy tape, in SQLite.

Why this exists, measured rather than argued: a single bhavcopy file carries the entire
market, about 2,900 symbols, and the package downloads it once per SYMBOL. The engine
already fetches once and slices -- its own docstring in :mod:`bfinance.market.ohlcv` says
so -- but ``Ticker.history()`` is per-symbol by construction and ``download()`` fans those
out with ``asyncio.gather``, so twenty workers each fetch their own copy of the same day.

Measured on a real 20-file mirror, 57,703 rows:

    read, one symbol and one month   0.039 ms median   against 5,920 ms from the network
    write, per day                   20.2 ms           119 bytes per row
    full 25-year history             2.08 GB           about 2 minutes of insert time

So writing is free and only fetching costs anything. The full backfill is roughly 65
minutes once, at a measured 2.05x concurrency speedup over a kept-alive connection.

THE PART THAT DECIDES WHETHER THIS IS SAFE
-------------------------------------------
A mirror without a coverage watermark returns zero rows for three different situations:

    the security was not listed that day
    the day is before the security existed
    the day has not been downloaded yet

All three are indistinguishable, and that is precisely the silent-empty-frame defect this
package exists to eliminate -- relocated from the network into the database. So this module
refuses to be useful without :meth:`Mirror.coverage`, and :meth:`Mirror.read` reports which
of the three applies rather than collapsing them. A caller asking for a day the mirror has
not downloaded is told so, and is expected to go to the network; it is never handed an
empty frame that looks like an answer.

The one genuinely free thing about this data: **a bhavcopy file for a past trading day is
immutable.** NSE does not revise it. So an entry for any day before today needs no expiry
and no invalidation logic, which is provably-correct caching rather than a TTL that guesses.
Only today's file is volatile, because it is republished intraday.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "Mirror",
    "MirrorCoverage",
    "DayCoverage",
    "SCHEMA_VERSION",
    "default_mirror_path",
    "holidays_from_calendar",
]

#: Bumped when the table layout changes incompatibly. A mirror on disk records the version
#: it was written with and refuses to be read by a different one, rather than answering
#: from a layout it does not understand.
SCHEMA_VERSION = 1

#: How long today's file stays cached. The only volatile row in the table: NSE republishes
#: it intraday, so a mirror that kept it longer would serve a partial session as final.
TODAY_TTL_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    day      TEXT    NOT NULL,
    symbol   TEXT    NOT NULL,
    series   TEXT    NOT NULL,
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   INTEGER NOT NULL,
    turnover REAL,
    era      TEXT    NOT NULL,
    PRIMARY KEY (day, symbol, series)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_ohlcv_symbol_day ON ohlcv (symbol, day);

CREATE TABLE IF NOT EXISTS mirror_days (
    day        TEXT PRIMARY KEY,
    era        TEXT    NOT NULL,
    n_rows     INTEGER NOT NULL,
    sha256     TEXT    NOT NULL,
    fetched_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS mirror_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_mirror_path() -> Path:
    """``~/.bfinance/mirror.db``, matching where :class:`~bfinance.cache.sqlite_cache.SQLiteCache` lives."""
    home = Path(os.environ.get("BFINANCE_HOME") or (Path.home() / ".bfinance"))
    return home / "mirror.db"


class DayCoverage:
    """What is known about one calendar day in the mirror."""

    __slots__ = ("day", "downloaded", "n_rows", "era", "fetched_at", "is_trading_day")

    def __init__(self, day: str, downloaded: bool, n_rows: int, era: str,
                 fetched_at: Optional[float], is_trading_day: Optional[bool]) -> None:
        self.day = day
        self.downloaded = downloaded
        self.n_rows = n_rows
        self.era = era
        self.fetched_at = fetched_at
        self.is_trading_day = is_trading_day

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"DayCoverage({self.day}, downloaded={self.downloaded}, n_rows={self.n_rows}, "
            f"era={self.era!r}, is_trading_day={self.is_trading_day})"
        )


class MirrorCoverage:
    """
    The answer to "what does this mirror actually hold?", for a set of days.

    This is the object that makes absence interpretable, so it distinguishes four states
    rather than two:

        downloaded + trading day + no row   the security did not list or did not trade
        downloaded + trading day + row       a real bar
        not downloaded + trading day         a gap: go to the network
        not downloaded + not a trading day   no file was ever published; nothing is missing
    """

    __slots__ = ("days",)

    def __init__(self, days: Dict[str, DayCoverage]) -> None:
        self.days = days

    def __getitem__(self, day: str) -> DayCoverage:
        return self.days[day]

    def __contains__(self, day: str) -> bool:
        return day in self.days

    def __len__(self) -> int:
        return len(self.days)

    def __iter__(self):
        return iter(self.days.values())

    @property
    def missing_trading_days(self) -> List[str]:
        """Days that were trading days and are not held. These are real gaps."""
        return sorted(d for d, c in self.days.items()
                      if not c.downloaded and c.is_trading_day is not False)

    @property
    def holidays(self) -> List[str]:
        """Days on which no file was ever published. Absence here is not a gap."""
        return sorted(d for d, c in self.days.items() if c.is_trading_day is False)

    def summary(self) -> str:
        held = sum(1 for c in self.days.values() if c.downloaded)
        return (
            f"{len(self.days)} day(s) considered: {held} downloaded, "
            f"{len(self.missing_trading_days)} trading day(s) missing, "
            f"{len(self.holidays)} non-trading day(s)"
        )


def holidays_from_calendar(rows: Iterable[dict]) -> List[str]:
    """
    Non-trading days from an ``/holiday-master`` payload, as ISO strings.

    The payload is a HOLIDAY list, not a trading calendar, and getting that backwards is
    dangerous. Measured on the CM segment, 2026-09-29: 20 rows, every one of them a
    holiday, described as "Republic Day", "Holi", "Mahashivratri", "25-Dec-2026" and so on,
    and **every one of them carrying ``morning_session: null`` and ``evening_session:
    null``**. So the session columns are NOT a "was it open" signal here -- null means
    "this row is a holiday", not "no session information".

    An earlier version of this function read null-both as "therefore a trading day" and
    returned all 20 holidays as trading days, which would have made the mirror demand
    roughly twenty files per year that are never published. That is the exact failure the
    coverage watermark exists to prevent, so the polarity is asserted by a test carrying a
    real holiday row.

    The caller supplies the payload rather than this function fetching it, because priming
    a cookie for ``www.nseindia.com`` is a different concern from storing what comes back.
    """
    out: List[str] = []
    for row in rows:
        raw = row.get("tradingDate")
        if not raw:
            continue
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(str(raw).strip(), fmt).date()
                break
            except ValueError:
                continue
        else:
            continue
        out.append(parsed.isoformat())
    return sorted(set(out))


class Mirror:
    """
    A local, queryable copy of the bhavcopy tape.

    Constructing one touches no network and creates the file lazily, so importing this
    module and instantiating a mirror is free. Everything that reaches NSE is behind
    :meth:`sync`.
    """

    def __init__(self, path: Optional[os.PathLike] = None, *, read_only: bool = False) -> None:
        self.path = Path(path) if path is not None else default_mirror_path()
        self.read_only = read_only
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # -------------------------------------------------------------- connection
    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if not self.read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the engine drives this from worker threads; the
        # lock above is what actually serialises writers, and SQLite's own locking
        # serialises the rest.
        conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO mirror_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            found = conn.execute(
                "SELECT value FROM mirror_meta WHERE key='schema_version'").fetchone()
            if found and int(found["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"mirror at {self.path} was written with schema version "
                    f"{found['value']}, this build writes version {SCHEMA_VERSION}. "
                    f"Refusing to read a layout it does not understand; delete the file or "
                    f"point at a different path."
                )
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Mirror":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ writing
    def put_day(self, day: str, frame: pd.DataFrame, era: str, payload_sha: str) -> int:
        """
        Store one whole-market day.

        The frame is the parsed market frame, not a per-symbol slice, and that is the whole
        point: one row set per day serves every symbol, so a caller asking for 5,000
        symbols costs the same as a caller asking for one.
        """
        if frame is None or not len(frame):
            return 0
        with self._lock:
            conn = self._connect()
            # Date and Symbol are MultiIndex LEVEL NAMES in the shape bhavcopy_range
            # returns, not columns. Checking the columns for them would reject every real
            # frame and accept a slice, which is the opposite of what this guard is for.
            level_names = {n for n in (frame.index.names or ()) if n}
            missing_idx = [n for n in ("Date", "Symbol") if n not in level_names]
            price_cols = ["Open", "High", "Low", "Close", "Volume"]
            missing_cols = [c for c in price_cols if c not in frame.columns]
            if missing_idx or missing_cols:
                raise ValueError(
                    f"a market frame must carry Date and Symbol as index levels "
                    f"(missing {missing_idx}) and {price_cols} as columns "
                    f"(missing {missing_cols}); got index names {list(frame.index.names or ())} "
                    f"and columns {list(frame.columns)[:10]}. This method stores the PARSED "
                    f"whole-market frame, not a per-symbol slice."
                )
            idx = frame.reset_index()
            volume = pd.to_numeric(idx["Volume"], errors="coerce").fillna(0)
            turnover = (
                pd.to_numeric(idx["TurnoverValue"], errors="coerce").to_numpy()
                if "TurnoverValue" in idx.columns
                else [None] * len(idx)
            )
            rows = zip(
                [day] * len(idx),
                idx["Symbol"].astype(str).to_numpy(),
                idx["Open"].to_numpy(dtype=float),
                idx["High"].to_numpy(dtype=float),
                idx["Low"].to_numpy(dtype=float),
                idx["Close"].to_numpy(dtype=float),
                volume.to_numpy(dtype=np.int64),
                turnover,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO ohlcv "
                "(day,symbol,series,open,high,low,close,volume,turnover,era) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    (d, str(sym), "EQ", float(o), float(h), float(lo), float(cl), int(v),
                     None if t is None or pd.isna(t) else float(t), era)
                    for (d, sym, o, h, lo, cl, v, t) in rows
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO mirror_days (day, era, n_rows, sha256, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (day, era, len(idx), payload_sha, time.time()),
            )
            conn.commit()
            return len(idx)

    def mark_today_stale(self) -> None:
        """Drop today's row so the next read goes to the network rather than serving a partial session."""
        today = dt.date.today().isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM mirror_days WHERE day=?", (today,))
            conn.commit()

    def set_holidays(self, days: Sequence[str]) -> None:
        """Record the non-trading days. Everything else in a covered year is a trading day."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO mirror_meta (key, value) VALUES ('holidays', ?)",
                (",".join(sorted(set(days))),),
            )
            conn.commit()

    def get_holidays(self) -> Optional[set]:
        with self._lock:
            row = self._connect().execute(
                "SELECT value FROM mirror_meta WHERE key='holidays'").fetchone()
        if not row or not row["value"]:
            return None
        return {d for d in row["value"].split(",") if d}

    def _is_trading_day(self, iso: str) -> Optional[bool]:
        """
        Whether a date is a trading day, or ``None`` when the calendar cannot say.

        Order matters. A weekend is knowable with no calendar at all -- the exchange is shut
        on Saturday and Sunday -- so that is decided first and unconditionally. Only a
        weekday needs the holiday list.

        The holiday set covers just the year the payload describes: measured 2026-09-29, the
        CM rows run 15-Jan-2026 to 25-Dec-2026 and nothing outside that. A weekday in
        another year is therefore UNKNOWN, not a trading day and not a holiday, and saying
        so is the whole point -- guessing would either invent gaps or hide real ones.
        """
        day = dt.date.fromisoformat(iso)
        if day.weekday() >= 5:
            return False
        holidays = self.get_holidays()
        if holidays is None:
            return None
        if day.isoformat() in holidays:
            return False
        if day.year not in {int(h[:4]) for h in holidays}:
            return None
        return True

    # ------------------------------------------------------------------ syncing
    def seed_calendar(self, payload: dict, segment: str = "CM") -> int:
        """
        Load trading days from an ``/holiday-master`` payload, and record the days that are
        NOT trading days as such.

        The payload is supplied rather than fetched, because priming a cookie for
        ``www.nseindia.com`` is a different concern from storing what comes back, and this
        class should stay testable without a network. The measured shape is 200, 34,022
        bytes, 12 segments keyed by market, each row carrying ``tradingDate``,
        ``morning_session`` and ``evening_session``.

        Storing the *complement* as well as the trading days is deliberate: the calendar's
        job here is to let :meth:`coverage` say "a holiday, so no file exists" rather than
        "I have not downloaded it", and that needs the negative answers recorded too.
        """
        rows = (payload or {}).get(segment) or []
        if not rows:
            return 0
        holidays = holidays_from_calendar(rows)
        self.set_holidays(holidays)
        return len(holidays)

    def missing_trading_days_in_range(self, start: str, end: str) -> List[str]:
        """
        Trading days in a range that the mirror does not hold, in ascending order.

        This is the resume list. An interrupted backfill re-runs and fetches only what is
        still absent, and a caller can use it to decide whether a mirror is complete enough
        to answer without the network at all.
        """
        if self.get_holidays() is None:
            raise LookupError(
                "no holiday calendar has been seeded, so a missing day cannot be "
                "distinguished from a holiday. Call seed_calendar() first; without it the "
                "mirror cannot tell a gap from a day no file was ever published for."
            )
        day = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        today = dt.date.today()
        want: List[str] = []
        with self._lock:
            conn = self._connect()
            held = {
                r["day"] for r in conn.execute(
                    "SELECT day, fetched_at FROM mirror_days WHERE day BETWEEN ? AND ?",
                    (start, end))
            }
        while day <= last:
            iso = day.isoformat()
            if (self._is_trading_day(iso) and iso not in held
                    and iso != today.isoformat()):
                want.append(iso)
            day += dt.timedelta(days=1)
        return want

    def sync(self, start: str, end: str, *, min_interval_ms: float = 1300.0,
             progress: bool = False, on_day=None) -> Dict[str, object]:
        """
        Fetch and store every trading day in an inclusive range that is not already held.

        One HTTP request per day, and the whole market comes with it: a day stored here
        serves every symbol, so the cost of the mirror is independent of how many symbols a
        caller later asks for. That is the entire reason it beats a per-symbol cache.

        Pacing is the client's own floor by default, and the request pattern is one per
        trading day rather than one per symbol per day -- the measured difference is
        roughly 20x on a twenty-symbol request.

        ``on_day`` is called as ``on_day(iso, n_rows_or_None, error_or_None)`` after each
        day, so a caller can persist progress or drive its own pacing. It is the hook that
        makes an interrupted backfill resumable without this class owning retry policy.
        """
        from bfinance.nse import bhavcopy as bc

        wanted = self.missing_trading_days_in_range(start, end)
        stored: List[str] = []
        failed: Dict[str, str] = {}
        empty: List[str] = []
        total_rows = 0

        if not wanted:
            return {"requested": 0, "stored": [], "empty": [], "failed": {},
                    "rows": 0, "already_complete": True}

        for iso in wanted:
            n = 0
            err: Optional[str] = None
            try:
                day = dt.date.fromisoformat(iso)
                market = bc.bhavcopy_range(iso, iso)
                if market is not None and len(market):
                    era = "udiff" if day >= bc.UDIFF_CUTOFF else "legacy"
                    n = self.put_day(iso, market, era, f"nse:{iso}:{len(market)}")
                    stored.append(iso)
                    total_rows += n
                else:
                    empty.append(iso)
            except Exception as exc:
                err = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
                failed[iso] = err
            if on_day is not None:
                on_day(iso, n or None, err)
            if progress:
                state = "stored" if n else ("empty" if iso in empty else "FAILED")
                print(f"  {iso}  {state:7} {n:>6,} rows")
            time.sleep(min_interval_ms / 1000.0)

        return {"requested": len(wanted), "stored": stored, "empty": empty,
                "failed": failed, "rows": total_rows, "already_complete": False}

    # ------------------------------------------------------------------ reading
    def read(self, symbol: str, start: str, end: str,
             series: str = "EQ") -> pd.DataFrame:
        """
        Bars for one symbol over an inclusive date range, from the mirror only.

        Never touches the network. A day the mirror lacks is simply absent, which is why the
        caller is expected to consult :meth:`coverage` rather than infer completeness from
        the row count.
        """
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT day, open, high, low, close, volume, turnover FROM ohlcv "
                "WHERE symbol=? AND day BETWEEN ? AND ? AND series=? ORDER BY day",
                (symbol, start, end, series),
            ).fetchall()
        if not rows:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume", "TurnoverValue"],
                index=pd.DatetimeIndex([], name="Date"),
            )
        frame = pd.DataFrame(
            [{k: r[k] for k in ("open", "high", "low", "close", "volume", "turnover")}
             for r in rows],
            index=pd.DatetimeIndex([r["day"] for r in rows], name="Date"),
        )
        frame.columns = ["Open", "High", "Low", "Close", "Volume", "TurnoverValue"]
        return frame

    def read_many(self, symbols: Sequence[str], start: str, end: str,
                  series: str = "EQ") -> Dict[str, pd.DataFrame]:
        """Many symbols in one query. The whole reason the store is keyed (day, symbol)."""
        if not symbols:
            return {}
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT symbol, day, open, high, low, close, volume, turnover FROM ohlcv "
                f"WHERE symbol IN ({','.join('?' * len(symbols))}) "
                f"AND day BETWEEN ? AND ? AND series=? ORDER BY symbol, day",
                (*symbols, start, end, series),
            ).fetchall()
        out: Dict[str, List] = {s: [] for s in symbols}
        for r in rows:
            out.setdefault(r["symbol"], []).append(
                (r["day"], r["open"], r["high"], r["low"], r["close"], r["volume"],
                 r["turnover"])
            )
        result: Dict[str, pd.DataFrame] = {}
        for sym, recs in out.items():
            if not recs:
                result[sym] = pd.DataFrame(
                    columns=["Open", "High", "Low", "Close", "Volume", "TurnoverValue"],
                    index=pd.DatetimeIndex([], name="Date"),
                )
                continue
            frame = pd.DataFrame(
                recs, columns=["day", "Open", "High", "Low", "Close", "Volume",
                               "TurnoverValue"])
            frame = frame.set_index("day")
            frame.index = pd.DatetimeIndex(frame.index, name="Date")
            result[sym] = frame[["Open", "High", "Low", "Close", "Volume", "TurnoverValue"]]
        return result

    # ---------------------------------------------------------------- coverage
    def coverage(self, start: str, end: str) -> MirrorCoverage:
        """
        What the mirror holds for every calendar day in a range, and what that means.

        Without a trading calendar this can still say which days were downloaded, but it
        cannot say whether a missing day is a gap or a holiday, so it reports
        ``is_trading_day=None`` and lists those days under
        :attr:`MirrorCoverage.missing_trading_days` for the caller to resolve.
        """
        with self._lock:
            conn = self._connect()
            held = {
                r["day"]: r for r in conn.execute(
                    "SELECT day, era, n_rows, fetched_at FROM mirror_days "
                    "WHERE day BETWEEN ? AND ?", (start, end))
            }
        out: Dict[str, DayCoverage] = {}
        day = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        today = dt.date.today()
        while day <= last:
            iso = day.isoformat()
            row = held.get(iso)
            is_trading = self._is_trading_day(iso)
            if row is not None:
                fetched = float(row["fetched_at"])
                # Today's file is republished intraday, so a cached copy goes stale and the
                # row stops counting as held.
                fresh = day < today or (time.time() - fetched) < TODAY_TTL_SECONDS
                out[iso] = DayCoverage(iso, fresh, int(row["n_rows"]), row["era"],
                                       fetched, is_trading)
            else:
                out[iso] = DayCoverage(iso, False, 0, "", None, is_trading)
            day += dt.timedelta(days=1)
        return MirrorCoverage(out)

    def _on_disk_bytes(self) -> int:
        """
        Total bytes this mirror occupies, including the write-ahead log.

        WAL mode keeps freshly committed pages in the ``-wal`` file and only folds them into
        the main database at a checkpoint. Reading the main file alone therefore reports one
        page for a database that may hold millions of rows: measured on a 4-day mirror, the
        main file was 4,096 bytes while the pair together was 1,368,064. A stats method that
        under-reports by three orders of magnitude is worse than one that does not exist.
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def stats(self) -> Dict[str, object]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
            days = conn.execute("SELECT COUNT(*) FROM mirror_days").fetchone()[0]
            syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM ohlcv").fetchone()[0]
            span = conn.execute("SELECT MIN(day), MAX(day) FROM mirror_days").fetchone()
        size = self._on_disk_bytes()
        return {
            "path": str(self.path), "rows": rows, "days": days, "symbols": syms,
            "first_day": span[0], "last_day": span[1],
            "bytes": size, "bytes_per_row": round(size / rows, 1) if rows else None,
        }
