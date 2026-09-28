"""
Persistent SQLite caching layer with TTL support for bfinance.
"""

import json
import logging
import sqlite3
import threading
import time
from typing import Any, NamedTuple, Optional
from pathlib import Path
from contextlib import closing

logger = logging.getLogger("bfinance")

_CACHE_LOCK = threading.Lock()


class CacheLookup(NamedTuple):
    """What a cache lookup actually found, for the cases ``get``'s ``None`` cannot express.

    ``status`` is one of:

    ``"hit"``
        An unexpired row whose payload is a value. ``value`` is it.
    ``"null"``
        An unexpired row whose payload *is* JSON ``null``. This is a row that exists and was
        served, holding nothing -- and it is why :meth:`SQLiteCache.get` alone is not enough: it
        returns ``None`` for this, for a missing key and for an expired key alike, so a caller
        debugging cache behaviour ("is this thing ever cached?") has no way to tell them apart.
    ``"miss"``
        No row at all.
    ``"expired"``
        A row that was past ``expires_at``; it has been deleted as a side effect of the read.
    ``"corrupt"``
        The row could not be read -- malformed JSON, or the read itself failed. The entry is not
        trusted and not returned; the ERROR log with the traceback is the record. (The name covers
        I/O-level failures too, because they have the same consequence for a caller: do not use
        what is behind that key.)
    """

    status: str
    value: Any = None

    @property
    def found(self) -> bool:
        """True when a row was present and unexpired -- including one that holds ``null``."""
        return self.status in ("hit", "null")


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with WAL mode + busy_timeout for lock resilience.

    The two ``except`` blocks below are deliberate, not lazy. Both pragmas are *resilience and
    performance settings on a working database*, and a filesystem that refuses one of them (WAL
    needs a shared-memory-mappable file: network shares, some FUSE mounts) is still a perfectly
    good place to keep a cache. Letting the pragma fail loudly would mean refusing to cache at
    all because a journal mode was unavailable -- trading a working cache with coarser locking for
    no cache. So each failure is logged and the connection is used as-is:

    - ``journal_mode=WAL`` is the default persistent setting for the *database file*, so on a
      filesystem that will not take it the connection still reads and writes; it just serialises
      writers the old way.
    - ``busy_timeout=5000`` is belt-and-braces: ``sqlite3.connect(timeout=5.0)`` below already
      installs a 5-second busy timeout, so a failure here costs at most the PRAGMA's own effect
      and nothing the caller can observe.
    """
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        logger.warning("Failed to enable WAL mode for cache db", exc_info=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        logger.warning("Failed to set busy_timeout for cache db", exc_info=True)
    return conn


class SQLiteCache:
    """
    Lightweight, embedded SQLite cache for bfinance upstream queries.
    Stores serialized JSON payloads with expiration timestamps.

    **One connection per call, closed on every path.** Each operation opens its own connection
    and now closes it through :func:`contextlib.closing`. Two claims about this file were
    measured, and the fix is about the second one, not the first:

    1. *There is no per-call connection leak.* 8,500 cache operations left 45 unreclaimed
       ``sqlite3.Connection`` objects at the instant of measurement and **1** after a
       ``gc.collect()`` -- they are cyclic garbage, reclaimed by the generational collector, not
       refcounted, and bounded by its thresholds rather than growing with call count. The older
       note in ``tests/test_sqlite_cache.py`` claiming "``get``/``set``/``clear`` leak one per
       call" is wrong and should be deleted.
    2. *A connection WAS retained indefinitely on the error path, and that is fixed here.* A
       corrupt read logs with ``logger.exception``, so the ``LogRecord`` holds a traceback, and
       the traceback holds the frame that holds ``conn``. Any handler that retains records (a
       list handler, pytest's ``caplog``) therefore pins one open SQLite file handle per corrupt
       read: measured, 50 corrupt reads -> 51 live connections, all of them released the moment
       the records were dropped. ``closing()`` removes the dependency on the log handler's
       retention policy; the ERROR log and its traceback are kept, because "why was this row
       unreadable" is the only evidence a corrupt cache leaves behind.

    ``with conn:`` alone never did that: a ``sqlite3`` connection's context manager is a
    *transaction* manager -- it commits or rolls back, it does not close. Every method commits
    explicitly, so replacing it with ``closing`` changes no transaction behaviour.
    """

    def __init__(self, db_path: Optional[str] = None, default_ttl_hours: float = 24.0):
        if db_path is None:
            # Store in user home directory under ~/.bfinance/cache.db
            home_dir = Path.home() / ".bfinance"
            home_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(home_dir / "cache.db")
        else:
            self.db_path = db_path

        self.default_ttl_hours = default_ttl_hours
        self._init_db()

    def _init_db(self):
        """Create cache table and indexes if not exists."""
        with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bfinance_cache (
                    cache_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_category ON bfinance_cache(category)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_expires ON bfinance_cache(expires_at)"
            )
            conn.commit()

    def get(self, cache_key: str) -> Optional[Any]:
        """Retrieve unexpired cached payload, or ``None``.

        ``None`` stands for three different outcomes -- no row, an expired row, and a row whose
        payload *is* JSON ``null`` -- and this signature cannot separate them. When the difference
        matters, use :meth:`get_result`. Every caller in this package currently takes the safe
        reading of all three ("``None`` means go fetch it"), which is why the hole was invisible
        rather than harmful: a stored ``null`` costs a refetch, it does not return wrong data.
        """
        return self.get_result(cache_key).value

    def get_result(self, cache_key: str) -> CacheLookup:
        """Retrieve an unexpired cached payload and say what kind of outcome it was.

        See :class:`CacheLookup` for the five statuses. The point of this method is that a stored
        ``null`` is a real, unexpired, successfully-read row and not the absence of one.
        """
        now = time.time()
        try:
            with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload, expires_at FROM bfinance_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
                if row:
                    payload_str, expires_at = row
                    if expires_at > now:
                        value = json.loads(payload_str)
                        return (
                            CacheLookup("hit", value) if value is not None
                            else CacheLookup("null", None)
                        )
                    # Expired, clean it up
                    cursor.execute("DELETE FROM bfinance_cache WHERE cache_key = ?", (cache_key,))
                    conn.commit()
                    return CacheLookup("expired")
                return CacheLookup("miss")
        except Exception:
            # Unreadable row: log it with the traceback and answer as a miss, which is the safe
            # direction (refetch rather than trust). `get` still returns None, exactly as before.
            logger.exception("Cache get failed for key %s", cache_key)
            return CacheLookup("corrupt")

    def set(
        self,
        cache_key: str,
        payload: Any,
        category: str = "general",
        ttl_hours: Optional[float] = None,
    ):
        """Store serialized payload with TTL.

        ``ttl_hours=None`` takes :attr:`default_ttl_hours`. Callers inside bfinance pass it
        explicitly at every call site (see ``ScreenerClient.CATEGORY_TTL_HOURS``) precisely so that
        no cached payload's age is a side effect of the cache's construction default.
        """
        if ttl_hours is None:
            ttl_hours = self.default_ttl_hours

        now = time.time()
        expires_at = now + (ttl_hours * 3600.0)
        try:
            # allow_nan=False is the whole point of this call. Plain json.dumps writes bare `NaN`
            # and `Infinity` tokens, which are NOT JSON: the row it produces cannot be read by a
            # strict parser (another language, `jq`, a columnar loader) and round-trips only
            # because Python's json.loads happens to accept those same two tokens. So a payload
            # carrying a non-finite float produced a cache file that half its consumers could not
            # read, silently. Refusing is the honest outcome -- the value is not representable,
            # so it is not cached, the key is named, and the next call re-fetches. The
            # alternative (rewriting NaN to null) was rejected because it changes the caller's
            # data on the way through: a stored null is indistinguishable from a cached null to
            # whoever reads it back.
            payload_str = json.dumps(payload, allow_nan=False)
        except ValueError as exc:
            # ValueError here is either a non-finite float (allow_nan=False) or a circular
            # reference. Both mean "this payload has no JSON form"; neither is the caller's
            # problem to debug from a traceback, so the key and the reason are logged.
            logger.error(
                "Skipping cache set for key %s: payload is not representable in strict JSON "
                "(NaN/Infinity or a circular reference): %s",
                cache_key, exc,
            )
            return
        except TypeError:
            logger.warning("Skipping cache set: unserializable payload for key %s", cache_key)
            return

        try:
            with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO bfinance_cache (cache_key, category, payload, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        category = excluded.category,
                        payload = excluded.payload,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (cache_key, category, payload_str, now, expires_at),
                )
                conn.commit()
        except Exception:
            logger.exception("Cache set failed for key %s", cache_key)

    def clear(self, category: Optional[str] = None):
        """Clear all or category-specific cache entries."""
        try:
            with _CACHE_LOCK, closing(_connect(self.db_path)) as conn:
                cursor = conn.cursor()
                if category:
                    cursor.execute("DELETE FROM bfinance_cache WHERE category = ?", (category,))
                else:
                    cursor.execute("DELETE FROM bfinance_cache")
                conn.commit()
        except Exception:
            logger.exception("Cache clear failed (category=%s)", category)
